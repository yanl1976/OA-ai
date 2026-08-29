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
# 绝对语义门槛（仅 BGE 向量生效）：用于「完整词法串全库未命中」时对纯向量召回
# 文档（档 0，零词法命中）的二次收紧。比 SIM_FLOOR(0.72) 更严格，过滤掉 BGE 把
# 专有名词（如人名「郑世勤」）语义关联到相邻主题（如「员工薪酬管理」）产生的弱误召回。
ABS_SEMANTIC_TIER = 0.80
# 每篇相关文档最多取多少个最相关 chunk 拼接进 content（控制 LLM 上下文长度，避免整本文档碎片噪声）
MAX_CHUNKS_PER_DOC = 3

# ============ 短语完整性加权（解决「检索结果碎片化」）============
# 问题：jieba.lcut_for_search 会把查询切成过细的 token（如「管理办法」→「管理」「办法」），
# BM25 对每个 token 独立打分，导致只命中「管理」或只命中「办法」的零碎片段也能拿到
# 高分并排到前面，而真正完整包含「管理办法」的段落反而被淹没。
#
# 方案（phrase boost + token coverage）：
#   1. 完整短语命中（exact phrase）：查询原串在文本中原样连续出现 → 最强信号，大幅加成；
#   2. 分词覆盖率（coverage）：命中的不同 token 数 / 总 token 数 → 覆盖越全越相关；
#   3. 低覆盖降权：只命中 1 个碎片 token 的文本视为弱相关，系数 <1 予以抑制。
# 加成后的分数同时用于 chunk 排序、文档级聚合与 RRF 融合，使「完整匹配」全程优先。
PHRASE_BOOST = 2.5        # 完整短语命中的加成强度（每出现 1 次累加，见 _phrase_boost 上限）
PHRASE_BOOST_MAX = 3.0    # 完整短语加成总上限（防止长文本靠堆次数刷分）
COVERAGE_WEIGHT = 1.2     # 覆盖率贡献权重：全覆盖额外加成，部分覆盖线性衰减
LOW_COVER_PENALTY = 0.35  # 低覆盖惩罚下限：仅命中极少 token 时最多降到该系数


def _tokenize(text):
    return [t for t in jieba.lcut_for_search(text) if t.strip()]


# 聚集度评分：把文本切成小段（句子/子句）后，评估查询 token 是否「挤在一起」出现。
# 长查询（如「安全生产责任制度」）在原文中往往没有完全一致的连续串，此时
# 「完整短语命中」加成无从触发，必须靠聚集度区分：
#   - 关键词集中在同一句 → 语义完整 → 高分
#   - 关键词散落在 1200 字各处（各命中一次）→ 碎片 → 低分
_SEG_SPLIT_RE = re.compile(r"[\n\r。！？；;!?，,、：:（）()\[\]【】\"'“”‘’]+")
PROX_WEIGHT = 2.0       # 聚集度贡献权重
# 聚集度达到该值即视为「语义完整」（各 token 集中在同一小句内），
# 在最终分档中进入第 2 档，优先于零散命中与纯语义结果。
PROX_TIER2 = 0.6
# 当结果中存在「完整包含查询原串」的文档（档 3）时，只保留 >= 该档的结果。
# 设为 2 表示：有精确命中就不同时展示那些正文里根本找不到关键词的文件。
# 调低为 1 可放宽（会重新出现零命中的干扰项），调高为 3 则只留精确命中。
TIER_KEEP_WHEN_EXACT = 2


def _merge_overlap_tokens(qtokens: list, idf: dict = None):
    """归并互为子串的重叠 token，避免冗余切分被重复计权。

    jieba 的 lcut_for_search 会产出大量子串重叠词（如「智能化工程专业分包」
    → 同时切出「智能」和「智能化」）。若各自独立计权，「智能」(IDF 4.9) 与
    「智能化」(IDF 4.9) 会被累加成 9.8，占去总权重的一半——文档只要出现
    「智能化」一次就白拿过半分数，导致漏掉真正关键词（如「分包」）的文档
    依然排在前面（真实 badcase：《公司章程》凭「智能化+工程」挤到第 2）。

    处理：按长度降序保留最长形式，子串 token 被吸收，组内权重取该组最大 IDF。
    返回 [(token, weight), ...]。
    """
    toks = [str(x).lower() for x in (qtokens or []) if str(x).strip()]
    if not toks:
        return []
    # 长词优先，保证「智能化」先于「智能」被保留
    toks = sorted(set(toks), key=lambda t: (-len(t), t))
    kept = []
    for t in toks:
        # 若 t 是任一已保留 token 的子串，则视为同义重叠，跳过
        if any(t != k[0] and t in k[0] for k in kept):
            continue
        w = float(idf.get(t, 1.0)) if idf else 1.0
        kept.append((t, w))
    return kept


def _proximity_score(text: str, qtokens: list, idf: dict = None) -> float:
    """关键词聚集度，返回 [0,1]。

    1 表示查询的各 token 集中在同一小段内同时出现（语义完整、不碎片）；
    0 表示 token 分散在文本各处或几乎未命中。

    实现：将文本按标点/换行切成小段，取「单段内命中的 token 的 IDF 权重之和 /
    全部 token 的 IDF 权重之和」的最大值。

    【为什么要按 IDF 加权】真实 badcase：检索「智能化工程专业分包」时，
    「工程/专业」在全库 58 个文档中出现（IDF 1.7，几乎没有区分度），
    而「分包」仅在 2 个文档中出现（IDF 5.5，区分度极高）。若等权计算覆盖率，
    只命中「工程+智能化」的《公司章程》会和真正命中「分包」的会议纪要拿到
    接近的分数，导致无关文件挤在前面。按 IDF 加权后，漏掉「分包」的文档
    覆盖率被显著拉低，正确结果自然胜出。

    idf 为 None 时退化为等权（保持向后兼容）。
    """
    if not text or not qtokens:
        return 0.0
    t = (text or "").lower()
    pairs = _merge_overlap_tokens(qtokens, idf)
    if not pairs:
        return 0.0

    total = sum(w for _, w in pairs)
    if total <= 0:
        return 0.0

    best = 0.0
    for seg in _SEG_SPLIT_RE.split(t):
        if not seg.strip():
            continue
        hit_w = sum(w for tk, w in pairs if tk in seg)
        if hit_w <= 0:
            continue
        cov = hit_w / total
        if cov > best:
            best = cov
        if best >= 1.0:
            break
    return best


def _phrase_boost(text: str, query: str, qtokens: list, idf: dict = None) -> float:
    """计算「短语完整性」加权系数（>=0.35，1.0 表示中性）。

    - query 原串在 text 中完整连续出现 → 强加成（最多 +PHRASE_BOOST_MAX）
    - 命中的 token 占比越高 → 越接近完整匹配，加成越高
    - 只命中个别碎片 token → 系数降到 LOW_COVER_PENALTY 附近，排到后面

    注：query 为单字/单词时（如「制度」），完整短语与分词结果重合，
    此时主要靠 coverage 与 BM25 原分区分，不会重复加成。
    """
    if not text:
        return LOW_COVER_PENALTY
    t = (text or "").lower()
    q = (query or "").strip().lower()
    if not q:
        return 1.0

    # 1) 完整短语命中次数（大小写不敏感，按原串连续匹配）
    exact_n = t.count(q) if len(q) >= 2 else 0

    # 2) 分词覆盖率：按 IDF 加权，且【必须在同一个句子内统计】。
    #
    # 【重要修复】原实现在【整个 chunk（约 1200 字）】范围内统计覆盖率，导致
    # 关键词散落在长文各处也算「全覆盖」，把检索带偏。真实 badcase：
    #     查询「安全生产」        → 正确命中《应急预案》《消防安全管理》
    #     查询「安全生产责任制」→ 却跑到《党风廉政建设责任制》《信访维稳》
    # 原因：「责任制」是稀有词（高 IDF），而它在党风廉政领域大量出现；整段
    # 统计时，只要 chunk 里同时飘着「安全生产」和「责任制」两个词（哪怕相隔
    # 八百字），覆盖率就是 100%，于是党风廉政文档凭高 IDF 词胜出。
    # 改为按句统计后，只有真正在同一句里同时出现「安全生产」和「责任制」的
    # 文档才算全覆盖，语义无关的长文自然被降权。
    coverage = _proximity_score(text, qtokens, idf=idf) if qtokens else 0.0

    # 3) 合成系数 = 基础分 + 完整短语加成 + 句级覆盖加成
    #    注：coverage 已改为句级 IDF 加权覆盖率，与「聚集度」是同一指标，
    #    故此处只计一次，避免同一信号被重复加成而失真。
    if exact_n > 0:
        # 完整短语命中：基础 1.0 + 按次数递增的加成（封顶）
        boost = 1.0 + min(exact_n * PHRASE_BOOST, PHRASE_BOOST_MAX)
        boost += PROX_WEIGHT * coverage
        return boost

    # 无完整短语：完全依赖「句级覆盖率」定分。
    # 场景：长查询（如「安全生产责任制」）在原文中未必有完全一致的连续串，
    # 但只要「安全生产 / 责任 / 制度」挤在同一句里出现，就仍应判为强相关；
    # 反之若它们散落在长文各处各命中一次，则属无关长文，应予降权。
    if coverage <= 0:
        return LOW_COVER_PENALTY
    return LOW_COVER_PENALTY + (1.0 - LOW_COVER_PENALTY) * coverage \
        + PROX_WEIGHT * coverage


def _highlight(text, query_tokens, query=None):
    """对 text 中出现的查询词做 HTML 转义 + <mark> 高亮。

    优先高亮【完整查询串】：先用整个 query 原串做一次整体高亮，再补充高亮各
    分词 token。这样用户在摘要里第一眼看到的是「完整关键词」被标出，而不是
    零散的字/词各自高亮（后者看起来就是碎片化）。
    """
    esc = _html.escape(text or "")

    # 1) 完整查询串优先整体高亮（长度>=2 才有意义，避免单字噪音）
    q = (query or "").strip()
    if len(q) >= 2:
        eq = _html.escape(q)
        esc = re.sub(re.escape(eq), lambda m: "<mark>" + m.group(0) + "</mark>",
                     esc, flags=re.IGNORECASE)

    # 2) 补充高亮未被完整串覆盖的分词（长词优先，避免短词抢先切割长词）
    seen = set()
    for t in sorted(query_tokens or [], key=lambda x: -len(str(x))):
        if len(t) < 2:  # 单字不强制高亮，避免噪音
            continue
        et = _html.escape(t)
        if et in seen:
            continue
        seen.add(et)
        # 跳过已被 <mark> 包裹的内容，避免破坏已生成的高亮标签
        def _sub(m):
            return "<mark>" + m.group(0) + "</mark>"
        parts = re.split(r"(<mark>.*?</mark>)", esc)
        parts = [p if p.startswith("<mark>") else re.sub(re.escape(et), _sub, p)
                 for p in parts]
        esc = "".join(parts)
    return esc


def _aggregate_doc_score(chunk_list):
    """文档级聚合分数：取文档内 top-3 chunk 分数之和（既体现"多命中"也抑制单碎片刷分）。"""
    if not chunk_list:
        return 0.0
    top = sorted((sc for _, sc in chunk_list), reverse=True)[:3]
    return float(sum(top))


def hybrid_search(query: str, top_k: int = 20, categories: list = None):
    """混合检索（文档级聚合重排 + 相对阈值过滤 + 每文档 top-N chunk 拼接）。

    科学范式（dense + sparse hybrid retrieval）:
      1. 向量(BGE语义) 与 BM25(词法) 各自召回同一文档的全部命中 chunk；
      2. 在【文档级】聚合 chunk 分数（top-3 求和），再分别排序得向量/BM25 两路文档排名；
      3. 加权 RRF 融合两路文档排名，消除"单文档多碎片"泛滥；
      4. 相对阈值过滤：归一化后低于 (最高分 * MIN_REL) 的长尾文档直接剔除；
      5. 对保留文档取最相关前 MAX_CHUNKS_PER_DOC 个 chunk 按原文顺序拼接为 content，
         既保证答案片段不丢失，又避免整本文档碎片噪声灌入 LLM。

    categories: 可选的文档分类白名单（类型名列表，如 ["管理标准", "会议纪要"] 或
        含子类名）。若提供，则只在白名单分类的 chunk 参与召回与融合——实现"按域硬约束"
        而非"全量召回后过滤"，既节省算力又杜绝越权片段进入结果。为 None 时不过滤。
    """
    query = (query or "").strip()
    if not query:
        return []
    qtokens = _tokenize(query)
    # 分类白名单（归一化为集合，O(1) 判定）
    _cat_allow = set(categories) if categories else None

    # ---- 向量语义召回（保留同一文档的全部命中 chunk）----
    # 【性能】BGE 模型冷启动需 ~20s（sentence-transformers 初始化要联网 HEAD 校验权重 +
    # CPU 推理）。若向量索引为空（从未重建 / 文档源为空），向量路必然 0 命中，
    # 却仍会触发模型加载，白白让用户多等 20s+。故先用 stats()（不触发模型加载，
    # 仅在 _INDEX 未加载时读文件判断）探测，为空则整段跳过，检索退化为纯 BM25（秒级）。
    vec_pairs = []
    try:
        _vs = vec_store.stats()
        _vec_ready = bool(_vs.get("ready")) and (_vs.get("chunks") or 0) > 0
    except Exception:
        _vec_ready = False
    if _vec_ready:
        try:
            vec_pairs = vec_store.search(query, top_k=top_k * 6)
        except Exception:
            vec_pairs = []
    vec_chunks = {}
    for meta, score in vec_pairs:
        d = meta.get("doc_id")
        if d is None:
            continue
        if _cat_allow is not None and meta.get("category") not in _cat_allow:
            continue
        vec_chunks.setdefault(d, []).append((meta, score))

    # ---- BM25 词法召回 ----
    bm25_chunks = {}
    # 复用 BM25 已算好的 IDF（区分度权重）：稀有词（如「分包」IDF 5.5）
    # 比常见词（如「工程」IDF 1.7）更能代表用户意图，用于加权聚集度与短语加成。
    _idf = {}
    try:
        bm25, chunks = rag_query.load_index()
        _idf = getattr(bm25, "idf", None) or {}
        scores = bm25.get_scores(qtokens)
        # 先按原始 BM25 分召回较宽的候选（保证不漏），再对候选做短语完整性重排序：
        # 完整包含查询原串的 chunk 分数被放大，只命中零碎 token 的被压低。
        # 召回宽度取 top_k*6，重排序后再取 top_k*3 进入后续融合，兼顾质量与开销。
        cand_n = min(len(scores), top_k * 6)
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:cand_n]
        rescored = []
        for i in order:
            if scores[i] <= 0:
                continue
            c = chunks[i]
            boost = _phrase_boost(c.get("text", ""), query, qtokens, idf=_idf)
            rescored.append((c, scores[i] * boost, scores[i], boost))
        # 按「完整串命中优先 + 加权分」重排，取前 top_k*3。
        # 【关键】必须让完整包含查询原串的 chunk 无条件排在最前：
        # 真实 badcase——检索「智能化工程专业分包」时，唯一真正命中的会议纪要
        # 全文仅 744 字（1 个 chunk），BM25 原始分不高；前端传 top_k=50 时候选池
        # 扩大到 300+，大量「工程/专业」等常见词的高分 chunk 挤进来，该文档即使
        # 乘上 6.1 倍加成也排不进截断名单，在分档前就被淘汰——导致最终 _max_tier
        # 达不到 3，分级门槛形同虚设，结果变成 21 条全部零命中的垃圾。
        # 置顶后，真正命中的文档必然进入候选，后续分档/门槛才能正确生效。
        _q_exact = (query or "").strip().lower()

        def _rescore_rank(item):
            c, final_sc, _raw, _b = item
            txt = (c.get("text", "") or "").lower()
            exact = 1 if (len(_q_exact) >= 2 and _q_exact in txt) else 0
            return (exact, final_sc)

        rescored.sort(key=_rescore_rank, reverse=True)
        for c, final_sc, _raw_sc, _boost in rescored[: top_k * 3]:
            d = c.get("doc_id")
            if d is None:
                continue
            if _cat_allow is not None and c.get("category") not in _cat_allow:
                continue
            bm25_chunks.setdefault(d, []).append((c, final_sc))
    except FileNotFoundError:
        bm25_chunks = {}
    except Exception:
        bm25_chunks = {}

    # ---- 绝对语义阈值过滤（仅清理「向量路」的弱语义结果）----
    # 单文档最高 chunk 余弦相似度 < SIM_FLOOR 视为语义不相关，从【向量路】剔除。
    #
    # 【重要修复·曾经的严重误杀】原实现会连坐删除 bm25_chunks：
    #     for d in low_docs:
    #         vec_chunks.pop(d, None)
    #         bm25_chunks.pop(d, None)   # ← 错误：把 BM25 强命中也删了
    # 后果（真实 badcase）：检索「智能化工程专业分包」时，唯一真正命中的
    # 会议纪要 BM25 得分 30.37 —— 全库第 1 名，但其向量余弦相似度低于 0.72
    # （会议纪要类文本普遍偏低，见下方说明），于是被判「语义不相关」并连坐
    # 从 BM25 结果中删除，最终用户看到 21 条全部零命中的无关文件。
    #
    # 语义相似度低 ≠ 词法不相关。BM25 精确命中本身就是强证据，不应被向量路
    # 的判定否决。两路独立召回、融合时各自贡献，才符合混合检索的本意。
    #
    # 注意：此处不可用 vec_store.get_index().embedder_type——get_index() 会构造
    # VecIndex 并触发 BGE 模型加载（~20s），即便向量路已在上文被跳过也会白白付费。
    # 复用上文 stats() 的 embedder 字段（不触发模型加载）。
    if _vec_ready and _vs.get("embedder") == "bge":
        low_docs = {
            d for d, lst in vec_chunks.items()
            if max((sc for _, sc in lst), default=0) < SIM_FLOOR
        }
        for d in low_docs:
            vec_chunks.pop(d, None)
            # 不再删除 bm25_chunks：BM25 命中是独立的词法证据，保留参与融合。

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
    # 注意：此处【不要】按 top_k 截断。截断必须发生在「质量分档排序之后」，
    # 否则会丢掉真正命中的文档。真实 badcase：检索「智能化工程专业分包」时，
    # 唯一命中的会议纪要只有 BM25 单路命中（它不在向量召回前列），RRF 分数
    # 低于那些「向量+BM25 双路都沾边」的无关文档；top_k=50 时候选暴增，它在
    # 分档前就被 [:top_k] 截掉，导致分档时看不到它、_max_tier 达不到 3、
    # 分级门槛形同虚设，最终返回 21 条全部零命中的垃圾文件。
    max_score = max(rrf.values()) if rrf else 0.0
    ranked = [(d, s) for d, s in sorted(rrf.items(), key=lambda kv: kv[1], reverse=True)
              if max_score > 0 and s >= max_score * MIN_REL]

    # ---- 匹配质量分档：把「真正完整匹配」的结果顶到最前面 ----
    # 背景（真实 badcase：检索「智能化工程专业分包」）：
    # RRF 是【纯排名】融合（1/(60+rank)），只关心文档在各路里的名次，完全丢弃
    # 分数差异。于是 BM25 路做的短语/聚集度加权只能影响该路内部排名，融合后被
    # 大幅稀释——真正完整包含查询原串的文档（会议纪要）被排到第 5，而只命中
    # 单个 token（甚至一个词都没命中、纯靠向量语义相似）的文档反而占据前 4。
    #
    # 对策：先按「词法匹配质量」分档，档内再按 RRF 分数排序。
    #   档 3 = 完整包含查询原串（最强信号，用户要的就是它）
    #   档 2 = 查询各 token 集中在同一小句内出现（语义完整，不碎片）
    #   档 1 = 有部分词法命中但较分散
    #   档 0 = 零词法命中（纯向量语义相似，最易产生「内容不符合」的误召回）
    _q_lower = (query or "").strip().lower()

    def _doc_quality(d):
        """返回 (tier, best_prox)：tier 越大匹配质量越高。"""
        hits = list(vec_chunks.get(d, [])) + list(bm25_chunks.get(d, []))
        best_exact = 0
        best_prox = 0.0
        for meta, _sc in hits:
            txt = (meta.get("text", "") or "")
            tl = txt.lower()
            if len(_q_lower) >= 2 and _q_lower in tl:
                best_exact += tl.count(_q_lower)
            p = _proximity_score(txt, qtokens, idf=_idf)
            if p > best_prox:
                best_prox = p
        if best_exact > 0:
            return 3, best_prox
        if best_prox >= PROX_TIER2:
            return 2, best_prox
        if best_prox > 0:
            return 1, best_prox
        return 0, 0.0

    _qual = {d: _doc_quality(d) for d, _ in ranked}
    ranked.sort(key=lambda ds: (_qual[ds[0]][0], _qual[ds[0]][1], ds[1]), reverse=True)

    # 供前端全文高亮用的匹配词：归并重叠词后的分词（如「智能/智能化」→「智能化」），
    # 并按长度降序（前端先标长词，避免短词抢先切割）。单字不返回，避免高亮噪音。
    _match_terms = [tk for tk, _w in _merge_overlap_tokens(qtokens, _idf) if len(tk) >= 2]
    if not _match_terms and query.strip():
        _match_terms = [query.strip()]

    # ---- 分级质量门槛：宁缺毋滥，杜绝「列出一堆却全文零命中」 ----
    # 真实 badcase：检索「智能化工程专业分包」返回 9 条，但只有 1 条的正文
    # 真正出现该词组，其余 8 条全文出现次数为 0——它们在列表里看起来像「匹配」，
    # 用户点开却找不到任何关键词（前端显示「正文未精确匹配关键词」）。
    #
    # 对策（分级收紧，最高档存在时就丢弃低档）：
    #   存在档 3（完整含查询原串）→ 只保留档 >= TIER_KEEP_WHEN_EXACT
    #   最高仅档 2（关键词高度聚集）→ 只保留档 >= 1，丢弃纯语义
    #   整体质量都低（最高档 <= 1）→ 不过滤，避免误杀召回
    # 若过滤后为空则回退到未过滤结果，保证「宁可宽松也不返回空」。
    _tiers = [_qual[d][0] for d, _ in ranked]
    _max_tier = max(_tiers) if _tiers else 0
    if _max_tier >= 3:
        _kept = [ds for ds in ranked if _qual[ds[0]][0] >= TIER_KEEP_WHEN_EXACT]
    elif _max_tier >= 2:
        _kept = [ds for ds in ranked if _qual[ds[0]][0] >= 1]
    else:
        # 完整词法串全库未命中（无任何文档含查询原串，最高档<=1）：
        # 此时结果里若存在文档，几乎全是「纯向量语义召回、零词法命中」的档 0 文档，
        # 极易误召回（如搜人名「郑世勤」却召回「员工薪酬管理」——BGE 把人名语义
        # 关联到相邻主题）。必须对档 0 加重过滤，否则会变成「列出一堆全文零命中」的垃圾。
        #   ① 先剔除档 0（纯向量零词法）；
        #   ② 若剔除后为空（全库无词法聚集文档），则仅保留向量单文档最高余弦相似度
        #      >= ABS_SEMANTIC_TIER(0.80，比 SIM_FLOOR 更严格) 的档 0 文档，过滤弱语义；
        #   ③ 仍为空则回退到未过滤结果（宁宽松也不空，避免误杀真实存在的弱相关）。
        _kept = [ds for ds in ranked if _qual[ds[0]][0] >= 1]
        if not _kept:
            _kept = [
                ds for ds in ranked
                if max((sc for _, sc in vec_chunks.get(ds[0], [])), default=0) >= ABS_SEMANTIC_TIER
            ]
            if not _kept:
                _kept = list(ranked)
    if _kept:
        ranked = _kept

    # 截断放在最后：分档排序 + 质量门槛都已完成，此时真正的命中必然排在最前，
    # 不会被数量截断挤掉（详见上方「不要按 top_k 截断」的说明）。
    ranked = ranked[:top_k]

    results = []
    for d, score in ranked:
        # 汇集该文档所有命中 chunk（向量 + BM25 两路），用于定位、拼接、最佳摘要
        all_hits = list(vec_chunks.get(d, [])) + list(bm25_chunks.get(d, []))

        # 最佳 chunk（用于摘要/定位）：优先「完整包含查询原串」的 chunk，
        # 其次才是分数最高者。原因：摘要给用户看的是「关键词在哪出现的上下文」，
        # 若挑中的 chunk 只有零散单字命中，摘要就会显得支离破碎（碎片化）。
        # 排序键 = (是否完整短语命中, 短语完整度系数, 原分数)，三者依次比较。
        _q = (query or "").strip().lower()

        def _chunk_rank(item):
            meta, sc = item
            txt = (meta.get("text", "") or "").lower()
            exact = 1 if (len(_q) >= 2 and _q in txt) else 0
            boost = _phrase_boost(meta.get("text", ""), query, qtokens, idf=_idf)
            return (exact, boost, sc)

        best_chunk = None
        best_sc = -1
        if all_hits:
            best_meta, best_sc = max(all_hits, key=_chunk_rank)
            best_chunk = best_meta

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
        # 选取时同样优先完整短语命中的 chunk（复用 _chunk_rank），保证拼接进
        # 上下文的是「关键词完整出现」的段落，而非零散碎片。
        top_hits = sorted(all_hits, key=_chunk_rank, reverse=True)[:MAX_CHUNKS_PER_DOC]
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
            "snippet": _highlight(chunk_text, qtokens, query),
            # 后端分词结果（已归并重叠词，如「智能/智能化」只保留「智能化」）。
            # 供前端在整篇全文里做关键词定位/高亮：前端没有分词能力，若只拿
            # 整个查询串去 indexOf，像「安全生产责任制度」这类在原文中写作
            # 「安全生产责任制」的长查询会永远匹配不到，导致用户看到
            # 「正文未精确匹配关键词」。按分词匹配即可正确标出命中位置。
            "terms": _match_terms,
        })
    return results
