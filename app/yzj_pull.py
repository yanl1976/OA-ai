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
    with open(SYNCED_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


# ---------------- 附件控件识别（通用，不写死 Od_0） ----------------
def _collect_attachment_files(form_data):
    """遍历 form_data(widgetMap) 中所有控件，提取文件列表。

    返回 [{file_id, file_name, widget_code}, ...]
    云之家不同业务单据控件 code 不同，且本库 widgetMap 控件不含 widgetType 字段，
    故改为「控件 value 为 list 且元素含文件 id 字段(sealedFileId/wpsFileId/fileId)」
    即判定为附件控件，遍历全部而非写死某个 code。
    """
    files = []
    if not isinstance(form_data, dict):
        return files
    for code, wv in form_data.items():
        if not isinstance(wv, dict):
            continue
        vals = wv.get("value")
        if not isinstance(vals, list):
            continue
        for item in vals:
            if not isinstance(item, dict):
                continue
            # 与 sync_yzj_minutes.py 一致：优先取盖章 pdf（sealedFileId），
            # 否则回退 wpsFileId（原文件）/ redFileId（红头文件实例 id）。
            fid = item.get("sealedFileId") or item.get("wpsFileId") or item.get("redFileId")
            if not fid:
                continue
            fname = (item.get("wpsFileName") or item.get("sealedFileName")
                     or item.get("fileName") or item.get("name") or "")
            files.append({"file_id": fid, "file_name": fname, "widget_code": code})
    return files


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
    }
    _ABORT.pop(task_id, None)

    # 1) 解析目标模板 form_code_id
    form_code_id = task.get("form_code_id")
    template_name = task.get("template_name")
    if not form_code_id and template_name:
        try:
            templates = get_templates()
            match = next((t for t in templates if template_name in (t.get("title") or "")), None)
            if match:
                form_code_id = match.get("formCodeId")
            else:
                stats["errors"].append("未匹配到模板: %s" % template_name)
                return stats
        except Exception as e:  # noqa: BLE001
            stats["errors"].append("拉取模板列表失败: %s" % e)
            return stats

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
        page = 1
        page_size = 50
        while True:
            resp = find_flows(
                form_code_ids=[form_code_id] if form_code_id else None,
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
            if total is not None and len(flows) >= total:
                break
            page += 1
            if page > 500:  # 安全阀，防接口异常时无限翻页
                logger.warning("翻页超过 500 页，停止继续翻页（已取 %d 条）", len(flows))
                break
    except Exception as e:  # noqa: BLE001
        stats["errors"].append("拉取流程列表失败: %s" % e)
        return stats

    stats["found"] = len(flows)
    if task_id in _PROGRESS:
        _PROGRESS[task_id]["total"] = len(flows)
        _PROGRESS[task_id]["stats"] = stats
    synced = _load_synced()
    cat = task.get("target_category") or "会议纪要"

    # 存活索引：用于「手动删除拉取文件后再次拉取能重新拉回」的判断。
    # 收集云之家来源且未软删的文档 doc_id 集合，以及文件名里的 inst_id 末6位后缀集合。
    try:
        _ups = kb_store._load_uploads() or []
    except Exception:  # noqa: BLE001
        _ups = []
    alive_doc_ids = set()
    alive_suffix = set()
    for u in _ups:
        if u.get("source") == "yunzhijia" and not u.get("deleted"):
            did = u.get("doc_id")
            if did:
                alive_doc_ids.add(did)
            m = re.search(r"_([0-9A-Za-z]{6})\.", u.get("filename", ""))
            if m:
                alive_suffix.add(m.group(1))

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

    processed = 0
    for idx, fl in enumerate(flows):
        # 中止检查：前端请求终止时，本轮之后立即退出（已下载的保留，未处理的跳过）
        if _ABORT.get(task_id):
            logger.info("任务 %s 被用户中止，已处理 %d/%d", task_id, processed, len(flows))
            stats["aborted"] = True
            break
        if batch_size is not None and processed >= batch_size:
            break
        inst_id = fl.get("formInstId") or fl.get("formInstId", "")
        if not inst_id:
            stats["skipped"] += 1
            continue
        if inst_id in synced and not force:
            rec = synced[inst_id]
            note = rec.get("note")
            # 无附件 / 试跑 记录：本就无文件，保持跳过
            if note == "no-attachment" or rec.get("dry"):
                stats["skipped"] += 1
                continue
            # 文件可能因手动删除而不存在 → 校验文档是否仍存活，已删则移记录并重拉
            doc_ids = rec.get("doc_ids") or []
            if doc_ids:
                alive = any(d in alive_doc_ids for d in doc_ids)
            else:
                # 旧格式记录无 doc_ids：用文件名后缀兜底判断
                alive = inst_id[-6:] in alive_suffix
            if alive:
                stats["skipped"] += 1
                continue
            # 文档已不存在（被手动删除）→ 清除去重记录，本次重新拉取
            del synced[inst_id]
        try:
            inst = view_form_inst(inst_id, form_code_id)
            form_info = (inst or {}).get("formInfo") or {}
            form_data = form_info.get("widgetMap") or {}
            files = _collect_attachment_files(form_data)
            if not files:
                # 无附件也记录已处理（避免每次重复拉详情）
                synced[inst_id] = {"ts": int(time.time()), "files": 0, "note": "no-attachment"}
                stats["skipped"] += 1
                continue
            if dry_run:
                # 试跑：仅预览命中，不下载、不落盘、不写去重记录（无损，不影响后续正式拉取）
                stats["tried"] = (stats.get("tried") or 0) + 1
                processed += 1
                continue
            saved_count = 0
            doc_ids = []
            for fi in files:
                fname = _safe_filename(fi.get("file_name") or ("file_%s" % fi["file_id"]))
                # download_file 写盘后返回路径；读 bytes 再删临时文件（与 sync_yzj_minutes 一致）
                import tempfile
                tmp = os.path.join(tempfile.gettempdir(), "yzj_%s_%s" % (inst_id[-6:], fi["file_id"]))
                try:
                    download_file(fi["file_id"], tmp)
                    with open(tmp, "rb") as fh:
                        raw = fh.read()
                except Exception as e:  # noqa: BLE001
                    stats["failed"] += 1
                    logger.warning("下载附件失败 %s: %s", fi.get("file_id"), e)
                    continue
                finally:
                    try:
                        if os.path.exists(tmp):
                            os.remove(tmp)
                    except Exception:  # noqa: BLE001
                        pass
                if not raw:
                    stats["failed"] += 1
                    continue
                # 与 sync_yzj_minutes.py 保持一致（已验证通过的口径）：
                # 1) 下载的是「盖章版 PDF」，故落盘文件名统一 .pdf —— 去掉原文件名的
                #    .docx/.doc/.pdf 等扩展名后统一加 .pdf，避免「内容=PDF 但文件名=docx」
                #    导致 extract_text 按 docx 解析器去解 PDF 字节流而提取失败。
                # 2) 不再拼接 inst_id 片段（doc_id 已全局唯一，避免文件名末尾多出随机码）。
                base, _decl_ext = os.path.splitext(fname)
                for _e in (".docx", ".doc", ".pdf", ".DOCX", ".DOC", ".PDF"):
                    if base.lower().endswith(_e.lower()):
                        base = base[: -len(_e)]
                        break
                uniq = (base.strip() or ("file_%s" % fi["file_id"])) + ".pdf"
                # 自动归类（按文件名/标题关键词匹配分类树，校验存在，否则兜底 target_category）
                category = _auto_classify(template_name, fl.get("title") or "", uniq) or cat
                doc_id = save_upload_raw(uniq, category, raw, source="yunzhijia")
                doc_ids.append(doc_id)
                if task.get("index_into_kb", True):
                    try:
                        text, _warn = extract_text.extract(raw, uniq, category=category)
                        update_upload_text_async(doc_id, text, category, _extract_year(uniq, text))
                    except Exception as e:  # noqa: BLE001
                        logger.warning("提取索引失败 %s: %s", doc_id, e)
                saved_count += 1
            synced[inst_id] = {"ts": int(time.time()), "files": saved_count, "doc_ids": doc_ids}
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
    try:
        run_task(task)
    except Exception as e:  # noqa: BLE001
        logger.warning("云之家拉取任务 %s 执行异常: %s", task.get("name"), e)


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

