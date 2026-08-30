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
    args = ap.parse_args()

    import kb_store

    ups = kb_store._load_uploads() or []
    files_dir = kb_store.UPLOAD_FILES_DIR
    print("部署根目录:", args.root)
    print("uploads 条数:", len(ups))
    print("文件目录:", files_dir)
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
        abspath = os.path.join(kb_store.UPLOAD_DIR, rel)
        if not os.path.exists(abspath):
            continue
        # 只看云之家拉取的（也可放开到全部）
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
            new_abs = os.path.join(kb_store.UPLOAD_DIR, new_rel)
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
        kb_store._save_uploads(ups)
        print("\n已保存 uploads 元数据")

    print("\n" + "=" * 72)
    print("需修复: %d | 无需改动: %d | 失败: %d" % (fixed, skipped, failed))
    if not args.apply and fixed:
        print("\n确认无误后加 --apply 执行修复。")


if __name__ == "__main__":
    main()
