#!/usr/bin/env python3
"""从 raw_data_full.json 生成 knowledge_graph_data.js"""
import json, re, os

# KB_ROOT: 知识库根目录。默认取本脚本所在目录的上一级（即部署根 /opt/OA-ai）。
KB_ROOT = os.environ.get("KB_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KB_DIR = os.path.join(KB_ROOT, "knowledge_base")

# 颜色映射
CAT_COLORS = [
    "#00E676", "#448AFF", "#B388FF", "#FF6E40", "#FF5252",
    "#76FF03", "#FFD740", "#FF4081", "#40C4FF", "#FFAB40",
    "#E040FB", "#64FFDA", "#FF6D00", "#69F0AE", "#FF1744",
    "#2979FF"
]

with open(os.path.join(KB_DIR, "raw_data_full.json"), "r", encoding="utf-8") as f:
    data = json.load(f)

categories = data["categories"]
docs = data["documents"]

# 按类别分组
cat_docs = {}
for d in docs:
    cat = d["category"]
    if cat not in cat_docs:
        cat_docs[cat] = []
    cat_docs[cat].append(d)

NODES = []
NODES.append({"id": "root", "name": "集团制度知识库", "type": "root", "count": len(docs), "color": "#ffd700"})

# 生成类别节点 + 文档节点
cat_list = sorted(cat_docs.keys(), key=lambda c: int(re.search(r'\d+', c).group()))
for ci, cat_name in enumerate(cat_list):
    cat_docs_list = cat_docs[cat_name]
    cat_id = f"cat_{ci}"
    NODES.append({
        "id": cat_id,
        "name": cat_name,
        "type": "category",
        "color": CAT_COLORS[ci % len(CAT_COLORS)],
        "code": f"{cat_name.split('.')[0]}",
        "count": len(cat_docs_list)
    })
    for di, doc in enumerate(cat_docs_list):
        doc_id = f"doc_{ci}_{di}"
        NODES.append({
            "id": doc_id,
            "name": doc["filename"].replace(".pdf", ""),
            "type": "doc",
            "color": CAT_COLORS[ci % len(CAT_COLORS)],
            "code": f"{doc['total_pages']}页",
            "parent": cat_id,
            "pages": doc["total_pages"]
        })

# 生成 LINKS
LINKS = []
for ci in range(len(cat_list)):
    LINKS.append({"source": "root", "target": f"cat_{ci}"})
    for di in range(len(cat_docs[cat_list[ci]])):
        LINKS.append({"source": f"cat_{ci}", "target": f"doc_{ci}_{di}"})

output = f"""// 自动生成：知识图谱完整数据（{len(NODES)} 节点，{len(LINKS)} 连线）
// 生成来源：raw_data_full.json
var NODES = {json.dumps(NODES, ensure_ascii=False)};
var LINKS = {json.dumps(LINKS, ensure_ascii=False)};
"""

with open(os.path.join(KB_DIR, "knowledge_graph_data.js"), "w", encoding="utf-8") as f:
    f.write(output)

print(f"✅ 已生成 knowledge_graph_data.js")
print(f"   节点总数: {len(NODES)} (根节点:1, 大类:{len(cat_list)}, 文档:{len(docs)})")
print(f"   连线总数: {len(LINKS)}")
