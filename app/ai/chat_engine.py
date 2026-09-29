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
from ai.chat_normalize import soft_check_references

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

    # 6) 持久化消息 + 构造 refs
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
    chat_store.add_message(sid, "user", question)
    chat_store.add_message(sid, "assistant", answer, refs)

    # 7) 引用软校验（不阻断，仅日志告警）
    soft_check_references(answer, refs)

    # 8) 审计（带提示词版本，便于追溯「为什么这次结论与上次不同」）
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
        detail="%s|prompt=%s" % (question, PROMPT_VERSION),
        user_id=uid, username=_uname)

    return {
        "session_id": sid,
        "answer": answer,
        "refs": refs,
        "scope": scope_names,
        "truncated": truncated,
        "prompt_version": PROMPT_VERSION,
    }
