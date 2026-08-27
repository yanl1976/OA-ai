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


def is_configured() -> bool:
    return bool(MINIMAX_API_KEY)


def chat(messages: list, *, temperature: float = 0.3, max_tokens: int = 2048,
         timeout: int = 300) -> str:
    """通用对话接口。

    messages: [{"role": "system"|"user"|"assistant", "content": "..."}, ...]
    返回模型回复文本（str）。
    """
    if not MINIMAX_API_KEY:
        raise RuntimeError("MINIMAX_API_KEY 未配置，请在 .env 中设置")

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
    if "choices" in result and result["choices"]:
        return result["choices"][0]["message"]["content"]
    if "reply" in result:  # 兼容旧版 MiniMax
        return result["reply"]
    raise RuntimeError("LLM 返回异常: %s" % json.dumps(result, ensure_ascii=False)[:500])


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
    ], temperature=0.1, max_tokens=8192)
