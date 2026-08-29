"""
RAG 索引构建脚本（BM25 + jieba 方案）
将本地知识库文档（含用户上传文档）建立 BM25 全文检索索引，全程本地运行。

数据源:
  - knowledge_base/raw_data_full.json            (原始 183 份文档，含 full_text)
  - knowledge_base/uploads/user_documents.json   (用户上传文档)

产物:
  - knowledge_base/bm25_index/bm25_index.pkl
  - knowledge_base/bm25_index/doc_metadata.pkl
  - knowledge_base/bm25_index/documents_manifest.json   (供前端浏览/查看)
"""
import os
import json
import pickle
import tempfile
# jieba 缓存写入应用私有目录，避免 /tmp 多用户权限冲突导致分词失败
_jieba_cache = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".jieba_cache")
os.makedirs(_jieba_cache, exist_ok=True)
tempfile.tempdir = os.path.abspath(_jieba_cache)
import jieba
from rank_bm25 import BM25Okapi
from tqdm import tqdm

import kb_store  # 复用统一文档加载与年份提取

# ========== 配置 ==========
KB_ROOT = os.environ.get("KB_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KB_DIR = os.path.join(KB_ROOT, "knowledge_base")
INDEX_DIR = os.path.join(KB_DIR, "bm25_index")
INDEX_FILE = os.path.join(INDEX_DIR, "bm25_index.pkl")
META_FILE = os.path.join(INDEX_DIR, "doc_metadata.pkl")
MANIFEST_FILE = os.path.join(INDEX_DIR, "documents_manifest.json")

CHUNK_SIZE = 1200  # 每个文本块的字符数（与 vec_store 一致，放大粒度降低碎片化）


def chunk_text(text, chunk_size=CHUNK_SIZE):
    """将长文本切分为小块（按段落切分），并记录在全文中的绝对字符偏移。"""
    paragraphs = text.split("\n")
    chunks = []
    buf = ""
    buf_start = None
    cursor = 0
    for para in paragraphs:
        p = para.rstrip("\n")
        if buf and len(buf) + len(p) + 1 > chunk_size:
            chunks.append((buf, buf_start, buf_start + len(buf)))
            buf = ""
            buf_start = None
        if buf_start is None:
            buf_start = cursor
        buf = (buf + "\n" + p) if buf else p
        cursor += len(p) + 1
    if buf.strip():
        chunks.append((buf, buf_start, buf_start + len(buf)))
    return chunks or [(text, 0, len(text))]


def build_index():
    print(f"\n{'='*50}")
    print("本地 RAG 知识库索引构建（BM25 + jieba）")
    print(f"{'='*50}\n")

    print("步骤1: 加载文档...")
    docs = kb_store.iter_all_documents()
    raw_n = sum(1 for d in docs if d["source"] == "raw")
    up_n = sum(1 for d in docs if d["source"] == "upload")
    print(f"  原始文档: {raw_n} 份 | 上传文档: {up_n} 份")

    print("步骤2: 切分文档为文本块...")
    all_chunks = []
    manifest = {}
    for doc in tqdm(docs, desc="切分文档"):
        manifest[doc["doc_id"]] = {
            "doc_id": doc["doc_id"],
            "filename": doc["filename"],
            "category": doc["category"],
            "year": doc.get("year"),
            "pages": doc.get("pages", 1),
            "label": doc["filename"],
            "source": doc["source"],
        }
        for i, (ct, cs, ce) in enumerate(chunk_text(doc["content"])):
            if not ct.strip():
                continue
            all_chunks.append({
                "text": ct,
                "category": doc["category"],
                "filename": doc["filename"],
                "pages": doc.get("pages", 1),
                "doc_id": doc["doc_id"],
                "chunk_index": i,
                "source": doc["source"],
                "char_start": cs,
                "char_end": ce,
                "label": f"{doc['category']} | {doc['filename']}（{doc.get('pages', 1)}页）"
            })

    print(f"共生成 {len(all_chunks)} 个文本块")

    print("\n步骤3: 中文分词处理...")
    corpus = [c["text"] for c in all_chunks]
    tokenized_corpus = []
    for text in tqdm(corpus, desc="分词"):
        # 精确模式（lcut）：与 search._tokenize 对齐，以完整词为单位、不拆字，
        # 避免「人名 / 专有词组」被碎成单字后污染 BM25 召回。
        tokenized_corpus.append(jieba.lcut(text))

    print("步骤4: 构建 BM25 索引...")
    if not tokenized_corpus:
        # 语料为空（如删光了所有文档）：保存空索引占位，避免 BM25Okapi([]) 抛 ZeroDivisionError
        bm25 = None
        print("  语料为空，写入空索引占位（检索将返回空结果）")
    else:
        bm25 = BM25Okapi(tokenized_corpus)

    print("步骤5: 保存索引文件...")
    os.makedirs(INDEX_DIR, exist_ok=True)
    # 原子写：先写临时文件再 replace，避免检索线程（每次 load_index 读 pkl）读到半成品
    _tmp = INDEX_FILE + ".tmp"
    with open(_tmp, "wb") as f:
        pickle.dump(bm25, f)
    os.replace(_tmp, INDEX_FILE)
    _tmp = META_FILE + ".tmp"
    with open(_tmp, "wb") as f:
        pickle.dump(all_chunks, f)
    os.replace(_tmp, META_FILE)
    with open(MANIFEST_FILE, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*50}")
    print("[OK] 索引构建完成！")
    print(f"   索引路径:    {INDEX_DIR}")
    print(f"   文档数量:    {len(docs)} 份")
    print(f"   文本块数量:  {len(all_chunks)} 个")
    print(f"   算法:        BM25 + jieba 分词")
    print(f"{'='*50}")


if __name__ == "__main__":
    build_index()
