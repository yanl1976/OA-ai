import sys
sys.path.insert(0, ".")
from app.extract_text import extract
import os

kb_root = "./knowledge_base/uploads/files"
category_path = os.path.join(kb_root, "管理标准分类", "01.标准化类")

print("=== 1.标准化类 文档提取结果 ===\n")

count = 0
for year_dir in os.listdir(category_path):
    year_path = os.path.join(category_path, year_dir)
    if not os.path.isdir(year_path):
        continue

    for f in os.listdir(year_path):
        if not f.endswith(".pdf"):
            continue

        print(f"\n{'='*60}")
        print(f"文档: {f}")
        print(f"{'='*60}")

        fp = os.path.join(year_path, f)
        with open(fp, "rb") as rf:
            raw = rf.read()
            text, warn = extract(raw, f, "01.标准化类")

        # 显示前20行
        lines = text.split("\n")[:20]
        for i, ln in enumerate(lines):
            print(f"{i}: {ln}")

        count += 1
        if count >= 3:  # 显示前3个文件
            break
    if count >= 3:
        break
