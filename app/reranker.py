"""Cross-Encoder Reranker 精排（chunk 级交互式打分，根治 BM25 长度偏好 / 文档级聚合丢信号）。

模型：BAAI/bge-reranker-base（中文友好，约 278MB，CPU 可跑）。
作用：混合召回（BM25 + BGE 向量）在「文档级」融合后仍受 BM25 短文档偏好与
「同文档多 chunk 信号被稀释」影响（典型 badcase：查「安宁宁」时，短「列席人员名单」
chunk 排在长「职务任免议案」chunk 前面）。Reranker 把 (query, chunk) 拼接进同一模型
做交互式打分，能直接判断「含安宁宁且含兼任」的议案 chunk 比「仅列席」更相关，
从而把答案片段正确顶上来。

可用性：模型未安装 / 加载失败 / 推理异常时 is_available() 返回 False，调用方
（search.hybrid_search）自动回退到既有的 tier + RRF 排序，绝不阻塞对话。
"""
import os

# 国内镜像拉取（避免 huggingface 直连超时）；若已本地缓存则直接命中，不触发联网。
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

_RERANKER = None
_NO_MODEL = False


def _load():
    """懒加载 CrossEncoder 单例；仅在第一次尝试且确认无模型后永久标记 _NO_MODEL，
    避免每次请求都重新尝试加载 278MB 模型。部署装上模型后重启进程即可生效。"""
    global _RERANKER, _NO_MODEL
    if _NO_MODEL:
        return None
    if _RERANKER is not None:
        return _RERANKER
    try:
        from sentence_transformers import CrossEncoder
        _RERANKER = CrossEncoder("BAAI/bge-reranker-base")
    except Exception:
        _RERANKER = None
        _NO_MODEL = True
    return _RERANKER


def is_available():
    return _load() is not None


def rerank(query: str, docs: list):
    """对 (query, doc) 逐对打分。docs: list[str]。返回 list[float]（与 docs 同序）；
    模型不可用或推理异常时返回 None，由调用方回退。"""
    model = _load()
    if model is None:
        return None
    try:
        pairs = [[query, d] for d in docs]
        scores = model.predict(pairs)
        return [float(s) for s in scores]
    except Exception:
        return None
