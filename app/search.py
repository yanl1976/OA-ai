"""
混合检索（BM25 词法召回 + 向量语义召回 + RRF 融合 + 内容定位）

  - 词法召回: 复用 rag_query 的 BM25Okapi 索引
  - 语义召回: 复用 vec_store 的余弦向量检索
  - 融合:     文档级 Reciprocal Rank Fusion (RRF)
  - 定位:     每个命中 chunk 携带在全文中的 char_start/char_end，
             前端据此打开文档并滚动/高亮到命中点
  - 高亮:     摘要中以 <mark> 包裹命中的查询词（已做 HTML 转义）
"""
import re
import html as _html

import jieba

import rag_query
import vec_store
import kb_store

RRF_K = vec_store.RRF_K


def _tokenize(text):
    return [t for t in jieba.lcut_for_search(text) if t.strip()]


def _highlight(text, query_tokens):
    """对 text 中出现的查询词做 HTML 转义 + <mark> 高亮。"""
    esc = _html.escape(text or "")
    seen = set()
    for t in query_tokens:
        if len(t) < 2:  # 单字不强制高亮，避免噪音
            continue
        et = _html.escape(t)
        if et in seen:
            continue
        seen.add(et)
        esc = re.sub(re.escape(et), lambda m: "<mark>" + m.group(0) + "</mark>", esc)
    return esc


def hybrid_search(query: str, top_k: int = 20):
    """返回融合后的检索结果列表（文档级去重）。

    每个结果除最佳命中 chunk 的定位(char_start/char_end)外，
    额外返回 regions：该文档所有命中 chunk 在全文中的 [start,end] 区间列表，
    供前端"内容高亮"使用。
    """
    query = (query or "").strip()
    if not query:
        return []
    qtokens = _tokenize(query)

    # ---- 向量语义召回（保留同一文档的全部命中 chunk）----
    try:
        vec_pairs = vec_store.search(query, top_k=top_k * 4)
    except Exception:
        vec_pairs = []
    vec_chunks = {}
    for meta, score in vec_pairs:
        d = meta.get("doc_id")
        if d is None:
            continue
        vec_chunks.setdefault(d, []).append((meta, score))

    # ---- BM25 词法召回 ----
    bm25_chunks = {}
    try:
        bm25, chunks = rag_query.load_index()
        scores = bm25.get_scores(qtokens)
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[: top_k * 4]
        for i in order:
            if scores[i] <= 0:
                continue
            c = chunks[i]
            d = c.get("doc_id")
            if d is None:
                continue
            bm25_chunks.setdefault(d, []).append((c, scores[i]))
    except FileNotFoundError:
        bm25_chunks = {}
    except Exception:
        bm25_chunks = {}

    # ---- RRF 文档级融合（按每文档最高分计算排名）----
    def doc_best_score(chunks_map):
        out = {}
        for d, lst in chunks_map.items():
            out[d] = max(sc for _, sc in lst)
        return out

    vec_best = doc_best_score(vec_chunks)
    bm25_best = doc_best_score(bm25_chunks)

    doc_ids = set(vec_chunks) | set(bm25_chunks)
    if not doc_ids:
        return []
    rrf = {}
    for rank, (d, sc) in enumerate(
        sorted(vec_best.items(), key=lambda kv: kv[1], reverse=True)
    ):
        rrf[d] = rrf.get(d, 0.0) + 1.0 / (RRF_K + rank + 1)
    for rank, (d, sc) in enumerate(
        sorted(bm25_best.items(), key=lambda kv: kv[1], reverse=True)
    ):
        rrf[d] = rrf.get(d, 0.0) + 1.0 / (RRF_K + rank + 1)

    ranked = sorted(rrf.items(), key=lambda kv: kv[1], reverse=True)[:top_k]

    results = []
    for d, score in ranked:
        # 取最佳 chunk 用作摘要/定位
        best_chunk = None
        best_sc = -1
        for lst in (vec_chunks.get(d, []), bm25_chunks.get(d, [])):
            for meta, sc in lst:
                if sc > best_sc:
                    best_sc = sc
                    best_chunk = meta

        # 汇总该文档所有命中 chunk 的字符区间（去重、排序），用于"内容高亮"
        regions = []
        seen_r = set()
        for lst in (vec_chunks.get(d, []), bm25_chunks.get(d, [])):
            for meta, _sc in lst:
                s = meta.get("char_start")
                e = meta.get("char_end")
                if s is None or e is None:
                    continue
                key = (s, e)
                if key not in seen_r:
                    seen_r.add(key)
                    regions.append([s, e])
        regions.sort()

        chunk_text = best_chunk.get("text", "") if best_chunk else ""
        filename = best_chunk.get("filename")
        category = best_chunk.get("category")
        year = best_chunk.get("year")
        source = best_chunk.get("source")
        start = best_chunk.get("char_start", 0) if best_chunk else 0
        end = best_chunk.get("char_end", 0) if best_chunk else 0

        results.append({
            "doc_id": d,
            "filename": filename,
            "label": filename,
            "category": category,
            "year": year,
            "source": source,
            "score": round(score, 4),
            "char_start": start,
            "char_end": end,
            "regions": regions,
            "text": chunk_text,
            "snippet": _highlight(chunk_text, qtokens),
        })
    return results
