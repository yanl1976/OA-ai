# -*- coding: utf-8 -*-
"""模型建立层（参照 AI 开发标准模板 2.1）。

设计要点：
  - 环境变量一律函数内懒读，绝不在模块顶层求值（顶层求值受 import 顺序 /
    dotenv 注入时机影响，是历史反例）。
  - 值做去引号处理（.env 里 KEY="v" 若不剥离，URL 会变成 "https://..." 报错）。
  - 能力开关 is_enabled()：api_key 非空即视为已启用。
  - 成本闸门全部显式封顶：max_tokens / timeout / retries / concurrency /
    doc_char_budget，正文规模绝不决定账单。
"""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
except ImportError:  # pragma: no cover
    pass


def _strip_q(v):
    """剥离 .env 可能带上的引号（"v" / 'v'）。"""
    if not v:
        return v
    v = v.strip()
    if len(v) >= 2 and v[0] in ('"', "'") and v[-1] == v[0]:
        v = v[1:-1].strip()
    return v


def api_key():
    return _strip_q(os.environ.get("MINIMAX_API_KEY", ""))


def api_url():
    return (_strip_q(os.environ.get(
        "MINIMAX_API_URL",
        "https://api.minimax.chat/v1/chat/completions"))
        or "https://api.minimax.chat/v1/chat/completions")


def model():
    return os.environ.get("MINIMAX_MODEL", "abab6.5s-chat")


def is_enabled():
    """能力开关：api_key 非空即视为 AI 已启用。"""
    return bool(api_key())


# ---------------- 成本闸门（全部显式封顶） ----------------
def max_tokens():
    try:
        return int(os.environ.get("CHAT_MAX_TOKENS", "8192"))
    except (TypeError, ValueError):
        return 8192


def timeout():
    """单次上游超时（秒）。收紧历史 300s → 120s，避免长尾拖死请求。"""
    try:
        return int(os.environ.get("CHAT_TIMEOUT", "120"))
    except (TypeError, ValueError):
        return 120


def retries():
    """瞬时故障重试次数（429/5xx/网络），非瞬态（401/格式）不重试。"""
    try:
        return int(os.environ.get("CHAT_RETRIES", "2"))
    except (TypeError, ValueError):
        return 2


def concurrency():
    """辅助调用（如对比问题分解）的并发上限，避免全并发限流。"""
    try:
        return int(os.environ.get("CHAT_CONCURRENCY", "3"))
    except (TypeError, ValueError):
        return 3


def doc_char_budget():
    """对话单篇参考文档的字符预算（1500-2500 区间，默认 2500）。

    上调至上限：长制度文档（如病假/医疗期待遇、跨多页的章节）在 2000 字处常被截断，
    缺失关键条款会诱发模型『漏答』或『凭空补条款』。2500 字可覆盖绝大多数单制度段落，
    代价仅是每个检索片段略多 token（top_k 文档总字符上限约 8×2500=2 万，仍在上下文内）。
    """
    try:
        v = int(os.environ.get("CHAT_DOC_CHAR_BUDGET", "2500"))
    except (TypeError, ValueError):
        v = 2500
    return max(1500, min(2500, v))


def ref_gate_enabled():
    """引用硬闸门开关（默认关闭）。

    开启后：若回答引用了未检索到的文档（疑似编造引用），引擎会追加纠正指令
    自动重答一次，把编造引用压回已提供的参考文档。关闭时仅把编造引用列表
    回传前端提示，不额外消耗一次 LLM 调用（保持低延迟）。
    """
    return os.environ.get("CHAT_REF_GATE", "0").strip().lower() in (
        "1", "true", "yes", "on")


def fact_check_enabled():
    """内容级事实数值校验开关（默认关闭）。

    开启后：引擎对回答中的关键数值（天数/金额/百分比）做一次保守校验，
    若数值未在参考原文中出现，返回 fact_warnings 供前端提示。关闭时完全不执行，
    零额外开销、零误报打扰。该功能易误报（refs 仅为相关 chunk、纯文字曲解检测不到），
    默认关闭，需评估对输出质量的影响后再开启。
    """
    return os.environ.get("CHAT_FACT_CHECK", "0").strip().lower() in (
        "1", "true", "yes", "on")
