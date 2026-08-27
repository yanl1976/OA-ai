"""
RAG 知识库检索主入口
供 WorkBuddy 助理直接调用
用法: python rag_query.py "你的问题"
"""

import sys
import os
import pickle
# jieba 默认把分词缓存写到 /tmp/jieba.cache，多用户/服务场景下易因权限冲突
# 导致分词失败、检索返回空。改为写入应用私有目录，彻底规避该问题。
import tempfile
_jieba_cache = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".jieba_cache")
os.makedirs(_jieba_cache, exist_ok=True)
tempfile.tempdir = os.path.abspath(_jieba_cache)
import jieba
import json

# ========== 配置 ==========
# KB_ROOT: 知识库根目录。默认取本脚本所在目录的上一级（即部署根 /opt/OA-ai）。
# 可用环境变量 KB_ROOT 覆盖，便于任意位置运行。
KB_ROOT = os.environ.get("KB_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INDEX_DIR = os.path.join(KB_ROOT, "knowledge_base", "bm25_index")
INDEX_FILE = os.path.join(INDEX_DIR, "bm25_index.pkl")
META_FILE = os.path.join(INDEX_DIR, "doc_metadata.pkl")
TOP_K = 5


def load_index():
    """加载已构建的 BM25 索引"""
    if not os.path.exists(INDEX_FILE):
        raise FileNotFoundError(f"索引文件不存在，请先运行 rag_build_index.py 构建索引。\n索引路径: {INDEX_DIR}")
    with open(INDEX_FILE, "rb") as f:
        bm25 = pickle.load(f)
    with open(META_FILE, "rb") as f:
        metadata = pickle.load(f)
    return bm25, metadata


def query(question, top_k=TOP_K):
    """检索与问题最相关的文档片段"""
    bm25, chunks = load_index()
    if bm25 is None:
        # 空索引占位（知识库无文档）：直接返回空结果，不崩溃
        return []
    query_tokens = jieba.lcut_for_search(question)
    scores = bm25.get_scores(query_tokens)
    top_indices = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]

    results = []
    for idx in top_indices:
        if scores[idx] > 0:
            chunk = chunks[idx]
            results.append({
                "text": chunk["text"],
                "category": chunk["category"],
                "filename": chunk["filename"],
                "pages": chunk["pages"],
                "score": round(scores[idx], 3),
                "label": chunk["label"]
            })
    return results


def search(question, top_k=TOP_K):
    """搜索入口，返回结构化 JSON（供 AI 助理解析）"""
    results = query(question, top_k)
    return json.dumps(results, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python rag_query.py \"你的问题\" [top_k]")
        sys.exit(1)

    question = sys.argv[1]
    top_k = int(sys.argv[2]) if len(sys.argv) > 2 else TOP_K

    results = query(question, top_k)

    if not results:
        print("未找到相关文档，请尝试用更通用的关键词。")
        sys.exit(0)

    print(f"找到 {len(results)} 条相关结果：\n")

    for i, r in enumerate(results, 1):
        print(f"{'─'*55}")
        print(f"【结果 {i}】{r['label']} | 评分: {r['score']}")
        print(f"\n{r['text']}")

    print(f"\n{'═'*55}")
    print(f"共返回 {len(results)} 条结果，来源：{r['category']}")
