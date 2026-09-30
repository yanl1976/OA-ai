# -*- coding: utf-8 -*-
"""对话回答的整段语义事实校验（参照 AI 开发标准模板 4.4 校验原则）。

为何需要：引用软校验只查《文件名》、数值软校验只查带单位数字，都兜不住
『文件名真实但内容被曲解』『人物-职务-会议等实体关系编造』（如李小宝任职案例）。
本层把「回答整段」与「参考原文整段」交事实核查模型做 NLI：
  给定参考原文，逐条核查回答中的事实陈述是否被原文直接支持，
  只标记『原文完全无支持 / 与原文矛盾』的陈述。

设计取舍：
  - 软校验、不阻断、可独立开关（CHAT_SEMANTIC_CHECK，默认关闭）：
    每轮多一次 LLM 调用（成本+延迟），故默认关，按需开启。
  - 用生成阶段所用的完整 context_block 作为「依据原文」（比 refs 截断 chunk 更全），
    降低因原文不全导致的误报。
  - 解析失败 / 模型不返回 JSON 时静默放行（校验层永不阻断回答、永不抛错给主链路）。
"""
import json
import logging

logger = logging.getLogger("kb.chat.semantic")

_SYSTEM = """你是一名严谨的事实核查员。下面会提供「参考原文」与「AI回答」。
请逐条核查回答中的事实性陈述（人物职务/任职、会议决议、日期、数字、条款、流程等），
判断是否能被参考原文直接支持。

规则：
1. 仅标记「在参考原文中完全找不到任何支持」或「明显与原文矛盾」的陈述。
2. 原文未提及但属合理推断/常识、或仅表述方式不同的，不要标记。
3. 对于原文未提供相关信息、无法判断的，也不要标记（只标『无支持/矛盾』）。

输出：仅输出一个 JSON 数组，每项形如
{"claim":"被核查的精简陈述","verdict":"unsupported(无支持)|contradicted(矛盾)","reason":"简要依据"}
若回答中所有陈述均可被原文支持，输出空数组 []。不要输出数组以外的任何文字。"""


def semantic_check(answer, context, max_claims=12):
    """整段语义事实校验。

    answer: 模型回答文本；context: 喂给生成模型的完整参考原文（context_block）。
    返回 list[{claim, verdict, reason}]（空表示无明显无支持/矛盾陈述）。
    任何异常均返回空（校验层永不阻断回答、永不抛错给主链路）。
    """
    if not answer or not context:
        return []
    try:
        from ai.llm_client import call_chat
        resp = call_chat([
            {"role": "system", "content": _SYSTEM + "\n\n=== 参考原文 ===\n" + context},
            {"role": "user", "content": "=== AI回答 ===\n" + answer + "\n\n请输出核查 JSON 数组："},
        ])
    except Exception as e:  # noqa: BLE001
        logger.warning("语义事实校验调用失败(放行): %s", e)
        return []

    txt = (resp or "").strip()
    if not txt:
        return []
    # 去 ```json 包裹
    if txt.startswith("```"):
        parts = txt.split("```")
        if len(parts) >= 2:
            txt = parts[1]
        if txt.lower().startswith("json"):
            txt = txt[4:]
        txt = txt.strip()
    try:
        data = json.loads(txt)
    except Exception:
        import re
        m = re.search(r"\[.*\]", txt, re.S)
        if not m:
            logger.warning("语义事实校验：模型未返回可解析 JSON(放行): %r", txt[:200])
            return []
        try:
            data = json.loads(m.group(0))
        except Exception as e:  # noqa: BLE001
            logger.warning("语义事实校验：JSON 解析失败(放行): %s", e)
            return []
    if not isinstance(data, list):
        return []
    out = []
    for it in data:
        if isinstance(it, dict) and it.get("claim"):
            out.append({
                "claim": str(it.get("claim"))[:200],
                "verdict": str(it.get("verdict") or "unsupported"),
                "reason": str(it.get("reason") or "")[:300],
            })
        if len(out) >= max_claims:
            break
    return out
