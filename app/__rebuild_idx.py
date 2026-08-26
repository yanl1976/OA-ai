#!/usr/bin/env python3
"""重新构建向量索引"""
import sys
sys.path.insert(0, ".")

from app.core import embed_fn
from app.extract_text import extract
import os

# 知识库根目录
KB_ROOT = "./knowledge_base/uploads/files"

# 需要重新索引的管理标准类别
CATEGORIES = [
    "管理标准分类/01.标准化类",
]

def process_file(fp, category):
    """提取并向量化单个文件"""
    try:
        with open(fp, "rb") as f:
            raw = f.read()

        # 提取文本
        text, warn = extract(raw, os.path.basename(fp), category)
        if not text:
            return None

        # 向量化
        vec = embed_fn(text)
        return {"text": text, "vec": vec}
    except Exception as e:
        print(f"处理失败 {fp}: {e}")
        return None

# 遍历处理
total = 0
for cat in CATEGORIES:
    cat_path = os.path.join(KB_ROOT, cat)
    if not os.path.exists(cat_path):
        print(f"目录不存在: {cat_path}")
        continue

    # 遍历年份目录
    for year in os.listdir(cat_path):
        year_path = os.path.join(cat_path, year)
        if not os.path.isdir(year_path):
            continue

        # 遍历文件
        for f in os.listdir(year_path):
            if not f.endswith(".pdf"):
                continue

            fp = os.path.join(year_path, f)
            result = process_file(fp, cat)
            if result:
                total += 1
                print(f"已处理: {cat}/{year}/{f}")

print(f"\n共处理 {total} 个文件")
