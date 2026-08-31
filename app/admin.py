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

import user_pool

# ============ 路径 ============
KB_ROOT = os.environ.get("KB_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(KB_ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "kb_admin.db")
# 用户池：工号/姓名/部门/预设角色全部落在 config/user_pool.json，
# 本模块只关联该文件（读写与文件格式见 app/user_pool.py），名单不再硬编码进源码。
USER_POOL_FILE = os.path.join(KB_ROOT, "config", "user_pool.json")
# 与本模块对齐根目录（user_pool 在 import 时已按 KB_ROOT 推过一次，这里再同步一次，
# 杜绝「admin 用一个根、user_pool 读写另一个根」的配置分裂）
user_pool.set_root(KB_ROOT)


def _env_get(key: str, default: str = "") -> str:
    """读取配置项：os.environ 优先，未命中再回落到 .env 文件文本。

    【为什么不用 load_dotenv】serve.py 启动时会以 override=True 加载 .env 并
    **校验/纠正** KB_ROOT（遇到非法值会回退到推导值）。本模块若在 import 时再
    加载一次 .env，会把已被纠正的 KB_ROOT 又覆盖成 .env 里的错误值，导致索引与
    文档全部找不着。因此这里只做「按需读取」，且**绝不写回 os.environ**。
    """
    v = (os.environ.get(key) or "").strip()
    if v:
        return v
    try:
        with open(os.path.join(KB_ROOT, ".env"), "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, val = line.partition("=")
                if k.strip().lstrip("export ").strip() == key:
                    return val.strip().strip('"').strip("'")
    except OSError:
        pass
    return default

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

# ============ 默认管理员 ============
# 【安全铁律】本文件受 Git 版本控制，**禁止出现任何明文口令**。
# 初始管理员口令只有两个来源（按优先级）：
#   1) 部署方在 .env 显式配置 KB_ADMIN_PASS（推荐，口令不进版本库）
#   2) 首次建库时随机生成，仅写入 data/initial_admin_password.txt（data/ 已被
#      .gitignore 排除）并打印到启动日志，由运维首次登录后自行修改并删除该文件。
# 历史教训：曾把默认口令硬编码在本文件，随仓库公开即等于口令泄露；
# 且改这里【不会】影响已存在库里的 admin 账号（建库后才用到），
# 存量环境请用 scripts/reset_admin_password.py 重置。
DEFAULT_ADMIN_USER = _env_get("KB_ADMIN_USER", "admin") or "admin"
INITIAL_PASS_FILE = os.path.join(DATA_DIR, "initial_admin_password.txt")

# ============ 用户池（自助注册白名单） ============
# 自助注册时，用户填写的「工号 + 姓名」必须命中用户池且姓名完全一致，
# 并继承池中为该工号预设的角色（即「用户池给定的权限」）。
#
# 池数据本身【不在本文件】：统一存于 config/user_pool.json（见 app/user_pool.py）。
# 该文件可直接用真实花名册替换，或在「用户管理 → 用户池」中批量导入写回。
# 注册占用状态不落库，按 users 表的工号实时派生（删账号即自动释放）。

# 功能开关默认项 (对“现有功能”的管理)
DEFAULT_FEATURES = [
    ("graph_enabled",   "3D 知识图谱",   "开启后用户可访问 3D 神经网络知识图谱", 1),
    ("search_enabled",  "检索接口",      "开启 BM25 检索与检索 API",              1),
    ("register_enabled","自助注册",      "允许用户按「工号+姓名」自助注册，命中的用户池赋予对应角色，且须管理员审批通过后方可登录", 1),
    ("upload_enabled",  "文档上传",      "允许用户上传文档扩充知识库",             1),
    ("api_public",      "开放检索 API",  "无需登录即可调用 /api/query",          0),
    ("watermark_enabled","页面水印",      "为所有内容显示页叠加使用者账号+姓名水印", 1),
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


# 随机口令字符表：已剔除易混淆字符 0/O、1/l/I，避免运维抄错
_PWD_ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789!@#$%^&*-_=+"


def generate_password(length: int = 16) -> str:
    """生成随机强口令（初始管理员口令未配置时使用）。"""
    return "".join(secrets.choice(_PWD_ALPHABET) for _ in range(length))


def _initial_admin_password() -> tuple:
    """返回 (口令, 来源)；来源为 'env'（.env 指定）或 'generated'（随机生成）。

    随机生成时同步落盘到 data/initial_admin_password.txt，便于运维查看；
    该文件位于已被 .gitignore 排除的 data/ 下，不会进版本库。
    """
    env_pass = _env_get("KB_ADMIN_PASS")
    if env_pass:
        return env_pass, "env"
    pwd = generate_password()
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(INITIAL_PASS_FILE, "w", encoding="utf-8") as f:
            f.write("账号: %s\n口令: %s\n\n"
                    "（首次初始化时随机生成。请登录后立即修改密码，并删除本文件）\n"
                    % (DEFAULT_ADMIN_USER, pwd))
    except OSError as e:  # noqa: BLE001
        print("[警告] 初始管理员口令写入失败（请留意上方日志中的口令）: %s" % e)
    return pwd, "generated"


def _now() -> str:
    """当前时间（本地时区）。早期用 UTC，导致记录时间比界面显示慢 8 小时。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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
    -- 注册申请：提交后为 pending，管理员审批(approved)时才真正建 users 账号。
    CREATE TABLE IF NOT EXISTS user_registrations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_no TEXT NOT NULL,
        name TEXT NOT NULL,
        dept TEXT DEFAULT '',
        password_salt TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        pool_role TEXT DEFAULT '',
        role_id INTEGER,
        status TEXT DEFAULT 'pending',
        apply_at TEXT,
        review_at TEXT,
        reviewer_id INTEGER,
        review_note TEXT DEFAULT '',
        user_id INTEGER,
        FOREIGN KEY (role_id) REFERENCES roles(id) ON DELETE SET NULL
    );
    """)
    # 迁移：用户池已改为文件驱动（config/user_pool.json），早期版本建的 user_pool
    # 表不再使用，直接删除，避免留下「改了表不生效 / 改了文件不生效」的双份数据源。
    # 该表只存在于尚未上线的开发库，无历史数据风险。
    try:
        cur.execute("PRAGMA foreign_keys=OFF")
        cur.execute("DROP TABLE IF EXISTS user_pool")
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            cur.execute("PRAGMA foreign_keys=ON")
        except Exception:  # noqa: BLE001
            pass
    # 迁移：申请单改为记录「池中的角色名」（pool_role），便于审批时核对；
    # 早期库没有该列，按需补齐（pool_id 列已废弃，保留不影响）。
    cur.execute("PRAGMA table_info(user_registrations)")
    rcols = [r[1] for r in cur.fetchall()]
    if "pool_role" not in rcols:
        cur.execute("ALTER TABLE user_registrations ADD COLUMN pool_role TEXT DEFAULT ''")
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
    # 【顺序要求】先建角色，再建分类树并同步分类权限：
    # sync_category_permissions 只对【已存在】的角色授予分类浏览/查询权限，
    # 若在角色创建之前调用，全新库下的 admin/editor/viewer 都拿不到分类权限，
    # 表现为「新注册用户审批通过后登录，侧边栏与知识库一片空白」。
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
    # 角色就绪后建立分类树并同步分类权限（admin 全量、editor/viewer 全量 view+search）
    ensure_category_hierarchy(conn=conn, cur=cur)
    sync_category_permissions(conn=conn, cur=cur)
    # 默认管理员
    admin_role = cur.execute("SELECT id FROM roles WHERE name='admin'").fetchone()["id"]
    if not cur.execute("SELECT 1 FROM users WHERE username=?", (DEFAULT_ADMIN_USER,)).fetchone():
        pwd, source = _initial_admin_password()
        salt, h = hash_password(pwd)
        cur.execute(
            "INSERT INTO users(username, display_name, password_salt, password_hash, role_id, status, created_at) "
            "VALUES (?,?,?,?,?,1,?)",
            (DEFAULT_ADMIN_USER, "系统管理员", salt, h, admin_role, _now()))
        if source == "generated":
            # 随机口令只在创建时出现这一次，必须显著打印，否则运维无从登录
            print("=" * 66)
            print("[重要] 已创建默认管理员账号：%s" % DEFAULT_ADMIN_USER)
            print("       初始口令（随机生成）：%s" % pwd)
            print("       口令同时已写入：%s" % INITIAL_PASS_FILE)
            print("       请首次登录后立即修改密码，并删除上述口令文件。")
            print("       如需固定口令，请在 .env 配置 KB_ADMIN_PASS 后重建 data/kb_admin.db。")
            print("=" * 66)
    # 功能开关
    for key, name, desc, en in DEFAULT_FEATURES:
        cur.execute("INSERT OR IGNORE INTO feature_flags(`key`, name, description, enabled) VALUES (?,?,?,?)",
                    (key, name, desc, en))
    _migrate_register_feature(conn, cur)


# 旧版「自助注册」开关的描述文案（用于识别"从未配置过"的存量库）
_OLD_REGISTER_DESC = "允许访客自行注册为只读账号"


def _migrate_register_feature(conn, cur):
    """把旧语义的 register_enabled 升级为「用户池 + 审批」语义。

    旧开关含义是「访客自助注册为只读账号」，默认关闭；新语义是「工号+姓名
    匹配用户池、按池授权、管理员审批」，默认应开启。这里仅在开关描述仍为旧
    文案（说明管理员从未按新语义配置过）时，一并刷新描述并置为开启；
    若管理员已显式配置过（描述已是新文案或已被改动），则只补描述、不覆盖其值。
    """
    # 【顺序要求】必须先读旧描述再刷新描述：若先把描述更新成新文案，
    # 下一次比对永远不相等，迁移条件形同虚设（开关会一直停留在旧值 0）。
    row = cur.execute(
        "SELECT description, enabled FROM feature_flags WHERE `key`='register_enabled'").fetchone()
    never_configured = bool(row) and (row["description"] or "") == _OLD_REGISTER_DESC
    for key, name, desc, _en in DEFAULT_FEATURES:
        cur.execute("UPDATE feature_flags SET name=?, description=? WHERE `key`=?", (name, desc, key))
    if never_configured:
        cur.execute("UPDATE feature_flags SET enabled=1 WHERE `key`='register_enabled'")


def _ensure_user_pool_file():
    """确保 config/user_pool.json 存在（缺失时按虚拟池模板生成）。

    在服务启动路径调用：让管理员第一次打开「用户池」页就有文件可看，
    也便于直接编辑文件。内容由 user_pool.DEFAULT_DOC 决定。
    """
    try:
        user_pool.load_doc()
    except Exception as e:  # noqa: BLE001
        print("[警告] 用户池文件不可用（%s）：%s" % (USER_POOL_FILE, e))


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
    conn.execute("UPDATE user_registrations SET user_id=NULL WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    conn.close()
    # 注：用户池占用状态按 users 表的工号实时派生，删除账号后工号自动可重新注册，
    # 无需在此清理任何占用标记（见 list_user_pool）。


# ============ 用户池（自助注册白名单） ============
# 池数据本身存于 config/user_pool.json（见 app/user_pool.py）；
# 本节函数只做「读文件 + 关联数据库派生状态」，不把名单复制进数据库。
def list_user_pool(only_enabled: bool = False) -> list:
    """用户池列表：文件内容 + 数据库派生的注册状态。

    派生字段（不落库，故不会出现与 users 表不一致的脏状态）：
      used            工号已注册（users 表中存在同名账号）
      used_user_id / used_username
      pending         存在待审批申请
      role_id         池中的角色名解析出的角色 id（角色不存在则为 None）
    """
    entries = user_pool.list_entries(only_enabled=only_enabled)
    if not entries:
        return []
    conn = _conn()
    role_ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM roles").fetchall()}
    users_by_no = {r["username"]: r for r in conn.execute(
        "SELECT id, username, display_name FROM users").fetchall()}
    pending = {r["emp_no"] for r in conn.execute(
        "SELECT emp_no FROM user_registrations WHERE status='pending'").fetchall()}
    conn.close()
    out = []
    for e in entries:
        u = users_by_no.get(e["emp_no"])
        out.append({
            "emp_no": e["emp_no"], "name": e["name"], "dept": e["dept"],
            "role": e["role"], "role_id": role_ids.get(e["role"]),
            "status": e["status"], "note": e["note"],
            "used": 1 if u else 0,
            "used_user_id": u["id"] if u else None,
            "used_username": u["username"] if u else None,
            "pending": 1 if e["emp_no"] in pending else 0,
        })
    return out


def _norm(v) -> str:
    return ("" if v is None else str(v)).strip()


def _role_names() -> set:
    conn = _conn()
    names = {r["name"] for r in conn.execute("SELECT name FROM roles").fetchall()}
    conn.close()
    return names


def user_pool_info() -> dict:
    """用户池文件元信息（路径 / 来源 / 更新时间），供界面提示管理员去哪里改。"""
    info = user_pool.pool_info()
    info["file"] = USER_POOL_FILE
    return info


def create_pool_entry(emp_no, name, dept="", role="", note="", status=1):
    """新增池条目（直接写入 config/user_pool.json）。"""
    if role and role not in _role_names():
        raise ValueError("角色「%s」不存在" % role)
    return user_pool.create_entry(emp_no, name, dept=dept, role=role,
                                  status=status, note=note)


def update_pool_entry(emp_no, data: dict):
    """更新池条目（按工号定位，允许改工号）。

    【防呆】已注册账号的工号不允许改名：改名后 users 表里的账号（username 仍是旧工号）
    再也匹配不上池中条目，会表现为「已注册却显示未注册、占用状态丢失」。
    """
    if "role" in data and data["role"] and data["role"] not in _role_names():
        raise ValueError("角色「%s」不存在" % data["role"])
    if "emp_no" in data and _norm(data.get("emp_no")) != _norm(emp_no):
        conn = _conn()
        exists = conn.execute("SELECT 1 FROM users WHERE username=?", (_norm(emp_no),)).fetchone()
        conn.close()
        if exists:
            raise ValueError("该工号已注册账号，不允许修改工号（请先删除对应账号）")
    return user_pool.update_entry(emp_no, data)


def delete_pool_entry(emp_no):
    """删除池条目（不影响已注册账号，仅使其无法再自助注册）。"""
    return user_pool.delete_entry(emp_no)


def import_user_pool(items: list, mode: str = "merge") -> dict:
    """批量导入用户池（真实花名册走这里），结果写回 config/user_pool.json。

    items: [{emp_no, name, dept?, role?}]，role 为【角色名】。
    mode:  merge   —— 同工号更新、新工号新增
           replace —— 先清空【未被注册占用】的条目再导入
    返回 {created, updated, skipped, cleared, errors:[{row, msg}]}
    """
    if not isinstance(items, list):
        raise ValueError("导入数据必须是数组")
    result = {"created": 0, "updated": 0, "skipped": 0, "cleared": 0, "errors": []}
    if mode == "replace":
        # 保留已注册工号：删掉它们的池条目会让「已注册员工」在界面上凭空消失
        keep = [p["emp_no"] for p in list_user_pool() if p["used"]]
        result["cleared"] = user_pool.clear_entries(keep_emp_nos=keep)
    r = user_pool.import_entries(items, mode=mode, valid_roles=_role_names())
    result.update({k: r[k] for k in ("created", "updated", "skipped", "errors")})
    return result


def _norm(v) -> str:
    return ("" if v is None else str(v)).strip()


# ============ 注册申请与审批 ============
def min_password_length() -> int:
    return 6


def create_registration(emp_no, name, password) -> int:
    """提交注册申请。

    校验链（用户池来自 config/user_pool.json）：
    工号在池 → 池条目启用 → 姓名与池中完全一致 → 未被注册（users 表无同名账号）
    → 无在审申请。全部通过才写入 pending 申请，由管理员审批后生成账号。
    """
    emp_no, name = _norm(emp_no), _norm(name)
    if not emp_no or not name:
        raise ValueError("工号与姓名不能为空")
    if not password or len(password) < min_password_length():
        raise ValueError("密码至少 %d 位" % min_password_length())
    entry = user_pool.get_entry(emp_no)
    if not entry:
        raise ValueError("工号「%s」不在用户池中，请联系系统管理员" % emp_no)
    if entry["status"] != 1:
        raise ValueError("工号「%s」已被停用，请联系系统管理员" % emp_no)
    if entry["name"] != name:
        raise ValueError("工号与姓名不匹配，请核对后重新填写")
    conn = _conn()
    try:
        if conn.execute("SELECT 1 FROM users WHERE username=?", (emp_no,)).fetchone():
            raise ValueError("该工号已注册，请直接登录")
        if conn.execute("SELECT 1 FROM user_registrations WHERE emp_no=? AND status='pending'",
                        (emp_no,)).fetchone():
            raise ValueError("该工号的注册申请已在审批中，请等待管理员处理")
        # 角色按【名称】解析：池文件里存的是角色名，避免角色 id 在环境间漂移
        role_id = None
        if entry["role"]:
            row = conn.execute("SELECT id FROM roles WHERE name=?", (entry["role"],)).fetchone()
            role_id = row["id"] if row else None
        salt, h = hash_password(password)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO user_registrations(emp_no, name, dept, password_salt, password_hash, "
            "pool_role, role_id, status, apply_at) VALUES (?,?,?,?,?,?,?,'pending',?)",
            (emp_no, name, entry["dept"], salt, h, entry["role"], role_id, _now()))
        rid = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    return rid


def list_registrations(status: str = None) -> list:
    """注册申请列表（不返回密码哈希）。pending 排在最前。"""
    conn = _conn()
    sql = ("SELECT r.id, r.emp_no, r.name, r.dept, r.role_id, ro.name AS role_name, "
           "r.status, r.apply_at, r.review_at, r.reviewer_id, r.review_note, r.user_id, "
           "u.username AS reviewer_name, au.username AS created_username "
           "FROM user_registrations r "
           "LEFT JOIN roles ro ON r.role_id=ro.id "
           "LEFT JOIN users u ON r.reviewer_id=u.id "
           "LEFT JOIN users au ON r.user_id=au.id")
    args = []
    if status:
        sql += " WHERE r.status=?"
        args.append(status)
    sql += " ORDER BY CASE r.status WHEN 'pending' THEN 0 ELSE 1 END, r.id DESC"
    rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    conn.close()
    return rows


def pending_registration_by_emp(emp_no):
    """按工号查在审申请（登录失败时用于给出「待审批」的准确提示）。"""
    conn = _conn()
    row = conn.execute(
        "SELECT id, emp_no, name, status FROM user_registrations "
        "WHERE emp_no=? AND status='pending'", (_norm(emp_no),)).fetchone()
    conn.close()
    return dict(row) if row else None


def approve_registration(reg_id: int, reviewer_id=None, role_id=None, note="") -> int:
    """审批通过：按申请中保存的密码哈希直接建账号（status=1 可登录）。"""
    conn = _conn()
    try:
        reg = conn.execute("SELECT * FROM user_registrations WHERE id=?", (reg_id,)).fetchone()
        if not reg:
            raise ValueError("申请不存在")
        if reg["status"] != "pending":
            raise ValueError("该申请已处理（%s）" % reg["status"])
        final_role = role_id if role_id not in (None, "", 0) else reg["role_id"]
        if final_role is None:
            raise ValueError("该工号未预设角色，请先为用户池条目指定角色")
        if not conn.execute("SELECT 1 FROM roles WHERE id=?", (final_role,)).fetchone():
            raise ValueError("角色不存在（id=%s）" % final_role)
        if conn.execute("SELECT 1 FROM users WHERE username=?", (reg["emp_no"],)).fetchone():
            raise ValueError("账号「%s」已存在，无法重复创建" % reg["emp_no"])
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users(username, display_name, password_salt, password_hash, role_id, status, created_at) "
            "VALUES (?,?,?,?,?,1,?)",
            (reg["emp_no"], reg["name"], reg["password_salt"], reg["password_hash"], final_role, _now()))
        uid = cur.lastrowid
        cur.execute(
            "UPDATE user_registrations SET status='approved', role_id=?, review_at=?, reviewer_id=?, "
            "review_note=?, user_id=? WHERE id=?",
            (final_role, _now(), reviewer_id, _norm(note), uid, reg_id))
        conn.commit()
        # 注：用户池不落库，账号建好后「已注册」状态由工号自动派生，无需回写文件。
    finally:
        conn.close()
    return uid


def reject_registration(reg_id: int, reviewer_id=None, note=""):
    """驳回申请（保留记录与原因，工号不占用，用户可重新提交）。"""
    conn = _conn()
    try:
        reg = conn.execute("SELECT * FROM user_registrations WHERE id=?", (reg_id,)).fetchone()
        if not reg:
            raise ValueError("申请不存在")
        if reg["status"] != "pending":
            raise ValueError("该申请已处理（%s）" % reg["status"])
        conn.execute(
            "UPDATE user_registrations SET status='rejected', review_at=?, reviewer_id=?, review_note=? "
            "WHERE id=?", (_now(), reviewer_id, _norm(note), reg_id))
        conn.commit()
    finally:
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


def _cascade_rename_category_docs(old_name: str, new_name: str):
    """分类改名级联：把 user_documents.json 中 category==old_name 的文档统一改为 new_name。

    通过 kb_store.reclassify_upload 处理，保证逻辑分类与物理存储目录一并迁移，
    不产生孤儿文档。kb_store 反向依赖本模块，故此处惰性导入以避免循环依赖。
    """
    try:
        from kb_store import reclassify_upload
    except Exception:  # noqa: BLE001
        return
    import json as _json
    from kb_store import _load_uploads, _STORE_LOCK, _save_uploads  # noqa: F401
    # 先收集受影响的 doc_id，再逐个 reclassify（reclassify 内部会加锁并落盘）
    affected = []
    with _STORE_LOCK:
        ups = _load_uploads()
        if not isinstance(ups, list):
            ups = []
        for u in ups:
            if isinstance(u, dict) and u.get("category") == old_name:
                affected.append(u.get("doc_id"))
    for doc_id in affected:
        try:
            reclassify_upload(doc_id, new_name)
        except Exception:  # noqa: BLE001
            continue


def update_category(cat_id: int, name: str = None, description: str = None,
                    sort_order: int = None, status: int = None, parent_id: object = None):
    conn = _conn()
    try:
        cur = conn.cursor()
        old_name = None
        if name is not None:
            # 同级（同 parent_id）范围内查重，排除自身
            row = conn.execute("SELECT parent_id, name FROM categories WHERE id=?", (cat_id,)).fetchone()
            ppid = row["parent_id"] if row else None
            old_name = row["name"] if row else None
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
        # 级联：分类改名后，把散落在旧名称下的孤儿文档一并归到新名称，
        # 避免「分类树已改名、文档 category 仍是旧名」造成的挂空/断链。
        if name is not None and old_name is not None and name != old_name:
            _cascade_rename_category_docs(old_name, name)
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
    # 幂等补种：确保 DEFAULT_FEATURES 中新增的开关（如 watermark_enabled）在旧库中也存在，
    # 否则 set_feature 的 UPDATE 会因找不到行而静默失效（Flask 启动不调用 init_db）。
    for key, name, desc, en in DEFAULT_FEATURES:
        conn.execute(
            "INSERT OR IGNORE INTO feature_flags(`key`, name, description, enabled) VALUES (?,?,?,?)",
            (key, name, desc, en))
    conn.commit()
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
