# -*- coding: utf-8 -*-
"""模型调用边界层（参照 AI 开发标准模板 2.1 / 4.3）。

职责：
  - call_chat()：统一封装 fetch（urllib）/ 超时 / 错误归一化 / 重试 / 思考关闭。
  - strip_thinking()：剥离推理模型的 <think> 思考过程（MiniMax-M2.x 无法关闭）。
  - 复用辅助函数：structured_extract（文档排版）/ rewrite_query（查询改写）/
    looks_like_comparison（对比判断）/ decompose_question（对比分解）。
      这些被对话以外的功能（extract_text）复用，故留在公共底座而非对话专用层。

设计纪律（与标准模板一致）：
  - 超时用 socket 超时（Python 对应 AbortController）。
  - 错误归一化：401/403 直接抛（不重试）；429/5xx/网络重试。
  - 日志只打印长度 / 状态 / 条数，绝不打印 key / 正文 / quote。
"""
import os
import re
import json
import time
import logging
import urllib.request
import urllib.error

from ai.llm_config import (
    api_key, api_url, model, is_enabled,
    max_tokens as _def_max_tokens, timeout as _def_timeout, retries as _def_retries,
)

logger = logging.getLogger("kb.llm.client")

# 推理模型（MiniMax-M2.x 系列）的思考过程【无法关闭】，响应 content 会包含
# <think>...</think> 标签。若不剥离，用户会在回答开头看到一长串思考过程。
STRIP_THINKING = os.environ.get("MINIMAX_STRIP_THINKING", "true").strip().lower() not in (
    "0", "false", "no", "off", "")


_THINK_RE = None


def _strip_thinking(text):
    """剥离响应中的 <think>...</think> 思考过程，只保留正式回答。"""
    global _THINK_RE
    if not text:
        return text
    if _THINK_RE is None:
        _THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
    cleaned = _THINK_RE.sub("", text)
    # 兼容未闭合的 <think>（响应被 max_tokens 截断时可能出现）
    if "<think>" in cleaned.lower():
        cleaned = re.sub(r"<think>.*$", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    return cleaned.strip()


def is_configured():
    return is_enabled()


def _classify_http(e):
    if isinstance(e, urllib.error.HTTPError):
        return e.code
    return None


def call_chat(messages, *, temperature=None, max_tokens=None,
              timeout=None, retries=None, strip_thinking=None):
    """通用对话调用（OpenAI 兼容格式）。

    messages: [{"role": "system"|"user"|"assistant", "content": "..."}, ...]
    返回模型回复文本（str，已剥离 <think> 思考过程）。

    参数全部有默认值（来自 llm_config 的成本闸门），调用方可按需覆盖。
    失败抛出 RuntimeError（带可诊断信息）；401/403 不重试，429/5xx/网络重试。
    """
    if not api_key():
        raise RuntimeError("MINIMAX_API_KEY 未配置，请在 .env 中设置")

    if temperature is None:
        temperature = 1.0  # 推理模型官方推荐；忠实还原类任务请显式传低值
    _max = _def_max_tokens() if max_tokens is None else max_tokens
    _timeout = _def_timeout() if timeout is None else timeout
    _retries = _def_retries() if retries is None else retries

    payload = {
        "model": model(),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": _max,
    }
    headers = {
        "Authorization": "Bearer %s" % api_key(),
        "Content-Type": "application/json",
    }
    req = urllib.request.Request(
        api_url(),
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    last_err = None
    for attempt in range(max(1, _retries + 1)):
        try:
            with urllib.request.urlopen(req, timeout=_timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            content = None
            if "choices" in result and result["choices"]:
                content = result["choices"][0]["message"]["content"]
            elif "reply" in result:  # 兼容旧版 MiniMax
                content = result["reply"]
            else:
                raise RuntimeError("LLM 返回异常: %s"
                                   % json.dumps(result, ensure_ascii=False)[:500])

            if strip_thinking if strip_thinking is not None else STRIP_THINKING:
                content = _strip_thinking(content)
                if not content:
                    raise RuntimeError(
                        "LLM 仅返回了思考过程而未给出正式回答，请重试或调大 max_tokens")
            logger.info("LLM 调用成功：输入 %d 条消息，回复 %d 字（第 %d 次尝试）",
                        len(messages), len(content), attempt + 1)
            return content

        except urllib.error.HTTPError as e:
            code = e.code
            # 401/403 是鉴权问题，重试只是浪费配额 —— 直接抛
            if code in (401, 403):
                raise RuntimeError("LLM 鉴权失败（HTTP %s）：请检查 MINIMAX_API_KEY" % code)
            last_err = RuntimeError("LLM HTTP 错误 %s: %s" % (code, e.read().decode()[:300]))
            logger.warning("LLM HTTP %s（瞬时故障，将重试 %d/%d）", code, attempt + 1, _retries)
        except urllib.error.URLError as e:
            last_err = RuntimeError("LLM 网络错误: %s" % e)
            logger.warning("LLM 网络错误（将重试 %d/%d）: %s", attempt + 1, _retries, e)
        except RuntimeError:
            raise
        except Exception as e:  # noqa: BLE001
            last_err = RuntimeError("LLM 未知错误: %s" % e)
            logger.warning("LLM 调用异常（将重试 %d/%d）: %s", attempt + 1, _retries, e)

        # 重试瞬时故障（指数退避，封顶 8s）
        if attempt < _retries:
            time.sleep(min(2 ** attempt, 8))

    raise last_err or RuntimeError("LLM 调用失败（未知原因）")


# ============ 检索增强：查询改写 + 对比问题分解 ============
# 这两项用于解决「检索喂料不准」导致的答非所问（多轮省略指代 / 跨文档对比）。
# 均为「轻量辅助调用」：低温、限长、失败即回退到原问题，绝不阻塞主流程。
_AUX_TIMEOUT = 30      # 辅助调用超时（秒），避免拖慢整体响应
_AUX_MAX_TOKENS = 512  # 辅助调用只需短输出


def structured_extract(raw_text):
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
    return call_chat([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": (
            "请将以下文档严格忠实原文地整理为干净的多行纯文本（不得遗漏任何内容）：\n\n"
            + snippet)},
    ], temperature=0.1, max_tokens=8192)


def rewrite_query(question, history, timeout=_AUX_TIMEOUT):
    """把多轮对话中的「省略指代」问题补全为可独立检索的问句。

    history: [{"role": "user"|"assistant", "content": "..."}, ...]
    失败或无需改写时返回原问题（保证主流程不被阻塞）。
    """
    if not question or not history:
        return question  # 首轮无历史，无需改写
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
        out = call_chat([
            {"role": "system", "content": system},
            {"role": "user", "content": "对话历史：\n%s\n\n用户最后的问题：%s\n\n改写后的检索问句："
             % (hist_txt, question)},
        ], temperature=0.1, max_tokens=_AUX_MAX_TOKENS, timeout=timeout)
        out = (out or "").strip().strip("\"'“”‘’ \n")
        if not out or len(out) > 200:
            return question
        return out
    except Exception:  # noqa: BLE001
        return question  # 改写失败不影响主流程


_COMPARE_HINTS = ("对比", "比较", "区别", "差异", "异同", "相比", "有何不同",
                  "哪个", "有哪些不同", "对照", " versus ", " vs ")


def looks_like_comparison(question):
    """快速判断是否疑似「对比/多主体」问题（纯规则，零延迟）。"""
    q = (question or "").lower()
    return any(h in q for h in _COMPARE_HINTS)


def decompose_question(question, timeout=_AUX_TIMEOUT):
    """把对比/多主体问题拆为若干可独立检索的子问题。

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
        out = call_chat([
            {"role": "system", "content": system},
            {"role": "user", "content": "原问题：%s\n\n子问题列表：" % question},
        ], temperature=0.1, max_tokens=_AUX_MAX_TOKENS, timeout=timeout)
        lines = [(l.strip().lstrip("0123456789.、)（- "))
                 for l in (out or "").splitlines()]
        subs = [l for l in lines if len(l) >= 4][:4]
        if not subs:
            return [question]
        return [question] + [s for s in subs if s != question][:3]
    except Exception:  # noqa: BLE001
        return [question]
