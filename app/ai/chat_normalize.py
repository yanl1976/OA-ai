# -*- coding: utf-8 -*-
"""对话引用软校验层（参照 AI 开发标准模板 2.4(c) 引用纪律 + 4.4 校验原则）。

职责：
  - 解析模型回答中声称引用的文档（《XXX》），与本轮实际检索到的 refs 比对。
  - 若回答引用了「未检索到的文档」，记为 dropped 并打 logger.warning。
  - 关键：软校验、不阻断 —— 回答原样返回，符合「校验层永不抛错」原则。

为何只软校验不阻断：知识库问答中，模型偶尔会泛化引用（如引用某上位制度），
强制重答会拖慢且未必更准确；先以日志暴露，便于后续评估是否需升级为硬约束。
"""
import re
import logging

logger = logging.getLogger("kb.chat.normalize")

# 匹配《文档名》，限制长度避免误吞正文
_DOC_REF_RE = re.compile(r"《([^》\n]{1,60})》")

# 文档名归一化：用于「引用名 ⇄ 检索文件名」的宽松匹配，消除别名/简称/版本号误报。
# 仅做安全变换：去扩展名、去括号及内部版本年份、去空白、转小写。
# 不剥离「标准/规定/制度」等中间词，避免把「管理标准」与「管理」错误合并。
_EXT_RE = re.compile(r"\.[a-z0-9]+$", re.I)
_PAREN_RE = re.compile(r"[（(][^（）()]*[)）]")
_DOC_TYPE_SUFFIX = (  # 仅去「末尾」冗余类型词，长词优先（避免「管理办法」被「办法」提前截断）
    "管理办法", "管理规定", "实施细则", "暂行办法", "试行办法",
    "实施方案", "指导意见", "管理制度", "工作规则",
    "制度", "规定", "标准", "办法", "细则", "条例", "指引",
    "规则", "通知", "意见", "方案", "管理",
)


def _norm_doc_name(name):
    """把文档名归一化为可比对的短键。

    例：『员工手册（2024版）.pdf』→『员工手册』；『病假与医疗期管理规定』→『病假与医疗期管理』。
    返回空串表示无法归一（如仅剩类型词），调用方应跳过而非误报。
    """
    if not name:
        return ""
    n = name.strip().lower()
    n = _EXT_RE.sub("", n)
    n = _PAREN_RE.sub("", n)
    n = re.sub(r"\s+", "", n)
    for s in _DOC_TYPE_SUFFIX:
        if n.endswith(s) and len(n) > len(s):
            n = n[: -len(s)]
            break
    return n


# 事实数值校验：提取回答中「带单位的关键数值短语」。
# 天数/月数/年、金额、百分比。仅用于「软提示」，不阻断、不重答。
_FACT_PATTERNS = [
    re.compile(r"\d+(?:\.\d+)?\s*(?:天|日|个月|月|年|周岁)"),
    re.compile(r"[¥￥]?\s*\d+(?:\.\d+)?\s*(?:元|万元|千元|万|千)"),
    re.compile(r"\d+(?:\.\d+)?\s*%"),
]
# 软化词：出现在数值短语语境中时，视为推断/估算，不算「硬性条款值」，不告警。
_SOFTEN = ("约", "大概", "一般", "通常", "可能", "建议", "推断", "左右",
           "以上", "以下", "不超过", "至少", "至多", "大约", "估计", "约")


def extract_cited_docs(answer):
    """从回答文本中提取所有《文档名》引用（去重，保序）。"""
    if not answer:
        return []
    seen = []
    for n in _DOC_REF_RE.findall(answer):
        if n not in seen:
            seen.append(n)
    return seen


def soft_check_references(answer, refs):
    """检查回答中《文档名》引用是否都在 refs 内。

    answer: 模型回答文本；refs: [{filename, ...}, ...]
    返回 dropped: 声称引用但不在 refs 的文档名列表（空表示无明显编造引用）。
    仅日志告警，不修改 answer、不抛异常。
    """
    if not answer or not refs:
        return []
    ref_names = set()
    for r in refs:
        fn = (r.get("filename") or "").strip()
        if fn:
            ref_names.add(fn)
    mentioned = set(extract_cited_docs(answer))
    if not mentioned:
        return []

    norm_refs = {_norm_doc_name(rn) for rn in ref_names}
    norm_refs.discard("")  # 空键会令 `nr in n` 恒真，必须剔除
    # 参考正文拼接：用于豁免「文档正文中提及的材料名/表单名/章节术语」，
    # 避免把《诊断证明书》这类「病假需提供的材料」误判为「编造的文档引用」。
    ref_text = "\n".join((r.get("content") or "") for r in refs)
    dropped = []
    for name in mentioned:
        n = _norm_doc_name(name)
        if not n:  # 归一后为空（如仅「办法」），无法判定，跳过而非误报
            continue
        # 宽松匹配：归一后双向包含（应对简称/全称/版本号/扩展名差异）
        hit = any(n in nr or nr in n for nr in norm_refs)
        if not hit:
            # 豁免：被引用名出现在任一参考正文里（材料/表单/术语，非编造文档）
            if name in ref_text or n in ref_text:
                continue
            dropped.append(name)
    if dropped:
        logger.warning(
            "对话回答疑似引用了未检索到的文档（软校验，未阻断）: %s；"
            "本轮 refs=%s", dropped, sorted(ref_names))
    return dropped


def soft_check_facts(answer, refs):
    """保守的事实数值软校验：检测回答中的关键数值（天数/金额/百分比）是否能在参考原文中找到。

    answer: 模型回答文本；refs: [{content, ...}, ...]（content 为检索命中的最相关 chunk 拼接）。
    返回 fact_warnings: 未在任一参考原文中直接出现的「带单位数值短语」列表（空表示无明显事实漏洞）。
    仅软提示、不修改 answer、不抛异常、不重答（因易误报，默认关闭，见 fact_check_enabled）。

    设计取舍（为何保守）：
      - 仅在「带单位的关键数值短语」层面比对，并要求数值出现在参考原文；找不到才告警。
        可兜住「把医疗期 3 个月说成 6 个月」这类明显条款数字错，与引用名校验互补；
        但无法覆盖纯文字性曲解。
      - 软化语境（约/一般/推断）下的数值不告警，避免把合理估算误伤。
      - refs 的 content 仅为相关 chunk，故『找不到』不等于『一定错』，仅作提示；
        误报风险由 fact_check_enabled 默认关闭兜底。
    """
    if not answer or not refs:
        return []
    ref_text = "\n".join((r.get("content") or "") for r in refs)
    warns = []
    for pat in _FACT_PATTERNS:
        for m in pat.finditer(answer):
            phrase = m.group().strip()
            nums = re.findall(r"\d+(?:\.\d+)?", phrase)
            if not nums:
                continue
            # 任一数字出现在参考原文即可视为有据；全都不在才告警
            if any(num in ref_text for num in nums):
                continue
            # 软化语境（约/一般/推断等）不告警
            ctx = answer[max(0, m.start() - 6): m.end() + 2]
            if any(w in ctx for w in _SOFTEN):
                continue
            if phrase not in warns:
                warns.append(phrase)
    return warns
