"""云之家会议纪要 -> 知识库 同步脚本（独立进程）。

功能：
  定时/手动拉取云之家「总经理会议纪要线上审批」「会议纪要线上审批发布」两个审批流
  的已完成纪要，下载盖章 pdf，提取文本，写入知识库（分类=会议纪要树），并重建索引。

设计：
  - 两个模板分别映射到分类「总经理会议纪要」「会议纪要」（extract_text 已注册 minutes
    后处理器，按红头/议题做规则结构化，零 LLM 成本）。
  - 去重：以审批单流水号 serialNo 为准（表单 _S_SERIAL 字段，稳定唯一）。已同步的
    serialNo 记录在 .yzj_synced.json，增量拉取遇到已同步即停止（按 finishTime 倒序）。
  - 入库两步法（与 serve.py 上传同源）：save_upload_raw(落盘二进制+占位) ->
    extract() 提取文本 -> update_upload_text_async(写文本+标记indexed) ->
    kb_store.rebuild_index_only() 重建 BM25+向量索引。

用法：
  python app/sync_yzj_minutes.py            # 增量同步（默认）
  python app/sync_yzj_minutes.py --full     # 忽略去重，全量重抓（用于初次灌库/补历史）
  python app/sync_yzj_minutes.py --dry      # 只列出可拉取的纪要，不下载不入库

依赖 .env：YUNZHIJIA_APP_ID / _APP_SECRET / _ECP_ID / _EID(可选) / _RESGROUP_SECRET
"""

import os
import sys
import json
import time
import argparse

# 使脚本可从项目根目录直接运行（python app/sync_yzj_minutes.py）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _PROJECT_ROOT)

from dotenv import load_dotenv
load_dotenv()

# 重要：.env 里可能含生产机 KB_ROOT（如 /opt/OA-ai/knowledge_base），会污染开发机
# 落盘路径，导致纪要写到错误目录。同步脚本强制以本地项目根为 KB_ROOT。
os.environ["KB_ROOT"] = _PROJECT_ROOT

import yunzhijia_client as yzj
import kb_store
import extract_text

# 待同步的审批流模板（仅列模板名，归类为「自动获取类别」，见 _auto_classify）
TEMPLATE_TARGETS = [
    {"name": "总经理会议纪要线上审批"},
    {"name": "会议纪要线上审批发布"},
]

SYNCED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           ".yzj_synced.json")


def _auto_classify(template_name, title, fname):
    """自动获取类别：按审批单标题/文件名关键词精细匹配分类树子节点，
    否则按模板名粗匹配，最后兜底到父级「会议纪要」。返回分类名。

    分类树节点（由用户在系统设置-分类管理中补齐，脚本只消费、不创建）：
      会议纪要
        ├─ 周工作例会会议纪要
        ├─ 总经理会会议纪要
        └─ 专题会议纪要
    """
    # 文件名(fname)来自红头文件标题=审批单标题，关键词最可靠；title 为兜底
    t = " ".join([fname or "", title or "", template_name or ""])
    # 1) 标题关键词细匹配（最可靠）
    if "周工作例会" in t:
        cand = "周工作例会会议纪要"
    elif "总经理" in t:
        cand = "总经理会会议纪要"
    elif "专题" in t:
        cand = "专题会议纪要"
    else:
        cand = None
    # 2) 模板名粗匹配（标题无明确信号时）
    if cand is None:
        if "总经理" in (template_name or ""):
            cand = "总经理会会议纪要"
        else:
            cand = "会议纪要"
    # 3) 校验分类树存在；不存在则回退父级「会议纪要」
    if kb_store.category_id_by_name(cand) is None:
        if kb_store.category_id_by_name("会议纪要") is not None:
            cand = "会议纪要"
        else:
            raise RuntimeError("分类树缺失『会议纪要』节点，请先在系统设置-分类管理中补齐")
    return cand


def _load_synced():
    if os.path.exists(SYNCED_FILE):
        try:
            with open(SYNCED_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def _save_synced(synced):
    with open(SYNCED_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(synced), f, ensure_ascii=False, indent=2)


def _resolve_code_map():
    """把模板名解析为 codeId。"""
    tpls = yzj.get_templates()
    code_map = {}
    for t in tpls:
        if t.get("title") in [x["name"] for x in TEMPLATE_TARGETS]:
            code_map[t["title"]] = t["formCodeId"]
    missing = [x["name"] for x in TEMPLATE_TARGETS if x["name"] not in code_map]
    if missing:
        raise RuntimeError("未找到目标模板：%s（请检查 .env 凭证或模板名）" % missing)
    return code_map


def _iter_flows(code_map, page_size=20):
    """生成 (模板名, 流程实例) 迭代器，按 finishTime 倒序。"""
    for tpl in TEMPLATE_TARGETS:
        name = tpl["name"]
        code = code_map[name]
        page = 1
        while True:
            flows = yzj.find_flows(form_code_ids=[code], status="FINISH",
                                   page_number=page, page_size=page_size)
            lst = flows.get("list", []) if isinstance(flows, dict) else flows
            if not lst:
                break
            for f in lst:
                yield name, f
            if len(lst) < page_size:
                break
            page += 1


def sync(full=False, dry=False, limit=None):
    synced = set() if full else _load_synced()
    code_map = _resolve_code_map()
    print("[sync] 目标模板:", {k: v[:8] + "..." for k, v in code_map.items()})

    total, added, skipped = 0, 0, 0
    per_tpl = {}  # 每模板已处理数，配合 limit
    for tpl_name, f in _iter_flows(code_map):
        if limit:
            n = per_tpl.get(tpl_name, 0)
            if n >= limit:
                continue
            per_tpl[tpl_name] = n + 1
        serial = f.get("serialNo") or f.get("flowInstId")
        title = f.get("title") or "(无标题)"
        fiid = f.get("formInstId")
        finish = f.get("finishTime")
        total += 1
        if not full and serial in synced:
            skipped += 1
            continue

        if dry:
            print("    (dry-run，跳过下载)")
            continue
        if not fiid:
            print("    [跳过] 无 formInstId")
            continue

        # 读表单，取纪要文件控件 Od_0
        inst = yzj.view_form_inst(fiid, code_map[tpl_name])
        wm = inst.get("formInfo", {}).get("widgetMap", {})
        od = wm.get("Od_0", {})
        files = od.get("value", []) or []
        if not files:
            print("    [跳过] Od_0 无文件")
            continue
        # 优先取盖章 pdf（sealedFileId），否则回退 wpsFileId
        fmeta = files[0]
        file_id = fmeta.get("sealedFileId") or fmeta.get("wpsFileId") \
            or fmeta.get("redFileId")
        if not file_id:
            print("    [跳过] 无可用 fileId")
            continue

        # 文件名用审批单标题（控件 Od_0 的 wpsFileName 已是"期号+红头"完整名，
        # 如 "2026年7月31日总经理办公会会议纪要2026第36期.docx"）。
        # 去掉 .docx/.pdf 后缀统一加 .pdf，sanitize 路径非法字符，保持可读性。
        # 回退顺序：wpsFileName -> sealedFileName -> 审批单 title -> 流水号。
        raw_title = (fmeta.get("wpsFileName") or fmeta.get("sealedFileName")
                     or f.get("title") or serial)
        base = raw_title
        for ext in (".docx", ".doc", ".pdf", ".PDF", ".DOCX"):
            if base.lower().endswith(ext.lower()):
                base = base[: -len(ext)]
                break
        safe_title = (base.replace("/", "-").replace("\\", "-")
                      .replace(":", "-").replace("*", "").replace("?", "")
                      .replace('"', "").replace("<", "").replace(">", "")
                      .replace("|", "").strip())
        if not safe_title:
            safe_title = serial
        fname = safe_title + ".pdf"
        # 自动获取类别（按文件名/标题/模板名匹配分类树，不写死）
        category = _auto_classify(tpl_name, title, fname)
        print("\n[%s] %s | serial=%s | 完成=%s | 归类=%s"
              % (tpl_name, title, serial, finish, category))
        try:
            pdf_path = "yzj_tmp_%s.pdf" % serial
            yzj.download_file(file_id, pdf_path)
            with open(pdf_path, "rb") as fh:
                raw = fh.read()
            os.remove(pdf_path)
        except Exception as e:
            print("    [下载失败] %s" % e)
            continue

        # 入库两步法（source="yunzhijia" 标记云之家拉取，与手动上传分开管理）
        doc_id = kb_store.save_upload_raw(fname, category, raw, source="yunzhijia")
        text, warn = extract_text.extract(raw, fname, category=category)
        kb_store.update_upload_text_async(doc_id, text, category,
                                          kb_store._extract_year(fname, text))
        print("    [入库] doc_id=%s 字数=%d %s"
              % (doc_id, len(text), ("warn=%s" % warn if warn else "")))
        synced.add(serial)
        added += 1

    _save_synced(synced)
    print("\n==== 同步完成 ==== 扫描=%d 新增=%d 已存在跳过=%d"
          % (total, added, skipped))

    if added and not dry:
        print("[sync] 重建索引（BM25+向量）...")
        kb_store.rebuild_index_only()
        print("[sync] 索引重建完成")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="全量重抓（忽略去重）")
    ap.add_argument("--dry", action="store_true", help="只列出可拉取，不下载不入库")
    ap.add_argument("--limit", type=int, default=None,
                    help="每模板最多同步 N 条（用于小批量验证）")
    args = ap.parse_args()
    try:
        print("[debug] kb_store.UPLOAD_FILE =", kb_store.UPLOAD_FILE)
        print("[debug] 当前 user_documents 条目数 =",
              len(kb_store._load_uploads()))
        sync(full=args.full, dry=args.dry, limit=args.limit)
        print("[debug] 同步后 user_documents 条目数 =",
              len(kb_store._load_uploads()))
    except Exception as e:
        print("[sync] 失败:", repr(e))
        sys.exit(1)
