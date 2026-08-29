#!/usr/bin/env python3
"""本地知识库服务 + 管理后台

端点:
  公开:
    GET  /                          -> 管理门户 SPA
    GET  /web/<path>                -> 门户静态资源
    GET  /graph                     -> 3D 神经网络知识图谱
    GET  /graph/<path>              -> 图谱静态资源
    POST /api/auth/login            -> 登录
    POST /api/auth/logout           -> 登出
    GET  /api/auth/me               -> 当前用户
    GET  /api/health                -> 健康检查
    GET  /api/query                 -> BM25 检索 (受 api_public 开关控制)

  知识库 (需 kb.view):
    GET  /api/kb/categories         -> 分类列表(含文档数)
    GET  /api/kb/documents          -> 文档列表(分页/筛选)
    GET  /api/kb/document?doc_id=   -> 文档详情(全文)
    GET  /api/kb/search?q=          -> 检索(需 kb.search)

  知识库管理:
    POST   /api/kb/category         -> 新建分类 (kb.category.manage)
    PUT    /api/kb/category/<id>    -> 编辑分类
    DELETE /api/kb/category/<id>    -> 停用/删除分类
    POST   /api/kb/upload           -> 上传文档 (kb.doc.upload)
    DELETE /api/kb/document/<id>    -> 删除上传文档 (kb.doc.delete)

  系统管理:
    GET  /api/admin/permissions     -> 权限目录 (permission.view)
    GET/POST/PUT/DELETE /api/admin/roles[/<id>]   (role.manage)
    GET/POST/PUT/DELETE /api/admin/users[/<id>]   (user.view / user.manage)
    GET/PUT /api/admin/features[/<key>]           (system.manage)
    POST /api/admin/reindex                          (system.manage)
    GET  /api/admin/stats                             (system.manage)
"""
import os
import sys
import subprocess
import json
import secrets
import functools

from flask import (Flask, request, jsonify, send_from_directory,
                   session, redirect, abort, make_response)


# ===================== 统一配置源：.env =====================
# 【背景】原先 serve.py 不加载 .env，端口/根目录只能靠 systemd 的 Environment
# 注入，导致同一项配置散落在 systemd 与 .env 两处，改一处常漏另一处。
# 这里在【读取 KB_ROOT 与端口之前】加载 .env，使配置统一收敛到 .env。
#
# 【为什么 override=True】必须让 .env 压过外部环境变量（如 systemd 残留的
# Environment=），否则 .env 里的修改永远不生效 —— 这正是此前端口改不动的根因。
def _load_env_file():
    """加载部署根目录下的 .env，返回其路径；未找到则返回 None。"""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return None
    from pathlib import Path
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        # override=True：.env 为最高优先级配置源
        load_dotenv(env_path, override=True)
        return env_path
    return None


ENV_FILE = _load_env_file()

# KB_ROOT：部署根目录。serve.py 位于 <root>/scripts/
# 【防御性校验】环境变量里的 KB_ROOT 可能是误设值（历史坑：.env 里曾被填成
# ".../knowledge_base"，而下方 KB_DIR 会再拼一次 knowledge_base，导致路径
# 二次叠加成 .../knowledge_base/knowledge_base，索引与文档全部找不着）。
# 合法部署根必须含 scripts/ 目录，据此自动纠正，避免服务带病启动。
_DEFAULT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_env_root = (os.environ.get("KB_ROOT") or "").strip()
if _env_root and os.path.isdir(os.path.join(_env_root, "scripts")):
    KB_ROOT = _env_root
else:
    if _env_root:
        print("[警告] KB_ROOT=%r 不是合法部署根（缺少 scripts/ 目录），"
              "已自动改用推导值 %s" % (_env_root, _DEFAULT_ROOT))
    KB_ROOT = _DEFAULT_ROOT
KB_DIR = os.path.join(KB_ROOT, "knowledge_base")
WEB_DIR = os.path.join(KB_ROOT, "web")            # 旧版静态资源（保留兼容）
DIST_DIR = os.path.join(KB_ROOT, "web_vue", "dist")  # Vue3 生产构建产物
DATA_DIR = os.path.join(KB_ROOT, "data")
INDEX_DIR = os.path.join(KB_DIR, "bm25_index")

# 强制把推导出的 KB_ROOT 注入环境，确保 app/vec_store.py 等子模块
# 使用同一根目录（其自身也读 KB_ROOT 环境变量），避免外部误设导致索引路径错乱
os.environ["KB_ROOT"] = KB_ROOT
sys.path.insert(0, os.path.join(KB_ROOT, "app"))

import admin
import kb_store
import search as kb_search_mod
import vec_store
import derived_store
import chat_store
import llm
import sqlite3
import zipfile
import tempfile
import io
import shutil
import extract_text
import pdf_make
import threading
import queue as _queue
import time

app = Flask(__name__, static_folder=None)

# ===================== 后台异步提取 / 建索引 =====================
# 上传接口只做「落盘 + 立即返回」，文件文本提取(extract+post_process) 与
# 索引重建(BM25+向量) 由后台 worker 线程串行处理，避免前端因大文件/全量重建
# 重建而长时间阻塞在「上传中」。
_EXTRACT_Q = _queue.Queue()
_INDEX_DIRTY = threading.Event()      # 有待建索引的脏文档
_EXTRACT_ABORT = threading.Event()   # 中止提取信号：set 后丢弃队列剩余任务
_REBUILDING = threading.Event()      # 正在重建索引：防止空闲轮询重复触发重建
# 空闲确认计数：worker 每轮 timeout=1s，连续 N 轮队列为空才认定「真正空闲」。
# 避免入队间隙「队列短暂为空」就误判空闲、反复触发全量重建。
_IDLE_TICKS = [0]
_IDLE_CONFIRM_TICKS = 3              # 约 3 秒持续空闲后才触发全量重建
_WORKER_STARTED = False


def _rebuild_indexes():
    """重建 BM25 索引（后台调用）。

    向量索引已改为单篇增量 upsert（见 _do_extract_task），此处只全量重建 BM25
    （BM25 不支持单篇增量，必须全量重算）。BM25 保存已做原子写，检索线程安全。
    函数本身不阻塞调用方；worker 在队列空闲时异步触发，不卡提取任务。
    """
    try:
        import rag_build_index
        rag_build_index.build_index()
        return True
    except Exception as e:  # noqa: BLE001
        print("[background] 索引重建失败: %s" % e)
        return False


def _drain_extract_queue():
    """清空提取队列（丢弃尚未执行的任务）。Queue 无原生 clear，逐个取走。"""
    dropped = 0
    while not _EXTRACT_Q.empty():
        try:
            _EXTRACT_Q.get_nowait()
            _EXTRACT_Q.task_done()
            dropped += 1
        except _queue.Empty:
            break
    return dropped


def _bm25_rebuild_async():
    """异步后台线程：全量重建 BM25（不阻塞提取 worker 取下一个任务）。

    【修复·重建风暴】原先 _INDEX_DIRTY 只在重建「完成后」才 clear，而重建（尤其
    BM25 全量 + BGE 向量编码）耗时常超过 worker 的 1 秒轮询间隔，导致 worker
    每秒都判定「脏」并再 spawn 一组重建线程 —— 线程无限叠加、CPU 打满、
    索引文件被反复并发重写（生产事故根源之一）。
    现改为：进入重建「前」立即抢锁 + 清脏标记，保证同一时刻只有一组重建在跑。
    """
    def _run():
        try:
            _rebuild_indexes()
        finally:
            _REBUILDING.clear()
    threading.Thread(target=_run, daemon=True).start()


def _vec_rebuild_async():
    """异步后台线程：全量重建向量索引（批量重提后调用，仅 1 次全量编码+1 次文件重写）。

    相比逐篇 upsert_document（每篇都 vstack + 原子重写整个 npy 文件），全量 rebuild
    只重写 1 次，批量场景下可把向量开销从 O(N 次文件重写) 降到 O(1)。
    """
    def _run():
        try:
            docs = list(kb_store.iter_all_documents())
            docs = [d for d in docs if d and d.get("content")]
            n = vec_store.rebuild(docs)
            print("[background] 向量全量重建完成: %d 篇 / %d chunks" % (len(docs), n))
        except Exception as e:  # noqa: BLE001
            print("[background] 向量全量重建失败: %s" % e)
    threading.Thread(target=_run, daemon=True).start()


def _extract_worker():
    while True:
        try:
            task = _EXTRACT_Q.get(timeout=1.0)
        except _queue.Empty:
            # 队列空：若有脏文档，异步触发一次「向量全量重建 + BM25 全量重建」。
            # 向量此前在队列非空时只标脏、未单篇写盘（避免每篇全库 pickle 重写），
            # 故此处统一做 1 次全量 BGE 编码 + 1 次文件重写（批量场景 O(1) 开销）；
            # BM25 为纯分词、开销极低，同样统一重建一次。
            # 抢锁式触发：先清脏标记 + 置 _REBUILDING，保证重建期间（可能数十秒）
            # 后续每秒的空闲轮询不会再 spawn 新的重建线程。
            # 入队与提取并发时，队列会「短暂为空」（后续任务尚未入队）。此时若立刻
            # 触发全量重建，会导致重建被反复打断/重启（每来一批新任务就重建一次）。
            # 这里做「空闲确认」：连续若干次轮询（约 3 秒）队列都为空，才认为真正
            # 空闲并触发重建；期间若来新任务则计数器归零。
            if _EXTRACT_Q.empty():
                _IDLE_TICKS[0] += 1
            else:
                _IDLE_TICKS[0] = 0

            if (_IDLE_TICKS[0] >= _IDLE_CONFIRM_TICKS and _INDEX_DIRTY.is_set()
                    and not _REBUILDING.is_set()):
                _IDLE_TICKS[0] = 0
                _INDEX_DIRTY.clear()
                _REBUILDING.set()
                _bm25_rebuild_async()
                _vec_rebuild_async()
            # 中止信号：清空剩余（理论上已空）并复位，避免再次入队时仍生效
            if _EXTRACT_ABORT.is_set():
                _EXTRACT_ABORT.clear()
            continue
        # 收到中止信号：丢弃本任务，不执行提取
        if _EXTRACT_ABORT.is_set():
            _EXTRACT_Q.task_done()
            continue
        try:
            _do_extract_task(task)
            # 向量已在 _do_extract_task 内增量 upsert；BM25 仅标记脏，空闲时异步重建
        except Exception as e:  # noqa: BLE001
            print("[background] 提取任务失败 %s: %s" % (task.get("filename"), e))
        finally:
            _EXTRACT_Q.task_done()


def _do_extract_task(task):
    """执行单个文件的文本提取 + 结构化，并补写 entry。"""
    filename = task["filename"]
    init_cat = task.get("category") or "未分类"
    doc_id = task["doc_id"]
    cat_hint = task.get("cat_hint") or ""
    # 读取原始二进制
    bin_path, _mime = kb_store.get_upload_binary(doc_id)
    if not bin_path or not os.path.exists(bin_path):
        print("[background] 找不到原始文件，跳过: %s" % doc_id)
        kb_store.update_upload_text_async(doc_id, "", init_cat)
        return
    with open(bin_path, "rb") as f:
        raw = f.read()
    # 分类：显式 hint 优先；否则按文件名+正文自动识别（在提取前确定，直接传给
    # extract()，避免 extract() 因拿不到 category 而回退嗅探、被 USE_LLM 误伤走 LLM 慢路径）
    #
    # 【分类自纠·关键】文件名是强信号：若文件名明确为「会议纪要/办公会」类，但既有
    # 分类（cat_hint / init_cat）却落在管理标准/其它树，说明该分类是历史误判（多由
    # zip 批量上传按目录建类、或早期版本缺陷导致）。此时必须按文件名纠正，否则
    # category_to_kind 会把它当 standard/regulation 处理，导致纪要红头被粘连、
    # 正文被金额数字乱切（实测「01.标准化类」下的纪要即为此种乱套）。
    # 只有当文件名无纪要特征时，才尊重既有分类。
    def _minutes_by_filename(name: str):
        """文件名命中会议纪要特征时返回规范分类名，否则 None。"""
        import re as _re_local
        if not name:
            return None
        if _re_local.search(r"(总经理|总办|经理)?\s*办公会", name) or "总经理会议纪要" in name:
            return "总经理会议纪要"
        if _re_local.search(r"(专项|专题)", name) and "纪要" in name:
            return "专项会议纪要"
        if "会议纪要" in name or _re_local.search(r"纪要\s*〔?\s*\[?\d{4}", name):
            return "会议纪要"
        return None

    _fn_cat = _minutes_by_filename(filename)
    if _fn_cat:
        cat = _fn_cat
    elif cat_hint:
        cat = cat_hint
    else:
        # 先快速解码一次正文仅用于分类嗅探（轻量，不联网）；落盘分类 init_cat
        # 若已是有效标准/纪要/合规类，直接复用，无需重新嗅探。
        _ext0 = os.path.splitext(filename)[1].lower()
        _probe = None
        if _ext0 == ".pdf":
            _probe = extract_text._decode_pdf(raw, category=init_cat)
        elif _ext0 == ".docx":
            _probe = extract_text._extract_docx(raw)
        if init_cat and init_cat != "未分类":
            cat = init_cat
        else:
            cat = _auto_classify(filename, _probe) or "未分类"
    text, warn = extract_text.extract(raw, filename, category=cat)
    if not text.strip():
        warn = (("%s；" % warn) if warn else "") + "未提取到文本内容"
    # 已按 cat 完成规则后处理，无需再次 post_process（避免二次规则排版）
    year = kb_store._extract_year(filename, text)
    # 后台分类与初始落盘分类不同（如 raw 阶段未指定、靠正文识别出会议纪要），
    # 移动归档文件到正确类别目录，保持磁盘归档与分类一致
    if cat != init_cat:
        try:
            kb_store.reclassify_upload(doc_id, cat)
        except Exception as e:  # noqa: BLE001
            print("[background] 重归类失败 %s: %s" % (doc_id, e))
    kb_store.update_upload_text_async(doc_id, text, cat, year)
    # 向量索引策略（性能关键，勿回退为每篇 upsert）：
    #   - 队列【非空】时：只标记脏、跳过任何向量写盘。把全部 BGE 编码 + 索引
    #     重写推迟到队列空闲后【统一全量 rebuild 1 次】（见 _extract_worker 空闲分支）。
    #     原因：每篇 upsert_document 会持锁做 BGE 编码 + np.vstack + 把整个 vec_index.pkl
    #     全量 pickle 重写一次；连续点击 N 篇 = 全库向量写盘 N 次，开销 O(N×全库)，
    #     这正是「连续点击变慢」的根因。统一 rebuild 仅 1 次文件重写，批量场景开销 O(1)。
    #   - 队列【空】时（即单篇 / 最后一篇）：直接增量 upsert，使该篇立即可搜，
    #     不触发全量 rebuild（单篇增量反而更省）。
    # BM25 同理（不支持单篇增量，始终标记脏、空闲时统一 rebuild 一次，纯分词开销极低）。
    # 向量索引策略（性能关键）：
    #   ！【修复·提取卡死的根因】原先「队列空就单篇 upsert」的分支已被移除。
    #   该判定存在竞态：入队与提取并发，worker 处理第 1 篇时队列常恰好为空
    #   （后续任务尚未入队），于是第 1 篇就走 upsert_document；而该函数首次调用
    #   会触发 get_embedder() 加载 BGE 模型（SentenceTransformer 初始化，首次需
    #   联网/读盘，耗时可达数分钟且不抛异常），直接在 worker 线程内阻塞 ——
    #   表现为「上传 N 篇只提取 1 篇就卡住，且无任何报错」。
    #   现统一改为：始终只标记脏，由【独立后台线程】在队列空闲后做 1 次全量重建。
    #   即使 BGE 首次加载慢，也只阻塞重建线程，不影响提取继续消费队列。
    _INDEX_DIRTY.set()  # 标脏：BM25 与向量都在空闲时统一全量重建


def start_extract_worker():
    global _WORKER_STARTED
    if _WORKER_STARTED:
        return
    _WORKER_STARTED = True
    t = threading.Thread(target=_extract_worker, daemon=True)
    t.start()


# 应用启动时拉起后台 worker（Flask 可能在 reload/多 worker 下重复调用，用标记防重）
start_extract_worker()

# ---------- 会话密钥（稳定持久化） ----------
SECRET_FILE = os.path.join(DATA_DIR, "secret.key")
os.makedirs(DATA_DIR, exist_ok=True)
if not os.path.exists(SECRET_FILE):
    with open(SECRET_FILE, "w") as f:
        f.write(secrets.token_hex(32))
with open(SECRET_FILE) as f:
    app.secret_key = f.read().strip()


# ===================== 鉴权装饰器 =====================
def login_required(perm=None):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            uid = session.get("user_id")
            if not uid:
                return jsonify({"error": "未登录"}), 401
            if perm:
                perms = admin.get_user_permissions(uid)
                if perm not in perms:
                    return jsonify({"error": "权限不足", "need": perm}), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def admin_required():
    """仅超级管理员（role.name == 'admin'）可访问。"""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            uid = session.get("user_id")
            if not uid:
                return jsonify({"error": "未登录"}), 401
            conn = admin._conn()
            row = conn.execute(
                "SELECT r.name AS role_name FROM users u LEFT JOIN roles r ON u.role_id=r.id WHERE u.id=?",
                (uid,)).fetchone()
            conn.close()
            if not row or row["role_name"] != "admin":
                return jsonify({"error": "仅管理员可执行此操作"}), 403
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def _current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    conn = admin._conn()
    row = conn.execute(
        "SELECT u.id, u.username, u.display_name, r.name AS role_name "
        "FROM users u LEFT JOIN roles r ON u.role_id=r.id WHERE u.id=?",
        (uid,)).fetchone()
    conn.close()
    if not row:
        return None
    u = dict(row)
    u["permissions"] = list(admin.get_user_permissions(u["id"]))
    return u


# ===================== 公开路由 =====================
import re as _re

_ASSET_RE = _re.compile(r'(href|src)="(/web/[^"]+)"')


def _version_assets(html):
    """给 index.html 里的 /web/* 资源引用追加 ?v=<文件修改时间>，
    前端文件一改动 URL 就变，浏览器必然拉取最新，根治缓存问题。"""
    def _sub(m):
        attr, url = m.group(1), m.group(2)
        path = os.path.join(WEB_DIR, url[len("/web/"):])
        try:
            v = int(os.path.getmtime(path))
        except OSError:
            v = 0
        return f'{attr}="{url}?v={v}"'
    return _ASSET_RE.sub(_sub, html)


@app.route("/")
def index():
    # 优先返回 Vue 生产构建；缺失时回退旧版 SPA
    try:
        with open(os.path.join(DIST_DIR, "index.html"), "r", encoding="utf-8") as f:
            html = f.read()
    except OSError:
        try:
            with open(os.path.join(WEB_DIR, "index.html"), "r", encoding="utf-8") as f:
                html = f.read()
        except OSError:
            return abort(404)
        html = _version_assets(html)
    r = make_response(html)
    r.headers["Content-Type"] = "text/html; charset=utf-8"
    r.headers["Cache-Control"] = "no-store, must-revalidate"
    return r


@app.route("/web/<path:filename>")
def web_static(filename):
    r = send_from_directory(WEB_DIR, filename)
    r.headers["Cache-Control"] = "no-store, must-revalidate"
    return r


@app.route("/assets/<path:filename>")
def dist_static(filename):
    # Vue 构建产物：文件名自带内容哈希，可长期缓存
    r = send_from_directory(os.path.join(DIST_DIR, "assets"), filename)
    r.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return r


@app.route("/graph")
def graph():
    if not admin.get_feature("graph_enabled", 1):
        return abort(403, description="3D 知识图谱已关闭")
    return send_from_directory(KB_DIR, "knowledge_graph_static.html")


@app.route("/graph/<path:filename>")
def graph_static(filename):
    return send_from_directory(KB_DIR, filename)


@app.route("/api/health")
def health():
    bm25_ready = os.path.exists(os.path.join(INDEX_DIR, "bm25_index.pkl"))
    vec_ready = vec_store.stats().get("ready", False)
    status = "ok" if (bm25_ready and vec_ready) else "index_missing"
    return jsonify({"status": status, "bm25_ready": bm25_ready, "vec_ready": vec_ready,
                    "kb_root": KB_ROOT, "index_dir": INDEX_DIR,
                    # 端口与配置来源：运维可直接据此确认 .env 是否生效，
                    # 无需登录服务器翻 systemd unit
                    "host": os.environ.get("KB_API_HOST", "0.0.0.0"),
                    "port": int(os.environ.get("KB_API_PORT", "8080")),
                    "env_file": str(ENV_FILE) if ENV_FILE else None})


# ===================== 鉴权 API =====================
@app.route("/api/auth/login", methods=["POST"])
def auth_login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    user = admin.authenticate(username, password)
    if not user:
        return jsonify({"error": "用户名或密码错误，或账号已禁用"}), 401
    session["user_id"] = user["id"]
    session.permanent = True
    admin.update_last_login(user["id"])
    return jsonify({"ok": True, "user": _current_user()})


@app.route("/api/auth/logout", methods=["POST"])
def auth_logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/auth/me")
def auth_me():
    u = _current_user()
    if not u:
        return jsonify({"user": None})
    u["permissions"] = list(admin.get_user_permissions(u["id"]))
    return jsonify({"user": u})


# ===================== 知识库 API =====================
@app.route("/api/kb/categories")
@login_required()
def kb_categories():
    perms = set(admin.get_user_permissions(session.get("user_id")))
    cats = admin.list_categories(only_enabled=True)
    # 静默过滤无浏览(view)权限的分类节点（含其子树）
    cats = [c for c in cats if admin.check_cat_action(perms, c["name"], "view")]
    counts = kb_store.category_doc_counts()
    # 构建 parent -> children 映射，将子分类文档数聚合到父级
    children_map = {}
    for c in cats:
        children_map.setdefault(c.get("parent_id"), []).append(c)
    direct = {c["name"]: counts.get(c["name"], 0) for c in cats}

    def _agg(cat_id):
        total = direct.get(next((c["name"] for c in cats if c["id"] == cat_id), ""), 0)
        for ch in children_map.get(cat_id, []):
            total += _agg(ch["id"])
        return total

    for c in cats:
        c["doc_count"] = _agg(c["id"]) if children_map.get(c["id"]) else direct.get(c["name"], 0)
    return jsonify({"categories": cats})


@app.route("/api/kb/categories_all")
@login_required()
def kb_categories_all():
    perms = set(admin.get_user_permissions(session.get("user_id")))
    if "kb.category.manage" not in perms and "role.manage" not in perms:
        return jsonify({"error": "权限不足"}), 403
    cats = admin.list_categories(only_enabled=False)
    counts = kb_store.category_doc_counts()
    for c in cats:
        c["doc_count"] = counts.get(c["name"], 0)
    return jsonify({"categories": cats})


@app.route("/api/kb/documents")
@login_required()
def kb_documents():
    perms = set(admin.get_user_permissions(session.get("user_id")))
    category = request.args.get("category")
    q = request.args.get("q", "").strip()
    year = request.args.get("year")
    year = int(year) if year else None
    # 若选择的是父分类，则连同其所有后代分类一起检索
    if category:
        kids = admin.get_category_descendants(category)
        if kids:
            category = [category] + kids
        # 校验所选分类的浏览权限（沿祖先）
        if not admin.check_cat_action(perms, admin.doc_category_to_node(category[0]) if isinstance(category, list) else admin.doc_category_to_node(category), "view"):
            return jsonify({"documents": [], "total": 0, "page": 1, "page_size": 20, "categories": []})
    try:
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 20))
    except ValueError:
        page, page_size = 1, 20

    # 【修复·权限过滤】原实现在拿到结果后过滤 res["documents"]，而 list_documents
    # 返回的键是 "items"，键名不匹配 -> 过滤从未生效，未授权文档直接泄漏给前端。
    # 现改为：先算出「允许浏览的分类集合」，下传给 list_documents 在【分页之前】
    # 完成过滤，保证 total / years / 分页结果三者一致，且翻页不错乱。
    allowed = _allowed_browse_categories(perms)
    res = kb_store.list_documents(category, q, year, page, page_size,
                                  allowed_categories=allowed)
    # 兼容：历史前端可能仍读 documents 键，这里同时提供两个键，指向同一份数据
    res["documents"] = res.get("items", [])
    return jsonify(res)


@app.route("/api/kb/document")
@login_required()
def kb_document():
    perms = set(admin.get_user_permissions(session.get("user_id")))
    doc_id = request.args.get("doc_id", "")
    if not doc_id:
        return jsonify({"error": "缺少 doc_id"}), 400
    doc = kb_store.get_document(doc_id)
    if not doc:
        return jsonify({"error": "文档不存在"}), 404
    if not admin.check_cat_action(perms, doc.get("category"), "view"):
        return jsonify({"error": "无权浏览该分类文档"}), 403
    doc["can_view"] = True
    doc["can_download"] = admin.check_cat_action(perms, doc.get("category"), "download")
    return jsonify({"document": doc})


@app.route("/api/kb/search")
@login_required()
def kb_search():
    perms = set(admin.get_user_permissions(session.get("user_id")))
    if not admin.get_feature("search_enabled", 1):
        return jsonify({"error": "检索功能已关闭"}), 403
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "缺少参数 q"}), 400
    # 域选择：用户可在前端主动选定检索范围（顶层分类名）；留空表示"全部我有权限的"。
    # 越权防护：选定域必须先通过账号授权校验，否则拒绝（防前端篡改越权选域）。
    sel_cat = request.args.get("category", "").strip()
    cat_allow = None
    if sel_cat:
        if not admin.check_cat_action(perms, sel_cat, "search"):
            return jsonify({"error": "无权检索该分类域"}), 403
        # 展开为「选中域 + 其全部后代」的白名单类型名集合，传入 hybrid_search 硬约束
        cat_allow = set([sel_cat] + admin.get_category_descendants(sel_cat))
    try:
        top_k = int(request.args.get("top_k", 20))
        results = kb_search_mod.hybrid_search(q, top_k, categories=cat_allow)
        # 兜底：按分类查询(search)权限过滤（静默过滤无权限类型，防白名单边界遗漏）
        results = [r for r in results
                   if admin.check_cat_action(perms, r.get("category"), "search")]
        return jsonify({"query": q, "count": len(results), "results": results,
                        "scope": sel_cat or "all"})
    except FileNotFoundError as e:
        return jsonify({"error": str(e), "hint": "请先运行 app/rag_build_index.py 构建索引"}), 500
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


# ===================== 知识库管理 API =====================
@app.route("/api/kb/category", methods=["POST"])
@login_required("kb.category.manage")
def kb_cat_create():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "分类名称不能为空"}), 400
    try:
        pid = data.get("parent_id")
        cid = admin.create_category(name, data.get("description", ""),
                                    int(data.get("sort_order", 0)),
                                    int(pid) if pid not in (None, "", "null") else None)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "id": cid})


@app.route("/api/kb/category/<int:cat_id>", methods=["PUT", "DELETE"])
@login_required("kb.category.manage")
def kb_cat_update(cat_id):
    if request.method == "PUT":
        data = request.get_json(silent=True) or {}
        pid = data.get("parent_id")
        admin.update_category(cat_id, data.get("name"), data.get("description"),
                              data.get("sort_order"), data.get("status"),
                              int(pid) if pid not in (None, "", "null") else None)
        return jsonify({"ok": True})
    else:
        try:
            admin.delete_category(cat_id)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"ok": True})


def _cat_depth(cats, cid):
    """返回分类节点在分类树中的深度（顶级=0，其直接子=1，以此类推）。"""
    depth = 0
    by_id = {c["id"]: c for c in cats}
    cur = by_id.get(cid)
    seen = set()
    while cur and cur.get("parent_id") is not None and cid not in seen:
        seen.add(cid)
        cur = by_id.get(cur["parent_id"])
        depth += 1
    return depth


def _auto_classify(filename, text):
    """根据文件名 + 正文自动判定分类（会议纪要 / 管理标准 / 其它）。

    返回分类名字符串；无法确定时返回 None，由调用方回退到用户所选或「未分类」。

    优先级：
      1) 手工规则（覆盖主要业务类型，纪要与标准严格区分，绝不混类）；
      2) 动态兜底：遍历分类树，用节点名核心词（去「数字.」前缀与「类/分类」后缀）
         在文件名+正文前 2000 字中匹配，取层级更深、名称更长的命中项。
    """
    hay = (filename or "") + "\n" + (text or "")[:2000]
    # 1) 手工规则：会议纪要 vs 管理标准 严格分流
    rules = [
        (["总经理办公会", "总经理会", "总办会", "办公会纪要", "总经理会议纪要",
          "总经理办公会议纪要"], "总经理会议纪要"),
        (["专项会议", "专题会议", "专项纪要", "专题纪要"], "专项会议纪要"),
        (["标准化", "管理标准"], "01.标准化类"),
    ]
    for kws, cat in rules:
        if any(kw in hay for kw in kws):
            return cat
    # 2) 动态兜底：分类树节点名核心词匹配
    try:
        cats = admin.list_categories(only_enabled=True)
    except Exception:
        return None
    best = None
    for c in cats:
        name = c.get("name", "") or ""
        if not name:
            continue
        if _re.search(r"\d{4}年度$", name):
            continue  # 跳过「YYYY年度」子分类，避免误归到年度节点
        core = _re.sub(r"^\d+\.", "", name)          # 去 "01."
        core = _re.sub(r"(类|分类|管理)$", "", core)  # 去泛后缀
        if len(core) >= 2 and core in hay:
            score = (_cat_depth(cats, c["id"]), len(name))
            if best is None or score > best[0]:
                best = (score, name)
    return best[1] if best else None


def _minutes_by_filename(name: str):
    """文件名命中会议纪要特征时返回规范分类名，否则 None。

    文件名是判断文档类型最廉价且最可靠的信号，用于分类自纠与上传校验。
    """
    import re as _re_local
    if not name:
        return None
    if _re_local.search(r"(总经理|总办|经理)?\s*办公会", name) or "总经理会议纪要" in name:
        return "总经理会议纪要"
    if _re_local.search(r"(专项|专题)", name) and "纪要" in name:
        return "专项会议纪要"
    if "会议纪要" in name or _re_local.search(r"纪要\s*〔?\s*\[?\d{4}", name):
        return "会议纪要"
    return None


# 分类树 -> 文档大类（用于上传时的一致性校验）
_CAT_FAMILY_STD_PREFIXES = (
    "01.", "02.", "03.", "04.", "05.", "06.", "07.", "08.",
    "09.", "10.", "11.", "12.", "13.", "14.", "15.",
)


def _family_of_category(cat: str):
    """把分类名归到文档大类：minutes（纪要）/ standard（管理标准/规章）/ None（其它）。"""
    if not cat:
        return None
    if "纪要" in cat:
        return "minutes"
    if cat.startswith(_CAT_FAMILY_STD_PREFIXES) or "标准" in cat or "管理标准分类" in cat:
        return "standard"
    if cat == "16.合规管理类":
        return "regulation"
    return None


def _check_category_consistency(fname: str, chosen_cat: str, raw: bytes = None):
    """【上传校验·只报不改】判断用户所选分类与文件实际类型是否冲突。

    返回 (suggested_cat, warning)。
      - 无冲突：(None, None)
      - 冲突：(建议分类, 提示文案)

    设计原则：本函数【只做校验、绝不改写分类】。冲突由前端呈现给用户，
    由用户决定「改用建议分类 / 仍按原分类上传 / 取消上传」。问题在源头
    （用户提交时）解决，而不是后端默默纠正——后端纠正会掩盖分类管理的
    真实问题（如分类树建错、选错习惯等），故严格禁止。

    判定优先级：文件名（强信号、零成本）> 正文嗅探（轻量解码，仅 PDF/DOCX）。
    仅当用户显式选了分类（未选时 chosen_cat 为空，表示交给系统自动识别）才校验。
    """
    if not chosen_cat:
        return None, None

    # 1) 文件名判定
    fn_cat = _minutes_by_filename(fname)
    if fn_cat:
        if _family_of_category(chosen_cat) != "minutes":
            return fn_cat, ("文件名是「会议纪要」，但所选分类为「%s」。"
                            "若按该分类上传，将按管理标准规则提取，会导致纪要正文被切碎。"
                            "建议改为「%s」。" % (chosen_cat, fn_cat))
        return None, None

    # 2) 正文嗅探（仅在文件名无纪要特征时，避免大文件开销）
    if raw:
        try:
            ext = os.path.splitext(fname)[1].lower()
            probe = None
            if ext == ".pdf":
                probe = extract_text._decode_pdf(raw, category=chosen_cat)
            elif ext == ".docx":
                probe = extract_text._extract_docx(raw)
            if probe:
                sniff = extract_text.detect_kind(probe)
                chosen_family = _family_of_category(chosen_cat)
                if sniff == "minutes" and chosen_family != "minutes":
                    return "会议纪要", ("正文内容识别为「会议纪要」，但所选分类为「%s」。"
                                        "建议改为「会议纪要」类，否则提取结果会错乱。" % chosen_cat)
                if sniff in ("standard", "regulation") and chosen_family == "minutes":
                    return "管理标准分类", ("正文内容识别为「管理标准/规章」，但所选分类为「%s」。"
                                            "建议改为管理标准类，否则提取结果会错乱。" % chosen_cat)
        except Exception:  # noqa: BLE001
            pass  # 嗅探失败不阻断上传，仅不提示
    return None, None


@app.route("/api/kb/upload", methods=["POST"])
@login_required("kb.doc.upload")
def kb_upload():
    if not admin.get_feature("upload_enabled", 1):
        return jsonify({"error": "上传功能已关闭"}), 403
    # 支持批量上传：files[] 数组；兼容旧的单文件 file 字段
    files = request.files.getlist("files")
    single = request.files.get("file")
    if not files and single:
        files = [single]
    files = [f for f in files if f and f.filename]
    if not files:
        return jsonify({"error": "未收到文件"}), 400
    req_cat = (request.form.get("category") or "").strip()
    # 安全网：用户若误选了「YYYY年度」年度子节点（它是年份桶、非真实分类），
    # 不能当作分类名落盘（否则会出现 files/2024年度/2024年度 这类畸形路径），
    # 直接回退为按文件名+正文自动识别分类。
    if req_cat and _re.match(r"^\d{4}年度$", req_cat):
        req_cat = ""
    # confirm_category=1：用户已在前端确认「仍按所选分类上传」，此时不再拦截。
    # 这样既保证冲突在源头暴露给用户，又允许用户在知情后自主决定。
    confirm_override = request.form.get("confirm_category") in ("1", "true", "yes")

    results = []
    conflicts = []
    for file in files:
        fname = file.filename or "未命名文档.txt"
        ext = os.path.splitext(fname)[1].lower()
        if ext not in extract_text.ALLOWED_EXT:
            results.append({"filename": fname, "ok": False,
                            "error": "不支持的文件格式: %s（支持 txt/md/csv/docx/xlsx/pptx/pdf）" % ext})
            continue
        raw = file.read()
        # 分类：用户显式选择优先（作为后台 hint，不重分）；否则留空，后台按正文自动识别
        if req_cat:
            cat = req_cat
            cat_explicit = True
        else:
            cat = "未分类"        # 暂占位，后台用正文重分类
            cat_explicit = False
        # 【上传校验·源头拦截】所选分类与文件实际类型冲突时，不落盘、不纠正，
        # 直接返回冲突详情，由前端请用户确认后重新提交（confirm_category=1 表示
        # 用户已确认仍按原分类上传）。这样把问题拦在源头，避免后端默默改写分类。
        if cat_explicit:
            suggested, warn_msg = _check_category_consistency(fname, cat, raw)
            if suggested and not confirm_override:
                conflicts.append({
                    "filename": fname,
                    "chosen_category": cat,
                    "suggested_category": suggested,
                    "warning": warn_msg,
                })
                continue  # 该文件不落盘，等待用户确认
        # 仅落盘原始二进制 + 占位 entry（极快），文本提取与索引重建交给后台线程，
        # 上传接口立即返回，避免大文件 / 全量索引重建阻塞前端。
        try:
            doc_id = kb_store.save_upload_raw(fname, cat, raw)
        except Exception as e:  # noqa: BLE001
            results.append({"filename": fname, "ok": False, "error": "保存失败: %s" % e})
            continue
        _EXTRACT_Q.put({
            "filename": fname,
            "category": cat,
            "doc_id": doc_id,
            "cat_hint": cat if cat_explicit else "",
        })
        results.append({"filename": fname, "ok": True, "doc_id": doc_id,
                        "category": cat if cat_explicit else "(识别中)",
                        "warn": "已保存，后台识别中"})

    # 【源头拦截】存在分类冲突：本次不落盘这些文件，返回 409 + 冲突清单，
    # 由前端弹窗请用户确认（改用建议分类 / 仍按原分类上传 / 取消）。
    if conflicts:
        return jsonify({
            "ok": False,
            "error": "分类与文件内容不符，请确认后再上传",
            "conflicts": conflicts,
            "results": results,     # 本次已成功上传的无冲突文件（可能为空）
        }), 409

    ok_count = sum(1 for r in results if r.get("ok"))
    if ok_count == 0:
        return jsonify({"ok": False, "results": results,
                        "error": "所有文件均处理失败"}), 400

    # 不再同步重建索引：后台 worker 在队列空闲时统一重建 BM25+向量索引，
    # 上传接口立即返回「上传成功」，识别与可检索在后台稍后完成。
    return jsonify({"ok": True, "results": results, "count": ok_count,
                    "note": "文件已保存，文本识别与索引将在后台完成，稍候即可检索"})


# 「年度归档目录」识别：2026 / 2026年度 / 2026 年 这类目录只是归档年份，
# 不是业务分类。zip 建分类树时必须把它们剥离，否则会把「2026年度」建成
# 真实分类，既污染分类树，又使最终分类名取成年度（导致分类校验失效、误判冲突）。
_YEAR_DIR_RE = _re.compile(r"^((?:19|20)\d{2})\s*(?:\u5e74\u5ea6|\u5e74)?$")


def _split_year_segs(path_segs):
    """把目录路径段拆分为 (业务分类段列表, 年度 or None)。

    例：["01.标准化类", "2026年度"] -> (["01.标准化类"], 2026)
        ["会议纪要", "2025"]        -> (["会议纪要"], 2025)
    """
    cat_segs, year = [], None
    for seg in path_segs:
        s = (seg or "").strip()
        m = _YEAR_DIR_RE.match(s)
        if m:
            year = int(m.group(1))
            continue          # 年度层不作为分类
        cat_segs.append(seg)
    return cat_segs, year


def _allowed_browse_categories(perms: set):
    """返回当前账号允许浏览（view）的文档分类集合。

    按「去重后的分类名」逐一判定，而非对每篇文档判定：权限判定本身要连库
    沿祖先链查询，若对 N 篇文档各判一次就是 N 次库查询；改为先取全库出现的
    分类名（通常几十个），判一次缓存复用，开销大幅下降。

    返回集合供 kb_store.list_documents 做「分页前过滤」，保证：
      - 未授权分类的文档一条都不会出现在列表里（而不是点进去才被拦）
      - total / years / 分页切片三者一致，翻页不错乱
    """
    try:
        cats = kb_store.all_category_names()
    except Exception:  # noqa: BLE001
        return None      # 取分类失败时不过滤，交由逐篇判定兜底
    return {c for c in cats if admin.check_cat_action(perms, c, "view")}


def _ensure_category(name: str, parent_id):
    """存在则返回分类 id，不存在则在指定父级下创建。失败返回 None。"""
    try:
        con = sqlite3.connect(admin.DB_PATH, timeout=30)
        con.row_factory = sqlite3.Row
        r = con.execute(
            "SELECT id FROM categories WHERE name=? AND (parent_id IS ? OR parent_id=?)",
            (name, parent_id, parent_id)).fetchone()
        con.close()
        if r:
            return r["id"]
        return admin.create_category(name, parent_id=parent_id)
    except Exception:  # noqa: BLE001
        return None


def _cat_name_by_id(cid):
    try:
        con = sqlite3.connect(admin.DB_PATH, timeout=30)
        con.row_factory = sqlite3.Row
        r = con.execute("SELECT name FROM categories WHERE id=?", (cid,)).fetchone()
        con.close()
        return r["name"] if r else "未分类"
    except Exception:  # noqa: BLE001
        return "未分类"


@app.route("/api/kb/upload-zip", methods=["POST"])
@login_required("kb.doc.upload")
def kb_upload_zip():
    """上传整个目录（打包为 zip）：按 zip 内部文件夹名自动建子类，文件归入对应分类+年份目录。

    与普通文件上传采用完全相同的「两步」机制：
    - 第一步（本接口内、极快）：按 zip 目录建分类树，仅落盘原始二进制 + 占位 entry，
      不提取文本、不重建索引，立即返回「已保存，后台识别中」；
    - 第二步（后台 worker）：与普通上传共用 _EXTRACT_Q，逐个提取文本 + 自动分类 + 索引。

    - 父分类由表单 `parent` 指定（默认「管理标准分类」）；zip 内的每一级目录都建成该父分类下的子类，
      已存在同名子类则复用，不重复创建。
    - 若 zip 根只有一个共同顶层目录（如用户把「管理标准」整体打包），则剥离该层，子类从第二级开始。
    """
    if not admin.get_feature("upload_enabled", 1):
        return jsonify({"error": "上传功能已关闭"}), 403
    f = request.files.get("file") or request.files.get("zip")
    if not f or not f.filename:
        return jsonify({"error": "未收到压缩包"}), 400
    if not f.filename.lower().endswith(".zip"):
        return jsonify({"error": "仅支持 .zip 压缩包"}), 400
    parent_name = (request.form.get("parent") or "管理标准分类").strip()
    parent_id = _ensure_category(parent_name, None)
    if parent_id is None:
        return jsonify({"error": "父分类「%s」不存在且无法创建" % parent_name}), 400

    raw_zip = f.read()
    # confirm_category=1：用户已确认「仍按 zip 目录名分类上传」，跳过拦截
    zip_confirm = request.form.get("confirm_category") in ("1", "true", "yes")
    # conflict_policy：冲突文件的处置策略（仅对未 confirm 的冲突文件生效）
    #   skip    (默认) 跳过冲突文件，其余照常上传，冲突清单回传给用户
    #   suggest       冲突文件改用建议分类上传
    #   keep          冲突文件仍按 zip 目录分类上传（用户已知情）
    _pol = (request.form.get("conflict_policy") or "skip").strip().lower()
    conflict_policy = _pol if _pol in ("skip", "suggest", "keep") else "skip"
    created_cats = []
    results = []
    zip_conflicts = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw_zip))
        bad = zf.testzip()
        if bad is not None:
            return jsonify({"error": "压缩包损坏，坏文件：%s" % bad}), 400
        names = [n for n in zf.namelist()
                 if not n.endswith("/")
                 and not n.startswith("__MACOSX")
                 and "/." not in n]
        if not names:
            return jsonify({"error": "压缩包内没有可处理的文件"}), 400
        # 若所有文件首段目录相同（用户把整个目录打包），剥离该顶层
        # 【修复·顶层剥离条件过宽】原逻辑「所有文件首段目录相同就剥离该层」，
        # 会把真实的子分类层也剥掉：例如 zip 结构为「01.标准化类/2026年度/xx.pdf」
        # 且父分类选「管理标准分类」时，首段恒为「01.标准化类」→ 被剥离 →
        # 文件全部错误地归到父分类「管理标准分类」，丢失子分类信息。
        # 正确条件：仅当顶层目录名 == 所选父分类名（用户把父目录也一起打包了，
        # 属于冗余层）时才剥离；否则该层是真实分类，必须保留。
        top_segs = set(n.split("/")[0] for n in names)
        strip_top = (len(top_segs) == 1 and parent_name in top_segs)

        for n in names:
            parts = n.split("/")
            if strip_top:
                parts = parts[1:]
            if not parts:
                continue
            base = parts[-1]
            ext = os.path.splitext(base)[1].lower()
            # 【修复】先从目录路径中剥离「年度归档层」再建分类。
            # 原实现把 parts[:-1] 整段都当分类建，于是「2026年度」被建成真实分类，
            # 最终分类名也随之取成「2026年度」，造成两个后果：
            #   1) 分类树被「2026年度」这类非业务节点污染；
            #   2) 分类校验按「2026年度」判断类别（_family_of_category 返回 None），
            #      使会议纪要类文件被误判为冲突，进而触发整包拒绝 ——
            #      这正是「上传压缩包被拒」的根因。
            cat_segs, zip_year = _split_year_segs(parts[:-1])
            # 逐级建分类（cat_segs 为剥离年度层后的分类路径）
            cur_parent = parent_id
            path_parts = []
            for seg in cat_segs:
                path_parts.append(seg)
                cid = _ensure_category(seg, cur_parent)
                if cid is None:
                    results.append({"filename": n, "ok": False,
                                    "error": "无法创建/定位分类 %s" % "/".join(path_parts)})
                    cur_parent = None
                    break
                created_cats.append(seg)
                cur_parent = cid
            if cur_parent is None:
                continue
            final_cat_name = _cat_name_by_id(cur_parent)
            if ext not in extract_text.ALLOWED_EXT:
                results.append({"filename": n, "ok": False,
                                "error": "不支持格式 %s（已跳过）" % ext})
                continue
            data = zf.read(n)
            # 【上传校验·源头拦截·按文件处理】
            # zip 是批量入口，一个包常有几十上百个文件。原实现「只要有 1 个文件
            # 冲突就整包返回 409」，既过于严格（一个文件废掉整批），还会造成
            # 数据重复：无冲突文件其实已经落盘，用户按提示「重新上传」就会重复入库。
            # 现改为按文件处理：冲突文件按策略处置，其余文件照常上传，
            # 最后统一在 skipped 清单里告知用户哪几个没传、为什么。
            suggested, zip_warn = _check_category_consistency(base, final_cat_name, data)
            if suggested and not zip_confirm:
                if conflict_policy == "suggest":
                    zip_cat = suggested       # 采纳建议分类上传
                elif conflict_policy == "keep":
                    zip_cat = final_cat_name  # 用户知情，仍按原分类上传
                else:
                    # 默认 skip：跳过该文件（不落盘），其余文件继续
                    zip_conflicts.append({
                        "filename": n,
                        "chosen_category": final_cat_name,
                        "suggested_category": suggested,
                        "warning": zip_warn,
                    })
                    continue
            else:
                zip_cat = final_cat_name
            # 第一步：仅落盘原始二进制 + 占位 entry（与普通上传一致，极快）
            # year 传 zip 目录中的年度层：既保证归档到正确年度目录，
            # 也降低同分类下同名文件发生 doc_id 冲突的概率（配合冲突检测兜底）。
            try:
                doc_id = kb_store.save_upload_raw(base, zip_cat, data, year=zip_year)
            except Exception as e:  # noqa: BLE001
                results.append({"filename": n, "ok": False, "error": "保存失败: %s" % e})
                continue
            # 第二步：入队，后台 worker 与普通上传共用 _EXTRACT_Q 完成提取+分类+索引
            _EXTRACT_Q.put({
                "filename": base,
                "category": zip_cat,
                "doc_id": doc_id,
                "cat_hint": zip_cat,
            })
            results.append({"filename": n, "ok": True, "doc_id": doc_id,
                            "category": zip_cat, "warn": "已保存，后台识别中"})
    except zipfile.BadZipFile:
        return jsonify({"error": "不是有效的 zip 文件"}), 400
    finally:
        pass

    created_cats = sorted(set(created_cats))

    ok_count = sum(1 for r in results if r.get("ok"))

    # 【批量语义】zip 是批量入口，个别文件冲突不再废掉整包：
    # 无冲突文件已正常上传，冲突文件按策略跳过并回传 skipped 清单，
    # 由用户针对这几个文件单独处理（调整目录或单独上传）。
    # 只有「全部失败」才返回错误——避免误导用户重传整包造成重复入库。
    if ok_count == 0 and not zip_conflicts:
        return jsonify({"ok": False, "results": results, "error": "所有文件均处理失败",
                        "created_categories": created_cats}), 400

    failed = [r for r in results if not r.get("ok")]

    # 【数量自检】zip 是批量入口，文件多、易因「同名同大小导致 doc_id 冲突覆盖」
    # 或「格式不支持」等原因少传。这里把「压缩包内文件数 vs 实际入库数」做对账，
    # 差异直接回传并在 note 中提示，避免用户事后才发现数量对不上。
    payload = {"ok": True, "results": results, "count": ok_count,
               "created_categories": created_cats,
               "total_in_zip": len(names),
               "uploaded": ok_count,
               "failed": failed,
               "note": "文件已保存，文本识别与索引将在后台完成，稍候即可检索"}

    notes = []
    if zip_conflicts:
        payload["skipped"] = zip_conflicts
        payload["skipped_count"] = len(zip_conflicts)
        notes.append("%d 个文件因分类与内容不符被跳过（详见 skipped）"
                     % len(zip_conflicts))
    if failed:
        notes.append("%d 个文件处理失败（详见 failed）" % len(failed))

    accounted = ok_count + len(zip_conflicts) + len(failed)
    if accounted != len(names):
        # 有文件既没成功、也没被记为跳过/失败 —— 说明发生了静默覆盖，必须告警
        payload["count_mismatch"] = {
            "in_zip": len(names),
            "accounted": accounted,
            "unaccounted": len(names) - accounted,
        }
        notes.append("注意：压缩包内 %d 个文件，仅 %d 个有明确结果，"
                     "可能有个别文件因重名同大小被覆盖，请核对数量"
                     % (len(names), accounted))

    if notes:
        payload["note"] = "已上传 %d 个文件；%s。" % (ok_count, "；".join(notes))
    elif ok_count != len(names):
        payload["note"] = ("压缩包内 %d 个文件，实际入库 %d 个，请核对。"
                           % (len(names), ok_count))
    return jsonify(payload)


@app.route("/api/kb/document/<doc_id>", methods=["DELETE"])
@login_required("kb.doc.delete")
def kb_doc_delete(doc_id):
    # 仅允许删除用户上传文档
    doc = kb_store.get_document(doc_id)
    if not doc:
        return jsonify({"error": "文档不存在"}), 404
    if doc.get("source") != "upload":
        return jsonify({"error": "原始库文档不可删除，仅可删除上传文档"}), 400
    # 彻底删除：移除原始二进制 + 上传条目 + 关联派生纪要 + 重建检索索引
    # （派生清理在 kb_store.delete_upload 内统一处理，避免派生文件残留）
    ok = kb_store.delete_upload(doc_id)
    if not ok:
        return jsonify({"error": "文档不存在"}), 404
    return jsonify({"ok": True})


@app.route("/api/kb/uploads")
@login_required("kb.upload.manage")
def kb_uploads_list():
    """上传文件管理：列表（支持关键词 q 与分页）。"""
    q = request.args.get("q", "").strip()
    try:
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 50))
    except ValueError:
        page, page_size = 1, 50
    return jsonify(kb_store.list_uploads(q, page, page_size))


@app.route("/api/kb/upload-status")
@login_required("kb.doc.upload")
def kb_upload_status():
    """轻量查询若干上传文档的识别进度（供前端轮询展示后台提取状态）。

    返回 {results: [{doc_id, indexed, category}]}，indexed=1 表示后台已提取文本并
    入索引、可检索；=0 表示仍在后台队列中等待/识别中。
    """
    ids = request.args.get("ids", "").strip()
    doc_ids = [x for x in ids.split(",") if x]
    ups = {u.get("doc_id"): u for u in kb_store._load_uploads()}
    results = []
    for d in doc_ids:
        u = ups.get(d)
        if u:
            results.append({
                "doc_id": d,
                "indexed": bool(u.get("indexed")),
                "category": u.get("category", "未分类"),
            })
        else:
            results.append({"doc_id": d, "indexed": False, "category": None})
    return jsonify({"results": results})


@app.route("/api/kb/document/<doc_id>", methods=["PUT"])
@login_required("kb.upload.manage")
def kb_doc_reclassify(doc_id):
    """调整上传文档的归类分类（上传文件管理用）。"""
    data = request.get_json(silent=True) or {}
    category = (data.get("category") or "").strip()
    if not category:
        return jsonify({"error": "分类不能为空"}), 400
    ok = kb_store.reclassify_upload(doc_id, category)
    if not ok:
        return jsonify({"error": "文档不存在或不可调整"}), 404
    return jsonify({"ok": True})


@app.route("/api/kb/uploads/<doc_id>", methods=["DELETE"])
@login_required("kb.upload.manage")
def kb_upload_delete(doc_id):
    """上传文件管理：删除指定上传文档（软删除，移入回收站可恢复）。"""
    ok = kb_store.soft_delete_upload(doc_id)
    if not ok:
        return jsonify({"error": "文档不存在"}), 404
    return jsonify({"ok": True})


@app.route("/api/kb/uploads/batch", methods=["DELETE"])
@login_required("kb.upload.manage")
def kb_upload_delete_batch():
    """上传文件管理：批量删除上传文档（软删除，移入回收站可恢复）。body: {"doc_ids": [...]}。"""
    data = request.get_json(silent=True) or {}
    doc_ids = data.get("doc_ids")
    if not isinstance(doc_ids, list) or not doc_ids:
        return jsonify({"error": "doc_ids 必须为非空数组"}), 400
    result = kb_store.soft_delete_uploads_batch(doc_ids)
    return jsonify({"ok": True, **result})


# ===================== 回收站 =====================
@app.route("/api/kb/trash")
@login_required("kb.upload.manage")
def kb_trash_list():
    """回收站列表（软删除文档）。"""
    q = request.args.get("q", "").strip()
    try:
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 50))
    except ValueError:
        page, page_size = 1, 50
    return jsonify(kb_store.list_trash(page=page, page_size=page_size, q=q))


@app.route("/api/kb/trash/<doc_id>", methods=["POST"])
@login_required("kb.upload.manage")
def kb_trash_restore(doc_id):
    """从回收站恢复文档。"""
    ok = kb_store.restore_upload(doc_id)
    if not ok:
        return jsonify({"error": "文档不在回收站或不存在"}), 404
    return jsonify({"ok": True})


@app.route("/api/kb/trash/<doc_id>", methods=["DELETE"])
@login_required("kb.upload.manage")
def kb_trash_purge(doc_id):
    """回收站彻底删除文档（不可恢复）。"""
    ok = kb_store.purge_upload(doc_id)
    if not ok:
        return jsonify({"error": "文档不存在"}), 404
    return jsonify({"ok": True})


@app.route("/api/kb/trash/batch", methods=["DELETE"])
@login_required("kb.upload.manage")
def kb_trash_purge_batch():
    """回收站批量彻底删除。body: {"doc_ids": [...]}。"""
    data = request.get_json(silent=True) or {}
    doc_ids = data.get("doc_ids")
    if not isinstance(doc_ids, list) or not doc_ids:
        return jsonify({"error": "doc_ids 必须为非空数组"}), 400
    result = kb_store.purge_uploads_batch(doc_ids)
    return jsonify({"ok": True, **result})


# ===================== 标签 =====================
@app.route("/api/kb/tags")
@login_required()
def kb_tags():
    """标签云：返回全部标签及文档数。"""
    return jsonify({"tags": kb_store.list_tags()})


@app.route("/api/kb/document/<doc_id>/tags", methods=["PUT"])
@login_required("kb.upload.manage")
def kb_doc_tags(doc_id):
    """设置文档标签。body: {"tags": ["标签1", "标签2"]}。"""
    data = request.get_json(silent=True) or {}
    tags = data.get("tags")
    if not isinstance(tags, list):
        return jsonify({"error": "tags 必须为数组"}), 400
    ok = kb_store.set_upload_tags(doc_id, tags)
    if not ok:
        return jsonify({"error": "文档不存在"}), 404
    return jsonify({"ok": True})


@app.route("/api/kb/tag/<tag>/documents")
@login_required()
def kb_tag_docs(tag):
    """按标签返回文档列表。"""
    try:
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 50))
    except ValueError:
        page, page_size = 1, 50
    return jsonify(kb_store.docs_by_tag(tag, page, page_size))


# ===================== 文档在线编辑 =====================
@app.route("/api/kb/document/<doc_id>/text", methods=["PUT"])
@login_required("kb.upload.manage")
def kb_doc_edit_text(doc_id):
    """在线编辑文档提取文本。body: {"text": "..."}。"""
    data = request.get_json(silent=True) or {}
    text = data.get("text")
    if text is None:
        return jsonify({"error": "text 不能为空"}), 400
    ok = kb_store.update_upload_text(doc_id, text)
    if not ok:
        return jsonify({"error": "文档不存在"}), 404
    return jsonify({"ok": True})


# ===================== 系统初始化（清除文档 / 提取内容 / 重建索引） =====================
@app.route("/api/admin/init/clear", methods=["POST"])
@admin_required()
def admin_init_clear():
    """清空全部文档（含回收站）+ 重建空索引。

    【修复·竞态】清除前必须先「中止 + 清空提取队列」，否则后台 worker 可能：
      - 正在提取某篇文档，提取完成后用「清除前读到的旧列表」写回，
        把已删除的文档条目复活（幽灵文档），或重建出含已删文档的索引。
    流程：abort(丢弃在途任务) -> drain(清空队列) -> 等待重建完成 -> 清除。
    """
    data = request.get_json(silent=True) or {}
    include_trash = data.get("include_trash", True)
    try:
        # 1) 中止并清空提取队列，阻断后台写回
        _EXTRACT_ABORT.set()
        dropped = _drain_extract_queue()
        # 2) 等待正在进行的索引重建结束，避免重建线程用到被删数据
        for _ in range(50):        # 最多等 5 秒
            if not _REBUILDING.is_set():
                break
            time.sleep(0.1)
        # 3) 执行清除
        res = kb_store.clear_all_documents(include_trash=bool(include_trash))
        # 4) 清脏标记，避免残留的脏状态触发一次空重建
        _INDEX_DIRTY.clear()
        res["dropped_tasks"] = dropped
        return jsonify({"ok": True, **res})
    except Exception as e:
        return jsonify({"error": "清除文档失败: %s" % e}), 500
    finally:
        _EXTRACT_ABORT.clear()


@app.route("/api/admin/init/extract", methods=["POST"])
@admin_required()
def admin_init_extract():
    """对所有活跃上传文档重新提取文本（后台异步执行，立即返回）。

    仅把每个活跃文档的 doc_id 入队 _EXTRACT_Q，由后台 worker 逐个重提取文本 +
    队列空闲时统一重建索引；接口本身不阻塞，前端可立即关闭弹窗继续操作。
    """
    try:
        docs = list(kb_store.iter_active_uploads())
        # 【修复·竞态】必须先「复位」再「入队」，顺序不可颠倒。
        # 原实现先 put 入队：后台 worker 可能在几毫秒内就取走任务并提取完成、
        # 写回 text 与 indexed=1；随后主线程才执行 mark_extracting(ids)，
        # 而它读的是复位前的旧快照（text 仍为空），写回后会把刚提取好的
        # 文本抹成空串，导致「重新提取后文档反而变空」的诡异现象。
        # 先复位再入队，则 worker 拿到的肯定是复位后的状态，回填顺序正确。
        ids = [d["doc_id"] for d in docs if d.get("doc_id")]
        reset_n = kb_store.mark_extracting(ids)
        queued = 0
        for d in docs:
            if not d.get("doc_id"):
                continue
            # cat_hint 传原分类，保留分类、只更新文本与索引（与上传两步法同源）
            _EXTRACT_Q.put({
                "filename": d.get("filename", d["doc_id"]),
                "category": d.get("category", "未分类"),
                "doc_id": d["doc_id"],
                "cat_hint": d.get("category", "未分类"),
            })
            queued += 1
        return jsonify({
            "ok": True,
            "queued": queued,
            "reset": reset_n,
            "note": "已提交后台重新提取 %d 篇，状态与字数已立即清空，完成后自动重建索引" % queued,
        })
    except Exception as e:
        return jsonify({"error": "提交重新提取失败: %s" % e}), 500


@app.route("/api/admin/init/extract-one", methods=["POST"])
@admin_required()
def admin_init_extract_one():
    """对单个文档重新提取文本（后台异步执行，立即返回）。

    请求体 {"doc_id": "..."}。仅把该文档入队 _EXTRACT_Q，由后台 worker 提取 +
    队列空闲时统一重建索引；接口不阻塞。提取前立即复位该篇状态与字数（indexed=0,
    text=""），使上传管理页即时反映「提取中」，worker 跑完再写回。
    """
    try:
        data = request.get_json(silent=True) or {}
        doc_id = data.get("doc_id")
        if not doc_id:
            return jsonify({"error": "缺少 doc_id"}), 400
        ups = kb_store._load_uploads()
        target = next((u for u in ups if u.get("doc_id") == doc_id and not u.get("deleted")), None)
        if not target:
            return jsonify({"error": "文档不存在或已删除"}), 404
        # 与批量重提取保持一致：先复位再入队，避免 worker 先跑完写回 text、
        # 随后 mark_extracting 用旧快照覆盖，把刚提取的文本抹成空串。
        reset_n = kb_store.mark_extracting([doc_id])
        _EXTRACT_Q.put({
            "filename": target.get("filename", doc_id),
            "category": target.get("category", "未分类"),
            "doc_id": doc_id,
            "cat_hint": target.get("category", "未分类"),
        })
        return jsonify({
            "ok": True,
            "doc_id": doc_id,
            "reset": reset_n,
            "note": "已提交后台重新提取 1 篇，状态与字数已立即清空，完成后自动重建索引",
        })
    except Exception as e:
        return jsonify({"error": "提交单篇提取失败: %s" % e}), 500


@app.route("/api/admin/init/clear-extract", methods=["POST"])
@admin_required()
def admin_init_clear_extract():
    """清空全部文档的提取内容（indexed=0, text=\"\"），保留文件与条目本身。

    用于「仅清除已提取文本、不再重提」的场景（如提取规则升级前先清空以便肉眼核对
    旧版排版问题），上传管理页状态立即变为「未识别」、字数归 0，且不触发后台提取
    或索引重建。需配合前端重新提取才会恢复可检索内容。
    """
    try:
        n = kb_store.clear_extract()
        return jsonify({
            "ok": True,
            "cleared": n,
            "note": "已清空 %d 篇文档的提取内容（状态/字数已复位），可重新提取恢复" % n,
        })
    except Exception as e:
        return jsonify({"error": "清空提取内容失败: %s" % e}), 500


@app.route("/api/admin/init/abort", methods=["POST"])
@admin_required()
def admin_init_abort():
    """中止后台提取：丢弃队列中尚未执行的任务，已提取的保留并重建索引。

    接口立即返回，worker 在下次取任务时检测到中止信号即跳过剩余任务；当前正在
    执行的单篇会在完成后停止（不会中断已开始的 PDF 解析）。中止后保留已成功提取
    的文档文本，并对已提取部分重建 BM25 + 向量索引，保证可检索。
    """
    try:
        _EXTRACT_ABORT.set()          # 通知 worker 丢弃后续任务
        dropped = _drain_extract_queue()  # 立即清空尚未取走的队列
        # 对已经提取成功的部分重建索引（已提取文档可检索，未提取的保持原状）
        rebuilt = _rebuild_indexes()
        # 复位中止信号（worker 空闲时也会再清一次，这里提前清避免影响下次提取）
        _EXTRACT_ABORT.clear()
        return jsonify({
            "ok": True,
            "dropped": dropped,
            "index_rebuilt": rebuilt,
            "note": "已中止后台提取，丢弃队列剩余 %d 篇；已提取文档已重建索引，可正常检索。" % dropped,
        })
    except Exception as e:
        return jsonify({"error": "中止提取失败: %s" % e}), 500


@app.route("/api/admin/init/index", methods=["POST"])
@admin_required()
def admin_init_index():
    """仅重建 BM25 + 向量索引（后台线程异步执行，立即返回，不阻塞页面）。"""
    def _run():
        try:
            kb_store.rebuild_index_only()
        except Exception:
            pass
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({"ok": True, "note": "已提交后台重建索引，可在后台执行期间继续操作，完成后自动生效"})


# ===================== 审计日志 =====================
@app.route("/api/admin/audit")
@login_required("system.manage")
def adm_audit():
    """操作审计日志（分页 + 过滤）。"""
    q = request.args.get("q", "").strip()
    action = request.args.get("action", "").strip() or None
    try:
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 50))
    except ValueError:
        page, page_size = 1, 50
    return jsonify(kb_store.list_audit(page, page_size, action, q))


@app.route("/api/admin/audit/actions")
@login_required("system.manage")
def adm_audit_actions():
    """审计动作类型列表（用于过滤下拉）。"""
    return jsonify({"actions": kb_store.audit_actions()})


# ===================== 门户首页概览 =====================
@app.route("/api/kb/overview")
@login_required()
def kb_overview():
    """门户首页统计概览。"""
    return jsonify(kb_store.kb_overview())


# ===================== 系统管理 API =====================
@app.route("/api/admin/permissions")
@login_required("permission.view")
def adm_perms():
    return jsonify({"permissions": admin.get_permission_catalog()})


@app.route("/api/admin/roles")
@login_required("role.manage")
def adm_roles():
    return jsonify({"roles": admin.list_roles(with_perms=True)})


@app.route("/api/admin/roles", methods=["POST"])
@login_required("role.manage")
def adm_role_create():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "角色名称不能为空"}), 400
    # 与 update_role 一致：使用关键字参数，避免签名调整后位置传参错位
    rid = admin.create_role(name, description=data.get("description", ""),
                            perms=data.get("permissions", []))
    return jsonify({"ok": True, "id": rid})


@app.route("/api/admin/roles/<int:rid>", methods=["PUT", "DELETE"])
@login_required("role.manage")
def adm_role_update(rid):
    if request.method == "PUT":
        data = request.get_json(silent=True) or {}
        try:
            # 【修复·参数错位】此前按位置传参 (rid, name, description, permissions)，
            # 但 update_role 签名是 (role_id, description, perms, name)，
            # 于是 name 收到了 list、description 收到了 name、perms 收到了 description，
            # 执行「UPDATE roles SET name=?」时绑定 list 直接报
            #   Error binding parameter 1: type 'list' is not supported
            # 且一旦侥幸不报错就会把角色名/描述/权限互相写错。
            # 改为关键字参数，杜绝顺序依赖。
            admin.update_role(
                rid,
                name=data.get("name"),
                description=data.get("description"),
                perms=data.get("permissions"),
            )
        except Exception as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"ok": True})
    else:
        try:
            admin.delete_role(rid)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"ok": True})


@app.route("/api/admin/users")
@login_required("user.view")
def adm_users():
    return jsonify({"users": admin.list_users(),
                    "roles": admin.list_roles(with_perms=False)})


@app.route("/api/admin/users", methods=["POST"])
@login_required("user.manage")
def adm_user_create():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify({"error": "用户名与密码不能为空"}), 400
    try:
        # 关键字参数：避免签名调整后 display_name / role_id 位置传参错位
        # （错位会导致用户被创建但无角色，权限全空且不报错）
        uid = admin.create_user(username, password,
                                display_name=data.get("display_name", ""),
                                role_id=data.get("role_id"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "id": uid})


@app.route("/api/admin/users/<int:uid>", methods=["PUT", "DELETE"])
@login_required("user.manage")
def adm_user_update(uid):
    if request.method == "PUT":
        data = request.get_json(silent=True) or {}
        try:
            admin.update_user(uid, data.get("display_name"), data.get("role_id"),
                              data.get("status"), data.get("password"))
        except Exception as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"ok": True})
    else:
        try:
            admin.delete_user(uid)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"ok": True})


@app.route("/api/kb/features")
@login_required()
def kb_features():
    return jsonify({"features": admin.list_features()})


@app.route("/api/admin/features")
@login_required("system.manage")
def adm_features():
    return jsonify({"features": admin.list_features()})


@app.route("/api/admin/features/<key>", methods=["PUT"])
@login_required("system.manage")
def adm_feature_set(key):
    data = request.get_json(silent=True) or {}
    admin.set_feature(key, int(data.get("enabled", 0)))
    return jsonify({"ok": True})


@app.route("/api/admin/reindex", methods=["POST"])
@login_required("system.manage")
def adm_reindex():
    try:
        import rag_build_index
        rag_build_index.build_index()
        vec_store.rebuild(kb_store.iter_all_documents())
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "stats": vec_store.stats()})


@app.route("/api/admin/vector_stats")
@login_required("system.manage")
def adm_vector_stats():
    # 轻量状态：不触发语义向量模型加载（避免设置页卡顿）
    s = vec_store.stats()
    return jsonify({
        "ready": s.get("ready"),
        "loaded": s.get("loaded", False),
        "chunk_count": s.get("chunks"),
        "doc_count": s.get("docs"),
        "embedder": s.get("embedder"),
        "dim": s.get("dim"),
    })


@app.route("/api/admin/stats")
@login_required("system.manage")
def adm_stats():
    index_ready = os.path.exists(os.path.join(INDEX_DIR, "bm25_index.pkl"))
    counts = kb_store.category_doc_counts()
    total_docs = sum(counts.values())
    users = admin.list_users()
    feats = admin.list_features()
    return jsonify({
        "index_ready": index_ready,
        "total_documents": total_docs,
        "category_count": len(counts),
        "user_count": len(users),
        "active_users": sum(1 for u in users if u["status"] == 1),
        "features": feats,
        "kb_root": KB_ROOT,
    })


@app.route("/api/admin/uploads/check")
@login_required("system.manage")
def adm_uploads_check():
    """诊断：列出所有『原文件不存在』的上传文档，便于定位/修复存储问题。"""
    missing = kb_store.check_upload_storage()
    return jsonify({"missing": missing, "missing_count": len(missing)})


# ===================== 系统名称 / 版本 =====================
# 系统名称：可编辑，持久化于 config/system_settings.json（仅此一项需持久化）。
# 版本号：由 git 提交次数派生（每次 git 更新即重新定义），不持久化。
#   规则：基准 1.0.0 对应 0 次提交；每位满 10 进位 -> 1.{commits//10}.{commits%10}
#   例如 55 次提交 -> 1.5.5。
_SYSTEM_SETTINGS_FILE = os.path.join(KB_ROOT, "config", "system_settings.json")
_DEFAULT_SYSTEM_NAME = "OA-AI 知识库"
_GIT_COMMITS_CACHE = {"value": None}


def _read_system_name():
    try:
        with open(_SYSTEM_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        name = (data.get("system_name") or "").strip()
        return name or _DEFAULT_SYSTEM_NAME
    except Exception:  # noqa: BLE001
        return _DEFAULT_SYSTEM_NAME


def _write_system_name(name):
    os.makedirs(os.path.dirname(_SYSTEM_SETTINGS_FILE), exist_ok=True)
    with open(_SYSTEM_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump({"system_name": name}, f, ensure_ascii=False, indent=2)


def _git_commits():
    """返回当前部署仓库的 git 提交总数（进程级缓存，重启后随 git 更新重算）。"""
    if _GIT_COMMITS_CACHE["value"] is not None:
        return _GIT_COMMITS_CACHE["value"]
    try:
        out = subprocess.check_output(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=KB_ROOT, stderr=subprocess.DEVNULL,
        ).decode("utf-8", "ignore").strip()
        n = int(out)
    except Exception:  # noqa: BLE001
        n = 0
    _GIT_COMMITS_CACHE["value"] = n
    return n


def _compute_version(commits):
    return "1.%d.%d" % (commits // 10, commits % 10)


@app.route("/api/system/info")
@login_required()
def sys_info():
    commits = _git_commits()
    return jsonify({
        "system_name": _read_system_name(),
        "version": _compute_version(commits),
        "commits": commits,
    })


@app.route("/api/system/info", methods=["PUT"])
@login_required("system.manage")
def sys_info_set():
    data = request.get_json(silent=True) or {}
    name = (data.get("system_name") or "").strip()
    if not name:
        return jsonify({"error": "系统名称不能为空"}), 400
    if len(name) > 60:
        return jsonify({"error": "系统名称过长（≤60字符）"}), 400
    _write_system_name(name)
    return jsonify({"system_name": name, "version": _compute_version(_git_commits())})


# ===================== 会议纪要二次生成 API =====================
@app.route("/api/derived/list")
@login_required("derived.manage")
def derived_list():
    source_doc_id = request.args.get("source_doc_id")
    items = derived_store.list_derived(source_doc_id)
    # 关联信息：原版是否存在、二次生成 PDF 是否已生成、年代，便于「分别预览」与管理
    for d in items:
        src_id = d.get("source_doc_id")
        _, _ = (None, None)
        d["has_source_pdf"] = False
        d["source_missing"] = False
        if src_id:
            try:
                p, _ = kb_store.get_upload_binary(src_id)
                d["has_source_pdf"] = bool(p)
                d["source_missing"] = not bool(p)
            except Exception:  # noqa: BLE001
                d["source_missing"] = True
        d["has_pdf"] = bool(derived_store.get_cached_pdf(d))
        d["year"] = derived_store._derived_year(d)
    return jsonify({"items": items})


@app.route("/api/derived/<derived_id>")
@login_required("derived.manage")
def derived_get(derived_id):
    d = derived_store.get_derived(derived_id)
    if not d:
        return jsonify({"error": "衍生版本不存在"}), 404
    return jsonify({"derived": d})


@app.route("/api/derived/parse", methods=["POST"])
@login_required("derived.manage")
def derived_parse():
    """将会议纪要正文按标准模板解析为结构化字段，供前端按议题粒度截取。"""
    data = request.get_json(silent=True) or {}
    text = data.get("text") or ""
    struct = derived_store.parse_minutes(text)
    return jsonify({"ok": True, "struct": struct})


@app.route("/api/derived", methods=["POST"])
@login_required("derived.manage")
def derived_create():
    data = request.get_json(silent=True) or {}
    tpl = data.get("template")
    content = (data.get("content") or "").strip()
    # 模板模式由后端按模板重算正文；非模板模式必须有正文
    if not (tpl and isinstance(tpl, dict) and tpl.get("structured")) and not content:
        return jsonify({"error": "二次生成内容不能为空"}), 400
    if not data.get("source_doc_id"):
        return jsonify({"error": "缺少来源纪要标识"}), 400
    data = dict(data)
    u = _current_user()
    data["created_by"] = u["username"] if u else ""
    d = derived_store.create_derived(data)
    return jsonify({"ok": True, "derived": d})


@app.route("/api/derived/<derived_id>", methods=["PUT"])
@login_required("derived.manage")
def derived_update(derived_id):
    data = request.get_json(silent=True) or {}
    d = derived_store.update_derived(derived_id, data)
    if not d:
        return jsonify({"error": "衍生版本不存在"}), 404
    return jsonify({"ok": True, "derived": d})


@app.route("/api/derived/<derived_id>", methods=["DELETE"])
@login_required("derived.manage")
def derived_delete(derived_id):
    ok = derived_store.delete_derived(derived_id)
    if not ok:
        return jsonify({"error": "衍生版本不存在"}), 404
    return jsonify({"ok": True})


def _serve_derived_pdf_bytes(derived_id):
    """生成（或读取缓存的）二次生成 PDF 字节；返回 (derived, data, error)。

    data 为 None 时：derived 为 None 表示版本不存在(404)，否则为生成失败(500)。
    命中缓存或新生成成功都会把 PDF 持久化到 uploads/derived/<年代>/<id>.pdf，
    与原始上传 PDF 物理隔离、可分别管理与关联。
    """
    import urllib.parse  # noqa: F401
    d = derived_store.get_derived(derived_id)
    if not d:
        return None, None, "衍生版本不存在"
    cached = derived_store.get_cached_pdf(d)
    if cached:
        return d, cached, None
    try:
        data = pdf_make.build_derived_pdf(d)
    except Exception as e:  # noqa: BLE001
        return d, None, "PDF 生成失败: %s" % e
    # 持久化（失败不致命，仍返回本次生成的字节）
    try:
        derived_store.save_derived_pdf(d, data)
    except Exception:  # noqa: BLE001
        pass
    return d, data, None


@app.route("/api/derived/<derived_id>/pdf")
@login_required("derived.manage")
def derived_pdf(derived_id):
    """将衍生版本生成正式的 PDF 文件并流式下载（基于原始会议纪要二次生成）。

    首次生成会持久化到 uploads/derived/<年代>/<id>.pdf，后续优先复用缓存，
    与原始上传 PDF 分别存储、互可关联。
    """
    import urllib.parse
    from flask import Response
    d, data, err = _serve_derived_pdf_bytes(derived_id)
    if data is None:
        return jsonify({"error": err or "衍生版本不存在"}), (404 if d is None else 500)
    fname = (d.get("title") or "二次生成会议纪要") + ".pdf"
    ascii_fallback = "derived_minutes.pdf"
    encoded = urllib.parse.quote(fname)
    disp = "attachment; filename=%s; filename*=UTF-8''%s" % (ascii_fallback, encoded)
    return Response(data, mimetype="application/pdf", headers={"Content-Disposition": disp})


@app.route("/api/derived/<derived_id>/source-pdf")
@login_required("derived.manage")
def derived_source_pdf(derived_id):
    """预览/下载该衍生版本所关联的**原版 PDF**（二次生成前的来源文件）。

    使『二次生成后，可分别预览原 pdf 文件和生成的二次 pdf 文件』：
    原版走此接口、二次生成走 /pdf 或 /pdf-preview。
    """
    import urllib.parse
    from flask import Response
    d = derived_store.get_derived(derived_id)
    if not d:
        return jsonify({"error": "衍生版本不存在"}), 404
    src_id = d.get("source_doc_id")
    if not src_id:
        return jsonify({"error": "该衍生版本未关联任何来源原版文件"}), 404
    path, mimetype = kb_store.get_upload_binary(src_id)
    if not path:
        return jsonify({"error": "原文件不存在，无法预览（来源原版 PDF 可能未上传或已被删除）",
                        "source_doc_id": src_id}), 404
    with open(path, "rb") as f:
        data = f.read()
    doc = kb_store.get_document(src_id) or {}
    fname = (doc.get("filename") or d.get("source_title") or "原版文档") + os.path.splitext(path)[1]
    encoded = urllib.parse.quote(fname)
    inline = request.args.get("inline") == "1"
    disp = ("inline; " if inline else "attachment; ")
    disp += "filename*=UTF-8''%s" % encoded
    return Response(data, mimetype=mimetype or "application/octet-stream",
                    headers={"Content-Disposition": disp})


@app.route("/api/derived/<derived_id>/lineage")
@login_required("derived.manage")
def derived_lineage(derived_id):
    """返回衍生版本的父子血缘：来源纪要(原版)、祖先链、下游子版本。"""
    lin = derived_store.lineage(derived_id)
    if not lin:
        return jsonify({"error": "衍生版本不存在"}), 404
    return jsonify({"lineage": lin})


@app.route("/api/kb/document/<doc_id>/pdf")
@login_required()
def kb_doc_pdf(doc_id):
    """文档预览/下载。

    优先返回用户上传的**原始文件**（如原版 PDF）直接内联预览；
    仅当没有原始二进制（原始库文档 / 纯文本文档）时，才回退为
    从抽取文本重新排版生成的 PDF，避免用“文本生成的 PDF”冒充原文件。
    """
    import urllib.parse
    from flask import Response
    perms = set(admin.get_user_permissions(session.get("user_id")))
    doc = kb_store.get_document(doc_id)
    if not doc:
        return jsonify({"error": "文档不存在"}), 404
    if not admin.check_cat_action(perms, doc.get("category"), "view"):
        return jsonify({"error": "无权浏览该分类文档"}), 403
    # 下载（非内联预览）需要 download 权限；内联预览仅需 view
    if request.args.get("inline") != "1" and not admin.check_cat_action(perms, doc.get("category"), "download"):
        return jsonify({"error": "无权下载该分类文档（无下载权限）"}), 403
    # 优先预览原始上传文件
    path, mimetype = kb_store.get_upload_binary(doc_id)
    if path:
        with open(path, "rb") as f:
            data = f.read()
        fname = doc.get("filename") or "document"
        encoded = urllib.parse.quote(fname)
        inline = request.args.get("inline") == "1"
        disp = ("inline; " if inline else "attachment; ")
        disp += "filename*=UTF-8''%s" % encoded
        return Response(data, mimetype=mimetype or "application/octet-stream",
                        headers={"Content-Disposition": disp})
    # 无原始二进制：回退为从文本生成 PDF（原始库文档等）
    try:
        data = pdf_make.build_source_pdf(doc)
    except FileNotFoundError as e:
        return jsonify({"error": "PDF 生成失败: %s" % e}), 500
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": "PDF 生成失败: %s" % e}), 500
    fname = ((doc.get("label") or doc.get("filename") or "文档") + ".pdf")
    ascii_fallback = "document.pdf"
    encoded = urllib.parse.quote(fname)
    inline = request.args.get("inline") == "1"
    disp = ("inline; " if inline else "attachment; ")
    disp += "filename=%s; filename*=UTF-8''%s" % (ascii_fallback, encoded)
    return Response(data, mimetype="application/pdf", headers={"Content-Disposition": disp})


# 衍生 PDF 支持在线预览（?inline=1 时内联而非下载）
@app.route("/api/derived/<derived_id>/pdf-preview")
@login_required("derived.manage")
def derived_pdf_preview(derived_id):
    """衍生 PDF 内联预览（与 /pdf 下载区分，便于前端 <iframe> 嵌入）。

    同样走持久化逻辑：缓存命中直接返回，否则生成并落盘。
    """
    import urllib.parse
    from flask import Response
    d, data, err = _serve_derived_pdf_bytes(derived_id)
    if data is None:
        return jsonify({"error": err or "衍生版本不存在"}), (404 if d is None else 500)
    fname = (d.get("title") or "二次生成会议纪要") + ".pdf"
    ascii_fallback = "derived_minutes.pdf"
    encoded = urllib.parse.quote(fname)
    disp = "inline; filename=%s; filename*=UTF-8''%s" % (ascii_fallback, encoded)
    return Response(data, mimetype="application/pdf", headers={"Content-Disposition": disp})


# 保留旧版检索 API（兼容企业微信/外部调用）
@app.route("/api/query")
def api_query():
    if not admin.get_feature("api_public", 0):
        u = _current_user()
        if not u:
            return jsonify({"error": "需要登录或开启开放检索"}), 401
        perms = set(admin.get_user_permissions(u["id"]))
        if not any(admin.check_cat_action(perms, c["name"], "search")
                   for c in admin.list_categories(only_enabled=False)):
            return jsonify({"error": "需要登录或开启开放检索"}), 401
    else:
        perms = set(admin.get_user_permissions(session.get("user_id"))) if session.get("user_id") else set()
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "缺少参数 q"}), 400
    sel_cat = request.args.get("category", "").strip()
    cat_allow = None
    if sel_cat:
        if perms and not admin.check_cat_action(perms, sel_cat, "search"):
            return jsonify({"error": "无权检索该分类域"}), 403
        cat_allow = set([sel_cat] + admin.get_category_descendants(sel_cat))
    try:
        top_k = int(request.args.get("top_k", 20))
        results = kb_search_mod.hybrid_search(q, top_k, categories=cat_allow)
        # 按分类查询(search)权限过滤（静默过滤无权限类型）
        if perms:
            results = [r for r in results
                       if admin.check_cat_action(perms, r.get("category"), "search")]
        return jsonify({"query": q, "count": len(results), "results": results,
                        "scope": sel_cat or "all"})
    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": str(e)}), 500


def _seed_categories_from_source():
    """首次启动时把 raw_data_full.json 的分类注入管理库。"""
    try:
        raw_path = os.path.join(KB_DIR, "raw_data_full.json")
        if os.path.exists(raw_path):
            with open(raw_path, encoding="utf-8") as f:
                data = json.load(f)
            names = list(data.get("categories", {}).keys())
            if names:
                admin.seed_categories(names)
    except Exception as e:  # noqa: BLE001
        print("[警告] 分类种子注入失败:", e)


# ===================== 对话式智能问答 =====================
def _build_chat_prompt(question: str, contexts: list, scope_names: list) -> str:
    """构造系统指令：强调边界、引用、汇报式中文回答。"""
    scope_desc = "、".join(scope_names) if scope_names else "全部知识库"
    ctx_block = "\n\n---\n\n".join(
        "【文档《%s》（分类：%s）】\n%s" % (c["filename"], c.get("category") or "—", c.get("content") or c.get("text") or "")
        for c in contexts
    ) or "（无相关文档）"
    system = (
        "你是企业知识库智能分析助手，负责结合下方【参考文档】回答用户问题，"
        "并给出有洞察的分析与总结。\n\n"
        "【对话范围边界】\n"
        "你当前仅被授权依据「%s」相关文档作答。\n"
        "若用户问题明显超出该范围（涉及其他分类或未收录内容），请明确说明："
        "『该问题超出我当前可对话的知识范围（仅限%s），无法作答。』\n\n"
        "【核心原则·忠实但不复读】\n"
        "1. 事实层面必须忠实：引用【参考文档】中的条款、数据、流程时，"
        "不得编造文档未提及的具体内容（如虚构条款号、数值、标准条目）。\n"
        "2. 分析层面应当主动：这是重点——你【需要且应当】在忠实原文事实的基础上，"
        "进行归纳、对比、提炼要点、识别风险与关联、给出判断。"
        "严禁只是把原文片段逐条罗列复制（那是复读机，用户不需要）。\n"
        "3. 区分「事实」与「推断」：文档写明的作为事实并标注出处；"
        "你基于事实做的推断或判断，请显式说明（如「由此可推断」「建议关注」），"
        "让用户能分辨哪句是原文、哪句是你的分析。\n"
        "4. 资料不足时如实说明：『依据现有资料不足以得出确切结论』，"
        "并说明还缺少哪方面的资料，不要臆测。\n\n"
        "【回答规范·汇报式分析】\n"
        "用简体中文作答，按问题复杂度选用结构（不必机械套用四段）：\n"
        "一、结论先行：先用 1-3 句话直接回答用户的问题（最重要，放最前）。\n"
        "二、依据与事实：引用参考文档的具体条款/数据/流程，注明来源文档名。"
        "必要时用表格对比多个文档的差异。\n"
        "三、分析洞察：归纳共性、对比差异、指出风险点/例外情况/关联影响。\n"
        "四、行动建议：给出可执行的下一步建议（若问题涉及操作）。\n"
        "简单问题可合并简化，不要为凑结构而冗长。\n\n"
        "【引用要求】\n"
        "凡涉及具体事实、数据、条款，必须标注出处文档名（如：《XXX标准》）。\n\n"
        "下方为本次检索到的参考文档（已限定在你被授权的范围内）：\n%s"
        % (scope_desc, scope_desc, ctx_block)
    )
    return system


def _retrieve_for_chat(question: str, perms: set, top_k: int = 4, category: str = None) -> list:
    """检索并按对话分类权限（search）过滤；返回 top_k 个命中文档（含 text/regions）。

    category: 用户前端主动选定的检索域（顶层类型名）。若提供，先校验授权，再展开为
        白名单（含后代）硬约束召回；None 表示"全部我有权限的"（仍受账号权限过滤）。
    """
    cat_allow = None
    if category:
        if not admin.check_cat_action(perms, category, "search"):
            return []
        cat_allow = set([category] + admin.get_category_descendants(category))
    raw = kb_search_mod.hybrid_search(question, top_k=top_k * 4, categories=cat_allow)
    filtered = [r for r in raw if admin.check_cat_action(perms, r.get("category"), "search")]
    return filtered[:top_k]


def _chat_scope_names(perms: set) -> list:
    """返回当前用户拥有 search 权限的顶层类型名（经反别名映射回文档类型名）。"""
    cats = admin.list_categories(only_enabled=False)
    tops = [c for c in cats if c.get("parent_id") is None]
    scope = []
    for t in tops:
        # 该顶层或其任一后代有 search 权限，即视为可对话类型
        subtree = [t["name"]] + (admin.get_category_descendants(t["name"]) or [])
        if any(admin.check_cat_action(perms, n, "search") for n in subtree):
            # 反别名：节点名 → 文档类型名
            label = t["name"]
            for k, v in admin.TYPE_ALIASES.items():
                if v == label:
                    label = k
                    break
            scope.append(label)
    return scope


@app.route("/api/kb/chat/scope")
@login_required()
def kb_chat_scope():
    """返回当前用户可对话的范围（类型名列表），供前端展示边界提示。"""
    perms = set(admin.get_user_permissions(session.get("user_id")))
    return jsonify({"domains": _chat_scope_names(perms)})


@app.route("/api/kb/accessible-categories")
@login_required()
def kb_accessible_categories():
    """返回当前用户被授权的顶层分类（供前端检索/对话域切换下拉）。

    每项含：node（分类树节点名，如"管理标准分类"）、label（文档类型名，如"管理标准"）、
    has_search（是否有查询权限）。前端据此渲染下拉选项；默认项"全部"由前端自行加。
    """
    perms = set(admin.get_user_permissions(session.get("user_id")))
    cats = admin.list_categories(only_enabled=False)
    tops = [c for c in cats if c.get("parent_id") is None]
    out = []
    for t in tops:
        subtree = [t["name"]] + (admin.get_category_descendants(t["name"]) or [])
        has_search = any(admin.check_cat_action(perms, n, "search") for n in subtree)
        if not has_search:
            continue
        label = t["name"]
        for k, v in admin.TYPE_ALIASES.items():
            if v == label:
                label = k
                break
        out.append({"node": t["name"], "label": label, "has_search": True})
    return jsonify({"categories": out})


@app.route("/api/kb/chat/sessions", methods=["GET", "POST"])
@login_required()
def kb_chat_sessions():
    """列出 / 新建会话。"""
    uid = session.get("user_id")
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        title = (data.get("title") or "新对话").strip()[:80] or "新对话"
        sid = chat_store.create_session(uid, title)
        return jsonify({"ok": True, "session_id": sid})
    return jsonify({"sessions": chat_store.list_sessions(uid)})


@app.route("/api/kb/chat/session/<int:sid>", methods=["GET", "DELETE"])
@login_required()
def kb_chat_session(sid):
    """获取某会话消息 / 删除会话（自定义删除）。"""
    uid = session.get("user_id")
    if not chat_store.get_session(sid, uid):
        return jsonify({"error": "会话不存在"}), 404
    if request.method == "DELETE":
        chat_store.delete_session(sid, uid)
        return jsonify({"ok": True})
    return jsonify({"messages": chat_store.list_messages(sid)})


@app.route("/api/kb/chat/session/<int:sid>/rename", methods=["POST"])
@login_required()
def kb_chat_session_rename(sid):
    """重命名会话。"""
    uid = session.get("user_id")
    if not chat_store.get_session(sid, uid):
        return jsonify({"error": "会话不存在"}), 404
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()[:80]
    if not title:
        return jsonify({"error": "标题不能为空"}), 400
    chat_store.rename_session(sid, uid, title)
    return jsonify({"ok": True})


@app.route("/api/kb/chat", methods=["POST"])
@login_required()
def kb_chat():
    """发送一条消息并获取智能回答（支持多轮，会话长期保存）。

    入参：{ session_id?, question, top_k? }
      - 不传 session_id 则自动新建会话。
      - 返回 { session_id, answer, refs, scope }
    """
    if not admin.get_feature("chat_enabled", 1):
        return jsonify({"error": "对话功能已关闭"}), 403
    if not llm.is_configured():
        return jsonify({"error": "LLM 未配置（MINIMAX_API_KEY）"}), 503

    uid = session.get("user_id")
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "缺少参数 question"}), 400
    # 参考文档数量：由 5 提到 8。MiniMax-M2.5 具备 20 万 token 上下文，
    # 足以容纳；更多资料 = 更完整的分析依据（原来只给 4-5 篇，容易「资料不足」）。
    top_k = int(data.get("top_k", 8))
    top_k = max(1, min(16, top_k))

    # 1) 对话边界：按账号分类 search 权限
    perms = set(admin.get_user_permissions(uid))
    scope_names = _chat_scope_names(perms)
    if not scope_names:
        return jsonify({"error": "当前账号无可对话的分类权限（需分类的查询权限）"}), 403

    # 1b) 域选择：用户可前端主动选定对话范围（顶层类型名）；留空 = 全部授权域。
    #     越权防护：选定域必须先通过账号授权校验（防前端篡改越权选域）。
    sel_cat = (data.get("category") or "").strip()
    if sel_cat:
        if not admin.check_cat_action(perms, sel_cat, "search"):
            return jsonify({"error": "无权在该分类域对话"}), 403

    # 2) 会话：复用或新建
    sid = data.get("session_id")
    if sid:
        if not chat_store.get_session(int(sid), uid):
            return jsonify({"error": "会话不存在"}), 404
    else:
        first_title = question[:40]
        sid = chat_store.create_session(uid, first_title)

    # 3) 组装多轮历史（仅用户/助手文本，不含系统）
    #    必须先于检索：查询改写需要结合上文才能补全指代。
    history = []
    for m in chat_store.list_messages(sid):
        if m["role"] in ("user", "assistant"):
            history.append({"role": m["role"], "content": m["content"]})
    history.append({"role": "user", "content": question})

    # 4) 检索（按分类 search 权限过滤）
    #    4a) 查询改写：多轮时把「那它的流程呢？」这类省略指代的问题补全，
    #        否则拿半句话去检索必然搜不到。首轮无历史时自动跳过，零开销。
    search_query = llm.rewrite_query(question, history[:-1])
    #    4b) 对比类问题分解：「A 和 B 有什么区别」单轮检索往往只召回一方，
    #        拆成子问题分别检索后合并，保证对比双方资料齐全。
    if llm.looks_like_comparison(search_query):
        sub_queries = llm.decompose_question(search_query)
    else:
        sub_queries = [search_query]
    #    4c) 多子问题检索并按文档去重合并（保留各自最高分）
    hits = []
    seen_doc = set()
    for sq in sub_queries:
        for h in _retrieve_for_chat(sq, perms, top_k=top_k, category=sel_cat or None):
            d = h.get("doc_id")
            if d in seen_doc:
                continue
            seen_doc.add(d)
            hits.append(h)
        if len(hits) >= top_k * 3:  # 合并池上限，避免上下文爆炸
            break
    hits = hits[:top_k * 2]

    # 5) 调 LLM：系统指令 + 历史 + 当前问题
    try:
        answer = llm.chat(
            [{"role": "system", "content": _build_chat_prompt(question, hits, scope_names)}]
            + history
        )
    except Exception as e:
        return jsonify({"error": "LLM 调用失败: %s" % e}), 502

    # 6) 持久化消息
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

    # 7) 审计
    try:
        _row = admin._conn().execute(
            "SELECT username FROM users WHERE id=?", (uid,)).fetchone()
        _uname = _row["username"] if _row else ""
    except Exception:
        _uname = ""
    kb_store.audit_log("kb.chat", target="session:%d" % sid, detail=question,
                       user_id=uid, username=_uname)

    return jsonify({
        "ok": True,
        "session_id": sid,
        "answer": answer,
        "refs": refs,
        "scope": scope_names,
        "selected_scope": sel_cat or "all",
    })


def _warmup_vector_index():
    """后台预热语义向量模型（BGE），消除首次检索的冷启动卡顿。

    BGE 模型首次加载实测 ~20-33s（sentence-transformers 初始化需联网 HEAD 校验
    权重 + CPU 推理准备）。若不在启动时预热，这份开销会落到第一个发起检索的
    用户头上，表现为「第一次检索卡几十秒，之后就快了」。

    两个保护：
      1. 向量索引为空时跳过预热——此时 search.hybrid_search 会直接跳过向量路
         （见 search.py），加载模型纯属浪费，且会拖慢服务启动。
      2. 放进 daemon 后台线程——不阻塞服务启动，服务可立即对外提供 BM25 检索，
         向量能力在预热完成后自动生效。
    """
    def _run():
        try:
            st = vec_store.stats()
            if not st.get("ready") or (st.get("chunks") or 0) <= 0:
                print("[信息] 向量索引为空，跳过 BGE 预热（检索走纯 BM25）")
                return
            t0 = time.time()
            vec_store.get_embedder()          # 加载 BGE 模型（主要开销）
            vec_store.search("预热", top_k=1)  # 触发索引加载 + 一次推理
            print("[信息] BGE 语义向量预热完成，用时 %.1fs" % (time.time() - t0))
        except Exception as e:  # noqa: BLE001
            # 预热失败不影响服务：检索仍可用 BM25
            print("[警告] BGE 预热失败（检索将回退 BM25）: %s" % e)

    threading.Thread(target=_run, daemon=True).start()


if __name__ == "__main__":
    admin.init_db()
    _seed_categories_from_source()
    try:
        admin.ensure_category_hierarchy()
    except Exception as e:  # noqa: BLE001
        print("[警告] 分类层级初始化失败（不影响服务启动）:", e)
    # 启动时将旧版扁平存放的上传文件迁移到「类别/年代」归类布局（幂等）
    try:
        moved = kb_store.migrate_upload_storage()
        if moved:
            print("[信息] 上传原文件归类迁移完成，共移动 %d 个文件" % moved)
    except Exception as e:  # noqa: BLE001
        print("[警告] 上传文件归类迁移失败（不影响服务启动）:", e)
    # 后台预热语义向量模型，避免首次检索冷启动卡顿（非阻塞）
    _warmup_vector_index()
    host = os.environ.get("KB_API_HOST", "0.0.0.0")
    port = int(os.environ.get("KB_API_PORT", "8080"))
    print(f"知识库管理服务启动: http://{host}:{port}/  (KB_ROOT={KB_ROOT})")
    # 明确打印配置来源，便于运维确认端口究竟由 .env 还是外部环境变量决定
    if ENV_FILE:
        print(f"配置文件: {ENV_FILE}  (端口/根目录以该文件为准)")
    else:
        print("配置文件: 未找到 .env，使用环境变量或内置默认值")
        print("          端口来源: KB_API_PORT 环境变量 or 默认 8080")
    app.run(host=host, port=port, debug=False)
