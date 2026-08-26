"""
本地向量索引（纯 numpy + jieba，零额外依赖）

设计:
  - 中文分词: jieba.lcut_for_search
  - 向量化:   特征哈希(Feature Hashing) 把词映射到固定维度 DIM，得到词频计数向量
  - 权重:     TF-IDF (tf = log1p(count), idf = log((1+N)/(1+df)) + 1)，L2 归一化
  - 检索:     查询向量与所有 chunk 向量做余弦相似度（点积，因已归一化）
  - 定位:     每个 chunk 记录其在所属文档全文中的绝对字符偏移(char_start/char_end)，
             供前端打开文档后跳转到命中点并高亮。

对外能力:
  - rebuild(documents)        全量重建
  - upsert_document(doc)      增量新增/替换单个文档的所有 chunk
  - remove_document(doc_id)   增量删除单个文档
  - search(query, top_k)      语义余弦召回，返回 (meta, score)
  - stats()                   索引统计

documents 元素字段: {doc_id, filename, category, content, source, year}
"""
import os
import re
import pickle
import tempfile

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
DIM = 4096          # 哈希维度
CHUNK_SIZE = 600    # 每个文本块字符数
RRF_K = 60          # RRF 融合常数

_TOKEN_RE = re.compile(r"[\u4e00-\u9fa5a-zA-Z0-9]+")


def _tokenize(text: str):
    """中文粗切 + 英文数字保留，返回 token 列表。"""
    toks = []
    for seg in jieba.lcut_for_search(text):
        seg = seg.strip()
        if not seg:
            continue
        # 长英文/数字再按正则细分
        if re.search(r"[a-zA-Z0-9]", seg) and len(seg) > 1:
            toks.extend(_TOKEN_RE.findall(seg.lower()))
        else:
            toks.append(seg)
    return toks


def _hash_vector(tokens):
    """把 token 列表映射到 DIM 维词频计数向量。"""
    v = np.zeros(DIM, dtype=np.float32)
    for t in tokens:
        h = hash(t) % DIM
        v[h] += 1.0
    return v


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


class VecIndex:
    def __init__(self):
        self.counts = np.zeros((0, DIM), dtype=np.float32)  # (N, D) 词频计数
        self.idf = np.ones(DIM, dtype=np.float32)
        self.vectors = np.zeros((0, DIM), dtype=np.float32)  # (N, D) 归一化 tf-idf
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
        out = []
        for i, (ct, cs, ce) in enumerate(_chunk_with_offsets(content)):
            if not ct.strip():
                continue
            out.append((self._make_chunk_meta(doc, ct, cs, ce, i),
                        _hash_vector(_tokenize(ct))))
        return out

    def _recompute(self):
        """根据当前 counts/meta 重新计算 idf / tf-idf / 归一化向量。"""
        n = self.counts.shape[0]
        if n == 0:
            self.idf = np.ones(DIM, dtype=np.float32)
            self.vectors = np.zeros((0, DIM), dtype=np.float32)
            return
        df = (self.counts > 0).sum(axis=0).astype(np.float32)
        self.idf = np.log((1.0 + n) / (1.0 + df)) + 1.0
        tf = np.log1p(self.counts)
        vecs = tf * self.idf
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        self.vectors = vecs / norms

    def rebuild(self, documents):
        """全量重建索引。documents: list[dict]。"""
        all_chunks = []  # (meta, vec)
        for doc in documents:
            all_chunks.extend(self._doc_chunks(doc))
        if all_chunks:
            self.meta = [m for m, _ in all_chunks]
            self.counts = np.stack([v for _, v in all_chunks]).astype(np.float32)
        else:
            self.meta = []
            self.counts = np.zeros((0, DIM), dtype=np.float32)
        self._recompute()
        self.save()
        return len(self.meta)

    def upsert_document(self, doc):
        """增量新增或替换某文档的所有 chunk。"""
        self.remove_document(doc.get("doc_id"))
        new = self._doc_chunks(doc)
        if not new:
            return 0
        add_meta = [m for m, _ in new]
        add_counts = np.stack([v for _, v in new]).astype(np.float32)
        self.meta = self.meta + add_meta
        self.counts = np.vstack([self.counts, add_counts]) if self.counts.shape[0] else add_counts
        self._recompute()
        self.save()
        return len(new)

    def remove_document(self, doc_id):
        if self.counts.shape[0] == 0 or doc_id is None:
            return
        keep = [i for i, m in enumerate(self.meta) if m.get("doc_id") != doc_id]
        if len(keep) == len(self.meta):
            return
        self.meta = [self.meta[i] for i in keep]
        self.counts = self.counts[keep]
        self._recompute()
        self.save()

    # ---------- 检索 ----------
    def search(self, query: str, top_k: int = 30):
        if self.counts.shape[0] == 0:
            return []
        qv = _hash_vector(_tokenize(query))
        if qv.sum() == 0:
            return []
        qtf = np.log1p(qv)
        qvec = qtf * self.idf
        qn = np.linalg.norm(qvec)
        if qn == 0:
            return []
        qvec = qvec / qn
        sims = self.vectors @ qvec  # (N,)
        k = min(top_k, sims.shape[0])
        idxs = np.argpartition(-sims, range(k))[:k]
        idxs = idxs[np.argsort(-sims[idxs])]
        return [(self.meta[i], float(sims[i])) for i in idxs if sims[i] > 0]

    # ---------- 持久化 ----------
    def save(self):
        os.makedirs(VEC_DIR, exist_ok=True)
        with open(VEC_FILE, "wb") as f:
            pickle.dump({
                "counts": self.counts,
                "idf": self.idf,
                "vectors": self.vectors,
                "meta": self.meta,
                "dim": DIM,
            }, f)

    def load(self):
        if not os.path.exists(VEC_FILE):
            return False
        with open(VEC_FILE, "rb") as f:
            d = pickle.load(f)
        self.counts = d["counts"]
        self.idf = d["idf"]
        self.vectors = d["vectors"]
        self.meta = d["meta"]
        return True

    def stats(self):
        return {
            "ready": os.path.exists(VEC_FILE),
            "chunks": int(self.counts.shape[0]),
            "dim": DIM,
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
