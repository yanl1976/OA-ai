#!/usr/bin/env python3
"""对话式智能问答的持久化与权限边界层。

职责：
  1. 定义「对话域 → 权限」映射（CHAT_DOMAINS），实现按账号权限限定可对话的文档范围。
  2. 会话/消息的 SQLite 持久化（复用 admin 管理库），支持长期保存与自定义删除。
  3. 提供按当前用户权限过滤出的「可对话分类集合」，供检索与 prompt 边界使用。
"""
import sqlite3

try:
    from .kb_store import category_subtree_names, category_id_by_name
except ImportError:  # 脚本直接运行回退
    from kb_store import category_subtree_names, category_id_by_name


# ============ 对话域（核心：按权限限定可对话范围） ============
# 每个域：name 展示名；permission 所需权限 key；roots 顶层分类名（含其全部后代子分类）。
# 用户提问时，只会检索其拥有权限的域所覆盖的分类；prompt 也会声明边界。
CHAT_DOMAINS = [
    {
        "key": "std",
        "name": "管理标准",
        "permission": "kb.chat.std",
        "roots": ["管理标准分类"],
    },
    {
        "key": "meeting",
        "name": "会议纪要",
        "permission": "kb.chat.meeting",
        "roots": ["会议纪要分类"],
    },
    {
        "key": "all",
        "name": "全部知识库",
        "permission": "kb.chat.all",
        "roots": None,  # None 表示不限分类（全库）
    },
]


def domains_for_permissions(permissions: list) -> list:
    """返回某账号有权限的对话域列表（按 CHAT_DOMAINS 顺序，all 在后）。"""
    perms = set(permissions or [])
    return [d for d in CHAT_DOMAINS if d["permission"] in perms]


def allowed_categories(permissions: list) -> dict:
    """返回当前账号可对话的范围信息。

    返回 dict：
      domains   : 有权限的域列表（展示用）
      categories: 允许检索的分类名集合（set）；空集合表示无权对话；None 表示全库（all 域）。
    """
    domains = domains_for_permissions(permissions)
    if not domains:
        return {"domains": [], "categories": set()}
    # 若拥有 all 域，则不限分类
    if any(d["key"] == "all" for d in domains):
        return {"domains": domains, "categories": None}
    cats = set()
    for d in domains:
        for root in (d["roots"] or []):
            # 取整棵子树（含根及其全部后代），用分类名匹配
            cats.update(category_subtree_names(root))
    return {"domains": domains, "categories": cats}


# ============ 持久化（复用 admin 管理库） ============
def _conn():
    import admin
    conn = admin._conn()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS chat_sessions (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               user_id INTEGER,
               title TEXT DEFAULT '新对话',
               created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now','localtime')),
               updated_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now','localtime'))
           )""")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS chat_messages (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               session_id INTEGER,
               role TEXT,            -- 'user' / 'assistant'
               content TEXT,
               refs TEXT,            -- JSON: [{doc_id,filename,score,snippet}]
               created_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now','localtime'))
           )""")
    return conn


def create_session(user_id: int, title: str = "新对话") -> int:
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO chat_sessions (user_id, title) VALUES (?,?)",
        (user_id, title))
    sid = cur.lastrowid
    conn.commit()
    conn.close()
    return sid


def rename_session(session_id: int, user_id: int, title: str):
    conn = _conn()
    conn.execute(
        "UPDATE chat_sessions SET title=?, updated_at=strftime('%Y-%m-%d %H:%M:%S','now','localtime') "
        "WHERE id=? AND user_id=?", (title, session_id, user_id))
    conn.commit()
    conn.close()


def list_sessions(user_id: int) -> list:
    conn = _conn()
    rows = conn.execute(
        "SELECT id, title, created_at, updated_at, "
        "(SELECT COUNT(*) FROM chat_messages m WHERE m.session_id=s.id) AS msg_count "
        "FROM chat_sessions s WHERE user_id=? ORDER BY updated_at DESC",
        (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_session(session_id: int, user_id: int) -> dict or None:
    conn = _conn()
    row = conn.execute(
        "SELECT id, title, user_id FROM chat_sessions WHERE id=? AND user_id=?",
        (session_id, user_id)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_session(session_id: int, user_id: int):
    """自定义删除：删除会话及其全部消息（用户手动清理）。"""
    conn = _conn()
    conn.execute("DELETE FROM chat_messages WHERE session_id=?", (session_id,))
    conn.execute(
        "DELETE FROM chat_sessions WHERE id=? AND user_id=?", (session_id, user_id))
    conn.commit()
    conn.close()


def list_messages(session_id: int) -> list:
    conn = _conn()
    rows = conn.execute(
        "SELECT id, role, content, refs, created_at FROM chat_messages "
        "WHERE session_id=? ORDER BY id ASC", (session_id,)).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["refs"] = __import__("json").loads(d["refs"] or "[]")
        except Exception:
            d["refs"] = []
        out.append(d)
    return out


def add_message(session_id: int, role: str, content: str, refs: list = None):
    import json as _json
    conn = _conn()
    conn.execute(
        "INSERT INTO chat_messages (session_id, role, content, refs) VALUES (?,?,?,?)",
        (session_id, role, content, _json.dumps(refs or [], ensure_ascii=False)))
    conn.execute(
        "UPDATE chat_sessions SET updated_at=strftime('%Y-%m-%d %H:%M:%S','now','localtime') "
        "WHERE id=?", (session_id,))
    conn.commit()
    conn.close()
