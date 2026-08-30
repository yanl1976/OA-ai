# -*- coding: utf-8 -*-
"""清空云之家已拉取的文档与去重记录，以便「从零重新拉取」。

适用场景：拉取过程中反复中断/调试，导致历史数据混乱（重名、名实不符、重复副本等），
直接用本脚本归零，再重新执行拉取任务，得到一份干净完整的数据。

会做两件事：
  1) 彻底删除 source=yunzhijia 的文档（物理文件 + 元数据条目 + 重建索引），
     默认**只删今天拉取的**（--today），也可 --all 删除全部云之家文档。
  2) 清空去重记录 config/.yzj_pull_synced.json（备份为 .bak），
     使下次拉取能从头重拉（否则已记录的会被跳过）。

**不会**碰手动上传的文档。

用法（默认预览，加 --apply 才实际执行）：
    python3 scripts/reset_yzj_pull.py                 # 预览：删今天的 + 清去重
    python3 scripts/reset_yzj_pull.py --apply         # 执行
    python3 scripts/reset_yzj_pull.py --all --apply   # 删除全部云之家文档（不限今天）
    python3 scripts/reset_yzj_pull.py --keep-synced --apply   # 只删文档，不清去重
"""
import os
import sys
import io
import json
import shutil
import argparse
import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "app"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际执行（默认仅预览）")
    ap.add_argument("--all", action="store_true",
                    help="删除全部云之家文档（默认只删今天拉取的）")
    ap.add_argument("--keep-synced", action="store_true", help="不清空去重记录")
    ap.add_argument("--meta", default="", help="显式指定 user_documents.json 路径")
    ap.add_argument("--synced", default="", help="显式指定 .yzj_pull_synced.json 路径")
    args = ap.parse_args()

    import kb_store

    ups = kb_store._load_uploads() or []
    meta_used = getattr(kb_store, "UPLOAD_FILE", "")
    if not ups and args.meta and os.path.isfile(args.meta):
        _d = json.load(open(args.meta, encoding="utf-8"))
        if isinstance(_d, dict):
            _d = _d.get("items") or _d.get("uploads") or []
        ups, meta_used = _d, args.meta
    if not ups:
        for cand in (os.path.join(_ROOT, "knowledge_base", "uploads", "user_documents.json"),
                     "/opt/OA-ai/knowledge_base/uploads/user_documents.json"):
            if os.path.isfile(cand):
                _d = json.load(open(cand, encoding="utf-8"))
                if isinstance(_d, dict):
                    _d = _d.get("items") or _d.get("uploads") or []
                ups, meta_used = _d, cand
                break

    print("元数据:", meta_used, "| 总条数:", len(ups))

    yzj = [u for u in ups if u.get("source") == "yunzhijia"]
    print("云之家文档数:", len(yzj))
    if not ups:
        print("未加载到元数据，请用 --meta 显式指定。")
        return 1

    if not args.all:
        today = datetime.date.today().isoformat()   # YYYY-MM-DD
        todo = [u for u in yzj if (u.get("created_at") or "").startswith(today)]
        print("今天(%s)拉取的: %d 条（其余 %d 条保留）" % (today, len(todo), len(yzj) - len(todo)))
    else:
        todo = yzj
        print("将删除全部云之家文档: %d 条" % len(todo))

    # 去重记录
    synced_path = args.synced or os.path.join(_ROOT, "config", ".yzj_pull_synced.json")
    if not os.path.isfile(synced_path):
        synced_path = "/opt/OA-ai/config/.yzj_pull_synced.json"
    synced_n = 0
    if os.path.isfile(synced_path):
        try:
            sd = json.load(open(synced_path, encoding="utf-8"))
            synced_n = len(sd) if isinstance(sd, dict) else 0
        except Exception:  # noqa: BLE001
            synced_n = 0
    print("去重记录:", synced_path, "| 条数:", synced_n)

    print("\n" + "=" * 72)
    print("待删除文档: %d 条" % len(todo))
    for u in todo[:10]:
        print("   ", u.get("filename"), "| created=", u.get("created_at"))
    if len(todo) > 10:
        print("    ...(还有 %d 条)" % (len(todo) - 10))
    if not args.keep_synced:
        print("清空去重记录: %d 条（备份为 .bak）" % synced_n)
    print("模式:", "实际执行" if args.apply else "预览（不改动）")
    print("=" * 72)

    if not args.apply:
        print("\n确认无误后加 --apply 执行。")
        return 0

    # 1) 删除文档
    doc_ids = [u.get("doc_id") for u in todo if u.get("doc_id")]
    removed = 0
    if doc_ids and hasattr(kb_store, "delete_uploads_batch"):
        try:
            res = kb_store.delete_uploads_batch(doc_ids)
            removed = res.get("deleted") or 0
            print("已彻底删除文档: %d 条（含物理文件）" % removed)
        except Exception as e:  # noqa: BLE001
            print("批量删除失败: %s" % repr(e)[:200])
            return 1
    elif doc_ids:
        # 兜底：逐个删（物理文件 + 元数据条目）
        for did in doc_ids:
            try:
                kb_store.delete_upload(did)
                removed += 1
            except Exception:  # noqa: BLE001
                pass
        print("已删除文档: %d 条" % removed)

    # 2) 清空去重记录
    if not args.keep_synced and os.path.isfile(synced_path):
        try:
            shutil.copy(synced_path, synced_path + ".bak")
            with open(synced_path, "w", encoding="utf-8") as f:
                json.dump({}, f, ensure_ascii=False, indent=2)
            print("去重记录已清空（备份: %s.bak）" % synced_path)
        except Exception as e:  # noqa: BLE001
            print("清空去重记录失败: %s" % repr(e)[:200])

    print("\n完成。现在可在界面执行「立即拉取」，将从零重新拉取全部单据。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
