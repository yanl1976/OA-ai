# -*- coding: utf-8 -*-
"""给已有去重记录回填 names/sizes，让记录「自包含」。

背景：早期 synced 记录只存 doc_ids，存活判断完全依赖能否读到元数据。
一旦元数据读取异常（路径拼接问题 / 文件损坏 / 条目丢失），已拉取内容会被
误判为「不存在」而整批重拉——这是历史上反复重复拉取的根因。

新版落盘时会写入 names（文件名）与 sizes（字节大小），使记录自带校验信息；
本脚本用于把**已存在的旧记录**也补齐，之后即使元数据读不到，也能靠物理文件
兜底校验，避免重复拉取。

用法（默认预览，加 --apply 才实际写入）：
    python3 scripts/backfill_synced_meta.py \
      --meta /opt/OA-ai/knowledge_base/uploads/user_documents.json
    # 确认后加 --apply
"""
import os
import sys
import io
import json
import shutil
import argparse

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "app"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际写入（默认仅预览）")
    ap.add_argument("--meta", default="", help="user_documents.json 路径")
    ap.add_argument("--synced", default="", help=".yzj_pull_synced.json 路径")
    args = ap.parse_args()

    # 定位元数据
    meta = args.meta
    if not meta or not os.path.isfile(meta):
        for cand in (os.path.join(_ROOT, "knowledge_base", "uploads", "user_documents.json"),
                     "/opt/OA-ai/knowledge_base/uploads/user_documents.json"):
            if os.path.isfile(cand):
                meta = cand
                break
    if not meta or not os.path.isfile(meta):
        print("未找到元数据文件，请用 --meta 指定。")
        return 1

    ups = json.load(open(meta, encoding="utf-8"))
    if isinstance(ups, dict):
        ups = ups.get("items") or ups.get("uploads") or []
    print("元数据:", meta, "| 条数:", len(ups))

    # doc_id → (filename, size)
    info = {}
    base_cands = []
    try:
        import kb_store
        for c in (getattr(kb_store, "UPLOAD_DIR", ""),
                  getattr(kb_store, "UPLOAD_FILES_DIR", ""),
                  os.path.dirname(getattr(kb_store, "UPLOAD_FILES_DIR", "") or "")):
            if c and os.path.isdir(c) and c not in base_cands:
                base_cands.append(c)
    except Exception:  # noqa: BLE001
        pass
    if not base_cands:
        _up = os.path.dirname(meta)
        base_cands = [_up, os.path.join(_up, "files")]

    def real_size(sp):
        for b in base_cands:
            p = os.path.join(b, sp)
            if os.path.isfile(p):
                try:
                    return os.path.getsize(p)
                except Exception:  # noqa: BLE001
                    return None
        return None

    for u in ups:
        did = u.get("doc_id")
        if not did:
            continue
        sz = real_size(u.get("stored_path") or "") if u.get("stored_path") else None
        info[did] = (u.get("filename") or "", sz)

    # 定位 synced
    synced_path = args.synced
    if not synced_path or not os.path.isfile(synced_path):
        for cand in (os.path.join(_ROOT, "config", ".yzj_pull_synced.json"),
                     "/opt/OA-ai/config/.yzj_pull_synced.json"):
            if os.path.isfile(cand):
                synced_path = cand
                break
    if not synced_path or not os.path.isfile(synced_path):
        print("未找到去重记录文件，请用 --synced 指定。")
        return 1

    synced = json.load(open(synced_path, encoding="utf-8"))
    print("去重记录:", synced_path, "| 条数:", len(synced))

    todo, already, missing = 0, 0, 0
    for k, rec in synced.items():
        if not isinstance(rec, dict):
            continue
        if rec.get("names") and rec.get("sizes"):
            already += 1
            continue
        ids = rec.get("doc_ids") or []
        if not ids:
            continue
        names, sizes = [], []
        ok = True
        for d in ids:
            if d in info:
                fn, sz = info[d]
                names.append(fn)
                sizes.append(sz)
            else:
                ok = False
        if ok and names and all(s is not None for s in sizes):
            rec["names"] = names
            rec["sizes"] = sizes
            todo += 1
        else:
            missing += 1

    print("\n" + "=" * 72)
    print("已自包含(跳过): %d" % already)
    print("可回填: %d" % todo)
    print("信息不全(元数据中找不到对应文档，将保持原样): %d" % missing)
    print("模式:", "实际写入" if args.apply else "预览（不改动）")
    print("=" * 72)

    if not args.apply:
        print("\n确认后加 --apply 写入。")
        return 0

    try:
        shutil.copy(synced_path, synced_path + ".bak")
        with open(synced_path, "w", encoding="utf-8") as f:
            json.dump(synced, f, ensure_ascii=False, indent=2)
        print("\n已回填 %d 条记录（备份: %s.bak）" % (todo, synced_path))
    except Exception as e:  # noqa: BLE001
        print("写入失败: %s" % repr(e)[:200])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
