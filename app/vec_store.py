"""
本地向量索引（dense embedding + 稀疏 BM25 互补）

设计（最科学的混合检索范式）:
  - 嵌入模型: BAAI/bge-small-zh（中文语义，维度 512），经 sentence-transformers 本地推理。
             若环境无法加载（离线/无依赖），自动回退到 feature-hashing 稀疏向量，保证不崩。
  - 文本切分: 按段落累积到 ~CHUNK_SIZE 字符（默认 1200，放大粒度以减少碎片化），保留绝对字符偏移。
  - 检索:     查询与所有 chunk 做余弦相似度（向量已 L2 归一化，点积即余弦）。
  - 定位:     每个 chunk 记录其在所属文档全文中的绝对字符偏移(char_start/char_end)，供前端跳转高亮。

对外能力（与旧版兼容，接口不变）:
  - rebuild(documents)        全量重建
  - upsert_document(doc)      增量新增/替换单个文档
  - remove_document(doc_id)   增量删除
  - search(query, top_k)      余弦召回，返回 [(meta, score), ...]
  - stats()                   索引统计

documents 元素字段: {doc_id, filename, category, content, source, year}
"""

import os
import re
import pickle
import tempfile
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vec_store")

# jieba 缓存写入应用私有目录，避免多用户权限冲突
_jieba_cache = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".jieba_cache")
os.makedirs(_jieba_cache, exist_ok=True)
tempfile.tempdir = os.path.abspath(_jieba_cache)
import jieba

import numpy as np

# ========== 配置 ==========
KB_ROOT = os.environ.get("KB_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VEC_DIR = os.path.join(KB_ROOT, "knowledge_base", "vec_index")
VEC_FILE = os.path.join(VEC_DIR, "vec_index.pkl")
CHUNK_SIZE = 1200   # 每个文本块字符数（放大粒度，降低碎片化）
RRF_K = 60          # RRF 融合常数（保留，供 search.py 使用）
HASH_DIM = 4096     # 回退哈希维度
BGE_DIM = 512       # bge-small-zh 维度
BGE_MODEL = "BAAI/bge-small-zh"
# bge 官方推荐的查询指令前缀，提升检索召回质量
BGE_QUERY_PROMPT = "为这个句子生成表示以用于检索相关文章："

_TOKEN_RE = re.compile(r"[\u4e00-\u9fa5a-zA-Z0-9]+")


# ==================== 嵌入层（可插拔） ====================
class _HashEmbedder:
    """回退方案：feature hashing 稀疏向量，零额外依赖。"""
    type = "hash"
    dim = HASH_DIM

    def _tokenize(self, text: str):
        toks = []
        for seg in jieba.lcut_for_search(text):
            seg = seg.strip()
            if not seg:
                continue
            if re.search(r"[a-zA-Z0-9]", seg) and len(seg) > 1:
                toks.extend(_TOKEN_RE.findall(seg.lower()))
            else:
                toks.append(seg)
        return toks

    def _vector(self, tokens):
        v = np.zeros(self.dim, dtype=np.float32)
        for t in tokens:
            v[hash(t) % self.dim] += 1.0
        return v

    def encode(self, texts, is_query=False):
        # 哈希方案忽略查询指令；调用方传入的是已切好的文本列表
        return np.stack([self._vector(self._tokenize(t)) for t in texts]).astype(np.float32)

    def encode_query(self, query):
        return self.encode([query], is_query=True)[0]


class _BGEEmbedder:
    """主方案：BAAI/bge-small-zh 语义向量。"""
    type = "bge"
    dim = BGE_DIM

    def __init__(self):
        # 优先走国内镜像，避免 hf.co 不通
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        from sentence_transformers import SentenceTransformer
        self._model = SentenceTransformer(BGE_MODEL)
        self._model.max_seq_length = 512

    def encode(self, texts, is_query=False):
        if is_query:
            texts = [BGE_QUERY_PROMPT + t for t in texts]
        vecs = self._model.encode(
            texts, normalize_embeddings=True,
            show_progress_bar=False, convert_to_numpy=True,
        )
        return np.asarray(vecs, dtype=np.float32)

    def encode_query(self, query):
        return self.encode([query], is_query=True)[0]


def _build_embedder():
    """自动选择嵌入模型：优先 bge，失败回退 hash。"""
    try:
        e = _BGEEmbedder()
        # 冒烟测试，确保能正常推理
        _ = e.encode(["测试"], is_query=True)
        logger.info("[vec_store] 使用 BGE 语义向量 (dim=%d)", e.dim)
        return e
    except Exception as ex:  # 离线 / 无依赖 / 模型缺失
        logger.warning("[vec_store] BGE 加载失败，回退 feature-hashing: %s", ex)
        return _HashEmbedder()


_EMBEDDER = None


def get_embedder():
    global _EMBEDDER
    if _EMBEDDER is None:
        _EMBEDDER = _build_embedder()
    return _EMBEDDER


# ==================== 文本切分 ====================
def _chunk_with_offsets(text: str):
    """按段落切分，返回 [(chunk_text, char_start, char_end), ...]。"""
    paragraphs = text.split("\n")
    chunks = []
    buf = ""
    buf_start = None
    cursor = 0
    for para in paragraphs:
        p = para.rstrip("\n")
        if buf and len(buf) + len(p) + 1 > CHUNK_SIZE:
            end = buf_start + len(buf)
            chunks.append((buf, buf_start, end))
            buf = ""
            buf_start = None
        if buf_start is None:
            buf_start = cursor
        buf = (buf + "\n" + p) if buf else p
        cursor += len(p) + 1
    if buf.strip():
        end = buf_start + len(buf)
        chunks.append((buf, buf_start, end))
    return chunks or [(text, 0, len(text))]


# ==================== 向量索引 ====================
class VecIndex:
    def __init__(self):
        self.embedder_type = get_embedder().type
        self.dim = get_embedder().dim
        self.vectors = np.zeros((0, self.dim), dtype=np.float32)
        self.meta = []  # 每个 chunk 的元信息

    # ---------- 构建 ----------
    def _make_chunk_meta(self, doc, chunk_text, cstart, cend, idx):
        return {
            "doc_id": doc.get("doc_id"),
            "filename": doc.get("filename"),
            "category": doc.get("category"),
            "source": doc.get("source"),
            "year": doc.get("year"),
            "chunk_index": idx,
            "char_start": cstart,
            "char_end": cend,
            "text": chunk_text,
        }

    def _doc_chunks(self, doc):
        content = doc.get("content") or ""
        pairs = []
        for i, (ct, cs, ce) in enumerate(_chunk_with_offsets(content)):
            if not ct.strip():
                continue
            pairs.append((self._make_chunk_meta(doc, ct, cs, ce, i), ct))
        return pairs

    def _recompute(self, doc_chunks):
        """批量编码 chunk 文本为 dense 向量。"""
        if not doc_chunks:
            return np.zeros((0, self.dim), dtype=np.float32)
        texts = [t for _, t in doc_chunks]
        return get_embedder().encode(texts, is_query=False)

    def rebuild(self, documents):
        """全量重建索引。documents: list[dict]。"""
        all_chunks = []
        for doc in documents:
            all_chunks.extend(self._doc_chunks(doc))
        self.meta = [m for m, _ in all_chunks]
        self.vectors = self._recompute(all_chunks)
        self.save()
        return len(self.meta)

    def upsert_document(self, doc):
        """增量新增或替换某文档的所有 chunk。"""
        doc_id = doc.get("doc_id")
        self.remove_document(doc_id)
        new = self._doc_chunks(doc)
        if not new:
            return 0
        add_meta = [m for m, _ in new]
        add_vecs = self._recompute(new)
        self.meta = self.meta + add_meta
        if self.vectors.shape[0] == 0:
            self.vectors = add_vecs
        else:
            self.vectors = np.vstack([self.vectors, add_vecs])
        self.save()
        return len(new)

    def remove_document(self, doc_id):
        if self.vectors.shape[0] == 0 or doc_id is None:
            return
        keep = [i for i, m in enumerate(self.meta) if m.get("doc_id") != doc_id]
        if len(keep) == len(self.meta):
            return
        self.meta = [self.meta[i] for i in keep]
        self.vectors = self.vectors[keep]
        self.save()

    # ---------- 检索 ----------
    def search(self, query: str, top_k: int = 30):
        if self.vectors.shape[0] == 0:
            return []
        qvec = get_embedder().encode_query(query)
        if qvec is None or np.linalg.norm(qvec) == 0:
            return []
        sims = self.vectors @ qvec  # (N,)，已归一化→点积即余弦
        k = min(top_k, sims.shape[0])
        idxs = np.argpartition(-sims, range(k))[:k]
        idxs = idxs[np.argsort(-sims[idxs])]
        return [(self.meta[i], float(sims[i])) for i in idxs if sims[i] > 0]

    # ---------- 持久化 ----------
    def save(self):
        os.makedirs(VEC_DIR, exist_ok=True)
        with open(VEC_FILE, "wb") as f:
            pickle.dump({
                "vectors": self.vectors,
                "meta": self.meta,
                "embedder_type": self.embedder_type,
                "dim": self.dim,
            }, f)

    def load(self):
        if not os.path.exists(VEC_FILE):
            return False
        with open(VEC_FILE, "rb") as f:
            d = pickle.load(f)
        cur = get_embedder().type
        if d.get("embedder_type") != cur:
            # 嵌入模型已切换，旧索引作废，需重建
            logger.warning(
                "[vec_store] 索引 embedder_type=%s 与当前 %s 不一致，已忽略旧索引（请重建）",
                d.get("embedder_type"), cur)
            return False
        self.vectors = d["vectors"]
        self.meta = d["meta"]
        self.embedder_type = d["embedder_type"]
        self.dim = d.get("dim", self.dim)
        return True

    def stats(self):
        return {
            "ready": os.path.exists(VEC_FILE),
            "chunks": int(self.vectors.shape[0]),
            "dim": self.dim,
            "embedder": self.embedder_type,
            "docs": len({m.get("doc_id") for m in self.meta}),
        }


# 模块级单例（惰性加载）
_INDEX = None


def get_index(force_reload=False):
    global _INDEX
    if _INDEX is None or force_reload:
        _INDEX = VecIndex()
        _INDEX.load()
    return _INDEX


def rebuild(documents):
    idx = get_index()
    return idx.rebuild(documents)


def upsert_document(doc):
    return get_index().upsert_document(doc)


def remove_document(doc_id):
    return get_index().remove_document(doc_id)


def search(query, top_k=30):
    return get_index().search(query, top_k)


def stats():
    return get_index().stats()
