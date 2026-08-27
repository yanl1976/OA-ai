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
# 文档级融合时，向量通道相对 BM25 的权重（bge 语义区分度更好，略加权）
RRF_WEIGHT_VEC = 1.0
RRF_WEIGHT_BM25 = 0.8
# 相对阈值：归一化后低于 (最高分 * MIN_REL) 的文档视为长尾噪声，剔除
MIN_REL = 0.3
# 绝对语义阈值（仅当使用 BGE 语义向量时生效）：单文档最高 chunk 余弦相似度
# 低于此值的文档视为语义不相关，直接剔除（如"会议纪要"类 0.67 远低于标准类 0.8+）。
# 该值基于归一化余弦的物理意义（<0.72 通常语义无关），回退哈希向量时不启用。
SIM_FLOOR = 0.72
# 每篇相关文档最多取多少个最相关 chunk 拼接进 content（控制 LLM 上下文长度，避免整本文档碎片噪声）
MAX_CHUNKS_PER_DOC = 3


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


def _aggregate_doc_score(chunk_list):
    """文档级聚合分数：取文档内 top-3 chunk 分数之和（既体现"多命中"也抑制单碎片刷分）。"""
    if not chunk_list:
        return 0.0
    top = sorted((sc for _, sc in chunk_list), reverse=True)[:3]
    return float(sum(top))


def hybrid_search(query: str, top_k: int = 20):
    """混合检索（文档级聚合重排 + 相对阈值过滤 + 每文档 top-N chunk 拼接）。

    科学范式（dense + sparse hybrid retrieval）:
      1. 向量(BGE语义) 与 BM25(词法) 各自召回同一文档的全部命中 chunk；
      2. 在【文档级】聚合 chunk 分数（top-3 求和），再分别排序得向量/BM25 两路文档排名；
      3. 加权 RRF 融合两路文档排名，消除"单文档多碎片"泛滥；
      4. 相对阈值过滤：归一化后低于 (最高分 * MIN_REL) 的长尾文档直接剔除；
      5. 对保留文档取最相关前 MAX_CHUNKS_PER_DOC 个 chunk 按原文顺序拼接为 content，
         既保证答案片段不丢失，又避免整本文档碎片噪声灌入 LLM。
    """
    query = (query or "").strip()
    if not query:
        return []
    qtokens = _tokenize(query)

    # ---- 向量语义召回（保留同一文档的全部命中 chunk）----
    try:
        vec_pairs = vec_store.search(query, top_k=top_k * 6)
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
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[: top_k * 6]
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

    # ---- 绝对语义阈值过滤（仅 BGE 语义向量生效，须在聚合/融合前执行）----
    # 单文档最高 chunk 余弦相似度 < SIM_FLOOR 视为语义不相关（如会议纪要类
    # 0.67 远低于标准类 0.8+）。直接在源头剔除该文档的两路 chunk，避免无关噪音进入融合。
    if vec_store.get_index().embedder_type == "bge":
        low_docs = {
            d for d, lst in vec_chunks.items()
            if max((sc for _, sc in lst), default=0) < SIM_FLOOR
        }
        for d in low_docs:
            vec_chunks.pop(d, None)
            bm25_chunks.pop(d, None)

    # ---- 文档级分数聚合 ----
    vec_doc = {d: _aggregate_doc_score(lst) for d, lst in vec_chunks.items()}
    bm25_doc = {d: _aggregate_doc_score(lst) for d, lst in bm25_chunks.items()}

    doc_ids = set(vec_chunks) | set(bm25_chunks)
    if not doc_ids:
        return []

    # ---- 加权 RRF 文档级融合 ----
    rrf = {}
    for rank, (d, _sc) in enumerate(sorted(vec_doc.items(), key=lambda kv: kv[1], reverse=True)):
        rrf[d] = rrf.get(d, 0.0) + RRF_WEIGHT_VEC / (RRF_K + rank + 1)
    for rank, (d, _sc) in enumerate(sorted(bm25_doc.items(), key=lambda kv: kv[1], reverse=True)):
        rrf[d] = rrf.get(d, 0.0) + RRF_WEIGHT_BM25 / (RRF_K + rank + 1)

    # ---- 相对阈值过滤（归一化后剔除长尾噪声）----
    max_score = max(rrf.values()) if rrf else 0.0
    ranked = [(d, s) for d, s in sorted(rrf.items(), key=lambda kv: kv[1], reverse=True)
              if max_score > 0 and s >= max_score * MIN_REL][:top_k]

    results = []
    for d, score in ranked:
        # 汇集该文档所有命中 chunk（向量 + BM25 两路），用于定位、拼接、最佳摘要
        all_hits = list(vec_chunks.get(d, [])) + list(bm25_chunks.get(d, []))

        # 最佳 chunk（用于摘要/定位）：取分数最高者
        best_chunk = None
        best_sc = -1
        for meta, sc in all_hits:
            if sc > best_sc:
                best_sc = sc
                best_chunk = meta

        # regions：所有命中 chunk 的字符区间（去重、排序），供前端"内容高亮"
        regions = []
        seen_r = set()
        for meta, _sc in all_hits:
            s = meta.get("char_start")
            e = meta.get("char_end")
            if s is None or e is None:
                continue
            key = (s, e)
            if key not in seen_r:
                seen_r.add(key)
                regions.append([s, e])
        regions.sort()

        # content：取该文档最相关的前 MAX_CHUNKS_PER_DOC 个 chunk，按原文顺序拼接。
        # 既不丢失答案片段，又避免整本文档所有碎片灌入上下文造成噪声。
        top_hits = sorted(all_hits, key=lambda ms: ms[1], reverse=True)[:MAX_CHUNKS_PER_DOC]
        raw_chunks = sorted((m for m, _ in top_hits),
                            key=lambda m: m.get("char_start", 0) or 0)
        content = "\n".join((m.get("text", "") or "").strip()
                            for m in raw_chunks if (m.get("text") or "").strip())

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
            "content": content,
            "snippet": _highlight(chunk_text, qtokens),
        })
    return results
