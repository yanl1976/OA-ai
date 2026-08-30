# -*- coding: utf-8 -*-
"""云之家审批单据「多任务拉取」通用引擎。

替代 sync_yzj_minutes.py 里写死的会议纪要单模板逻辑，改由
config/yzj_pull_tasks.json 驱动：每个任务可独立配置
模板 / 状态 / 时间范围 / 目标分类 / 是否下载附件 / 是否入索引 / cron 计划 / 开关。

与 sync_yzj_minutes.py 的关系：
- 本模块是通用引擎，读配置、按任务拉取、自动识别附件控件（不写死 Od_0）。
- sync_yzj_minutes.py 的纪要专用逻辑可保留作兼容入口，后续迁移到本引擎。

注意：云之家不同业务单据的表单控件 code 各不相同（会议纪要用 Od_0），
故附件识别改为「遍历 form_data 中所有 onlineDocumentWidget 控件」，而非写死某个 code。
"""
import os
import sys
import re
import json
import time
import datetime
import logging

logger = logging.getLogger("yzj_pull")

# ---------------- 任务运行态（供前端轮询 / 中止） ----------------
# _PROGRESS[task_id] = {"running": bool, "aborted": bool, "total": int,
#                       "processed": int, "stats": {...}, "started_at": ts, "done_at": ts}
# _ABORT[task_id]    = True 时 run_task 在下一轮循环break退出。
_PROGRESS = {}
_ABORT = {}


def get_progress(task_id):
    return _PROGRESS.get(task_id)


def request_abort(task_id):
    _ABORT[task_id] = True
    p = _PROGRESS.get(task_id)
    if p:
        p["aborted"] = True


def reset_progress(task_id):
    _PROGRESS.pop(task_id, None)
    _ABORT.pop(task_id, None)


def flush_task_synced(task_id):
    """兜底保存去重记录。

    run_task 正常情况下每条处理完都会增量保存；但若因未预料的异常提前退出，
    调用方（调度器 / 接口层）应在 finally 中调用本函数，把已处理部分的
    去重记录落盘，避免「文档已入库但去重丢失」造成下次重复拉取。
    """
    p = _PROGRESS.get(task_id)
    d = (p or {}).get("_synced")
    if not isinstance(d, dict) or not d:
        return False
    try:
        _save_synced(d)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("兜底保存去重记录失败: %s", e)
        return False


_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

# 配置落盘位置：与 system_settings.json 同级，独立于 SQLite，便于 git 跟踪/不跟踪均可。
TASKS_FILE = os.path.join(os.path.dirname(_HERE), "config", "yzj_pull_tasks.json")
# 去重记录：按 formInstId 防重复落盘（含文件名指纹，便于文件被清后重抓）
SYNCED_FILE = os.path.join(os.path.dirname(_HERE), "config", ".yzj_pull_synced.json")

DEFAULT_TASKS = {
    "tasks": [
        {
            "id": "minutes",
            "name": "会议纪要",
            "enabled": True,
            "form_code_id": "",            # 留空时按 template_name 模糊匹配 get_templates 结果
            "template_name": "会议纪要",
            "status": "FINISH",            # FINISH / RUNNING / 空(全部)
            "time_range": "all",           # all / recent_days / custom
            "recent_days": 7,
            "start_date": "",
            "end_date": "",
            "target_category": "会议纪要",
            "download_attachments": True,
            "index_into_kb": True,
            "batch_size": 10,              # 每次拉取最多处理的流程数（防 IP 被封）
            "interval_sec": 3,             # 每条流程处理完后的间隔秒数（限流）
            "schedule": "daily",           # manual / daily / weekly
            "schedule_hour": 2,
            "schedule_minute": 0,
            "schedule_weekday": 1,         # 0=周一 .. 6=周日（weekly 用）
        }
    ]
}


# ---------------- 配置读写 ----------------
def _ensure_config():
    if not os.path.exists(TASKS_FILE):
        os.makedirs(os.path.dirname(TASKS_FILE), exist_ok=True)
        with open(TASKS_FILE, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_TASKS, f, ensure_ascii=False, indent=2)
    return TASKS_FILE


def load_tasks():
    _ensure_config()
    try:
        with open(TASKS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "tasks" not in data:
            data = dict(DEFAULT_TASKS)
    except Exception as e:  # noqa: BLE001
        logger.warning("读取拉取任务配置失败，回退默认: %s", e)
        data = dict(DEFAULT_TASKS)
    return data.get("tasks", [])


def save_tasks(tasks):
    _ensure_config()
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump({"tasks": tasks}, f, ensure_ascii=False, indent=2)


def get_task(task_id):
    for t in load_tasks():
        if t.get("id") == task_id:
            return t
    return None


def upsert_task(task):
    tasks = load_tasks()
    replaced = False
    for i, t in enumerate(tasks):
        if t.get("id") == task.get("id"):
            tasks[i] = task
            replaced = True
            break
    if not replaced:
        tasks.append(task)
    save_tasks(tasks)
    return task


def delete_task(task_id):
    tasks = [t for t in load_tasks() if t.get("id") != task_id]
    save_tasks(tasks)


# ---------------- 去重 ----------------
def _load_synced():
    if not os.path.exists(SYNCED_FILE):
        return {}
    try:
        with open(SYNCED_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save_synced(d):
    """原子写入去重记录。

    先写临时文件再 os.replace 覆盖：拉取任务可能长时间运行，若直接写目标文件，
    进程在写一半时被中断会留下截断/损坏的 JSON，导致下次启动 `json.load` 失败、
    去重记录被整体丢弃（进而全量重复拉取）。原子替换可保证目标文件始终完整。
    """
    tmp = SYNCED_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SYNCED_FILE)
    except Exception:  # noqa: BLE001
        # 兜底：原子写失败时退回直接写，尽量不丢进度
        try:
            with open(SYNCED_FILE, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
        except Exception as e:  # noqa: BLE001
            logger.warning("保存去重记录失败: %s", e)
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:  # noqa: BLE001
                pass


# ---------------- 附件控件识别（通用，不写死 Od_0） ----------------
def _collect_attachment_files(form_data):
    """遍历 form_data(widgetMap) 中所有控件，提取文件列表。

    返回 [{file_id, file_name, widget_code, kind, fallback_id, fallback_kind}, ...]

    云之家存在「两种」附件载体，必须都支持，否则会漏掉整批单据：
    1) 普通附件控件（如 Ps_0 / Od_0）：value 是 list，元素含
       sealedFileId(盖章 pdf) / wpsFileId(原文件) / redFileId。
    2) 金山在线文档控件（如 Kg_0，type=kingGrid）：value 是 dict，含
       pdfFileId(盖章 pdf) / fileId(原 docx/doc) / fileName / ofdFileId。
       2024 年度那批纪要走的正是 Kg_0 —— 旧实现只认 list 型控件，
       导致这 54 条被误判为「无附件」而永久跳过（实测确认）。

    下载策略（在原有「优先盖章 PDF」基础上，增加兼容原文件格式）：
      优先取盖章版 PDF；仅当该单据没有生成盖章 PDF 时，才回退下载原文件
      （docx/doc/ofd 等）。kind 记录本次实际下载的是哪一类：
        "pdf"    → 盖章版 PDF，落盘扩展名 .pdf
        "origin" → 原文件，落盘扩展名沿用文件自身的 .docx/.doc/...（不再强改 .pdf）
    """
    files = []
    if not isinstance(form_data, dict):
        return files
    for code, wv in form_data.items():
        if not isinstance(wv, dict):
            continue
        val = wv.get("value")

        # 类型 2：金山在线文档控件（value 为 dict）
        if isinstance(val, dict):
            fname = (val.get("fileName") or val.get("name") or "")
            # 原文件（docx/doc）id，用作「盖章 PDF 不可用」时的回退
            origin_id = val.get("fileId") or val.get("kingGridWidgetFileId") or ""
            # 优先盖章 pdf（pdfFileId）→ 其次 OFD → 最后原文件（fileId，docx/doc）
            if val.get("pdfFileId"):
                fid, kind = val["pdfFileId"], "pdf"
            elif val.get("ofdFileId"):
                fid, kind = val["ofdFileId"], "ofd"
            elif val.get("fileId"):
                fid, kind = val["fileId"], "origin"
            elif val.get("kingGridWidgetFileId"):
                fid, kind = val["kingGridWidgetFileId"], "origin"
            else:
                continue
            files.append({
                "file_id": fid, "file_name": fname, "widget_code": code, "kind": kind,
                # 仅当主选是 PDF/OFD 时，才登记原文件做回退（避免无谓重复下载）
                "fallback_id": (origin_id if kind in ("pdf", "ofd") else ""),
                "fallback_kind": ("origin" if kind in ("pdf", "ofd") else ""),
            })
            continue

        # 类型 1：普通附件控件（value 为 list）
        if not isinstance(val, list):
            continue
        for item in val:
            if not isinstance(item, dict):
                continue
            # 优先盖章 pdf（sealedFileId）；回退原文件 wpsFileId（docx/doc）；
            # 最后红头文件实例 id（红头本身为 pdf）。
            fname = (item.get("wpsFileName") or item.get("sealedFileName")
                     or item.get("fileName") or item.get("name") or "")
            origin_id = item.get("wpsFileId") or ""
            if item.get("sealedFileId"):
                fid, kind = item["sealedFileId"], "pdf"
            elif item.get("wpsFileId"):
                fid, kind = item["wpsFileId"], "origin"
            elif item.get("redFileId"):
                fid, kind = item["redFileId"], "pdf"
            else:
                continue
            files.append({
                "file_id": fid, "file_name": fname, "widget_code": code, "kind": kind,
                "fallback_id": (origin_id if kind == "pdf" else ""),
                "fallback_kind": ("origin" if kind == "pdf" else ""),
            })
    return files


def _doc_date_prefix(form_data, flow=None):
    """取单据业务日期，生成「YYYYMMDD_」文件名前缀。

    背景：云之家同类单据的附件名常完全相同（实测「天传所集团生产经营工作
    例会会议纪要」一份模板下 15 份附件同名），落盘后重名难以区分。
    故用单据日期做前缀。优先级：
      1) Da_0  —— 业务/会议日期（语义最准，每条例会各不相同）
      2) _S_DATE / flow.createTime —— 单据提交时间
    取不到时返回空串（不强制加前缀）。
    """
    ts = None
    wm = form_data if isinstance(form_data, dict) else {}
    for code in ("Da_0", "_S_DATE"):
        v = (wm.get(code) or {}).get("value")
        if isinstance(v, (int, float)) and v > 0:
            ts = v
            break
    if ts is None and isinstance(flow, dict):
        for key in ("createTime", "finishTime", "submitTime"):
            v = flow.get(key)
            if isinstance(v, (int, float)) and v > 0:
                ts = v
                break
    if not ts:
        return ""
    try:
        if ts > 1e12:  # 毫秒
            ts = ts / 1000.0
        return datetime.datetime.fromtimestamp(ts).strftime("%Y%m%d") + "_"
    except Exception:  # noqa: BLE001
        return ""


# ---------------- 单次任务执行 ----------------
def _safe_filename(name):
    """把云之家返回的文件名清洗为可用作落盘的文件名（移除路径分隔与非法字符）。"""
    if not name:
        return "file"
    for ch in '/\\:*?"<>|':
        name = name.replace(ch, "_")
    name = name.strip().strip(".")
    return name or "file"


def run_task(task, dry_run=False, limit=None, force=False):
    """执行单个拉取任务。返回统计 dict。

    force=True 时忽略去重记录，强制重新拉取（即便文档仍存活）。
    """
    from yunzhijia_client import (
        get_templates, find_flows, view_form_inst, download_file,
    )
    # 注意：必须同时 `import kb_store`（模块对象）与 from-import（具体函数）。
    # 本文件历史上只写了 from-import，而下方存活校验用到了 `kb_store._load_uploads()`，
    # 触发 NameError 却被 `except Exception: _ups = []` 静默吞掉，导致存活集合恒为空、
    # 每次拉取都把所有已拉取单据判定为「文档不存在」而全量重拉——这正是长期
    # 反复重复拉取、重复文件堆积的根因。故此处显式导入模块对象。
    import kb_store  # noqa: F401  （模块对象，供下方 kb_store._load_uploads 等使用）
    from kb_store import save_upload_raw, update_upload_text_async, rebuild_index_only, _extract_year
    import extract_text
    from sync_yzj_minutes import _auto_classify

    task_id = task.get("id")
    stats = {"task": task.get("name"), "found": 0, "downloaded": 0, "tried": 0, "skipped": 0, "failed": 0, "errors": []}
    # 初始化运行态（供前端轮询/中止）
    _PROGRESS[task_id] = {
        "running": True, "aborted": False, "total": 0,
        "processed": 0, "stats": stats,
        "started_at": int(time.time()), "done_at": None,
        # 本次运行新落盘的文档（逐条追加，供前端「拉一个刷一个」实时显示）
        "new_docs": [],
    }
    _ABORT.pop(task_id, None)

    # 1) 解析目标模板 form_code_id（支持「一个任务匹配多个模板」）
    # 重要：云之家同名/近名模板常有多个（如「总经理会议纪要线上审批」「会议纪要线上审批发布」
    # 「资金管理委员会会议纪要」），旧实现 next(...) 只取第一个匹配，会漏掉其余模板的全部单据
    # （实测只拉到 7 条，而全量纪要 255 条）。findFlows 的 formCodeIds 本身是数组，
    # 故改为收集「所有」含关键词的模板，一次传入批量拉取。
    form_code_ids = []
    _raw_ids = task.get("form_code_id")
    if isinstance(_raw_ids, (list, tuple)):
        form_code_ids = [str(x) for x in _raw_ids if x]
    elif _raw_ids:
        form_code_ids = [str(_raw_ids)]
    # 任务可显式配置多个模板名（template_names 列表，逗号分隔字符串亦可）
    _names = task.get("template_names")
    if isinstance(_names, str):
        _names = [n.strip() for n in _names.split(",") if n.strip()]
    if not _names:
        _n = task.get("template_name")
        _names = [_n] if _n else []
    template_name = _names[0] if _names else ""
    if not form_code_ids and _names:
        try:
            templates = get_templates()
            for _n in _names:
                for t in templates:
                    cid = t.get("formCodeId")
                    if cid and _n in (t.get("title") or "") and cid not in form_code_ids:
                        form_code_ids.append(cid)
            if not form_code_ids:
                stats["errors"].append("未匹配到模板: %s" % ",".join(_names))
                return stats
            logger.info("模板关键词 %s 匹配到 %d 个模板: %s",
                        _names, len(form_code_ids), form_code_ids)
        except Exception as e:  # noqa: BLE001
            stats["errors"].append("拉取模板列表失败: %s" % e)
            return stats
    if not form_code_ids:
        stats["errors"].append("未配置模板（template_name / form_code_id）")
        return stats
    # 兼容后续单值引用
    form_code_id = form_code_ids[0]

    # 2) 时间范围
    ct_start = ct_end = None
    tr = task.get("time_range", "all")
    if tr == "recent_days":
        days = int(task.get("recent_days", 7) or 7)
        ct_end = int(time.time() * 1000)
        ct_start = ct_end - days * 86400 * 1000
    elif tr == "custom":
        fmt = "%Y-%m-%d"
        try:
            if task.get("start_date"):
                ct_start = int(datetime.datetime.strptime(task["start_date"], fmt).timestamp() * 1000)
            if task.get("end_date"):
                d = datetime.datetime.strptime(task["end_date"], fmt)
                d = d.replace(hour=23, minute=59, second=59)
                ct_end = int(d.timestamp() * 1000)
        except Exception as e:  # noqa: BLE001
            stats["errors"].append("时间范围解析失败: %s" % e)

    # 3) 拉流程列表（分页翻完全部，避免只拿首页导致第 N 页之后的单据永久丢失）
    ct_pair = [ct_start, ct_end] if (ct_start or ct_end) else None
    flows = []
    try:
        # 多模板时逐模板翻页（findFlows 对 formCodeIds 数组的组合过滤不可靠，
        # 逐模板拉取可确保每个模板的单据都被完整取到）
        for cid in form_code_ids:
            page = 1
            page_size = 50
            while True:
                resp = find_flows(
                    form_code_ids=[cid],
                    status=task.get("status") or None,
                    create_time=ct_pair,
                    page_number=page,
                    page_size=page_size,
                )
                batch = (resp or {}).get("list") or [] if isinstance(resp, dict) else (resp or [])
                flows.extend(batch)
                # 提前退出：本页不足一页（已到末页）
                if len(batch) < page_size:
                    break
                # 以服务端返回的 total 为准（有则用它，避免多翻一次空页）
                total = (resp or {}).get("total") if isinstance(resp, dict) else None
                try:
                    total = int(total) if total is not None else None
                except (TypeError, ValueError):  # noqa: BLE001
                    total = None
                if total is not None and len(batch) * page >= total:
                    break
                page += 1
                if page > 500:  # 安全阀，防接口异常时无限翻页
                    logger.warning("模板 %s 翻页超过 500 页，停止（已取 %d 条）", cid, len(flows))
                    break
            logger.info("模板 %s 取到 %d 条，累计 %d 条", cid, len(batch), len(flows))
    except Exception as e:  # noqa: BLE001
        stats["errors"].append("拉取流程列表失败: %s" % e)
        return stats

    stats["found"] = len(flows)
    if task_id in _PROGRESS:
        _PROGRESS[task_id]["total"] = len(flows)
        _PROGRESS[task_id]["stats"] = stats
    synced = _load_synced()
    # 把 synced 引用挂到运行态：若 run_task 因未预料异常提前退出，
    # 调用方可通过 flush_task_synced(task_id) 兜底保存，避免整轮进度丢失。
    if task_id in _PROGRESS:
        _PROGRESS[task_id]["_synced"] = synced
    cat = task.get("target_category") or "会议纪要"

    # 存活索引：用于「手动删除拉取文件后再次拉取能重新拉回」的判断。
    # 收集云之家来源且未软删的文档 doc_id 集合，以及文件名里的 inst_id 末6位后缀集合。
    try:
        _ups = kb_store._load_uploads() or []
    except Exception as _e:  # noqa: BLE001
        # 不可再静默吞异常：早期这里 `except: _ups=[]` 把 NameError 也吞掉，
        # 导致存活集合恒为空、每次全量重拉，且日志无踪迹、长期无法定位。
        # 现在显式告警，便于第一时间发现「读不到元数据」类问题。
        logger.error("读取上传元数据失败，存活校验将不可靠（可能导致重复拉取）: %s", _e)
        stats["errors"].append("读取元数据失败: %s" % _e)
        _ups = []
    if not _ups:
        logger.warning("上传元数据为空：若此前已拉取过文档，请检查 KB_ROOT / 元数据文件路径，"
                       "否则本次会把已拉取单据误判为丢失而重复拉取")
    alive_doc_ids = set()
    alive_suffix = set()
    # 已存在的落盘文件名（小写）：用于「重名检测」——仅当新文件与既有文件重名时
    # 才加单据日期前缀，不重名的文件保持原名，避免无差别给所有文件加时间戳。
    existing_names = set()
    # 文件名(小写) → stored_path：用于「物理文件兜底校验」。
    # 当元数据读取异常（路径拼接问题 / 文件损坏 / 条目丢失）导致 alive_doc_ids
    # 为空时，仍可按文件名定位物理文件并校验其存在，避免误判已拉取内容为
    # 「不存在」而整批重拉（历史上一再发生重复拉取的根因）。
    name_to_path = {}
    for u in _ups:
        if not u.get("deleted"):
            _fn = (u.get("filename") or "").strip().lower()
            if _fn:
                existing_names.add(_fn)
                _sp = u.get("stored_path")
                if _sp:
                    name_to_path.setdefault(_fn, _sp)
        if u.get("source") == "yunzhijia" and not u.get("deleted"):
            did = u.get("doc_id")
            if did:
                alive_doc_ids.add(did)
            m = re.search(r"_([0-9A-Za-z]{6})\.", u.get("filename", ""))
            if m:
                alive_suffix.add(m.group(1))
    # 本轮内已用过的落盘名（同一轮里多份同名附件也要能区分）
    used_names = set()

    # 物理文件基准目录候选：stored_path 可能是 "files/xxx.pdf"（相对 uploads 目录）
    # 也可能是 "xxx.pdf"（相对 files 目录），逐个尝试，命中即用。
    _upload_bases = []
    for _cand in (getattr(kb_store, "UPLOAD_DIR", ""),
                  getattr(kb_store, "UPLOAD_FILES_DIR", ""),
                  os.path.dirname(getattr(kb_store, "UPLOAD_FILES_DIR", "") or "")):
        if _cand and os.path.isdir(_cand) and _cand not in _upload_bases:
            _upload_bases.append(_cand)

    # doc_id → 元数据条目：用于补提取时判断正文是否为空
    doc_entry = {}
    for _u in _ups:
        _did = _u.get("doc_id")
        if _did:
            doc_entry[_did] = _u

    def _reextract_missing(doc_ids, stats):
        """对「已落盘但正文为空」的文档补做提取（**不重新下载**）。

        场景：下载成功、落盘成功，但提取/索引环节失败（解析异常、进程中断等），
        此时去重记录已标记为已拉取，后续不会重下，导致文档长期正文为空、
        检索不到。这里在判定「已拉取」时顺带检查正文，为空则直接读物理文件
        重新提取，无需重新联网下载，代价极低。
        """
        if not doc_ids:
            return 0
        n = 0
        for _d in doc_ids:
            e = doc_entry.get(_d)
            if not e or (e.get("text") or "").strip():
                continue
            sp = e.get("stored_path")
            if not sp:
                continue
            raw = None
            for _b in _upload_bases:
                _p = os.path.join(_b, sp)
                if os.path.isfile(_p):
                    try:
                        with open(_p, "rb") as _fh:
                            raw = _fh.read()
                    except Exception:  # noqa: BLE001
                        raw = None
                    break
            if not raw:
                continue
            try:
                import extract_text
                from kb_store import update_upload_text_async, _extract_year
                _fn = e.get("filename") or ""
                _cat = e.get("category")
                _text, _w = extract_text.extract(raw, _fn, category=_cat)
                update_upload_text_async(_d, _text, _cat, _extract_year(_fn, _text))
                stats["reextracted"] = stats.get("reextracted", 0) + 1
                n += 1
                logger.info("补提取成功: %s（%d 字）", _fn, len(_text))
            except Exception as _ex:  # noqa: BLE001
                logger.warning("补提取失败 %s: %s", e.get("filename"), _ex)
        return n

    def _physically_exists(names, sizes):
        """物理文件兜底：按文件名找到 stored_path，校验文件存在且大小一致。"""
        if not names or not _upload_bases:
            return False
        for i, nm in enumerate(names):
            sp = name_to_path.get((nm or "").strip().lower())
            if not sp:
                continue
            for _b in _upload_bases:
                _p = os.path.join(_b, sp)
                if os.path.isfile(_p):
                    # 记了大小就一并校验，防止同名但内容不同被误判为已拉取
                    try:
                        if sizes and i < len(sizes) and sizes[i]:
                            if os.path.getsize(_p) == sizes[i]:
                                return True
                            continue
                    except Exception:  # noqa: BLE001
                        pass
                    return True if not sizes else False
        return False

    # 实际处理上限：取「调试 limit」与「任务配置的 batch_size」较小值。
    # batch_size 留空/0/负数 → 视为不限（按计划全部拉取）；
    # 仅当显式配置了正数时才限制单次处理条数。
    batch_size = task.get("batch_size")
    try:
        if batch_size is not None:
            batch_size = int(batch_size)
    except (TypeError, ValueError):  # noqa: BLE001
        batch_size = None
    if batch_size is not None and batch_size <= 0:
        batch_size = None  # 0 / 负数 → 不限
    if batch_size is not None and limit is not None:
        batch_size = min(batch_size, int(limit))
    elif limit is not None:
        batch_size = int(limit)
    interval_sec = task.get("interval_sec") or 0
    try:
        interval_sec = float(interval_sec)
    except (TypeError, ValueError):  # noqa: BLE001
        interval_sec = 0
    if interval_sec < 0:
        interval_sec = 0

    # 整轮运行时长上限（秒）：兜底防「卡住无进展」。
    # 单个网络请求本身有超时（view_form_inst 15s / download_file 60s），
    # 但异常组合下仍可能拖很久；超过上限则保存已处理进度后有序退出，
    # 配合增量保存的 synced，下次重跑会接着拉，不会重复已完成的单据。
    max_runtime_sec = task.get("max_runtime_sec", 3600)
    try:
        max_runtime_sec = float(max_runtime_sec)
    except (TypeError, ValueError):  # noqa: BLE001
        max_runtime_sec = 3600
    if max_runtime_sec <= 0:
        max_runtime_sec = 3600
    _t_start = time.time()

    processed = 0
    for idx, fl in enumerate(flows):
        # 中止检查：前端请求终止时，本轮之后立即退出（已下载的保留，未处理的跳过）
        if _ABORT.get(task_id):
            logger.info("任务 %s 被用户中止，已处理 %d/%d", task_id, processed, len(flows))
            stats["aborted"] = True
            break
        # 运行时长上限：超时则有序退出（进度已增量保存，下次续拉）
        if time.time() - _t_start > max_runtime_sec:
            logger.warning("任务 %s 达到运行时长上限 %ss，已处理 %d/%d 后退出（下次续拉）",
                           task_id, max_runtime_sec, processed, len(flows))
            stats["timeout"] = True
            break
        if batch_size is not None and processed >= batch_size:
            break
        inst_id = fl.get("formInstId") or fl.get("formInstId", "")
        if not inst_id:
            stats["skipped"] += 1
            continue
        # 标记：本条是否处于「自愈重校验」中（是则跳过 alive 判断直接重拉）
        _rechecking = False
        if inst_id in synced and not force:
            rec = synced[inst_id]
            note = rec.get("note")
            # 试跑记录：未落盘，正常跳过（下次正式拉取仍会处理）
            if rec.get("dry"):
                stats["skipped"] += 1
                continue
            # 自愈：旧版本只识别 list 型附件控件（Ps_0），把金山在线文档控件（Kg_0）
            # 的单据误判为「无附件」并永久跳过（实测 2024 年度有 54 条因此漏拉）。
            # 故对「无 ver 标记的旧 no-attachment 记录」重新校验一次：
            # 若确无附件则补写 ver=2 标记（此后不再重复校验），若有附件则清记录重拉。
            if note == "no-attachment":
                if rec.get("ver") is None:
                    # 自愈：清记录后必须直接进入下面的重拉流程。
                    # 注意：此处不能继续往下走 alive 判断 —— no-attachment 记录没有
                    # doc_ids，会 fallback 用「inst_id 末6位是否出现在旧文件名后缀里」
                    # 判断存活，一旦后缀撞上（生产机存在带 _XXXXXX 后缀的旧文件）
                    # 就会被误判为「已落盘」而 continue，导致自愈失效、
                    # 这批单据永远拉不下来（实测 2024 年度 47 条因此卡住）。
                    del synced[inst_id]
                    stats["recheck"] = stats.get("recheck", 0) + 1
                    logger.info("重新校验旧 no-attachment 记录: %s", inst_id)
                    _rechecking = True
                else:
                    stats["skipped"] += 1
                    continue
            if _rechecking:
                pass  # 自愈重校验：跳过 alive 判断，直接重拉
            else:
                # 文件可能因手动删除而不存在 → 校验是否仍存活，已删则移记录并重拉。
                # 三级校验，逐级放宽，最大程度避免「已拉取却被误判为不存在」：
                #   1) 元数据：doc_id 仍在未删文档中（最快、最准）
                #   2) 物理文件：按记录里的 names/sizes 定位文件并校验存在与大小
                #      （元数据读取异常时的兜底，历史重复拉取的主因）
                #   3) 旧格式兜底：文件名里的 inst_id 末6位后缀（仅无 doc_ids/names 时）
                doc_ids = rec.get("doc_ids") or []
                if doc_ids:
                    alive = any(d in alive_doc_ids for d in doc_ids)
                else:
                    alive = False
                if not alive:
                    alive = _physically_exists(rec.get("names") or [],
                                               rec.get("sizes") or [])
                if not alive and not doc_ids and not rec.get("names"):
                    # 最老格式记录（既无 doc_ids 也无 names）：用文件名后缀兜底
                    alive = inst_id[-6:] in alive_suffix
                if alive:
                    # 已拉取：顺带检查正文，为空则补提取（不重新下载）
                    if task.get("index_into_kb", True) and doc_ids:
                        _reextract_missing(doc_ids, stats)
                    stats["skipped"] += 1
                    continue
                # 文档确已不存在（手动删除或文件丢失）→ 清除去重记录，本次重新拉取
                del synced[inst_id]
        try:
            inst = view_form_inst(inst_id, form_code_id)
            form_info = (inst or {}).get("formInfo") or {}
            form_data = form_info.get("widgetMap") or {}
            files = _collect_attachment_files(form_data)
            if not files:
                # 无附件也记录已处理（避免每次重复拉详情）。
                # ver=2 表示「已用支持 Kg_0 的新逻辑校验过确无附件」，
                # 下次直接跳过，不再重复校验（否则自愈会每轮都重扫这些单据）。
                synced[inst_id] = {"ts": int(time.time()), "files": 0,
                                   "note": "no-attachment", "ver": 2}
                stats["skipped"] += 1
                continue
            if dry_run:
                # 试跑：仅预览命中，不下载、不落盘、不写去重记录（无损，不影响后续正式拉取）
                stats["tried"] = (stats.get("tried") or 0) + 1
                processed += 1
                continue
            saved_count = 0
            doc_ids = []
            # 同步记录文件名与字节大小：让去重记录「自包含」。
            # 早期只记 doc_ids，存活判断完全依赖能否读到元数据；一旦元数据
            # 读取异常（路径拼接问题 / 文件损坏）就会把已拉取内容误判为不存在
            # 而整批重拉。记下 names/sizes 后，即使元数据读不到，也能用
            # 物理文件兜底校验，避免重复拉取。
            doc_names = []
            doc_sizes = []
            for fi in files:
                fname = _safe_filename(fi.get("file_name") or ("file_%s" % fi["file_id"]))
                # download_file 写盘后返回路径；读 bytes 再删临时文件（与 sync_yzj_minutes 一致）
                import tempfile
                kind = fi.get("kind") or "pdf"
                raw = b""
                fb_id = fi.get("fallback_id") or ""
                # 策略：优先下载盖章版 PDF；只有拿不到可用的 PDF 才回退原文件（docx/doc）。
                # 这里的「拿不到可用」包含两种情形：
                #   1) 单据本就没有盖章 PDF（无 pdfFileId/sealedFileId，直接选的 origin）
                #   2) 有 pdfFileId 但下载失败 / 返回的不是 PDF（接口偶发、权限等）
                for attempt, (use_id, use_kind) in enumerate(
                        [(fi["file_id"], kind)] + ([(fb_id, "origin")] if fb_id else [])):
                    tmp = os.path.join(tempfile.gettempdir(),
                                       "yzj_%s_%s_%d" % (inst_id[-6:], use_id, attempt))
                    try:
                        download_file(use_id, tmp)
                        with open(tmp, "rb") as fh:
                            _raw = fh.read()
                    except Exception as e:  # noqa: BLE001
                        logger.warning("下载附件失败 %s (kind=%s): %s", use_id, use_kind, e)
                        _raw = b""
                    finally:
                        try:
                            if os.path.exists(tmp):
                                os.remove(tmp)
                        except Exception:  # noqa: BLE001
                            pass
                    if not _raw:
                        continue
                    # 校验：声明为 PDF 就必须真的是 PDF 字节；否则视为不可用，回退原文件
                    if use_kind == "pdf" and _raw[:4] != b"%PDF":
                        logger.warning("盖章 PDF 内容异常(非 PDF 头) fileId=%s，回退原文件", use_id)
                        continue
                    raw, kind = _raw, use_kind
                    break
                if not raw:
                    stats["failed"] += 1
                    logger.warning("附件下载失败（PDF 与原文件均不可用）: %s", fi.get("file_name"))
                    continue
                # 落盘文件名：扩展名必须与实际下载到的内容一致。
                # - kind="pdf"（盖章版 PDF）→ 统一 .pdf（与已验证的 sync_yzj_minutes 口径一致）
                # - kind="origin"（该单据无盖章 PDF，回退下载原文件）→ 沿用文件自身的
                #   .docx/.doc 扩展名，绝不改名为 .pdf。因为 extract_text 严格按扩展名
                #   路由（.pdf→PDF 解析器 / .docx→OOXML 解析器），名实不符会导致
                #   原文件被 PDF 解析器去解，实测会少提取约 3% 文本。
                # 同时不再拼接 inst_id 片段（doc_id 已全局唯一，避免文件名末尾多出随机码）。
                base, decl_ext = os.path.splitext(fname)
                # 防御：原文件名可能把扩展名写进了主干（如 "xx.docx.pdf"），先剥一层
                for _e in (".docx", ".doc", ".pdf", ".ofd", ".xlsx", ".xls"):
                    if base.lower().endswith(_e.lower()):
                        base = base[: -len(_e)]
                        break
                base = base.strip() or ("file_%s" % fi["file_id"])
                # 注意：此处的 kind 必须是「实际下载成功」的那个（可能在上面的
                # PDF 校验失败后已回退为 origin），故不能再从 fi 重新取值覆盖。
                if kind == "pdf":
                    uniq = base + ".pdf"
                elif kind == "ofd":
                    uniq = base + ".ofd"
                else:  # origin：沿用原文件扩展名，拿不到则按 .docx 兜底
                    uniq = base + (decl_ext.lower() if decl_ext else ".docx")
                # 重名处理：仅当该落盘名「已被占用」时才加单据日期前缀。
                # 同类单据附件名常完全相同（实测同一模板下 32 份
                # 「天传所集团生产经营工作例会会议纪要」重名），但绝大多数文件
                # 并不重名——故只在真正冲突时加前缀，不重名的保持原文件名，
                # 避免无差别给所有文件都加时间戳。
                if uniq.strip().lower() in existing_names or uniq.strip().lower() in used_names:
                    _prefix = _doc_date_prefix(form_data, fl)
                    if _prefix and not base.startswith(_prefix):
                        uniq = _prefix + uniq
                    # 极端情况：加日期后仍重名（同一天多份同名），再补序号
                    if uniq.strip().lower() in existing_names or uniq.strip().lower() in used_names:
                        _stem, _dot, _extx = uniq.rpartition(".")
                        for _i in range(2, 100):
                            _cand = "%s(%d).%s" % (_stem, _i, _extx) if _dot else "%s(%d)" % (uniq, _i)
                            if _cand.strip().lower() not in existing_names and _cand.strip().lower() not in used_names:
                                uniq = _cand
                                break
                used_names.add(uniq.strip().lower())
                # 自动归类（按文件名/标题关键词匹配分类树，校验存在，否则兜底 target_category）
                category = _auto_classify(template_name, fl.get("title") or "", uniq) or cat
                doc_id = save_upload_raw(uniq, category, raw, source="yunzhijia")
                doc_ids.append(doc_id)
                doc_names.append(uniq)
                doc_sizes.append(len(raw))
                # 逐条追加到运行态，前端轮询即可「拉一个显示一个」
                if task_id in _PROGRESS:
                    _PROGRESS[task_id]["new_docs"].append({
                        "doc_id": doc_id,
                        "filename": uniq,
                        "category": category,
                        "year": _extract_year(uniq, ""),
                        "chars": None,
                        "indexed": 0,
                        # 与 kb_store._now() 同格式（UTC "YYYY-MM-DD HH:MM:SS"）
                        "created_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    })
                if task.get("index_into_kb", True):
                    try:
                        text, _warn = extract_text.extract(raw, uniq, category=category)
                        update_upload_text_async(doc_id, text, category, _extract_year(uniq, text))
                    except Exception as e:  # noqa: BLE001
                        logger.warning("提取索引失败 %s: %s", doc_id, e)
                saved_count += 1
            synced[inst_id] = {"ts": int(time.time()), "files": saved_count,
                               "doc_ids": doc_ids,
                               "names": doc_names, "sizes": doc_sizes}
            stats["downloaded"] += 1 if saved_count else 0
            processed += 1
        except Exception as e:  # noqa: BLE001
            stats["failed"] += 1
            stats["errors"].append("%s: %s" % (inst_id, e))
            logger.warning("处理流程 %s 失败: %s", inst_id, e)
            processed += 1

        # 实时回写进度（供前端轮询）
        if task_id in _PROGRESS:
            _PROGRESS[task_id]["processed"] = processed
            _PROGRESS[task_id]["stats"] = stats

        # 增量持久化：每处理完一条就落盘去重记录。
        # 旧实现只在整轮结束后保存一次，导致任务中途中断（网络/重启/超时）时
        # 「文档已入库但去重记录丢失」，下次重跑会重复拉取、且总停在同一位置。
        # 每条保存后即可断点续传，中断也不丢进度。
        try:
            _save_synced(synced)
        except Exception as e:  # noqa: BLE001
            logger.warning("增量保存去重记录失败: %s", e)

        # 限流：每条流程处理完后间隔 interval_sec 秒，防止请求过密被封 IP
        if interval_sec > 0 and not (batch_size is not None and processed >= batch_size):
            time.sleep(interval_sec)

    _save_synced(synced)
    # 标记运行态结束
    if task_id in _PROGRESS:
        _PROGRESS[task_id]["running"] = False
        _PROGRESS[task_id]["done_at"] = int(time.time())
        _PROGRESS[task_id]["stats"] = stats
    return stats


# ---------------- 调度器（APScheduler 后台线程） ----------------
_scheduler = None


def _cron_trigger_for(task):
    sched = task.get("schedule", "manual")
    if sched == "daily":
        return "cron", {"hour": int(task.get("schedule_hour", 2)), "minute": int(task.get("schedule_minute", 0))}
    if sched == "weekly":
        return "cron", {"day_of_week": int(task.get("schedule_weekday", 1)),
                        "hour": int(task.get("schedule_hour", 2)),
                        "minute": int(task.get("schedule_minute", 0))}
    return None, None


def start_scheduler():
    """由 serve.py 启动时调用。注册所有 enabled 且有 cron 计划的任务。"""
    global _scheduler
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:  # noqa: BLE001
        logger.warning("未安装 apscheduler，云之家拉取定时调度不可用（手动触发仍可用）")
        return None
    if _scheduler is not None:
        return _scheduler
    _scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    for task in load_tasks():
        if not task.get("enabled"):
            continue
        kind, trig = _cron_trigger_for(task)
        if kind is None:
            continue
        tid = task.get("id")
        _scheduler.add_job(lambda t=task: _safe_run(t), kind, **trig, id="yzj_pull_%s" % tid, replace_existing=True)
        logger.info("注册云之家拉取任务: %s (%s)", tid, trig)
    _scheduler.start()
    return _scheduler


def _safe_run(task):
    tid = task.get("id")
    try:
        run_task(task)
    except Exception as e:  # noqa: BLE001
        logger.warning("云之家拉取任务 %s 执行异常: %s", task.get("name"), e)
    finally:
        # 兜底：异常退出时也要把已处理部分的去重记录落盘（增量保存之外的保险）
        flush_task_synced(tid)


def shutdown_scheduler():
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)


def list_scheduler_jobs():
    if _scheduler is None:
        return []
    return [{"id": j.id, "next_run": str(j.next_run_time)} for j in _scheduler.get_jobs()]


# ---------------- CLI 入口（供生产 cron / systemd timer 调用，免 APScheduler 依赖） ----------------
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="云之家审批单据拉取（按 config/yzj_pull_tasks.json 任务）")
    ap.add_argument("--run", help="执行指定任务 id（enabled 校验后仍执行）")
    ap.add_argument("--run-all", action="store_true", help="执行全部已启用任务")
    ap.add_argument("--dry", action="store_true", help="只列出可拉取单据，不下载不入库")
    ap.add_argument("--limit", type=int, default=None, help="每个任务最多处理的流程数（调试用）")
    ap.add_argument("--force", action="store_true", help="忽略去重记录，强制重新拉取")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    tasks = load_tasks()
    if args.run:
        t = next((x for x in tasks if x.get("id") == args.run), None)
        if not t:
            print("[错误] 任务不存在: %s" % args.run); sys.exit(2)
        print(run_task(t, dry_run=args.dry, limit=args.limit, force=args.force))
    elif args.run_all:
        for t in tasks:
            if not t.get("enabled"):
                continue
            print("== 任务 %s ==" % t.get("name"))
            print(run_task(t, dry_run=args.dry, limit=args.limit, force=args.force))
    else:
        print("用法: python app/yzj_pull.py --run <task_id> | --run-all [--dry] [--limit N]")
        print("已配置任务: %s" % [t.get("id") for t in tasks])

