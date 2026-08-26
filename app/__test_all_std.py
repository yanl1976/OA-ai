import sys
sys.path.insert(0, ".")
from app.extract_text import extract
import os

kb_root = "./knowledge_base/uploads/files"
category_path = os.path.join(kb_root, "管理标准分类", "01.标准化类")

print("=== 1.标准化类 全部6个文档提取结果 ===\n")

count = 0
for year_dir in os.listdir(category_path):
    year_path = os.path.join(category_path, year_dir)
    if not os.path.isdir(year_path):
        continue

    for f in sorted(os.listdir(year_path)):
        if not f.endswith(".pdf"):
            continue

        count += 1
        print(f"\n{'='*60}")
        print(f"文档 {count}: {f}")
        print(f"{'='*60}")

        fp = os.path.join(year_path, f)
        with open(fp, "rb") as rf:
            raw = rf.read()
            text, warn = extract(raw, f, "01.标准化类")

        # 显示前25行
        lines = text.split("\n")[:25]
        for i, ln in enumerate(lines):
            print(f"{i}: {ln}")

print(f"\n共提取 {count} 个文档")
