#!/usr/bin/env python3
"""LLM 公共调用层（MiniMax，OpenAI 兼容格式）。

统一承载系统内的所有大模型调用：
  - 文档结构化排版（extract_text 复用）
  - 对话式智能问答（serve.py 对话接口复用）

所有调用均走 .env 配置，无 key 时显式抛错，不降级为"静默空答"。
"""
import os
import json
import urllib.request
import urllib.error
from pathlib import Path

try:
    from dotenv import load_dotenv
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
except ImportError:  # pragma: no cover
    pass

MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY", "")
MINIMAX_API_URL = os.environ.get(
    "MINIMAX_API_URL",
    "https://api.minimax.chat/v1/chat/completions",
)
MINIMAX_MODEL = os.environ.get("MINIMAX_MODEL", "abab6.5s-chat")

# 推理模型（MiniMax-M2.x 系列）的思考过程【无法关闭】，响应 content 会包含
# <think>...</think> 标签。若不剥离，用户会在回答开头看到一长串思考过程
# （M2.5 的思考常为英文），观感极差。默认剥离，可通过环境变量关闭（调试用）。
STRIP_THINKING = os.environ.get("MINIMAX_STRIP_THINKING", "true").strip().lower() not in (
    "0", "false", "no", "off", "")

# 推理模型推荐 temperature=1.0（官方建议），低温会压制其推理能力。
# 但文档结构化排版等「忠实还原」类任务仍需低温，故保留按场景传参。
_DEFAULT_TEMPERATURE = 1.0

_THINK_RE = None


def _strip_thinking(text: str) -> str:
    """剥离响应中的 <think>...</think> 思考过程，只保留正式回答。"""
    global _THINK_RE
    if not text:
        return text
    if _THINK_RE is None:
        import re
        _THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
    cleaned = _THINK_RE.sub("", text)
    # 兼容未闭合的 <think>（响应被 max_tokens 截断时可能出现）
    if "<think>" in cleaned.lower():
        import re
        cleaned = re.sub(r"<think>.*$", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    return cleaned.strip()


def is_configured() -> bool:
    return bool(MINIMAX_API_KEY)


def chat(messages: list, *, temperature: float = None, max_tokens: int = 8192,
         timeout: int = 300) -> str:
    """通用对话接口。

    messages: [{"role": "system"|"user"|"assistant", "content": "..."}, ...]
    返回模型回复文本（str），已自动剥离推理模型的 <think> 思考过程。

    参数说明：
      temperature: 默认 1.0（推理模型官方推荐）。忠实还原类任务请显式传低值。
      max_tokens:  默认 8192。推理模型会先消耗 token 做思考，原来的 2048
                   会导致正式回答被截断，故调大。
    """
    if not MINIMAX_API_KEY:
        raise RuntimeError("MINIMAX_API_KEY 未配置，请在 .env 中设置")

    if temperature is None:
        temperature = _DEFAULT_TEMPERATURE

    payload = {
        "model": MINIMAX_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {MINIMAX_API_KEY}",
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(
        MINIMAX_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError("LLM HTTP 错误 %s: %s" % (e.code, e.read().decode()))
    except urllib.error.URLError as e:
        raise RuntimeError("LLM 网络错误: %s" % e)

    # OpenAI 兼容返回格式
    content = None
    if "choices" in result and result["choices"]:
        content = result["choices"][0]["message"]["content"]
    elif "reply" in result:  # 兼容旧版 MiniMax
        content = result["reply"]
    else:
        raise RuntimeError("LLM 返回异常: %s" % json.dumps(result, ensure_ascii=False)[:500])

    # 剥离推理模型的思考过程（<think>...</think>），只保留正式回答
    if STRIP_THINKING:
        content = _strip_thinking(content)
        # 思考被剥离后可能整体为空（极少数情况：模型只思考未作答）
        if not content:
            raise RuntimeError(
                "LLM 仅返回了思考过程而未给出正式回答，请重试或调大 max_tokens")
    return content


def structured_extract(raw_text: str) -> str:
    """文档结构化排版（忠实原文重排）。供 extract_text 复用。"""
    system_prompt = (
        "你是一个中文文档排版还原工具。输入是 PDF/Word 抽取出的原始文本"
        "（可能所有内容挤在一起、缺失换行、章节条目粘连、表格数字密集）。\n"
        "你的任务 ONLY 是重新排版输出，必须严格忠实于原文。\n\n"
        "铁律（违反即失败）：\n"
        "1. 不得删除、省略、跳过原文的任何文字、数字、符号、表格单元格。\n"
        "2. 不得改写、概括、翻译、纠正原文语义；保留所有数字（含页码、尺寸、规格、代号）。\n"
        "3. 不得新增原文没有的内容。\n\n"
        "排版规则（仅调整换行与间距，不改变内容）：\n"
        "A. 章节编号（1、2、2.1、3.1、3.2.1、第一章、第一条、（一）、Q/CT 300-2022）保持原样，单独成行或随原文。\n"
        "B. 普通正文按自然段落整理换行（连续的中文句子可合并为一段，段间空行）。\n"
        "C. 表格内容必须完整保留——每一行的每一个格子数字/文字都要输出，"
        "可用制表符分隔列；印刷规格表（字体字号、封面尺寸标注等）同样完整保留。\n"
        "D. 封面区（单位名、文件标题、文号、发布/实施日期）保持原文排列方式，不要重排为字段名。\n"
        "E. 直接输出整理后的纯文本，不要任何说明文字、不要 Markdown 代码块标记。\n"
    )
    snippet = raw_text[:60000]
    return chat([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": (
            "请将以下文档严格忠实原文地整理为干净的多行纯文本（不得遗漏任何内容）：\n\n"
            + snippet)},
        # 「忠实还原原文」类任务必须低温，避免推理模型的默认高温导致自由发挥
    ], temperature=0.1, max_tokens=8192)


# ============ 检索增强：查询改写 + 对比问题分解 ============
# 这两项用于解决「检索喂料不准」导致的答非所问：
#   1) 多轮对话中用户常省略主语（如「那它的流程呢？」），直接用这句话去检索
#      必然搜不到，需结合上文补全为独立问句。
#   2) 跨文档对比类问题（如「A 和 B 有什么区别」）单轮检索往往只召回其中一方，
#      需拆成多个子问题分别检索后合并。
# 均为「轻量辅助调用」：低温、限长、失败即回退到原问题，绝不阻塞主流程。

_AUX_TIMEOUT = 30      # 辅助调用超时（秒），避免拖慢整体响应
_AUX_MAX_TOKENS = 512  # 辅助调用只需短输出


def rewrite_query(question: str, history: list, timeout: int = _AUX_TIMEOUT) -> str:
    """把多轮对话中的「省略指代」问题补全为可独立检索的问句。

    例：上文谈「安全生产责任制」，用户问「那它的流程呢？」
        → 「安全生产责任制的工作流程是什么？」

    history: [{"role": "user"|"assistant", "content": "..."}, ...]
    失败或无需改写时返回原问题（保证主流程不被阻塞）。
    """
    if not question or not history:
        return question  # 首轮无历史，无需改写
    # 只取最近若干轮，控制 prompt 体积与延迟
    recent = history[-6:]
    hist_txt = "\n".join(
        ("用户：" if m.get("role") == "user" else "助手：") + (m.get("content") or "")[:400]
        for m in recent)
    system = (
        "你是检索查询改写器。结合对话历史，把用户最后的问题改写为"
        "【可独立理解】的完整问句，用于全文检索。\n"
        "铁律：\n"
        "1. 只输出改写后的问句本身，不要任何解释、引号、前缀。\n"
        "2. 必须保留原问题的核心意图，不得改变提问方向。\n"
        "3. 若原问题本身已完整独立（无指代、无省略），原样输出即可。\n"
        "4. 补全时优先使用上文出现过的【文档名/制度名/术语】原词。\n"
    )
    try:
        out = chat([
            {"role": "system", "content": system},
            {"role": "user", "content": "对话历史：\n%s\n\n用户最后的问题：%s\n\n改写后的检索问句："
             % (hist_txt, question)},
        ], temperature=0.1, max_tokens=_AUX_MAX_TOKENS, timeout=timeout)
        out = (out or "").strip().strip("\"'“”‘’ \n")
        # 异常保护：改写结果为空或过长则回退原问题
        if not out or len(out) > 200:
            return question
        return out
    except Exception:
        return question  # 改写失败不影响主流程


# 对比/多主体意图的关键词（用于避免对每个简单问题都做 LLM 分解，控制延迟）
_COMPARE_HINTS = ("对比", "比较", "区别", "差异", "异同", "相比", "有何不同",
                  "哪个", "有哪些不同", "对照", " versus ", " vs ")


def looks_like_comparison(question: str) -> bool:
    """快速判断是否疑似「对比/多主体」问题（纯规则，零延迟）。"""
    q = (question or "").lower()
    return any(h in q for h in _COMPARE_HINTS)


def decompose_question(question: str, timeout: int = _AUX_TIMEOUT) -> list:
    """把对比/多主体问题拆为若干可独立检索的子问题。

    例：「对比安全生产责任制和党风廉政责任制的差异」
        → ["安全生产责任制的要求和内容", "党风廉政建设责任制的要求和内容"]

    返回子问题列表；失败或不适用时返回 [原问题]（主流程不受影响）。
    """
    if not question:
        return []
    system = (
        "你是检索任务分解器。把需要【跨多份资料对比】的问题，拆成若干个"
        "可独立检索的子问题，确保每个子问题都能单独检索到其中一方的资料。\n"
        "铁律：\n"
        "1. 每行一个子问题，不要编号、不要解释、不要多余文字。\n"
        "2. 子问题数量控制在 2-4 个。\n"
        "3. 每个子问题必须保留原问题中的【具体对象名称原文】。\n"
        "4. 若问题只涉及单一对象、无需对比，则只输出原问题本身一行。\n"
    )
    try:
        out = chat([
            {"role": "system", "content": system},
            {"role": "user", "content": "原问题：%s\n\n子问题列表：" % question},
        ], temperature=0.1, max_tokens=_AUX_MAX_TOKENS, timeout=timeout)
        lines = [(l.strip().lstrip("0123456789.、)（- "))
                 for l in (out or "").splitlines()]
        subs = [l for l in lines if len(l) >= 4][:4]
        if not subs:
            return [question]
        # 始终保留原问题作为第一个检索入口（保证整体语义不丢）
        return [question] + [s for s in subs if s != question][:3]
    except Exception:
        return [question]
