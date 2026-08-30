# -*- coding: utf-8 -*-
"""恢复被软删除（deleted=1）的文档。

背景：文档删除是「软删」——仅置 deleted=1，**物理文件与正文 text 都仍在**，
      只是所有接口以 `not u.get("deleted")` 过滤，导致界面完全看不到。
      本脚本把 deleted 改回 0 即可无损恢复，**无需重新下载**。

实测（2026-08-30）：生产机 344 条元数据中「物理文件缺失: 0」，45 条软删文档的
      文件与正文全部完好（如「2025.3.11会议纪要.pdf」text=5046 字完好）。
      故遇到「列表在但文件看不到」应先恢复，而非重下载。

用法（默认预览，加 --apply 才实际执行）：
    # 恢复文件名含关键词的
    python3 scripts/restore_deleted_docs.py --match "2025.3.11" --match "呆滞物料"
    # 恢复全部被软删的云之家文档
    python3 scripts/restore_deleted_docs.py --source yunzhijia
    # 恢复全部，但跳过正文异常巨大的（如 text > 100 万字，多为 .doc 解析异常）
    python3 scripts/restore_deleted_docs.py --source yunzhijia --max-text 1000000
    # 恢复全部软删文档
    python3 scripts/restore_deleted_docs.py --all

注意：--match 可重复传多个，命中任意一个即恢复。
"""
import os
import sys
import io
import json
import argparse

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "app"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际恢复（默认仅预览）")
    ap.add_argument("--match", action="append", default=[],
                    help="文件名关键词，可重复传多个")
    ap.add_argument("--source", default="", help="按来源过滤，如 yunzhijia")
    ap.add_argument("--doc-id", action="append", default=[], help="按 doc_id 恢复，可重复")
    ap.add_argument("--rebuild-index", action="store_true",
                    help="恢复后重建检索索引（推荐，否则界面可能仍查不到）")
    ap.add_argument("--all", action="store_true", help="恢复全部软删文档")
    ap.add_argument("--max-text", type=int, default=0,
                    help="跳过正文长度超过该值的文档（0 表示不限制）")
    ap.add_argument("--meta", default="", help="显式指定 user_documents.json 路径")
    args = ap.parse_args()

    if not (args.match or args.source or args.all or args.doc_id):
        print("请指定范围：--match / --source / --doc-id / --all（详见 --help）")
        return 1

    import kb_store

    ups = kb_store._load_uploads() or []
    meta_used = getattr(kb_store, "UPLOAD_FILE", "")
    if not ups and args.meta and os.path.isfile(args.meta):
        _d = json.load(open(args.meta, encoding="utf-8"))
        if isinstance(_d, dict):
            _d = _d.get("items") or _d.get("uploads") or []
        ups, meta_used = _d, args.meta
    if not ups:
        # 兜底：按常见路径推断
        for cand in (os.path.join(_ROOT, "knowledge_base", "uploads", "user_documents.json"),
                     "/opt/OA-ai/knowledge_base/uploads/user_documents.json"):
            if os.path.isfile(cand):
                _d = json.load(open(cand, encoding="utf-8"))
                if isinstance(_d, dict):
                    _d = _d.get("items") or _d.get("uploads") or []
                ups, meta_used = _d, cand
                break

    print("元数据:", meta_used, "| 条数:", len(ups))
    deleted = [u for u in ups if u.get("deleted")]
    print("软删文档总数:", len(deleted))
    print("=" * 72)

    def selected(u):
        if args.doc_id:
            ok = (u.get("doc_id") or "") in args.doc_id
        elif args.all:
            ok = True
        elif args.match:
            fn = u.get("filename") or ""
            ok = any(m in fn for m in args.match)
        else:
            ok = True
        if ok and args.source:
            ok = (u.get("source") or "") == args.source
        if ok and args.max_text > 0:
            ok = len(u.get("text") or "") <= args.max_text
        return ok

    todo = [u for u in deleted if selected(u)]
    skipped_big = [u for u in deleted
                   if not selected(u) and args.max_text > 0
                   and len(u.get("text") or "") > args.max_text]

    for i, u in enumerate(todo, 1):
        print("%2d. %s" % (i, u.get("filename")))
        print("     created=%s | deleted_at=%s | text=%d字 | source=%s" % (
            u.get("created_at"), u.get("deleted_at"),
            len(u.get("text") or ""), u.get("source")))

    if skipped_big:
        print("\n因正文过大被跳过（--max-text=%d）:" % args.max_text)
        for u in skipped_big:
            print("   %s | text=%d字" % (u.get("filename"), len(u.get("text") or "")))

    print("\n" + "=" * 72)
    print("待恢复: %d 个" % len(todo))

    if not args.apply:
        print("\n预览模式，未改动。确认后加 --apply 执行恢复。")
        return 0

    n = 0
    for u in todo:
        u["deleted"] = 0
        u.pop("deleted_at", None)
        u.pop("deleted_by", None)
        n += 1
    try:
        if meta_used and os.path.isfile(meta_used):
            with open(meta_used + ".bak", "w", encoding="utf-8") as bf:
                json.dump(ups, bf, ensure_ascii=False)
            with open(meta_used, "w", encoding="utf-8") as f:
                json.dump(ups, f, ensure_ascii=False)
            print("已恢复 %d 个文档，元数据已保存（备份: %s.bak）" % (n, meta_used))
        else:
            kb_store._save_uploads(ups)
            print("已恢复 %d 个文档（kb_store 默认路径）" % n)
    except Exception as e:  # noqa: BLE001
        print("保存失败: %s" % repr(e)[:200])
        return 1

    if args.rebuild_index:
        print("\n=== 重建检索索引 ===")
        try:
            if hasattr(kb_store, "rebuild_index_only"):
                kb_store.rebuild_index_only()
                print("索引重建完成（rebuild_index_only）")
            elif hasattr(kb_store, "rebuild_index"):
                kb_store.rebuild_index()
                print("索引重建完成（rebuild_index）")
            else:
                print("未找到重建函数，请在界面「系统设置 → 重建索引」手动执行")
        except Exception as e:  # noqa: BLE001
            print("重建索引失败（可在界面手动重建）: %s" % repr(e)[:200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
