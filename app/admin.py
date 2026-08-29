"""
管理后台数据层 + 鉴权 + RBAC

职责:
  - 初始化 SQLite 管理库 (data/kb_admin.db)
  - 用户 / 角色 / 权限 的增删改查
  - 分类管理 (管理库中为权威分类树)
  - 功能开关 (对“现有功能”的管理)
  - PBKDF2 密码哈希 + 会话鉴权助手

权限目录 (PERMISSION_CATALOG) 为固定集合，角色从中分配。
"""
import os
import sqlite3
import hashlib
import secrets
import json
from datetime import datetime, timezone

# ============ 路径 ============
KB_ROOT = os.environ.get("KB_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(KB_ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "kb_admin.db")

# ============ 权限目录 (固定) ============
# 每个权限项: (key, name, description, group)
#   group 用于在权限目录/角色管理界面中分组展示，并给出中文说明。
# 每项: (key, 名称, 描述, 分组)
# 分类维度的「浏览/查询/下载」权限是动态的（按分类树节点 × 操作生成，
# 见 sync_category_permissions，key 形如 kb.cat.<分类id>.view/search/download），
# 不在此静态列出。文档「类型名 → 分类树根节点」映射见 TYPE_ALIASES。
PERMISSION_CATALOG = [
    ("graph.view",       "知识图谱",     "查看 3D 神经网络知识图谱", "知识库"),
    ("kb.doc.upload",    "文档上传",     "上传新文档并重建检索索引", "知识库"),
    ("kb.doc.delete",    "文档删除",     "删除/隐藏文档（含上传文档）", "知识库"),
    ("kb.upload.manage", "上传文件管理", "查看已上传文件列表，删除或调整其归类", "知识库"),
    ("kb.category.manage","分类管理",    "新增/编辑/启停/移动分类节点", "知识库"),
    ("derived.manage",   "会议纪要二次生成", "截取会议纪要、管理衍生版本（需求/去向）与 PDF", "会议纪要"),
    ("user.view",        "查看用户",     "查看用户列表与基本信息", "用户与权限"),
    ("user.manage",      "用户管理",     "新增/编辑/禁用/删除用户", "用户与权限"),
    ("role.manage",      "角色管理",     "新增/编辑/分配角色权限", "用户与权限"),
    ("permission.view",  "查看权限",     "查看系统权限目录与说明", "用户与权限"),
    ("system.manage",    "系统管理",     "功能开关、索引重建与系统维护", "系统"),
]

# 分类权限的三个操作维度
CAT_ACTIONS = ("view", "search", "download")
CAT_ACTION_LABELS = {"view": "浏览", "search": "查询", "download": "下载"}

# 文档「类型名 → 分类树根节点名」映射。
# 文档的 category 字段实际存的是类型名（如「管理标准」「会议纪要」），
# 而分类树顶层节点名为「管理标准分类」「会议纪要」，二者通过此表对齐。
# 新增文档类型时：在分类树加对应顶层节点，并在此登记一行即可；
# 若文档 category 已直接等于分类树某节点名（含子类），则无需映射（原样命中）。
TYPE_ALIASES = {
    "管理标准": "管理标准分类",
    "会议纪要": "会议纪要",
}

# 新增权限 → 关联权限：存量库中，凡是已拥有「关联权限」之一的角色，自动授予新权限。
NEW_PERM_RELATED = {
    "kb.upload.manage": ["kb.doc.delete", "kb.doc.upload"],
}

# ============ 默认内置角色 ============
# 分类维度的浏览/查询权限是动态的，由 sync_category_permissions 在分类树建好后
# 自动授予 admin（全量）以及全新库下的 editor/viewer（全量 view+search）。
# 这里仅声明非分类维度的静态权限；分类权限在 _seed 阶段叠加。
DEFAULT_ROLES = {
    "admin":  {"desc": "超级管理员，拥有全部权限", "perms": [p[0] for p in PERMISSION_CATALOG]},
    "editor": {"desc": "内容编辑，可管理分类、上传文档与会议纪要二次生成", "perms": [
        "kb.category.manage", "kb.doc.upload",
        "graph.view", "derived.manage"]},
    "viewer": {"desc": "只读访客，仅可浏览与检索", "perms": [
        "graph.view"]},
    # 示例角色（非内置，可删改）：仅声明非分类维度权限骨架；
    # 其分类维度的检索/浏览/下载范围需管理员在「角色管理」中按顶层分类勾选
    # （勾顶层即级联继承其全部子类，无需逐子类设置）。
    "领导":   {"desc": "公司领导：可在授权分类域内检索/对话（分类域由管理员勾选）", "builtin": 0, "perms": [
        "graph.view"]},
    "秘书":   {"desc": "办公室秘书：通常仅授权「会议纪要」域的检索/对话/二次生成", "perms": [
        "kb.doc.upload", "derived.manage", "graph.view"], "builtin": 0},
}

# 默认管理员
DEFAULT_ADMIN_USER = "admin"
DEFAULT_ADMIN_PASS = "Admin@123"

# 功能开关默认项 (对“现有功能”的管理)
DEFAULT_FEATURES = [
    ("graph_enabled",   "3D 知识图谱",   "开启后用户可访问 3D 神经网络知识图谱", 1),
    ("search_enabled",  "检索接口",      "开启 BM25 检索与检索 API",              1),
    ("register_enabled","自助注册",      "允许访客自行注册为只读账号",             0),
    ("upload_enabled",  "文档上传",      "允许用户上传文档扩充知识库",             1),
    ("api_public",      "开放检索 API",  "无需登录即可调用 /api/query",          0),
]


# ============ 密码哈希 (PBKDF2-HMAC-SHA256) ============
def hash_password(password: str, salt: bytes = None) -> tuple:
    if salt is None:
        salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return salt.hex(), dk.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    salt = bytes.fromhex(salt_hex)
    _, dk = hash_password(password, salt)
    return secrets.compare_digest(dk, hash_hex)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _conn() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    # 并发写时避免「database is locked」：设置等待超时并启用 WAL
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(seed: bool = True):
    """创建表结构并注入种子数据（仅首次）。"""
    conn = _conn()
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS permissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        `key` TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        description TEXT,
        `group` TEXT DEFAULT '其他'
    );
    CREATE TABLE IF NOT EXISTS roles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        description TEXT,
        builtin INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS role_permissions (
        role_id INTEGER NOT NULL,
        permission_id INTEGER NOT NULL,
        PRIMARY KEY (role_id, permission_id),
        FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE CASCADE,
        FOREIGN KEY (permission_id) REFERENCES permissions(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        display_name TEXT,
        password_salt TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        role_id INTEGER,
        status INTEGER DEFAULT 1,
        created_at TEXT,
        last_login TEXT,
        FOREIGN KEY (role_id) REFERENCES roles(id)
    );
    CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT,
        sort_order INTEGER DEFAULT 0,
        status INTEGER DEFAULT 1,
        builtin INTEGER DEFAULT 0,
        parent_id INTEGER DEFAULT NULL,
        FOREIGN KEY (parent_id) REFERENCES categories(id) ON DELETE SET NULL
    );
    CREATE TABLE IF NOT EXISTS feature_flags (
        `key` TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT,
        enabled INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS settings (
        `key` TEXT PRIMARY KEY,
        value TEXT
    );
    """)
    # 迁移：为已存在的库补充 parent_id 列（支持分类层级）
    cur.execute("PRAGMA table_info(categories)")
    cols = [r[1] for r in cur.fetchall()]
    if "parent_id" not in cols:
        cur.execute("ALTER TABLE categories ADD COLUMN parent_id INTEGER DEFAULT NULL")
    # 迁移：为已存在的库补充 permissions.group 列
    cur.execute("PRAGMA table_info(permissions)")
    pcols = [r[1] for r in cur.fetchall()]
    if "group" not in pcols:
        cur.execute("ALTER TABLE permissions ADD COLUMN `group` TEXT DEFAULT '其他'")
    # 迁移：将 categories.name 的「全局唯一」改为「(name, parent_id) 同级唯一」。
    # 旧库由 UNIQUE 约束自动生成的索引无法直接 DROP，因此通过重建表移除约束，
    # 再建立复合唯一索引；COALESCE(parent_id,-1) 使顶级(NULL)同名也能正确冲突。
    try:
        cur.execute("PRAGMA index_list(categories)")
        need_rebuild = False
        for idx in cur.fetchall():
            iname, iunique, iorigin = idx[1], idx[2], idx[3]
            if iunique and iorigin == "u":
                cur.execute("PRAGMA index_info(%s)" % iname)
                cols = [r[2] for r in cur.fetchall()]
                if cols == ["name"]:
                    need_rebuild = True
                    break
        if need_rebuild:
            cur.execute("PRAGMA foreign_keys=OFF")
            cur.execute("ALTER TABLE categories RENAME TO _categories_old")
            cur.execute(
                "CREATE TABLE categories ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " name TEXT NOT NULL,"
                " description TEXT,"
                " sort_order INTEGER DEFAULT 0,"
                " status INTEGER DEFAULT 1,"
                " builtin INTEGER DEFAULT 0,"
                " parent_id INTEGER DEFAULT NULL,"
                " FOREIGN KEY (parent_id) REFERENCES categories(id) ON DELETE SET NULL)")
            cur.execute(
                "INSERT INTO categories(id,name,description,sort_order,status,builtin,parent_id) "
                " SELECT id,name,description,sort_order,status,builtin,parent_id FROM _categories_old")
            cur.execute("DROP TABLE _categories_old")
            cur.execute("PRAGMA foreign_keys=ON")
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_categories_name_parent "
            "ON categories(name, COALESCE(parent_id, -1))")
    except Exception:  # noqa: BLE001
        pass
    if seed:
        _seed(conn, cur)
    conn.commit()
    conn.close()


def _seed(conn, cur):
    # 权限目录（含分组）
    for key, name, desc, group in PERMISSION_CATALOG:
        cur.execute(
            "INSERT OR IGNORE INTO permissions(`key`, name, description, `group`) "
            "VALUES (?,?,?,?)", (key, name, desc, group))
        # 已存在的权限补写 group/description（升级场景）
        cur.execute("UPDATE permissions SET `group`=?, description=? WHERE `key`=?",
                    (group, desc, key))
    perm_ids = {r["key"]: r["id"] for r in cur.execute("SELECT id,`key` FROM permissions").fetchall()}
    # 存量库自动关联新权限到已拥有相关权限的角色
    _wire_new_permissions(conn, cur, perm_ids)
    # 建立分类树（幂等）→ 同步分类维度的浏览/查询/下载权限，并迁移旧权限、授权默认角色
    ensure_category_hierarchy(conn=conn, cur=cur)
    sync_category_permissions(conn=conn, cur=cur)
    # 重新读取权限 id 映射（已含动态分类权限）
    perm_ids = {r["key"]: r["id"] for r in cur.execute("SELECT id,`key` FROM permissions").fetchall()}
    # 角色（builtin 标志控制是否允许删除；示例角色 builtin=0 可删改）
    for rname, meta in DEFAULT_ROLES.items():
        is_builtin = int(meta.get("builtin", 1))
        cur.execute("INSERT OR IGNORE INTO roles(name, description, builtin) VALUES (?,?,?)",
                    (rname, meta["desc"], is_builtin))
        rid = cur.execute("SELECT id FROM roles WHERE name=?", (rname,)).fetchone()["id"]
        for pk in meta["perms"]:
            if pk in perm_ids:
                cur.execute("INSERT OR IGNORE INTO role_permissions(role_id, permission_id) VALUES (?,?)",
                            (rid, perm_ids[pk]))
    # 默认管理员
    admin_role = cur.execute("SELECT id FROM roles WHERE name='admin'").fetchone()["id"]
    if not cur.execute("SELECT 1 FROM users WHERE username=?", (DEFAULT_ADMIN_USER,)).fetchone():
        salt, h = hash_password(DEFAULT_ADMIN_PASS)
        cur.execute(
            "INSERT INTO users(username, display_name, password_salt, password_hash, role_id, status, created_at) "
            "VALUES (?,?,?,?,?,1,?)",
            (DEFAULT_ADMIN_USER, "系统管理员", salt, h, admin_role, _now()))
    # 功能开关
    for key, name, desc, en in DEFAULT_FEATURES:
        cur.execute("INSERT OR IGNORE INTO feature_flags(`key`, name, description, enabled) VALUES (?,?,?,?)",
                    (key, name, desc, en))


# ============ 权限 / 角色 查询 ============
def get_user_permissions(user_id: int) -> set:
    conn = _conn()
    rows = conn.execute(
        "SELECT p.`key` FROM permissions p "
        "JOIN role_permissions rp ON rp.permission_id=p.id "
        "JOIN users u ON u.role_id=rp.role_id WHERE u.id=?",
        (user_id,)).fetchall()
    conn.close()
    return {r["key"] for r in rows}


def get_permission_catalog() -> list:
    conn = _conn()
    rows = conn.execute(
        "SELECT `key`, name, description, `group` FROM permissions ORDER BY id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _wire_new_permissions(conn, cur, perm_ids: dict):
    """存量库升级时，把新增权限自动授予已拥有相关权限的角色（幂等）。"""
    for new_key, related in NEW_PERM_RELATED.items():
        if new_key not in perm_ids:
            continue
        new_pid = perm_ids[new_key]
        rel_ids = [perm_ids[r] for r in related if r in perm_ids]
        if not rel_ids:
            continue
        placeholders = ",".join("?" * len(rel_ids))
        role_ids = [r["role_id"] for r in conn.execute(
            "SELECT DISTINCT role_id FROM role_permissions WHERE permission_id IN (%s)" % placeholders,
            rel_ids).fetchall()]
        for rid in role_ids:
            cur.execute("INSERT OR IGNORE INTO role_permissions(role_id, permission_id) VALUES (?,?)",
                        (rid, new_pid))


def list_roles(with_perms: bool = True) -> list:
    conn = _conn()
    roles = [dict(r) for r in conn.execute(
        "SELECT id, name, description, builtin FROM roles ORDER BY id").fetchall()]
    if with_perms:
        for role in roles:
            pids = [r["permission_id"] for r in conn.execute(
                "SELECT permission_id FROM role_permissions WHERE role_id=?", (role["id"],)).fetchall()]
            keys = [r["key"] for r in conn.execute(
                "SELECT `key` FROM permissions WHERE id IN (%s)" % (",".join("?" * len(pids)) or "0"),
                pids).fetchall()] if pids else []
            role["permissions"] = keys
    conn.close()
    return roles


def create_role(name: str, description: str, perms: list) -> int:
    conn = _conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO roles(name, description, builtin) VALUES (?,?,0)", (name, description))
    rid = cur.lastrowid
    perm_ids = {r["key"]: r["id"] for r in conn.execute("SELECT id,`key` FROM permissions").fetchall()}
    for pk in perms:
        if pk in perm_ids:
            cur.execute("INSERT OR IGNORE INTO role_permissions(role_id, permission_id) VALUES (?,?)",
                        (rid, perm_ids[pk]))
    conn.commit()
    conn.close()
    return rid


def update_role(role_id: int, description: str = None, perms: list = None, name: str = None):
    """更新角色（名称/描述/权限）。

    【入参防御·修复】调用方曾按位置传参导致 name 收到 list、perms 收到 str，
    执行 SQL 时报「type 'list' is not supported」，且侥幸不报错时会把角色
    名/描述/权限互相写错。这里对类型做强校验：
      - name / description 必须是字符串（空串合法，None 表示不改）
      - perms 必须是 list/tuple，且元素为字符串；None 表示不改
    类型不符直接抛 ValueError，由路由返回 400，避免脏数据入库。
    """
    if name is not None:
        if isinstance(name, (list, tuple, dict)):
            raise ValueError("角色名称必须是字符串，收到 %s" % type(name).__name__)
        name = str(name).strip()
        if not name:
            raise ValueError("角色名称不能为空")
    if description is not None:
        if isinstance(description, (list, tuple, dict)):
            raise ValueError("角色描述必须是字符串，收到 %s" % type(description).__name__)
        description = str(description)
    if perms is not None:
        if isinstance(perms, str):
            raise ValueError("权限列表必须是数组，收到字符串")
        if not isinstance(perms, (list, tuple)):
            raise ValueError("权限列表必须是数组，收到 %s" % type(perms).__name__)
        perms = [str(p) for p in perms if isinstance(p, (str, int))]

    conn = _conn()
    cur = conn.cursor()
    if name is not None:
        cur.execute("UPDATE roles SET name=? WHERE id=?", (name, role_id))
    if description is not None:
        cur.execute("UPDATE roles SET description=? WHERE id=?", (description, role_id))
    if perms is not None:
        perm_ids = {r["key"]: r["id"] for r in conn.execute("SELECT id,`key` FROM permissions").fetchall()}
        cur.execute("DELETE FROM role_permissions WHERE role_id=?", (role_id,))
        for pk in perms:
            if pk in perm_ids:
                cur.execute("INSERT OR IGNORE INTO role_permissions(role_id, permission_id) VALUES (?,?)",
                            (role_id, perm_ids[pk]))
    conn.commit()
    conn.close()


def delete_role(role_id: int):
    conn = _conn()
    role = conn.execute("SELECT builtin FROM roles WHERE id=?", (role_id,)).fetchone()
    if role and role["builtin"]:
        conn.close()
        raise ValueError("内置角色不可删除")
    # 解除用户绑定
    conn.execute("UPDATE users SET role_id=NULL WHERE role_id=?", (role_id,))
    conn.execute("DELETE FROM roles WHERE id=?", (role_id,))
    conn.commit()
    conn.close()


# ============ 用户 ============
def authenticate(username: str, password: str) -> dict or None:
    conn = _conn()
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    conn.close()
    if not row:
        return None
    if row["status"] != 1:
        return None
    if not verify_password(password, row["password_salt"], row["password_hash"]):
        return None
    return dict(row)


def update_last_login(user_id: int):
    conn = _conn()
    conn.execute("UPDATE users SET last_login=? WHERE id=?", (_now(), user_id))
    conn.commit()
    conn.close()


def list_users() -> list:
    conn = _conn()
    rows = conn.execute(
        "SELECT u.id, u.username, u.display_name, u.role_id, u.status, u.created_at, u.last_login, "
        "r.name AS role_name FROM users u LEFT JOIN roles r ON u.role_id=r.id ORDER BY u.id").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_user(username: str, password: str, display_name: str = "", role_id: int = None) -> int:
    """创建用户。

    【防呆】签名中 display_name 位于 role_id 之前，按位置传易把角色 id 当成
    显示名（如 create_user(u, p, rid)），结果 display_name 变成数字、role_id 为
    None —— 用户被创建但【没有任何角色】，权限全空，且不报错、极难排查。
    这里做类型校验：display_name 必须是字符串，role_id 必须是整数。
    """
    if display_name is not None and not isinstance(display_name, str):
        # 常见误用：把 role_id 按位置传给了 display_name
        if isinstance(display_name, int) and role_id is None:
            role_id = display_name     # 纠正：挪到 role_id
            display_name = ""
        else:
            raise ValueError("显示名必须是字符串，收到 %s" % type(display_name).__name__)
    if role_id is not None and not isinstance(role_id, int):
        raise ValueError("角色 id 必须是整数，收到 %s" % type(role_id).__name__)
    conn = _conn()
    if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        conn.close()
        raise ValueError("用户名已存在")
    salt, h = hash_password(password)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO users(username, display_name, password_salt, password_hash, role_id, status, created_at) "
        "VALUES (?,?,?,?,?,1,?)",
        (username, display_name or username, salt, h, role_id, _now()))
    uid = cur.lastrowid
    conn.commit()
    conn.close()
    return uid


def update_user(user_id: int, display_name: str = None, role_id: int = None,
                status: int = None, password: str = None):
    conn = _conn()
    cur = conn.cursor()
    if display_name is not None:
        cur.execute("UPDATE users SET display_name=? WHERE id=?", (display_name, user_id))
    if role_id is not None:
        cur.execute("UPDATE users SET role_id=? WHERE id=?", (role_id, user_id))
    if status is not None:
        cur.execute("UPDATE users SET status=? WHERE id=?", (status, user_id))
    if password:
        salt, h = hash_password(password)
        cur.execute("UPDATE users SET password_salt=?, password_hash=? WHERE id=?",
                    (salt, h, user_id))
    conn.commit()
    conn.close()


def delete_user(user_id: int):
    conn = _conn()
    user = conn.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
    if user and user["username"] == DEFAULT_ADMIN_USER:
        conn.close()
        raise ValueError("默认管理员不可删除")
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    conn.close()


# ============ 分类 ============
def seed_categories(names: list):
    """从 raw_data 注入初始分类（仅新增，不覆盖）。"""
    conn = _conn()
    cur = conn.cursor()
    for i, name in enumerate(names):
        cur.execute("INSERT OR IGNORE INTO categories(name, sort_order, builtin) VALUES (?,?,1)",
                    (name, i))
    conn.commit()
    conn.close()


def list_categories(only_enabled: bool = False) -> list:
    conn = _conn()
    sql = "SELECT id, name, description, sort_order, status, builtin, parent_id FROM categories"
    if only_enabled:
        sql += " WHERE status=1"
    sql += " ORDER BY sort_order, id"
    rows = conn.execute(sql).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def ensure_category_hierarchy(conn=None, cur=None):
    """建立一级分类『管理标准分类』，并将尚无上级的内置分类挂到其下（幂等）。

    同时保证顶层『会议纪要』分类存在（独立于管理标准分类，便于按年代归档）。
    支持传入已有的 (conn, cur) 以融入外部事务（如 _seed）。
    """
    own = conn is None
    if own:
        conn = _conn()
        cur = conn.cursor()
    row = cur.execute(
        "SELECT id FROM categories WHERE name=? AND parent_id IS NULL",
        ("管理标准分类",)).fetchone()
    if not row:
        cur.execute(
            "INSERT INTO categories(name, description, sort_order, status, builtin, parent_id) "
            "VALUES (?,?,?,1,1,NULL)",
            ("管理标准分类", "公司管理标准一级分类，归集全部公司管理标准文档", 0))
        top_id = cur.lastrowid
    else:
        top_id = row["id"]
    # 清理冗余：原始库 ingestion 可能已把同名一级分类建在「管理标准分类」下，
    # 此处先把『顶层 builtin 中、名字已存在于管理标准分类下』的重复项删除，避免挂接时
    # 触发 (name, parent_id) 复合唯一约束冲突。
    cur.execute(
        "DELETE FROM categories "
        "WHERE builtin=1 AND parent_id IS NULL AND id!=? "
        "  AND name IN (SELECT name FROM categories WHERE parent_id=?)",
        (top_id, top_id))
    # 仅将剩余的未挂接内置分类挂到顶层，避免影响用户自建的顶层分类
    cur.execute(
        "UPDATE categories SET parent_id=? WHERE builtin=1 AND parent_id IS NULL AND id!=?",
        (top_id, top_id))
    # 会议纪要：独立顶层分类（非 builtin，避免被挂到管理标准分类下）
    if not cur.execute("SELECT 1 FROM categories WHERE name=?", ("会议纪要",)).fetchone():
        cur.execute(
            "INSERT INTO categories(name, description, sort_order, status, builtin, parent_id) "
            "VALUES (?,?,?,1,0,NULL)",
            ("会议纪要", "公司会议纪要，按年代归档与检索", 1))
    if own:
        conn.commit()
        conn.close()
    return top_id


def cat_perm_key(cat_id: int, action: str) -> str:
    """分类权限 key：kb.cat.<分类id>.<view|search|download>。"""
    return "kb.cat.%d.%s" % (cat_id, action)


def doc_category_to_node(category: str or None):
    """把文档的 category（类型名）映射为分类树中的节点名（经 TYPE_ALIASES）。"""
    if not category:
        return None
    return TYPE_ALIASES.get(category, category)


def category_ancestor_ids(conn, category_node_name: str):
    """返回某分类节点名（含自身）的全部祖先节点 id 列表。"""
    rows = conn.execute(
        "SELECT id, name, parent_id FROM categories").fetchall()
    by_name = {r["name"]: r["id"] for r in rows}
    by_id = {r["id"]: r["parent_id"] for r in rows}
    target = by_name.get(category_node_name)
    if target is None:
        return []
    chain, guard = [target], 0
    pid = by_id.get(target)
    while pid is not None and guard < 200:
        chain.append(pid)
        pid = by_id.get(pid)
        guard += 1
    return chain


def sync_category_permissions(conn=None, cur=None):
    """同步分类维度的权限行（仅顶级分类 × {浏览,查询,下载}）。

    设计简化（2026-08-29）：权限点只在「顶级分类」上生成，子分类不在权限表中单独列出，
    而是在运行时沿祖先链继承顶级权限（见 check_cat_action / category_ancestor_ids）。
    本库顶级分类固定为「管理标准」「会议纪要」两个，因此稳定只有 2×3=6 条分类权限。

    职责：
      1. 仅对 parent_id IS NULL 的顶级节点生成 kb.cat.<id>.{view,search,download} 权限行；
         同时清理所有非顶级的 kb.cat.* 存量权限行（子分类不再单列）。
      2. 存量迁移：角色若拥有旧全量权限 kb.view → 授予全部顶级节点 view；kb.search → 全部顶级节点 search。
         迁移后从角色移除旧 key（彻底切到分类维度）。
      3. 默认授权：admin 授予全部顶级权限（保持管理员全量可见）；全新库下 editor/viewer
         也授予全部顶级 view+search（默认全量只读，子分类经继承自动获得，后续可由管理员收窄）。
    支持传入 (conn, cur) 融入外部事务。
    """
    own = conn is None
    if own:
        conn = _conn()
        cur = conn.cursor()
    cats = conn.execute(
        "SELECT id, name, parent_id FROM categories ORDER BY parent_id IS NULL DESC, sort_order, id").fetchall()
    # 仅顶级节点参与权限点生成与授权
    top_cats = [c for c in cats if c["parent_id"] is None]

    # 1) 生成分类权限行（仅顶级）
    perm_rows = []
    for c in top_cats:
        for action in CAT_ACTIONS:
            key = cat_perm_key(c["id"], action)
            label = CAT_ACTION_LABELS[action]
            name = "%s · %s" % (c["name"], label)
            desc = "分类【%s】的%s权限（子分类继承）" % (c["name"], label)
            perm_rows.append((key, name, desc, "分类权限"))
    for key, name, desc, group in perm_rows:
        cur.execute(
            "INSERT OR IGNORE INTO permissions(`key`, name, description, `group`) VALUES (?,?,?,?)",
            (key, name, desc, group))
        cur.execute("UPDATE permissions SET name=?, description=?, `group`=? WHERE `key`=?",
                    (name, desc, group, key))
    perm_ids = {r["key"]: r["id"] for r in cur.execute("SELECT id,`key` FROM permissions").fetchall()}

    # 1b) 清理非顶级的存量 kb.cat.* 权限（子分类不再单列，避免权限表膨胀/重复）
    top_keys = {cat_perm_key(c["id"], a) for c in top_cats for a in CAT_ACTIONS}
    for r in cur.execute(
            "SELECT id,`key` FROM permissions WHERE `key` LIKE 'kb.cat.%'").fetchall():
        if r["key"] not in top_keys:
            cur.execute("DELETE FROM role_permissions WHERE permission_id=?", (r["id"],))
            cur.execute("DELETE FROM permissions WHERE id=?", (r["id"],))

    # 2) 存量迁移：kb.view/kb.search → 顶级节点 view/search
    role_rows = cur.execute(
        "SELECT rp.role_id, rp.permission_id, p.`key` FROM role_permissions rp "
        "JOIN permissions p ON p.id=rp.permission_id").fetchall()
    roles_with_view = {r["role_id"] for r in role_rows if r["key"] == "kb.view"}
    roles_with_search = {r["role_id"] for r in role_rows if r["key"] == "kb.search"}
    for role_id in set(list(roles_with_view) + list(roles_with_search)):
        want = set()
        if role_id in roles_with_view:
            want.update(cat_perm_key(c["id"], "view") for c in top_cats)
        if role_id in roles_with_search:
            want.update(cat_perm_key(c["id"], "search") for c in top_cats)
        for k in want:
            if k in perm_ids:
                cur.execute("INSERT OR IGNORE INTO role_permissions(role_id, permission_id) VALUES (?,?)",
                            (role_id, perm_ids[k]))
    # 移除旧全量 key
    for old_key in ("kb.view", "kb.search"):
        if old_key in perm_ids:
            cur.execute("DELETE FROM role_permissions WHERE permission_id=?", (perm_ids[old_key],))
            cur.execute("DELETE FROM permissions WHERE id=?", (perm_ids[old_key],))

    # 3) 默认授权：admin 全量；全新库（角色尚未拥有任何 kb.cat.*）的 editor/viewer 授顶级 view+search
    admin_id = cur.execute("SELECT id FROM roles WHERE name='admin'").fetchone()
    if admin_id:
        admin_id = admin_id["id"]
        for k in [cat_perm_key(c["id"], a) for c in top_cats for a in CAT_ACTIONS]:
            if k in perm_ids:
                cur.execute("INSERT OR IGNORE INTO role_permissions(role_id, permission_id) VALUES (?,?)",
                            (admin_id, perm_ids[k]))
    for rname in ("editor", "viewer"):
        rid = cur.execute("SELECT id FROM roles WHERE name=?", (rname,)).fetchone()
        if not rid:
            continue
        rid = rid["id"]
        has_cat = cur.execute(
            "SELECT 1 FROM role_permissions rp JOIN permissions p ON p.id=rp.permission_id "
            "WHERE rp.role_id=? AND p.`key` LIKE 'kb.cat.%'", (rid,)).fetchone()
        if has_cat:
            continue  # 已手动配置过，不覆盖
        for a in ("view", "search"):
            for k in [cat_perm_key(c["id"], a) for c in top_cats]:
                if k in perm_ids:
                    cur.execute("INSERT OR IGNORE INTO role_permissions(role_id, permission_id) VALUES (?,?)",
                                (rid, perm_ids[k]))

    if own:
        conn.commit()
        conn.close()


def check_cat_action(permissions: set, category: str or None, action: str) -> bool:
    """判断某账号是否对「文档所属分类」拥有某操作权限（沿祖先链继承）。

    category 为文档的 category 字段（类型名），经 TYPE_ALIASES 映射到分类树节点，
    再沿该节点及其全部祖先检查 kb.cat.<id>.<action>。
    """
    if action not in CAT_ACTIONS:
        return False
    node = doc_category_to_node(category)
    if node is None:
        return False
    conn = _conn()
    try:
        ids = category_ancestor_ids(conn, node)
    finally:
        conn.close()
    if not ids:
        return False
    return any(cat_perm_key(i, action) in permissions for i in ids)


def get_category_descendants(name: str) -> list:
    """返回某分类下所有后代分类的名称列表（不含自身）。"""
    cats = list_categories(only_enabled=False)
    by_name = {c["name"]: c for c in cats}
    by_parent = {}
    for c in cats:
        by_parent.setdefault(c["parent_id"], []).append(c)
    root = by_name.get(name)
    if not root:
        return []
    out, stack = [], [root["id"]]
    while stack:
        cid = stack.pop()
        node = next((x for x in cats if x["id"] == cid), None)
        if node is None:
            continue
        for child in by_parent.get(cid, []):
            stack.append(child["id"])
            out.append(child["name"])
    return out


def category_children(parent_id) -> list:
    """返回某父节点下的直接子分类（id/name/parent_id）。"""
    conn = _conn()
    rows = conn.execute(
        "SELECT id, name, parent_id FROM categories WHERE parent_id IS ? ORDER BY sort_order, id",
        (parent_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_category(name: str, description: str = "", sort_order: int = 0,
                    parent_id: int = None) -> int:
    """新建分类。

    重复名校验限定在「同一上级」范围内：不同父级下允许出现同名子分类，
    仅当与某个同级（同 parent_id）分类重名时才报错。
    """
    conn = _conn()
    try:
        dup = conn.execute(
            "SELECT 1 FROM categories WHERE name=? AND (parent_id IS ? OR parent_id = ?)",
            (name, parent_id, parent_id)).fetchone()
        if dup:
            scope = conn.execute("SELECT name FROM categories WHERE id=?", (parent_id,)).fetchone()
            parent_name = scope["name"] if scope else "顶级"
            raise ValueError("分类名称「%s」在「%s」下已存在" % (name, parent_name))
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO categories(name, description, sort_order, status, builtin, parent_id) "
            "VALUES (?,?,?,1,0,?)",
            (name, description, sort_order, parent_id))
        cid = cur.lastrowid
        conn.commit()
        sync_category_permissions()  # 新分类自动进入权限目录（默认授予 admin）
        return cid
    except sqlite3.IntegrityError:
        conn.rollback()
        parent_name = "顶级"
        try:
            sc = conn.execute("SELECT name FROM categories WHERE id=?", (parent_id,)).fetchone()
            if sc:
                parent_name = sc["name"]
        except Exception:  # noqa: BLE001
            pass
        raise ValueError("分类名称「%s」在「%s」下已存在" % (name, parent_name))
    finally:
        conn.close()


def update_category(cat_id: int, name: str = None, description: str = None,
                    sort_order: int = None, status: int = None, parent_id: object = None):
    conn = _conn()
    try:
        cur = conn.cursor()
        if name is not None:
            # 同级（同 parent_id）范围内查重，排除自身
            row = conn.execute("SELECT parent_id FROM categories WHERE id=?", (cat_id,)).fetchone()
            ppid = row["parent_id"] if row else None
            dup = conn.execute(
                "SELECT 1 FROM categories WHERE name=? AND id<>? AND (parent_id IS ? OR parent_id = ?)",
                (name, cat_id, ppid, ppid)).fetchone()
            if dup:
                scope = conn.execute("SELECT name FROM categories WHERE id=?", (ppid,)).fetchone()
                parent_name = scope["name"] if scope else "顶级"
                raise ValueError("分类名称「%s」在「%s」下已存在" % (name, parent_name))
            cur.execute("UPDATE categories SET name=? WHERE id=?", (name, cat_id))
        if description is not None:
            cur.execute("UPDATE categories SET description=? WHERE id=?", (description, cat_id))
        if sort_order is not None:
            cur.execute("UPDATE categories SET sort_order=? WHERE id=?", (sort_order, cat_id))
        if status is not None:
            cur.execute("UPDATE categories SET status=? WHERE id=?", (status, cat_id))
        if parent_id is not None:
            if parent_id == cat_id:
                raise ValueError("不能将分类设为自身的上级")
            cur.execute("UPDATE categories SET parent_id=? WHERE id=?", (parent_id, cat_id))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ValueError("分类名称在该分类下已存在")
    finally:
        conn.close()


def delete_category(cat_id: int):
    conn = _conn()
    cat = conn.execute("SELECT builtin FROM categories WHERE id=?", (cat_id,)).fetchone()
    if cat and cat["builtin"]:
        conn.close()
        raise ValueError("内置分类不可删除，仅可停用")
    kids = conn.execute("SELECT 1 FROM categories WHERE parent_id=?", (cat_id,)).fetchone()
    if kids:
        conn.close()
        raise ValueError("该分类下存在子分类，请先处理子分类后再删除")
    conn.execute("DELETE FROM categories WHERE id=?", (cat_id,))
    conn.commit()
    conn.close()
    sync_category_permissions()  # 清理该分类对应的权限行（admin 等角色的关联自动失效）


# ============ 功能开关 ============
def list_features() -> list:
    conn = _conn()
    rows = conn.execute(
        "SELECT `key`, name, description, enabled FROM feature_flags ORDER BY `key`").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_feature(key: str, enabled: int):
    conn = _conn()
    conn.execute("UPDATE feature_flags SET enabled=? WHERE `key`=?", (1 if enabled else 0, key))
    conn.commit()
    conn.close()


def get_feature(key: str, default: int = 1) -> int:
    conn = _conn()
    row = conn.execute("SELECT enabled FROM feature_flags WHERE `key`=?", (key,)).fetchone()
    conn.close()
    return row["enabled"] if row else default


if __name__ == "__main__":
    init_db()
    print("管理库初始化完成:", DB_PATH)
