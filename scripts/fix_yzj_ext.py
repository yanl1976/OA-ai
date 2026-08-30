# -*- coding: utf-8 -*-
"""修复云之家拉取的「名实不符」历史文件。

背景：早期版本把下载到的盖章 PDF 统一按 .docx 命名落盘，而 extract_text 严格
按扩展名路由（.docx→OOXML 解析器），导致这些文件报「File is not a zip file」
而无法读取/提取。

本脚本按「真实文件魔数」校正落盘文件名与元数据，并重新提取文本：
  - 内容为 PDF（%PDF）但扩展名是 .docx/.doc → 改名为 .pdf
  - 内容为 OOXML（PK）但扩展名是 .pdf     → 改名为 .docx
  - 其余（真 docx / 真 OLE doc）保持不变

用法（在部署目录执行，默认只读预览）：
    python3 scripts/fix_yzj_ext.py            # 预览，不改动
    python3 scripts/fix_yzj_ext.py --apply    # 实际修复
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


def resolve_upload_dir(kb_store):
    """探测真实的文件存放目录。

    kb_store 的路径拼接为 KB_ROOT + "knowledge_base/uploads/files"，
    但生产机 .env 的 KB_ROOT 常已指向 .../knowledge_base，导致拼出重复的
    knowledge_base 而实际不存在（实测生产机即如此）。故这里做候选探测，
    优先取真实存在的目录；也可用 --files-dir 显式指定。
    """
    kb_root = os.environ.get("KB_ROOT", "")
    cands = [
        getattr(kb_store, "UPLOAD_FILES_DIR", ""),
        os.path.join(kb_root, "knowledge_base", "uploads", "files"),
        os.path.join(kb_root, "uploads", "files"),
        os.path.join(_ROOT, "knowledge_base", "uploads", "files"),
    ]
    for c in cands:
        if c and os.path.isdir(c):
            return c
    # 兜底：在部署根下递归找名为 uploads/files 的目录
    for base in [_ROOT, os.path.dirname(_ROOT), os.environ.get("KB_ROOT", "")]:
        if not base or not os.path.isdir(base):
            continue
        for dirpath, dirnames, _ in os.walk(base):
            if os.path.basename(dirpath) == "files" and os.path.basename(os.path.dirname(dirpath)) == "uploads":
                return dirpath
            dirnames[:] = [d for d in dirnames if d not in ("node_modules", ".git", "__pycache__")]
    return ""


def sniff_ext(raw: bytes) -> str:
    """按文件魔数判断真实类型。"""
    if raw[:4] == b"%PDF":
        return ".pdf"
    if raw[:2] == b"PK":
        return ".docx"
    if raw[:4] == b"\xd0\xcf\x11\xe0":
        return ".doc"  # 旧版 OLE
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际执行修复（默认仅预览）")
    ap.add_argument("--root", default=_ROOT, help="部署根目录")
    ap.add_argument("--files-dir", default="", help="显式指定上传文件目录（自动探测失败时用）")
    ap.add_argument("--meta", default="", help="显式指定 user_documents.json 路径")
    ap.add_argument("--dedupe", action="store_true",
                    help="清理内容完全相同的冗余副本（保留 text 非空、命名更规范的那份）")
    args = ap.parse_args()

    import kb_store

    root = args.files_dir or resolve_upload_dir(kb_store)
    if not root:
        print("错误：找不到 uploads 目录，请检查 KB_ROOT 配置；"
              "可用 --files-dir 显式指定，例如"
              " --files-dir /opt/OA-ai/knowledge_base/uploads/files")
        return 1

    # 元数据读取：kb_store._load_uploads() 用的是 UPLOAD_FILE，而生产机的
    # UPLOAD_DIR 会拼出重复的 knowledge_base（KB_ROOT 已含 knowledge_base 时），
    # 导致读到空数组。故这里按「实际文件目录」反推元数据路径作为兜底。
    ups = kb_store._load_uploads() or []
    meta_used = getattr(kb_store, "UPLOAD_FILE", "")
    if not ups:
        guessed = os.path.join(os.path.dirname(root.rstrip("/\\")), "user_documents.json")
        if os.path.isfile(guessed):
            try:
                import json as _json
                _d = _json.load(open(guessed, encoding="utf-8"))
                if isinstance(_d, dict):
                    _d = _d.get("items") or _d.get("uploads") or []
                if isinstance(_d, list):
                    ups, meta_used = _d, guessed
            except Exception as e:  # noqa: BLE001
                print("兜底读取元数据失败 %s: %s" % (guessed, e))
    if args.meta and os.path.isfile(args.meta):
        try:
            import json as _json
            _d = _json.load(open(args.meta, encoding="utf-8"))
            if isinstance(_d, dict):
                _d = _d.get("items") or _d.get("uploads") or []
            if isinstance(_d, list):
                ups, meta_used = _d, args.meta
        except Exception as e:  # noqa: BLE001
            print("读取指定元数据失败: %s" % e)

    print("部署根目录:", args.root)
    print("元数据文件:", meta_used, "(存在: %s)" % os.path.isfile(meta_used))
    print("uploads 条数:", len(ups))

    # 基准校准：stored_path 有两种形态，必须与 --files-dir 的层级匹配，否则
    # 拼出的路径全部不存在，导致「需修复 0 / 无需改动 0」（实测踩坑）。
    #   形态 A: "会议纪要/2024年度/up_xxx.pdf"        → 基准 = <files-dir>
    #   形态 B: "files/会议纪要/2024年度/up_xxx.pdf"  → 基准 = <files-dir> 的父目录
    # 这里用「实际能命中的文件数」自动挑选正确基准。
    def _count_hit(base):
        n = 0
        for _u in ups:
            if not isinstance(_u, dict):
                continue
            _rel = _u.get("stored_path")
            if _rel and os.path.exists(os.path.join(base, _rel)):
                n += 1
        return n

    bases = [root]
    parent = os.path.dirname(root.rstrip("/\\"))
    if parent and os.path.isdir(parent):
        bases.append(parent)
    best_base, best_hit = root, _count_hit(root)
    for b in bases[1:]:
        h = _count_hit(b)
        if h > best_hit:
            best_base, best_hit = b, h
    if best_base != root:
        print("提示：stored_path 含 'files/' 前缀，已自动改用上级目录作为基准")
    root = best_base
    print("文件目录:", root, "(可命中文件 %d / %d)" % (best_hit, len(ups)))
    print("模式:", "实际修复 --apply" if args.apply else "预览（不改动）")
    print("=" * 72)

    fixed, skipped, failed = 0, 0, 0
    for u in ups:
        if not isinstance(u, dict):
            continue
        rel = u.get("stored_path")
        fn = u.get("filename") or ""
        if not rel:
            continue
        abspath = os.path.join(root, rel)
        if not os.path.exists(abspath):
            continue
        try:
            with open(abspath, "rb") as f:
                head = f.read(8)
        except Exception as e:  # noqa: BLE001
            print("  读取失败 %s: %s" % (abspath, e))
            failed += 1
            continue

        decl = os.path.splitext(rel)[1].lower()
        real = sniff_ext(head)
        if not real or decl == real:
            skipped += 1
            continue

        print("\n[错配] %s" % fn)
        print("   当前: %s (声明 %s，实际 %s)" % (rel, decl or "(无)", real))
        new_rel = rel[: -len(decl)] + real if decl else rel + real
        new_fn = (fn[: -len(decl)] + real) if fn.lower().endswith(decl) else (fn + real)

        if args.apply:
            new_abs = os.path.join(root, new_rel)
            try:
                os.makedirs(os.path.dirname(new_abs), exist_ok=True)
                shutil.move(abspath, new_abs)
                u["stored_path"] = new_rel
                u["filename"] = new_fn
                u["ext"] = real
                u["mimetype"] = kb_store.mimetype_for_ext(real)
                # 重新提取
                try:
                    import extract_text
                    with open(new_abs, "rb") as f:
                        raw = f.read()
                    text, warn = extract_text.extract(raw, new_fn, category=u.get("category"))
                    u["text"] = text
                    u["chars"] = len(text)
                    u["indexed"] = 1 if text.strip() else 0
                    print("   -> %s | 提取 %d 字 %s" % (new_rel, len(text), ("warn=%s" % warn) if warn else ""))
                except Exception as e:  # noqa: BLE001
                    print("   -> %s | 重新提取失败: %s" % (new_rel, repr(e)[:120]))
                    failed += 1
                fixed += 1
            except Exception as e:  # noqa: BLE001
                print("   修复失败: %s" % repr(e)[:150])
                failed += 1
        else:
            print("   -> 将改为: %s" % new_rel)
            fixed += 1

    if args.apply:
        # 写回「实际读取到的那个」元数据文件，避免写回 kb_store 拼错的路径
        # （生产机 UPLOAD_FILE 会拼出重复的 knowledge_base，导致修复结果丢失）。
        try:
            if meta_used and os.path.isfile(meta_used):
                with open(meta_used + ".bak", "w", encoding="utf-8") as bf:
                    import json as _json
                    _json.dump(ups, bf, ensure_ascii=False)
                import json as _json
                with open(meta_used, "w", encoding="utf-8") as f:
                    _json.dump(ups, f, ensure_ascii=False)
                print("\n已保存元数据:", meta_used, "(备份:", meta_used + ".bak)")
            else:
                kb_store._save_uploads(ups)
                print("\n已保存 uploads 元数据（kb_store 默认路径）")
        except Exception as e:  # noqa: BLE001
            print("保存失败: %s" % repr(e)[:200])

    # ---------------- 冗余清理（--dedupe） ----------------
    removed = 0
    if args.dedupe:
        print("\n" + "=" * 72)
        print("=== 冗余清理（内容相同 md5 的重复项）===")
        import hashlib as _hl

        # 按内容 md5 分组（只看能读到文件的）
        groups = {}
        for u in ups:
            if not isinstance(u, dict):
                continue
            rel = u.get("stored_path")
            if not rel:
                continue
            ap = os.path.join(root, rel)
            if not os.path.exists(ap):
                continue
            try:
                h = _hl.md5(open(ap, "rb").read()).hexdigest()
            except Exception:  # noqa: BLE001
                continue
            groups.setdefault(h, []).append((u, ap))

        dups = {h: v for h, v in groups.items() if len(v) > 1}
        print("原则：仅处理「内容 md5 完全相同」的组，每组保留 1 份、删除其余较晚的。")
        print("      文件名不同但 md5 相同的，是同一份文件被重复拉取，属冗余；")
        print("      文件名带 YYYYMMDD_ 前缀但 md5 各不相同的，是不同日期的真实文档，不受影响。")
        print("\n内容相同的重复组: %d 组" % len(dups))

        to_remove = []
        for h, items in dups.items():
            # 排序打分：同一 md5 组内保留 1 份、删除其余。
            # 判据只用「创建时间」与「text 是否非空」，**不使用文件名前缀**：
            # 带 YYYYMMDD_ 前缀的文件可能是「重名但内容不同」的真实文档
            # （如不同日期的天传所例会，md5 各不相同，本就不会进入本流程），
            # 用前缀当判据会误导；同组内 md5 已相同，删较晚那份最稳妥。
            def score(it):
                u, _ap = it
                has_text = 1 if (u.get("text") or "").strip() else 0
                created = u.get("created_at") or ""
                return (-has_text, created)

            items_sorted = sorted(items, key=score)
            keep, drop = items_sorted[0], items_sorted[1:]
            print("\n  [md5=%s] 共 %d 份（内容完全相同）" % (h[:10], len(items)))
            print("  保留: %s" % keep[0].get("filename"))
            print("        text=%d字 created=%s" % (
                len(keep[0].get("text") or ""), keep[0].get("created_at")))
            for u, ap in drop:
                print("  删除: %s" % u.get("filename"))
                print("        text=%d字 created=%s" % (
                    len(u.get("text") or ""), u.get("created_at")))
                to_remove.append((u, ap))

        print("\n合计待删除: %d 份（保留 %d 份）" % (len(to_remove), len(dups)))

        if args.apply and to_remove:
            drop_ids = set(id(u) for u, _ in to_remove)
            ok = 0
            for u, ap in to_remove:
                try:
                    os.remove(ap)
                    ok += 1
                except Exception as e:  # noqa: BLE001
                    print("  删除失败 %s: %s" % (ap, repr(e)[:100]))
            ups = [u for u in ups if id(u) not in drop_ids]
            # 保存元数据
            try:
                if meta_used and os.path.isfile(meta_used):
                    import json as _json
                    with open(meta_used, "w", encoding="utf-8") as f:
                        _json.dump(ups, f, ensure_ascii=False)
                    print("  已删除 %d 个文件并更新元数据" % ok)
                else:
                    kb_store._save_uploads(ups)
                    print("  已删除 %d 个文件并更新元数据（默认路径）" % ok)
            except Exception as e:  # noqa: BLE001
                print("  保存元数据失败: %s" % repr(e)[:200])
            removed = ok
        elif to_remove:
            print("\n确认无误后加 --apply 执行删除。")

    print("\n" + "=" * 72)
    print("需修复(改名): %d | 无需改动: %d | 失败: %d" % (fixed, skipped, failed))
    if args.dedupe:
        print("冗余删除: %d 份" % removed)
    if not args.apply and (fixed or (args.dedupe and True)):
        print("\n确认无误后加 --apply 执行。")


if __name__ == "__main__":
    main()
