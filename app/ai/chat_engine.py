# -*- coding: utf-8 -*-
"""对话编排层（参照 AI 开发标准模板 4.1 的 <Feature>.js 等价物）。

职责划分：
  - 路由层（serve.py）只负责 HTTP 契约与鉴权（权限 / feature 开关 / 配置前置校验）。
  - 本层收敛全部业务逻辑：检索（查询改写 + 对比分解）→ 构造 prompt
    （角色 + 安全边界 + 带防注入标记的上下文）→ 调用 LLM → 引用软校验
    → 持久化 → 审计。

注意：权限 / feature / 配置的前置校验由 serve 完成；本层接收 perms 仅用于
「数据可见性」过滤（检索结果按分类 search 权限裁剪），不抛 403/503
（信任调用方已鉴权），保持本层独立可测。
"""
import logging

from ai.llm_client import (
    call_chat, rewrite_query, looks_like_comparison, decompose_question,
)
from ai.chat_prompt import build_system_prompt, PROMPT_VERSION
from ai.chat_context import build_context_block
from ai.chat_normalize import soft_check_references, extract_cited_docs, soft_check_facts
from ai.llm_config import ref_gate_enabled, fact_check_enabled

logger = logging.getLogger("kb.chat.engine")


def _scope_names(perms):
    """返回当前用户拥有 search 权限的顶层类型名（经反别名映射回文档类型名）。"""
    import admin
    cats = admin.list_categories(only_enabled=False)
    tops = [c for c in cats if c.get("parent_id") is None]
    scope = []
    for t in tops:
        subtree = [t["name"]] + (admin.get_category_descendants(t["name"]) or [])
        if any(admin.check_cat_action(perms, n, "search") for n in subtree):
            label = t["name"]
            for k, v in admin.TYPE_ALIASES.items():
                if v == label:
                    label = k
                    break
            scope.append(label)
    return scope


def _retrieve(question, perms, top_k, category):
    """检索并按对话分类权限（search）过滤；返回命中文档列表。

    category: 用户前端主动选定的检索域（顶层类型名）；提供则展开为白名单硬约束。
    """
    import admin
    import search as kb_search_mod
    cat_allow = None
    if category:
        if not admin.check_cat_action(perms, category, "search"):
            return []
        cat_allow = set([category] + admin.get_category_descendants(category))
    raw = kb_search_mod.hybrid_search(question, top_k=top_k * 4, categories=cat_allow)
    filtered = [r for r in raw if admin.check_cat_action(perms, r.get("category"), "search")]
    return filtered[:top_k]


def run(uid, question, perms, session_id=None, top_k=8, category=None):
    """执行一轮对话并返回结果 dict。

    入参：
      uid         用户 id（持久化 / 审计用）
      question    已 strip 的当前问题
      perms       当前用户权限集合（仅用于检索数据过滤）
      session_id  可选，None 则自动新建会话
      top_k       参考文档数量上限
      category    可选，前端选定的检索域（顶层类型名）
    返回：{session_id, answer, refs, scope, truncated, prompt_version}
    """
    import chat_store
    # 1) 会话：复用或新建
    sid = session_id
    if sid:
        if not chat_store.get_session(int(sid), uid):
            raise ValueError("会话不存在")
    else:
        sid = chat_store.create_session(uid, question[:40])

    # 2) 组装多轮历史（仅用户/助手文本，不含系统）
    history = []
    for m in chat_store.list_messages(sid):
        if m["role"] in ("user", "assistant"):
            history.append({"role": m["role"], "content": m["content"]})
    history.append({"role": "user", "content": question})

    # 3) 检索（按分类 search 权限过滤）
    #    3a) 查询改写：多轮时补全省略指代，否则拿半句话去检索搜不到。
    search_query = rewrite_query(question, history[:-1])
    #    3b) 对比类问题分解：拆成子问题分别检索后合并，保证对比双方资料齐全。
    if looks_like_comparison(search_query):
        sub_queries = decompose_question(search_query)
    else:
        sub_queries = [search_query]
    #    3c) 多子问题检索并按文档去重合并（保留各自最高分）
    hits = []
    seen_doc = set()
    for sq in sub_queries:
        for h in _retrieve(sq, perms, top_k, category):
            d = h.get("doc_id")
            if d in seen_doc:
                continue
            seen_doc.add(d)
            hits.append(h)
        if len(hits) >= top_k * 3:  # 合并池上限，避免上下文爆炸
            break
    hits = hits[:top_k * 2]

    # 4) 构造 prompt：角色 + 安全边界 + 范围边界 + 带防注入标记的上下文
    scope_names = _scope_names(perms)
    scope_desc = "、".join(scope_names) if scope_names else "全部知识库"
    context_block, truncated = build_context_block(hits)
    system = (build_system_prompt(scope_desc)
              + "\n\n下方为本次检索到的参考文档（已限定在你被授权的范围内）：\n"
              + context_block)

    # 5) 调 LLM（系统指令 + 历史 + 当前问题；history 末尾即当前问题）
    answer = call_chat([{"role": "system", "content": system}] + history)

    # 6) 构造 refs + 引用校验
    refs = [{
        "doc_id": h["doc_id"],
        "filename": h.get("filename"),
        "category": h.get("category"),
        "score": h.get("score"),
        "snippet": (h.get("snippet") or "")[:300],
        "content": h.get("content") or h.get("text") or "",
        "char_start": h.get("char_start"),
        "char_end": h.get("char_end"),
        "regions": h.get("regions", []),
    } for h in hits]
    cited = extract_cited_docs(answer)
    dropped = soft_check_references(answer, refs)

    # 7) 引用硬闸门（可选，默认关闭）：若回答引用了未检索到的文档（疑似编造），
    #    追加纠正指令重答一次，把编造引用压回已提供的参考文档；重答仍失败则保留原答案并告警。
    if dropped and ref_gate_enabled():
        logger.info("引用硬闸门触发：回答引用了未检索到的文档 %s，尝试重答一次", dropped)
        correct = (
            "\n\n【纠正要求】你上面的回答引用了以下不在参考文档中的资料：%s。"
            "这些资料并未提供给你，属于无依据的引用。请仅依据上方《KB_CONTEXT》"
            "中实际给出的参考文档作答，删除所有无法由这些文档佐证的内容；"
            "如确有不确定之处，明确说明『依据现有资料不足以得出确切结论』。"
            % ("、".join("《%s》" % d for d in dropped))
        )
        try:
            answer2 = call_chat([{"role": "system", "content": system}] + history
                                + [{"role": "assistant", "content": answer},
                                   {"role": "user", "content": correct}])
            cited2 = extract_cited_docs(answer2)
            dropped2 = soft_check_references(answer2, refs)
            if not dropped2:  # 重答后无编造引用，采纳
                answer, cited, dropped = answer2, cited2, dropped2
                logger.info("引用硬闸门：重答成功，编造引用已消除")
            else:
                logger.warning("引用硬闸门：重答后仍有编造引用 %s，保留原答案并告警", dropped2)
        except Exception as e:  # noqa: BLE001
            logger.warning("引用硬闸门：重答失败（%s），保留原答案并告警", e)

    # 6b) 事实数值软校验（可选，默认关闭）：检测回答关键数值是否能在参考原文中找到，
    #     仅作提示（fact_warn），不阻断、不重答。
    fact_warn = soft_check_facts(answer, refs) if fact_check_enabled() else []

    # 8) 持久化消息
    chat_store.add_message(sid, "user", question)
    chat_store.add_message(sid, "assistant", answer, refs, dropped_refs=dropped,
                           fact_warnings=fact_warn)

    # 9) 审计（带提示词版本，便于追溯「为什么这次结论与上次不同」）
    import admin
    import kb_store
    try:
        _row = admin._conn().execute(
            "SELECT username FROM users WHERE id=?", (uid,)).fetchone()
        _uname = _row["username"] if _row else ""
    except Exception:  # noqa: BLE001
        _uname = ""
    kb_store.audit_log(
        "kb.chat", target="session:%d" % sid,
        detail="%s|prompt=%s|cited=%s|dropped=%s|fact=%s"
               % (question, PROMPT_VERSION, cited, dropped, fact_warn),
        user_id=uid, username=_uname)

    return {
        "session_id": sid,
        "answer": answer,
        "refs": refs,
        "scope": scope_names,
        "truncated": truncated,
        "prompt_version": PROMPT_VERSION,
        "cited_refs": cited,
        "dropped_refs": dropped,
        "fact_warnings": fact_warn,
    }
