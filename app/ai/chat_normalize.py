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

    dropped = []
    for name in mentioned:
        # 宽松匹配：refs 文件名包含 name 或 name 包含 refs 文件名（应对简称/全称）
        hit = any((name in rn) or (rn in name) for rn in ref_names)
        if not hit:
            dropped.append(name)
    if dropped:
        logger.warning(
            "对话回答疑似引用了未检索到的文档（软校验，未阻断）: %s；"
            "本轮 refs=%s", dropped, sorted(ref_names))
    return dropped
