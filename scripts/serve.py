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
import json
import secrets
import functools

from flask import (Flask, request, jsonify, send_from_directory,
                   session, redirect, abort, make_response)

# KB_ROOT：部署根目录。serve.py 位于 <root>/scripts/
KB_ROOT = os.environ.get("KB_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
import sqlite3
import zipfile
import tempfile
import io
import shutil
import extract_text
import pdf_make

app = Flask(__name__, static_folder=None)

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
                    "kb_root": KB_ROOT, "index_dir": INDEX_DIR})


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
@login_required("kb.view")
def kb_categories():
    cats = admin.list_categories(only_enabled=True)
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
@login_required("kb.view")
def kb_categories_all():
    cats = admin.list_categories(only_enabled=False)
    counts = kb_store.category_doc_counts()
    for c in cats:
        c["doc_count"] = counts.get(c["name"], 0)
    return jsonify({"categories": cats})


@app.route("/api/kb/documents")
@login_required("kb.view")
def kb_documents():
    category = request.args.get("category")
    q = request.args.get("q", "").strip()
    year = request.args.get("year")
    year = int(year) if year else None
    # 若选择的是父分类，则连同其所有后代分类一起检索
    if category:
        kids = admin.get_category_descendants(category)
        if kids:
            category = [category] + kids
    try:
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 20))
    except ValueError:
        page, page_size = 1, 20
    res = kb_store.list_documents(category, q, year, page, page_size)
    return jsonify(res)


@app.route("/api/kb/document")
@login_required("kb.view")
def kb_document():
    doc_id = request.args.get("doc_id", "")
    if not doc_id:
        return jsonify({"error": "缺少 doc_id"}), 400
    doc = kb_store.get_document(doc_id)
    if not doc:
        return jsonify({"error": "文档不存在"}), 404
    return jsonify({"document": doc})


@app.route("/api/kb/search")
@login_required("kb.search")
def kb_search():
    if not admin.get_feature("search_enabled", 1):
        return jsonify({"error": "检索功能已关闭"}), 403
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "缺少参数 q"}), 400
    try:
        top_k = int(request.args.get("top_k", 20))
        results = kb_search_mod.hybrid_search(q, top_k)
        return jsonify({"query": q, "count": len(results), "results": results})
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

    results = []
    for file in files:
        fname = file.filename or "未命名文档.txt"
        ext = os.path.splitext(fname)[1].lower()
        if ext not in extract_text.ALLOWED_EXT:
            results.append({"filename": fname, "ok": False,
                            "error": "不支持的文件格式: %s（支持 txt/md/csv/docx/xlsx/pptx/pdf）" % ext})
            continue
        raw = file.read()
        try:
            text, warn = extract_text.extract(raw, fname)
        except Exception as e:  # noqa: BLE001
            results.append({"filename": fname, "ok": False, "error": "解析失败: %s" % e})
            continue
        if not text.strip():
            warn = (("%s；" % warn) if warn else "") + "未提取到文本内容"
        # 分类确定：用户显式选择优先；否则按文件名+正文自动识别（会议纪要/管理标准等，严格分流）
        if req_cat:
            cat = req_cat
        else:
            cat = _auto_classify(fname, text) or "未分类"
        # 按文档类型做专属结构化提取（会议纪要/管理标准/默认），提升检索与预览质量
        text = extract_text.post_process(text, cat)
        # 保存抽取文本用于检索；同时保留原始上传二进制，使「PDF 预览」能直接展示原文件
        doc_id = kb_store.save_upload(fname, cat, text, raw_bytes=raw)
        results.append({"filename": fname, "ok": True, "doc_id": doc_id,
                        "category": cat, "warn": warn})

    ok_count = sum(1 for r in results if r.get("ok"))
    if ok_count == 0:
        return jsonify({"ok": False, "results": results,
                        "error": "所有文件均处理失败"}), 400

    # 统一重建一次索引（含全部新上传文档）：BM25 + 向量双索引，避免逐文件重建
    try:
        import rag_build_index
        rag_build_index.build_index()
        vec_store.rebuild(kb_store.iter_all_documents())
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": True, "results": results,
                        "warn": "文档已保存（%d 个），但重建索引失败: %s" % (ok_count, e)})
    return jsonify({"ok": True, "results": results, "count": ok_count})


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

    - 父分类由表单 `parent` 指定（默认「管理标准分类」）；zip 内的每一级目录都建成该父分类下的子类，
      已存在同名子类则复用，不重复创建。
    - 若 zip 根只有一个共同顶层目录（如用户把「管理标准」整体打包），则剥离该层，子类从第二级开始。
    - 统一重建一次索引。
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
    created_cats = []
    results = []
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
        top_segs = set(n.split("/")[0] for n in names)
        strip_top = (len(top_segs) == 1)

        for n in names:
            parts = n.split("/")
            if strip_top:
                parts = parts[1:]
            if not parts:
                continue
            base = parts[-1]
            ext = os.path.splitext(base)[1].lower()
            # 逐级建分类（parts[:-1] 为分类路径）
            cur_parent = parent_id
            path_parts = []
            for seg in parts[:-1]:
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
            try:
                text, warn = extract_text.extract(data, base, category=final_cat_name)
            except Exception as e:  # noqa: BLE001
                results.append({"filename": n, "ok": False, "error": "解析失败: %s" % e})
                continue
            if not text.strip():
                warn = (("%s；" % warn) if warn else "") + "未提取到文本内容"
            doc_id = kb_store.save_upload(base, final_cat_name, text, raw_bytes=data)
            results.append({"filename": n, "ok": True, "doc_id": doc_id,
                            "category": final_cat_name, "warn": warn})
    except zipfile.BadZipFile:
        return jsonify({"error": "不是有效的 zip 文件"}), 400
    finally:
        pass

    ok_count = sum(1 for r in results if r.get("ok"))
    created_cats = sorted(set(created_cats))
    if ok_count == 0:
        return jsonify({"ok": False, "results": results, "error": "所有文件均处理失败",
                        "created_categories": created_cats}), 400
    try:
        import rag_build_index
        rag_build_index.build_index()
        vec_store.rebuild(kb_store.iter_all_documents())
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": True, "results": results, "count": ok_count,
                        "created_categories": created_cats,
                        "warn": "文档已保存（%d 个），但重建索引失败: %s" % (ok_count, e)})
    return jsonify({"ok": True, "results": results, "count": ok_count,
                    "created_categories": created_cats})


@app.route("/api/kb/document/<doc_id>", methods=["DELETE"])
@login_required("kb.doc.delete")
def kb_doc_delete(doc_id):
    # 仅允许删除用户上传文档
    doc = kb_store.get_document(doc_id)
    if not doc:
        return jsonify({"error": "文档不存在"}), 404
    if doc.get("source") != "upload":
        return jsonify({"error": "原始库文档不可删除，仅可删除上传文档"}), 400
    # 同时删除关联的原始二进制文件，避免孤儿文件
    kb_store.delete_upload_binary(doc_id)
    up_file = os.path.join(KB_DIR, "uploads", "user_documents.json")
    if os.path.exists(up_file):
        ups = json.load(open(up_file, encoding="utf-8"))
        ups = [u for u in ups if u["doc_id"] != doc_id]
        json.dump(ups, open(up_file, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    try:
        import rag_build_index
        rag_build_index.build_index()
        vec_store.rebuild(kb_store.iter_all_documents())
    except Exception:
        pass
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
    """上传文件管理：删除指定上传文档。"""
    ok = kb_store.delete_upload(doc_id)
    if not ok:
        return jsonify({"error": "文档不存在"}), 404
    return jsonify({"ok": True})


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
    rid = admin.create_role(name, data.get("description", ""), data.get("permissions", []))
    return jsonify({"ok": True, "id": rid})


@app.route("/api/admin/roles/<int:rid>", methods=["PUT", "DELETE"])
@login_required("role.manage")
def adm_role_update(rid):
    if request.method == "PUT":
        data = request.get_json(silent=True) or {}
        try:
            admin.update_role(rid, data.get("name"), data.get("description"),
                              data.get("permissions"))
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
        uid = admin.create_user(username, password, data.get("display_name", ""),
                                data.get("role_id"))
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
    return jsonify(vec_store.stats())


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
@login_required("kb.view")
def kb_doc_pdf(doc_id):
    """文档预览/下载。

    优先返回用户上传的**原始文件**（如原版 PDF）直接内联预览；
    仅当没有原始二进制（原始库文档 / 纯文本文档）时，才回退为
    从抽取文本重新排版生成的 PDF，避免用“文本生成的 PDF”冒充原文件。
    """
    import urllib.parse
    from flask import Response
    doc = kb_store.get_document(doc_id)
    if not doc:
        return jsonify({"error": "文档不存在"}), 404
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
        if not u or "kb.search" not in admin.get_user_permissions(u["id"]):
            return jsonify({"error": "需要登录或开启开放检索"}), 401
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "缺少参数 q"}), 400
    try:
        top_k = int(request.args.get("top_k", 20))
        results = kb_search_mod.hybrid_search(q, top_k)
        return jsonify({"query": q, "count": len(results), "results": results})
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
    host = os.environ.get("KB_API_HOST", "0.0.0.0")
    port = int(os.environ.get("KB_API_PORT", "8080"))
    print(f"知识库管理服务启动: http://{host}:{port}/  (KB_ROOT={KB_ROOT})")
    app.run(host=host, port=port, debug=False)
