"""
知识库文档存储层

统一对外提供“全部文档”的清单与详情，数据源包括:
  - knowledge_base/raw_data_full.json        (原始 183 份文档)
  - knowledge_base/uploads/user_documents.json (用户上传文档)
  - knowledge_base/bm25_index/documents_manifest.json (构建索引时生成的清单，含分页信息)

文档详情 (full_text) 由原始文件按 doc_id 取回，前端按 pages 分页展示。
"""
import os
import re
import json
import time
import random
import threading
import hashlib

from extract_text import merge_lines_to_paragraphs as _merge_lines

KB_ROOT = os.environ.get("KB_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KB_DIR = os.path.join(KB_ROOT, "knowledge_base")
RAW_DATA = os.path.join(KB_DIR, "raw_data_full.json")
UPLOAD_DIR = os.path.join(KB_DIR, "uploads")
UPLOAD_FILES_DIR = os.path.join(UPLOAD_DIR, "files")  # 上传原始二进制存放目录
UPLOAD_FILE = os.path.join(UPLOAD_DIR, "user_documents.json")
MANIFEST = os.path.join(KB_DIR, "bm25_index", "documents_manifest.json")

_YEAR_RE = re.compile(r"(?:18|19|20)\d{2}")

# ---------------------------------------------------------------------------
# 并发安全基础设施
#
# 背景（生产事故）：user_documents.json 曾出现「数组套数组」的损坏结构，
# 根因是「多进程/多线程并发读-改-写」同一文件且写盘非原子：
#   进程A 读到旧快照 -> 进程B 写入新数据 -> 进程A 用旧快照覆盖（丢数据/结构错乱）
# 因此这里统一提供：
#   1) _STORE_LOCK：同一进程内所有「读-改-写」必须持锁，保证复合操作原子性；
#   2) _atomic_write_json：先写临时文件再 os.replace，保证落盘原子性
#      （进程崩溃 / 断电不会留下半截 JSON）。
# 注意：本锁只保护单进程内线程。跨进程需依赖部署层保证单实例（见 start.sh / systemd）。
# ---------------------------------------------------------------------------
_STORE_LOCK = threading.RLock()


def store_lock():
    """返回文档存储层的全局可重入锁。

    所有对 user_documents.json 的「读取-修改-写回」复合操作都必须在此锁内完成，
    否则并发场景下会互相覆盖（历史事故：文件被写成 [[...]] 导致上传 500）。
    """
    return _STORE_LOCK


def _atomic_write_json(path: str, data) -> None:
    """原子写 JSON：先写临时文件并 fsync，再 os.replace 覆盖目标。

    保证任意时刻读取该文件都拿到完整合法的 JSON，不会出现半截/损坏内容。

    【健壮性·Windows】两点加固：
      1) 临时文件名加入「进程ID+线程ID+随机数」：原实现仅用 PID，同一进程内
         多线程并发写同一文件时共用同一 tmp 名，会互相覆盖/争抢句柄。
      2) os.replace 带重试：Windows 上杀毒软件、文件索引器、甚至本进程刚关闭的
         句柄未完全释放时，替换会抛 [WinError 5] 拒绝访问。实测 40 篇批量提取
         中有 2 篇因此失败（表现为「提取任务失败: 拒绝访问」）。退避重试即可。
    """
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(
        d, ".tmp_%s.%d.%d.%d" % (os.path.basename(path), os.getpid(),
                                 threading.get_ident() % 100000,
                                 random.randint(1000, 9999)))
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        # 带退避的重试替换
        last_err = None
        for attempt in range(6):
            try:
                os.replace(tmp, path)
                return
            except OSError as e:
                last_err = e
                time.sleep(0.05 * (attempt + 1))
        # 最终仍失败：抛出以便上层记录，而不是静默丢数据
        raise last_err
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def _stable_hash(*parts) -> str:
    """稳定哈希：替代内置 hash()。

    内置 hash() 对 str 带 PYTHONHASHSEED 随机化，同一文件名在不同进程/重启后
    会得到不同值，会导致 doc_id 漂移、归档目录不一致、重复上传产生重复条目。
    这里用 blake2b 保证跨进程、跨重启稳定。
    """
    h = hashlib.blake2b(digest_size=8)
    for p in parts:
        h.update(str(p).encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()

# 上传文件预览时用于返回原始二进制（而不是从文本重新生成 PDF）的 MIME 类型
_EXT_MIME = {
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def mimetype_for_ext(ext: str) -> str:
    return _EXT_MIME.get((ext or "").lower(), "application/octet-stream")


def slugify_category(name: str) -> str:
    """把分类名转为安全的目录名（保留中文/字母数字，其余替换为下划线）。"""
    s = (name or "未分类").strip()
    s = re.sub(r'[\\/:*?"<>|]', "_", s)
    s = re.sub(r"\s+", "_", s)
    return s or "未分类"


def _category_ancestors(name: str) -> list:
    """返回分类名从顶级到自身的祖先链（含自身），用于物理目录层级。

    例：'01.标准化类'      -> ['管理标准分类', '01.标准化类']
        '总经理会议纪要'   -> ['会议纪要', '总经理会议纪要']
    若分类不在库中（如 '未分类'），返回 [slugify(name)]（扁平兼容）。
    """
    try:
        import sqlite3
        import admin
        con = sqlite3.connect(admin.DB_PATH, timeout=30)
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT id, name, parent_id FROM categories").fetchall()
        con.close()
        by_id = {r["id"]: r for r in rows}
        node = next((r for r in rows if r["name"] == name), None)
        if node is None:
            return [slugify_category(name)]
        chain, cur, guard = [], node, 0
        while cur is not None and guard < 20:
            chain.append(cur["name"])
            cur = by_id.get(cur["parent_id"]) if cur["parent_id"] is not None else None
            guard += 1
        chain.reverse()
        return chain or [slugify_category(name)]
    except Exception:
        return [slugify_category(name)]


def category_id_by_name(name: str):
    """按分类名返回其 id（不在库中返回 None）。"""
    try:
        import sqlite3
        import admin
        con = sqlite3.connect(admin.DB_PATH, timeout=30)
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT id FROM categories WHERE name=?", (name,)).fetchone()
        con.close()
        return row["id"] if row else None
    except Exception:
        return None


def category_subtree_names(name: str) -> list:
    """返回以 name 为根的整棵分类子树（含自身）的全部分类名列表。

    用于「对话域边界」：给定一个顶层分类（如『管理标准分类』），
    取其自身及其全部后代子分类名，作为该域允许检索的分类集合。
    """
    try:
        import sqlite3
        import admin
        con = sqlite3.connect(admin.DB_PATH, timeout=30)
        con.row_factory = sqlite3.Row
        rows = con.execute("SELECT id, name, parent_id FROM categories").fetchall()
        con.close()
        by_parent = {}
        by_id = {}
        for r in rows:
            by_id[r["id"]] = r["name"]
            by_parent.setdefault(r["parent_id"], []).append(r["id"])
        root_id = next((r["id"] for r in rows if r["name"] == name), None)
        if root_id is None:
            # 不在库中：当作扁平单节点
            return [name]
        # BFS 收集整棵子树
        out, queue, guard = [], [root_id], 0
        while queue and guard < 200:
            cur = queue.pop(0)
            out.append(by_id.get(cur, name))
            queue.extend(by_parent.get(cur, []))
            guard += 1
        return out
    except Exception:
        return [name]


def _stored_rel_for(category: str, year, doc_id: str, ext: str) -> str:
    """计算『类别/年代』归类落盘相对路径，类别段包含完整祖先链。

    例：('01.标准化类', 2022, 'up_x', '.pdf')
        -> 'files/管理标准分类/01.标准化类/2022年度/up_x.pdf'
    """
    chain = _category_ancestors(category)
    cat_path = "/".join(slugify_category(c) for c in chain)
    year_dir = ("%d年度" % year) if year else "unknown"
    return "files/%s/%s/%s%s" % (cat_path, year_dir, doc_id, ext)


def _resolve_binary_path(u: dict):
    """解析上传文档的原始二进制绝对路径，兼容旧版扁平布局与新版归类布局。

    优先用记录的 stored_path；若缺失或损坏，再回退到旧版扁平文件
    files/<doc_id><ext>，避免『原文件不存在』误报。
    """
    if not u:
        return None
    rel = u.get("stored_path")
    if rel:
        p = os.path.join(UPLOAD_DIR, rel)
        if os.path.exists(p):
            return p
    # 回退：旧版扁平布局
    legacy = os.path.join(UPLOAD_DIR, "files", u["doc_id"] + (u.get("ext") or ""))
    if os.path.exists(legacy):
        return legacy
    return None


def _parse_doc_date(filename: str):
    """从文件名解析文档日期（会议/发文日期），用于会议纪要等按日期倒序排列。

    优先级（用户规则：以文号日期为准，文件名日期辅助）：
      1) 文号日期段：形如 HYJYXSSPFB-20241021-003 / XXX-20241021-001 中的 8 位日期
         （紧跟 '-' 后的 YYYYMMDD，范围 19800101~21001231）；
      2) 文件名中文日期：(2024年10月21日) / 2024年10月21日；
      3) 通用分隔日期：YYYY-MM-DD / YYYY.MM.DD / YYYY/MM/DD；
      4) 仅含 4 位年份（无月日）：按当年 1 月 1 日处理（弱精度，仍可比）。

    返回 (year, month, day) 可比较元组；无法解析返回 None。
    """
    fn = filename or ""
    # 1) 文号日期段：-\d{8}- 或 整体形如 *-YYYYMMDD-*
    m = re.search(r"-(\d{8})(?:-|$)", fn)
    if m:
        s = m.group(1)
        y, mo, d = int(s[:4]), int(s[4:6]), int(s[6:8])
        if 1980 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
            return (y, mo, d)
    # 2) 中文日期：YYYY年M月D日（含半角括号/全角括号）
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})[日号]", fn)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1980 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
            return (y, mo, d)
    # 3) 通用分隔日期
    m = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", fn)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1980 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
            return (y, mo, d)
    # 4) 仅 4 位年份
    yrs = re.findall(r"(?:18|19|20)\d{2}", fn)
    for ys in yrs:
        y = int(ys)
        if 1980 <= y <= 2100:
            return (y, 1, 1)
    return None


def _doc_sort_date_ord(d: dict):
    """会议纪要排序键：有日期按日期倒序（用负数），无日期按入库时间倒序兜底。

    返回用于 list.sort(reverse=True) 的 (日期序数或时间序数, 次级稳定键)。
    约定：用 reverse=True 时，较大的值排前面。
    """
    dt = _parse_doc_date(d.get("filename", ""))
    if dt:
        ord_val = dt[0] * 10000 + dt[1] * 100 + dt[2]
        return (1, ord_val)  # 有日期的组，按 ord_val 大者在前
    # 无日期：入库时间倒序兜底（ISO 字符串字典序即可）
    ca = d.get("created_at") or ""
    return (0, _iso_ord(ca))


def _iso_ord(iso_str: str):
    """把 ISO 时间字符串转成可比较整数（越大越新）；非法返回 0。"""
    if not iso_str:
        return 0
    digits = re.sub(r"\D", "", iso_str)
    return int(digits) if digits else 0


_MEETING_SUBTREE_CACHE = None


def _in_meeting_subtree(cats: list):
    """判断给定分类名集合是否落在『会议纪要分类』子树内（含根及全部子类）。

    用于「会议纪要及其子类」统一按日期倒序排列。结果带模块级缓存，避免
    每次列表查询都查库。
    """
    global _MEETING_SUBTREE_CACHE
    if _MEETING_SUBTREE_CACHE is None:
        try:
            _MEETING_SUBTREE_CACHE = set(category_subtree_names("会议纪要分类"))
        except Exception:
            _MEETING_SUBTREE_CACHE = set()
    if not _MEETING_SUBTREE_CACHE:
        return False
    return any(c in _MEETING_SUBTREE_CACHE for c in cats)


def _extract_year(filename: str, content: str):
    """年份提取：文件名优先，其次正文年份，兜底上传当前年份。

    返回 int（始终有值，便于「类别/年份」目录稳定归类，避免出现 unknown 桶）。
    年份来源优先级：
      1) 文件名中的 4 位年份（靠近「年 / 〔 / ( / -」的优先，排除电话/编号误判）；
      2) 正文中「YYYY年」「〔YYYY〕」「(YYYY)」「-YYYY」等明确年份写法；
      3) 正文前 2000 字内任意合理 4 位年份；
      4) 兜底：上传时的当前年份。
    """
    def _valid(y):
        return 1980 <= y <= 2100

    # 1) 文件名年份：优先靠近「年/〔/(/-」者
    fm = re.findall(r"(?:18|19|20)\d{2}", filename or "")
    if fm:
        # 计算每个候选到最近标记字符的距离，取最近的
        marks = [i for i, ch in enumerate(filename or "") if ch in "年〔(-"]
        def _dist(ystr):
            idx = filename.find(ystr)
            if idx < 0:
                return 10 ** 9
            if not marks:
                return abs(idx - len(filename))
            return min(abs(idx - mk) for mk in marks)
        fm_sorted = sorted(fm, key=_dist)
        for ystr in fm_sorted:
            y = int(ystr)
            if _valid(y):
                return y

    c = content or ""
    head = c[:2000]
    # 2) 明确年份写法：YYYY年 / 〔YYYY〕 / (YYYY) / -YYYY
    m = re.search(r"(?:18|19|20)\d{2}(?=年)", head)
    if m and _valid(int(m.group(0))):
        return int(m.group(0))
    m = re.search(r"[（(〔]((?:19|20)\d{2})[）)〕]", head)
    if m and _valid(int(m.group(1))):
        return int(m.group(1))
    m = re.search(r"-((?:19|20)\d{2})\b", head)
    if m and _valid(int(m.group(1))):
        return int(m.group(1))
    # 3) 任意合理 4 位年份（取第一个）
    for ystr in re.findall(r"(?:18|19|20)\d{2}", head):
        y = int(ystr)
        if _valid(y):
            return y
    # 4) 兜底：上传当前年份
    from datetime import datetime
    return datetime.now().year


def _load_json_safe(path, default, kind="list"):
    """安全读取 JSON 文件：损坏时自动截断恢复为合法结构，避免单文件损坏瘫痪整个功能。

    恢复策略：若整体解析失败，逐对象回退（JSONDecoder.raw_decode），仅保留能解析的前缀；
    原损坏文件备份为 <file>.bad. 供人工核查。无法恢复时返回 default。
    """
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # 类型兜底：防止并发/历史损坏导致「数组套数组」或混入非预期类型
        if kind == "list":
            if isinstance(data, list):
                flat = []
                for x in data:
                    if isinstance(x, list):
                        flat.extend(x)
                    elif isinstance(x, dict):
                        flat.append(x)
                data = flat
            else:
                data = default
        return data
    except (json.JSONDecodeError, ValueError):
        try:
            raw = open(path, "r", encoding="utf-8").read()
            dec = json.JSONDecoder()
            items = []
            s = raw.lstrip()
            while s:
                s = s.lstrip()
                if not s:
                    break
                try:
                    obj, end = dec.raw_decode(s)
                except Exception:
                    break
                if kind == "list":
                    items.append(obj)
                else:
                    items = obj  # dict 场景：取首个合法对象
                    break
                s = s[end:]
            # 备份原文件
            shutil_move = path + ".bad."
            try:
                import shutil as _sh
                _sh.copy(path, shutil_move)
            except Exception:
                pass
            with open(path, "w", encoding="utf-8") as f:
                json.dump(items if kind == "list" else (items or default), f,
                          ensure_ascii=False, indent=2)
            return items if kind == "list" else (items or default)
        except Exception:
            return default


def _load_raw() -> dict:
    if not os.path.exists(RAW_DATA):
        return {"categories": {}, "documents": []}
    return _load_json_safe(RAW_DATA, {"categories": {}, "documents": []}, kind="dict")


def _load_uploads() -> list:
    if not os.path.exists(UPLOAD_FILE):
        return []
    return _load_json_safe(UPLOAD_FILE, [], kind="list")


def _load_manifest() -> dict:
    """manifest: {doc_id: {filename, category, pages, label, source}}"""
    if not os.path.exists(MANIFEST):
        return {}
    return _load_json_safe(MANIFEST, {}, kind="dict")


def _upload_to_doc(u: dict) -> dict:
    """把一条上传记录转换为与 manifest 一致的浏览文档条目。"""
    return {
        "doc_id": u["doc_id"],
        "filename": u.get("filename", u["doc_id"]),
        "category": u.get("category", "未分类"),
        "pages": u.get("pages", 1),
        "label": u.get("label", u.get("filename", u["doc_id"])),
        "source": "upload",
        "year": u.get("year"),
        "stored": bool(_resolve_binary_path(u)),
    }


def _all_browse_docs() -> list:
    """合并「浏览用文档全集」：manifest(或 raw) + 用户上传文档。

    上传文档独立于索引 manifest 持久化在 user_documents.json；即便索引重建
    失败或尚未触发，上传文档也应立即可在知识浏览中查看到。按 doc_id 去重，
    避免索引成功后与 manifest 中的同一条目重复计数。
    """
    manifest = _load_manifest()
    seen = set()
    if manifest:
        docs = list(manifest.values())
    else:
        raw = _load_raw()
        docs = [{"doc_id": d["filename"], "filename": d["filename"], "category": d["category"],
                 "pages": d.get("total_pages", 0), "label": d["filename"], "source": "raw",
                 "year": None}
                for d in raw["documents"]]
    for d in docs:
        seen.add(d.get("doc_id"))
    for u in _load_uploads():
        if u.get("deleted"):
            continue  # 软删除（回收站）文档不在知识浏览中展示
        if u["doc_id"] not in seen:
            docs.append(_upload_to_doc(u))
            seen.add(u["doc_id"])
    return docs


def all_category_names() -> set:
    """返回全部浏览文档中出现过的分类名（去重）。

    供权限过滤使用：按「分类」而非「文档」判定权限，把原本 N 篇文档的
    逐篇数据库查询（每篇都要沿祖先链查一次）降为「去重后的分类数」次，
    开销从 O(文档数) 降到 O(分类数)，通常只有几十个。
    """
    return {d.get("category") for d in _all_browse_docs() if d.get("category")}


def list_documents(category: str = None, q: str = None, year: int = None,
                   page: int = 1, page_size: int = 20,
                   allowed_categories: set = None) -> dict:
    """返回分页文档列表 + 总数 + 可选年份 facet。

    年份 facet(years): 当前筛选条件下出现的全部年份，供前端按年代出按钮。

    allowed_categories: 允许浏览的分类集合（None 表示不过滤）。
      【修复·未授权文档泄漏】权限过滤【必须】在这里、且【必须在分页之前】完成。
      原因有二：
        1) 原实现在路由层对返回结果的 "documents" 键做过滤，而本函数实际返回的
           是 "items" 键 —— 键名对不上，过滤遍历空列表，等于完全没过滤，
           前端直接拿到未授权文档（这正是「秘书能看到管理标准」的根因）。
        2) 即便改对键名，若在【分页之后】过滤，会导致 total 只统计当前页过滤后
           的条数、翻页错乱、每页数量忽多忽少。
      下沉到分页前过滤，则 total / years / 分页切片三者天然一致。
    """
    docs = _all_browse_docs()
    # 权限过滤：分页前执行，保证 total 与分页结果一致
    if allowed_categories is not None:
        docs = [d for d in docs if d.get("category") in allowed_categories]
    # 补全 year 字段
    for d in docs:
        if d.get("year") is None:
            d["year"] = _extract_year(d.get("filename", ""), "")
    if category:
        if isinstance(category, (list, tuple, set)):
            cs = set(category)
            docs = [d for d in docs if d.get("category") in cs]
        else:
            docs = [d for d in docs if d.get("category") == category]
    if year:
        yv = int(year)
        docs = [d for d in docs if d.get("year") == yv]
    if q:
        ql = q.lower()
        docs = [d for d in docs if ql in d.get("filename", "").lower()
                or ql in d.get("label", "").lower() or ql in d.get("category", "").lower()]
    # 年份 facet（在分页前统计，反映当前分类/关键词下的全部可选年份）
    years = sorted({d.get("year") for d in docs if d.get("year")})
    total = len(docs)
    # 会议纪要（含所有子类）：按文档日期倒序（最新在前），有日期优先、无日期按入库时间兜底
    _is_meeting = False
    if category:
        if isinstance(category, (list, tuple, set)):
            _cats = [str(c) for c in category]
            _is_meeting = any("会议纪要" in c for c in _cats) or _in_meeting_subtree(_cats)
        else:
            _cat = str(category)
            _is_meeting = ("会议纪要" in _cat) or _in_meeting_subtree([_cat])
    if _is_meeting:
        docs.sort(key=_doc_sort_date_ord, reverse=True)
    else:
        docs.sort(key=lambda d: (d.get("category", ""), -(d.get("year") or 0), d.get("filename", "")))
    start = (page - 1) * page_size
    page_items = docs[start:start + page_size]
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "years": years,
        "items": page_items,
    }


def get_document(doc_id: str) -> dict or None:
    """返回文档元信息与全文（含 text/year/preview 别名，供前端预览使用）。"""
    manifest = _load_manifest()
    meta = manifest.get(doc_id) if manifest else None
    full_text = ""
    if meta is None:
        # 退回 raw 直接查找
        raw = _load_raw()
        for d in raw["documents"]:
            if d["filename"] == doc_id:
                meta = {"doc_id": doc_id, "filename": d["filename"], "category": d["category"],
                        "pages": d.get("total_pages", 0), "label": d["filename"], "source": "raw",
                        "full_text": d.get("full_text", "")}
                break
        # raw 未命中：再回退到上传记录（含未进入索引的扫描件等）
        if meta is None:
            for u in _load_uploads():
                if u.get("doc_id") == doc_id:
                    meta = {"doc_id": doc_id, "filename": u.get("filename", doc_id),
                            "category": u.get("category", "未分类"),
                            "pages": u.get("pages", 1), "label": u.get("filename", doc_id),
                            "source": "upload", "full_text": u.get("text", "")}
                    break
        if meta is None:
            return None
        full_text = meta.get("full_text", "")
    elif meta.get("source") == "upload":
        ups = {u["doc_id"]: u for u in _load_uploads()}
        u = ups.get(doc_id)
        if u:
            meta = dict(meta)
            full_text = u.get("text", "")
            meta["full_text"] = full_text
    else:
        # manifest / raw 均未命中：直接回退到上传记录（含未进入索引的扫描件等）
        for u in _load_uploads():
            if u.get("doc_id") == doc_id:
                meta = {"doc_id": doc_id, "filename": u.get("filename", doc_id),
                        "category": u.get("category", "未分类"),
                        "pages": u.get("pages", 1), "label": u.get("filename", doc_id),
                        "source": "upload", "full_text": u.get("text", "")}
                full_text = u.get("text", "")
                break
        # raw 文档：从 raw_data_full.json 取全文
        raw = _load_raw()
        for d in raw["documents"]:
            if d["filename"] == meta.get("filename"):
                meta = dict(meta)
                full_text = d.get("full_text", "")
                meta["full_text"] = full_text
                break

    meta = dict(meta)
    # 展示/预览前归一化：把 PDF 逐行抽取产生的『行内换行』合并为自然段落，
    # 避免右侧正文与生成 PDF 出现每行一段、版面混乱的问题（旧数据也一并修复）。
    full_text = "\n\n".join(_merge_lines(full_text.split("\n"))) if full_text else ""
    # 别名与预览字段（修复前端预览读取字段不一致的问题）
    meta["text"] = full_text
    meta["full_text"] = full_text
    meta["year"] = _extract_year(meta.get("filename", ""), full_text)
    meta["preview"] = (full_text or "")[:800]
    # 原文件（原始上传二进制）是否存在，供前端区分「预览原文件」可用性
    if meta.get("source") == "upload":
        _u = next((u for u in _load_uploads() if u.get("doc_id") == doc_id), None)
        meta["stored"] = bool(_resolve_binary_path(_u)) if _u else False
        meta["storage_path"] = _u.get("stored_path") if _u else None
        meta["mimetype"] = _u.get("mimetype") if _u else None
        meta["tags"] = _u.get("tags", []) if _u else []
        meta["updated_at"] = _u.get("updated_at", "") if _u else ""
        meta["deleted"] = bool(_u.get("deleted")) if _u else False
    else:
        meta["stored"] = False
        meta["storage_path"] = None
    return meta


def iter_all_documents() -> list:
    """统一加载全部文档（原始库 + 上传），返回向量/BM25 重建所需的扁平列表。

    元素: {doc_id, filename, category, content, source, year}
    """
    docs = []
    raw = _load_raw()
    for d in raw.get("documents", []):
        text = d.get("full_text", "")
        if text and len(text.strip()) > 10:
            docs.append({
                "doc_id": d["filename"], "filename": d["filename"],
                "category": d.get("category", "未分类"), "content": text.strip(),
                "source": "raw", "year": _extract_year(d["filename"], text),
            })
    for u in _load_uploads():
        if u.get("deleted"):
            continue
        text = u.get("text", "")
        if text and len(text.strip()) > 5:
            docs.append({
                "doc_id": u["doc_id"], "filename": u.get("filename", u["doc_id"]),
                "category": u.get("category", "未分类"), "content": text.strip(),
                "source": "upload", "year": _extract_year(u.get("filename", ""), text),
            })
    return docs


def category_doc_counts() -> dict:
    """返回 {分类名: 文档数}（含用户上传文档）。"""
    counts = {}
    for d in _all_browse_docs():
        c = d.get("category", "未分类")
        counts[c] = counts.get(c, 0) + 1
    return counts


def save_upload(filename: str, category: str, text: str, raw_bytes: bytes = None) -> str:
    """保存用户上传文档，返回 doc_id。

    同名文件视为同一文档：再次上传时更新已有条目（含分类/正文与原始二进制），
    避免重复追加导致知识浏览中同一文件出现多条。

    原始二进制按「类别/年代」归类落盘到
        uploads/files/<类别>/<年代>/<doc_id>.<ext>
    使后续「PDF 预览」能直接返回用户上传的原始文件（如原版 PDF），
    而不是用抽取文本重新排版生成的 PDF。写入后会校验落盘字节数，
    仅当写入成功才记录 stored_path，否则标记缺失，避免『原文件不存在』误用。
    """
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(UPLOAD_FILES_DIR, exist_ok=True)
    ext = os.path.splitext(filename)[1].lower()
    doc_id = "up_%d" % (abs(hash(filename + "|" + category + "|" + str(len(text)))) % (10 ** 12))
    year = _extract_year(filename, text)
    stored_rel = None
    mimetype = None
    if raw_bytes is not None:
        rel = _stored_rel_for(category, year, doc_id, ext)
        abs_path = os.path.join(UPLOAD_DIR, rel)
        try:
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "wb") as f:
                f.write(raw_bytes)
            # 校验落盘：字节数一致且文件可读才算成功
            if os.path.exists(abs_path) and os.path.getsize(abs_path) == len(raw_bytes):
                stored_rel = rel
                mimetype = mimetype_for_ext(ext)
            else:
                # 落盘异常：清理半截文件并标记缺失
                try:
                    if os.path.exists(abs_path):
                        os.remove(abs_path)
                except Exception:
                    pass
        except Exception:
            stored_rel = None
    ups = _load_uploads()
    if not isinstance(ups, list):
        ups = []
    ups = [u for u in ups if isinstance(u, dict)]
    entry = {"doc_id": doc_id, "filename": filename, "category": category,
             "pages": max(1, text.count("\n") // 40 + 1), "label": filename,
             "text": text, "created_at": _now(),
             "stored_path": stored_rel,
             "mimetype": mimetype,
             "ext": ext, "year": year, "tags": [], "deleted": 0, "deleted_at": None}
    existed = False
    for u in ups:
        if u.get("filename") == filename and u.get("category") == category:
            u.update(entry)
            existed = True
            break
    else:
        ups.append(entry)
    _atomic_write_json(UPLOAD_FILE, ups)
    uid, uname = audit_current_user()
    audit_log("doc.upload", doc_id, "%s -> %s" % (filename, category), uid, uname)
    return doc_id


def _save_uploads(ups: list):
    """原子写入 uploads.json（先写临时文件再 os.replace，避免并发/崩溃损坏）。

    与 _atomic_write_json 保持一致，并额外 fsync 保证断电安全。
    """
    _atomic_write_json(UPLOAD_FILE, ups)


def _doc_id_for(filename: str, category: str, raw_bytes: bytes) -> str:
    """稳定的 doc_id：仅依赖 文件名+分类+原始字节数（与提取文本长度无关），
    使「先落盘(raw) → 后补文本(update)」两次写入指向同一条 entry。

    【修复】原用内置 hash() 取模，但 Python 对 str 的 hash 带 PYTHONHASHSEED
    随机化：同一文件在不同进程/重启后会得到不同 doc_id，导致
      - 重复上传产生重复条目（旧条目孤儿化）
      - 归档目录名随 doc_id 漂移
      - 上传与提取两阶段指向不同 entry
    改用 blake2b 稳定哈希，跨进程、跨重启恒等。
    """
    return "up_%d" % (int(_stable_hash(filename, category, len(raw_bytes))[:12], 16) % (10 ** 12))


def _resolve_doc_id_conflict(doc_id: str, raw_bytes: bytes, ups: list) -> str:
    """解决 doc_id 冲突：已存在且内容不同时，生成唯一变体 id。

    【修复·静默丢文件】doc_id = hash(文件名+分类+字节数)，不含年度/目录。
    因此 zip 里「01.标准化类/2025年度/A.pdf」与「01.标准化类/2026年度/A.pdf」
    （同名、同分类、恰好同字节数）会算出同一个 doc_id；而磁盘文件名就是
    doc_id，后写的会【直接覆盖】先写的，json 条目也随之 update 覆盖 ——
    文件静默丢失、无任何报错（正是「压缩包 183 个文件只上传 179 个」的原因）。

    策略：
      - 内容相同  -> 复用原 doc_id（重复上传幂等，不产生重复条目）
      - 内容不同  -> 追加序号派生新 id，直到不冲突（保住两个文件）
    这样既不改变已有无冲突文档的 doc_id（向后兼容），又避免覆盖丢文件。
    """
    existing = None
    for u in ups:
        if u.get("doc_id") == doc_id:
            existing = u
            break
    if existing is None:
        return doc_id

    # 与已存在文件比对内容
    old_path = _resolve_binary_path(existing)
    same = False
    if old_path and os.path.exists(old_path):
        try:
            same = open(old_path, "rb").read() == raw_bytes
        except Exception:
            same = False
    if same:
        return doc_id          # 幂等：同一文件重复上传，复用原条目

    # 内容不同 -> 派生唯一 id
    for i in range(1, 1000):
        alt = "up_%d" % (int(_stable_hash(doc_id, i)[:12], 16) % (10 ** 12))
        if not any(u.get("doc_id") == alt for u in ups):
            return alt
    return doc_id


def save_upload_raw(filename: str, category: str, raw_bytes: bytes, year: int = None,
                     source: str = "upload") -> str:
    """仅落盘原始二进制 + 占位 entry（text 暂空），极快，供上传接口同步返回。

    真正的文本提取/结构化（extract + post_process）由后台任务异步补写，
    见 update_upload_text()。返回稳定 doc_id。

    year：归档年度。zip 批量上传时应传入「目录中的年度层」（如 2026年度），
    否则不同年度的同名文件会归到同一年度目录、并更容易触发 doc_id 冲突。
    不传时回退为从文件名中解析年份。
    """
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    os.makedirs(UPLOAD_FILES_DIR, exist_ok=True)
    ext = os.path.splitext(filename)[1].lower()
    # 优先使用调用方指定的年度（zip 目录层），回退到文件名解析
    if year is None:
        year = _extract_year(filename, "")

    # 「读-改-写」必须整体持锁：并发上传同一文件时，两个线程可能同时读到旧列表、
    # 各自 append 后再写回，造成后写覆盖先写（丢条目）或结构错乱。
    # 冲突检测与落盘也在锁内，保证 doc_id 判定的原子性。
    with _STORE_LOCK:
        ups = _load_uploads()
        if not isinstance(ups, list):
            ups = []
        ups = [u for u in ups if isinstance(u, dict)]

        doc_id = _doc_id_for(filename, category, raw_bytes)
        doc_id = _resolve_doc_id_conflict(doc_id, raw_bytes, ups)

        stored_rel = None
        mimetype = None
        if raw_bytes is not None:
            rel = _stored_rel_for(category, year, doc_id, ext)
            abs_path = os.path.join(UPLOAD_DIR, rel)
            try:
                os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                with open(abs_path, "wb") as f:
                    f.write(raw_bytes)
                if os.path.exists(abs_path) and os.path.getsize(abs_path) == len(raw_bytes):
                    stored_rel = rel
                    mimetype = mimetype_for_ext(ext)
                else:
                    try:
                        if os.path.exists(abs_path):
                            os.remove(abs_path)
                    except Exception:
                        pass
            except Exception:
                stored_rel = None

        entry = {"doc_id": doc_id, "filename": filename, "category": category,
                 "pages": 1, "label": filename,
                 "text": "", "created_at": _now(),
                 "stored_path": stored_rel,
                 "mimetype": mimetype,
                 "ext": ext, "year": year, "tags": [], "deleted": 0, "deleted_at": None,
                 "source": source,
                 "indexed": 0}   # indexed=0 标记尚未经后台提取/建索引
        existed = False
        for u in ups:
            if u.get("doc_id") == doc_id:
                u.update(entry)
                existed = True
                break
        else:
            ups.append(entry)
        _save_uploads(ups)
    uid, uname = audit_current_user()
    audit_log("doc.upload", doc_id, "%s -> %s (raw, 待后台提取)" % (filename, category), uid, uname)
    return doc_id


def update_upload_text_async(doc_id: str, text: str, category: str = None, year: int = None) -> bool:
    """后台任务补写提取文本与结构化结果，并标记已索引（不在此处重建索引）。

    与 update_upload_text（在线编辑，同步重建索引）分离：后台提取由 worker
    在队列空闲时统一全量重建，避免每个文件各重建一次。
    按 doc_id 定位 entry（与 save_upload_raw 同公式），补充 text/pages/year，
    若后台重新判定了分类（category 非空且与原值不同）也一并更新。
    """
    # 持锁：worker 并发回填多篇文本时，避免「读-改-写」互相覆盖（丢文本/丢条目）
    with _STORE_LOCK:
        ups = _load_uploads()
        if not isinstance(ups, list):
            ups = []
        ups = [u for u in ups if isinstance(u, dict)]
        for u in ups:
            if u.get("doc_id") == doc_id:
                u["text"] = text
                u["pages"] = max(1, text.count("\n") // 40 + 1)
                if category is not None and category != u.get("category"):
                    u["category"] = category
                u["year"] = year if year is not None else u.get("year")
                u["indexed"] = 1
                u["updated_at"] = _now()
                _save_uploads(ups)
                return True
    return False


def get_upload_binary(doc_id: str):
    """返回上传文档的原始二进制 (abs_path, mimetype)，无则返回 (None, None)。

    用于「PDF 预览」直接返回用户上传的原始文件（如原版 PDF），
    避免用抽取文本重新排版生成的 PDF 替代原文件。
    """
    for u in _load_uploads():
        if u.get("doc_id") == doc_id:
            p = _resolve_binary_path(u)
            if p:
                return p, (u.get("mimetype") or "application/octet-stream")
            return None, None
    return None, None


def replace_upload_binary(doc_id: str, filename: str, raw_bytes: bytes) -> dict:
    """界面直接替换某上传文档的原始二进制文件（不新增条目，沿用原 doc_id）。

    场景：用户已通过 FTP / 其他途径把新文件传到服务器，或仅是同名格式升级
    （如 .doc -> .docx），希望在不重新走「整篇上传」流程、不丢失归类/标签/
    原 doc_id 的前提下，直接覆盖物理文件并重提取文本。

    行为：
      1) 在「原类别/原年代」目录下，用新 ext 计算目标 stored_rel（doc_id 不变）；
      2) 写入新文件并校验字节数；成功后删除旧二进制文件（ext 可能不同）；
      3) 更新 entry 的 ext / stored_path / mimetype / filename / updated_at，
         并把 indexed 复位为 0（等待后台重提取，真正文本由调用方入队触发）；
      4) 不在此处重建索引（重提取由后台 worker 统一重建，保持与「内容提取」一致）。

    返回 {"ok": bool, "doc_id": str, "stored": bool, "error": str}。
    """
    if not raw_bytes:
        return {"ok": False, "doc_id": doc_id, "stored": False, "error": "文件内容为空"}
    ext = os.path.splitext(filename)[1].lower()
    with _STORE_LOCK:
        ups = _load_uploads()
        if not isinstance(ups, list):
            ups = []
        ups = [u for u in ups if isinstance(u, dict)]
        target = next((u for u in ups if u.get("doc_id") == doc_id), None)
        if target is None:
            return {"ok": False, "doc_id": doc_id, "stored": False, "error": "文档不存在"}
        if target.get("deleted"):
            return {"ok": False, "doc_id": doc_id, "stored": False, "error": "文档已在回收站，不可替换"}
        category = target.get("category", "未分类")
        year = target.get("year") or _extract_year(filename, "")
        old_path = _resolve_binary_path(target)
        new_rel = _stored_rel_for(category, year, doc_id, ext)
        new_path = os.path.join(UPLOAD_DIR, new_rel)
        try:
            os.makedirs(os.path.dirname(new_path), exist_ok=True)
            with open(new_path, "wb") as f:
                f.write(raw_bytes)
            if not (os.path.exists(new_path) and os.path.getsize(new_path) == len(raw_bytes)):
                # 落盘异常：清理半截文件
                try:
                    if os.path.exists(new_path):
                        os.remove(new_path)
                except Exception:
                    pass
                return {"ok": False, "doc_id": doc_id, "stored": False,
                        "error": "新文件落盘校验失败"}
        except Exception as e:
            return {"ok": False, "doc_id": doc_id, "stored": False,
                    "error": "写入失败: %s" % e}
        # 删除旧二进制（ext 可能不同，避免残留两份）
        if old_path and os.path.abspath(old_path) != os.path.abspath(new_path):
            try:
                if os.path.exists(old_path):
                    os.remove(old_path)
                # 清理可能已空的旧目录
                od = os.path.dirname(old_path)
                if od.startswith(UPLOAD_FILES_DIR) and not os.listdir(od):
                    os.rmdir(od)
            except Exception:
                pass
        target["filename"] = filename
        target["ext"] = ext
        target["stored_path"] = new_rel
        target["mimetype"] = mimetype_for_ext(ext)
        target["year"] = year
        target["indexed"] = 0        # 等待后台重提取
        target["updated_at"] = _now()
        _save_uploads(ups)
    uid, uname = audit_current_user()
    audit_log("doc.replace_binary", doc_id, "%s -> %s" % (filename, category), uid, uname)
    return {"ok": True, "doc_id": doc_id, "stored": True, "error": ""}


def delete_upload_binary(doc_id: str):
    """删除上传文档关联的原始二进制文件（忽略不存在/异常）。"""
    import shutil
    for u in _load_uploads():
        if u.get("doc_id") == doc_id:
            p = _resolve_binary_path(u)
            if p:
                try:
                    os.remove(p)
                    # 清理可能已空的归类目录
                    try:
                        d = os.path.dirname(p)
                        if d.startswith(UPLOAD_FILES_DIR) and not os.listdir(d):
                            os.rmdir(d)
                    except Exception:
                        pass
                except Exception:
                    pass
            return


def mark_extracting(doc_ids: list) -> int:
    """重新提取开始时，立即把这批文档的索引状态与文本清空（indexed=0, text=""），
    使上传管理页能即时从「已识别/字数」复位为「未识别/0」，直观反映「提取中」。
    后台 worker 逐篇跑完会再写回 text 与 indexed=1。返回被复位的文档数。
    """
    ids = set(doc_ids)
    if not ids:
        return 0
    # 持锁：避免与 worker 回填 / 清除操作并发导致互相覆盖
    with _STORE_LOCK:
        ups = _load_uploads()
        if not isinstance(ups, list):
            ups = []
        ups = [u for u in ups if isinstance(u, dict)]
        n = 0
        for u in ups:
            if u.get("doc_id") in ids:
                u["indexed"] = 0
                u["text"] = ""
                u["updated_at"] = _now()
                n += 1
        if n:
            _atomic_write_json(UPLOAD_FILE, ups)
        return n


def clear_extract() -> int:
    """清空全部文档的提取内容（indexed=0, text=\"\"），但保留文件与条目本身。

    与 mark_extracting（重新提取前的瞬时复位）不同：本函数用于「只清除、不再重提」，
    使上传管理页状态立即变为「未识别」、字数归 0，且不触发后台提取/索引重建。
    返回被清空的文档数。
    """
    with _STORE_LOCK:
        ups = _load_uploads()
        if not isinstance(ups, list):
            ups = []
        ups = [u for u in ups if isinstance(u, dict)]
        n = 0
        for u in ups:
            if u.get("deleted"):
                continue  # 回收站不处理
            if u.get("indexed") or u.get("text"):
                u["indexed"] = 0
                u["text"] = ""
                u["updated_at"] = _now()
                n += 1
        if n:
            _atomic_write_json(UPLOAD_FILE, ups)
        return n


def list_uploads(q: str = None, page: int = 1, page_size: int = 50,
                 include_deleted: bool = False, source_filter: str = None) -> dict:
    """返回上传文档管理列表（含归类/年代/存储路径/原文件状态），支持关键词与分页。

    include_deleted=False（默认）时仅列出活跃文档；回收站页单独调用 list_trash。
    source_filter: 按来源过滤 uploads/raw 之外，额外支持 "upload"(手动上传) /
    "yunzhijia"(云之家拉取) 两个标签分开展示。None=全部。
    """
    ups = _load_uploads()
    items = []
    for u in ups:
        if u.get("deleted") and not include_deleted:
            continue
        src = u.get("source", "upload")
        if source_filter and src != source_filter:
            continue
        text = u.get("text", "") or ""
        p = _resolve_binary_path(u)
        items.append({
            "doc_id": u.get("doc_id"),
            "filename": u.get("filename", u.get("doc_id")),
            "category": u.get("category", "未分类"),
            "year": u.get("year"),
            "pages": u.get("pages", 1),
            "chars": len(re.sub(r"\s", "", text)),
            "created_at": u.get("created_at", ""),
            "updated_at": u.get("updated_at", ""),
            "deleted": bool(u.get("deleted")),
            "deleted_at": u.get("deleted_at", ""),
            "tags": u.get("tags", []),
            "source": src,
            "stored": bool(p),
            "storage_path": u.get("stored_path"),
            "mimetype": u.get("mimetype"),
            "indexed": bool(u.get("indexed")),
        })
    items.sort(key=lambda d: d.get("created_at") or "", reverse=True)
    if q:
        ql = q.lower()
        items = [d for d in items
                 if ql in d["filename"].lower() or ql in d["category"].lower()
                 or (d.get("year") and ql in str(d["year"]))]
    total = len(items)
    start = (page - 1) * page_size
    return {"total": total, "page": page, "page_size": page_size,
            "items": items[start:start + page_size]}


def delete_upload(doc_id: str) -> bool:
    """彻底删除一条上传文档（移除原始二进制 + user_documents.json 条目并重建索引）。

    注意：这是硬删除（purge）。上传管理页的『删除』应走 soft_delete_upload（回收站）。
    """
    ups = _load_uploads()
    new_ups = [u for u in ups if u.get("doc_id") != doc_id]
    if len(new_ups) == len(ups):
        return False
    delete_upload_binary(doc_id)
    _atomic_write_json(UPLOAD_FILE, new_ups)
    # 一并清理该上传文档关联的「二次生成纪要」派生记录与 PDF（提取文件），
    # 避免删除源码后仍残留衍生内容（知识浏览“衍生文档”/派生 PDF）。
    _purge_derived_for_doc(doc_id)
    try:
        import rag_build_index
        rag_build_index.build_index()
        import vec_store
        vec_store.rebuild(iter_all_documents())
    except Exception:
        pass
    uid, uname = audit_current_user()
    audit_log("doc.purge", doc_id, "彻底删除文档", uid, uname)
    return True


def _purge_derived_for_doc(doc_id: str):
    """删除某上传文档关联的全部派生纪要记录与二次生成 PDF（按 source_doc_id）。"""
    try:
        import derived_store
        items = derived_store.list_derived(source_doc_id=doc_id)
        for d in items:
            derived_store.delete_derived(d.get("id"))
    except Exception as e:  # noqa: BLE001
        print("[delete] 清理派生纪要失败 %s: %s" % (doc_id, e))


def delete_uploads_batch(doc_ids: list) -> dict:
    """批量删除上传文档（移除二进制 + user_documents.json 条目，重建索引一次）。

    返回 {"deleted": int, "not_found": list}。
    """
    doc_ids = [str(x) for x in (doc_ids or [])]
    if not doc_ids:
        return {"deleted": 0, "not_found": []}
    with _STORE_LOCK:
        ups = _load_uploads()
        if not isinstance(ups, list):
            ups = []
        ups = [u for u in ups if isinstance(u, dict)]
        id_set = set(doc_ids)
        new_ups = [u for u in ups if u.get("doc_id") not in id_set]
        not_found = [d for d in doc_ids if not any(u.get("doc_id") == d for u in ups)]
        deleted = len(ups) - len(new_ups)
        if deleted == 0:
            return {"deleted": 0, "not_found": not_found}
        for d in doc_ids:
            delete_upload_binary(d)
            _purge_derived_for_doc(d)
        _atomic_write_json(UPLOAD_FILE, new_ups)
    try:
        import rag_build_index
        rag_build_index.build_index()
        import vec_store
        vec_store.rebuild(iter_all_documents())
    except Exception:
        pass
    return {"deleted": deleted, "not_found": not_found}


def reclassify_upload(doc_id: str, category: str) -> bool:
    """调整上传文档的归类分类，并同步移动原始二进制到新归类目录。"""
    if not category:
        return False
    with _STORE_LOCK:
        ups = _load_uploads()
        if not isinstance(ups, list):
            ups = []
        ups = [u for u in ups if isinstance(u, dict)]
        found = False
        for u in ups:
            if u.get("doc_id") != doc_id:
                continue
            old_path = _resolve_binary_path(u)
            u["category"] = category
            found = True
            # 重新归类：把原文件移动到 <新类别>/<年代>/ 下，保持物理归类一致
            if old_path:
                ext = u.get("ext") or os.path.splitext(u.get("filename", ""))[1].lower()
                year = u.get("year") or _extract_year(u.get("filename", ""), u.get("text", ""))
                new_rel = _stored_rel_for(category, year, u["doc_id"], ext)
                new_path = os.path.join(UPLOAD_DIR, new_rel)
                if os.path.abspath(old_path) != os.path.abspath(new_path):
                    try:
                        os.makedirs(os.path.dirname(new_path), exist_ok=True)
                        # 【修复】目标已存在时 shutil.move 会抛异常（尤其 Windows），
                        # 原实现被 except 静默吞掉，导致「分类已改、文件仍在旧目录」，
                        # 物理归档与逻辑分类长期不一致（后续查 check_upload_storage
                        # 会报缺文件）。这里显式处理目标已存在的场景：
                        if os.path.exists(new_path):
                            try:
                                same = (os.path.getsize(old_path) == os.path.getsize(new_path)
                                        and open(old_path, "rb").read()
                                        == open(new_path, "rb").read())
                            except Exception:
                                same = False
                            if same:
                                os.remove(old_path)     # 内容一致：去重，保留目标
                                u["stored_path"] = new_rel
                            else:
                                # 内容不同：改用带序号的唯一文件名，避免覆盖/丢失
                                stem, e2 = os.path.splitext(new_rel)
                                k = 1
                                while os.path.exists(os.path.join(UPLOAD_DIR, "%s_%d%s" % (stem, k, e2))):
                                    k += 1
                                new_rel = "%s_%d%s" % (stem, k, e2)
                                new_path = os.path.join(UPLOAD_DIR, new_rel)
                                import shutil
                                shutil.move(old_path, new_path)
                                u["stored_path"] = new_rel
                        else:
                            import shutil
                            shutil.move(old_path, new_path)
                            u["stored_path"] = new_rel
                        # 清理旧目录
                        try:
                            od = os.path.dirname(old_path)
                            if od.startswith(UPLOAD_FILES_DIR) and not os.listdir(od):
                                os.rmdir(od)
                        except Exception:
                            pass
                    except Exception:
                        # 移动失败则保留原 stored_path，不破坏预览
                        pass
            break
        if not found:
            return False
        _atomic_write_json(UPLOAD_FILE, ups)
    # 归类影响检索分类，重建索引
    try:
        import rag_build_index
        rag_build_index.build_index()
        import vec_store
        vec_store.rebuild(iter_all_documents())
    except Exception:
        pass
    return True


def reclassify_uploads_batch(doc_ids: list, category: str) -> dict:
    """批量调整归类（性能优化版）。

    相比逐篇调用 reclassify_upload（每篇都会全量重写 uploads.json + 整库重建向量索引，
    N 篇 = N 次全库重建，是「批量改分类很慢」的根因），本函数：
      - 仅加载/保存 uploads.json 一次；
      - 仅在循环结束后统一重建一次检索索引（rag_build_index + vec_store.rebuild）。
    物理文件移动仍逐篇执行（必须），但不再触发 N 次索引重建。
    返回 {"done": [...], "failed": [...]}。重提取入队由调用方负责（见 serve 批量路由）。
    """
    if not category:
        return {"done": [], "failed": list(doc_ids or [])}
    ids = set(doc_ids or [])
    done, failed = [], []
    with _STORE_LOCK:
        ups = _load_uploads()
        if not isinstance(ups, list):
            ups = []
        ups = [u for u in ups if isinstance(u, dict)]
        targets = [u for u in ups if u.get("doc_id") in ids and not u.get("deleted")]
        found_ids = {u.get("doc_id") for u in targets}
        for doc_id in doc_ids:
            if doc_id not in found_ids:
                failed.append(doc_id)
        for u in targets:
            doc_id = u.get("doc_id")
            old_path = _resolve_binary_path(u)
            u["category"] = category
            # 物理移动（保持与 reclassify_upload 一致的逻辑，但批量合并到一次保存）
            if old_path:
                ext = u.get("ext") or os.path.splitext(u.get("filename", ""))[1].lower()
                year = u.get("year") or _extract_year(u.get("filename", ""), u.get("text", ""))
                new_rel = _stored_rel_for(category, year, doc_id, ext)
                new_path = os.path.join(UPLOAD_DIR, new_rel)
                if os.path.abspath(old_path) != os.path.abspath(new_path):
                    try:
                        os.makedirs(os.path.dirname(new_path), exist_ok=True)
                        if os.path.exists(new_path):
                            try:
                                same = (os.path.getsize(old_path) == os.path.getsize(new_path)
                                        and open(old_path, "rb").read()
                                        == open(new_path, "rb").read())
                            except Exception:
                                same = False
                            if same:
                                os.remove(old_path)
                                u["stored_path"] = new_rel
                            else:
                                stem, e2 = os.path.splitext(new_rel)
                                k = 1
                                while os.path.exists(os.path.join(UPLOAD_DIR, "%s_%d%s" % (stem, k, e2))):
                                    k += 1
                                new_rel = "%s_%d%s" % (stem, k, e2)
                                new_path = os.path.join(UPLOAD_DIR, new_rel)
                                import shutil
                                shutil.move(old_path, new_path)
                                u["stored_path"] = new_rel
                        else:
                            import shutil
                            shutil.move(old_path, new_path)
                            u["stored_path"] = new_rel
                        try:
                            od = os.path.dirname(old_path)
                            if od.startswith(UPLOAD_FILES_DIR) and not os.listdir(od):
                                os.rmdir(od)
                        except Exception:
                            pass
                    except Exception:
                        pass
            done.append(doc_id)
        _atomic_write_json(UPLOAD_FILE, ups)
    # 循环外统一重建一次索引（关键：避免 N 次全库重建）
    try:
        import rag_build_index
        rag_build_index.build_index()
        import vec_store
        vec_store.rebuild(iter_all_documents())
    except Exception:
        pass
    return {"done": done, "failed": failed}


def migrate_upload_storage() -> int:
    """一次性迁移：把旧版扁平布局 files/<doc_id><ext> 重定位到
    files/<类别>/<年代>/<doc_id><ext> 归类布局（幂等，可重复运行）。

    返回实际移动的文件数（用于启动日志）。
    """
    ups = _load_uploads()
    moved = 0
    for u in ups:
        doc_id = u.get("doc_id")
        ext = u.get("ext") or os.path.splitext(u.get("filename", ""))[1].lower()
        year = u.get("year") or _extract_year(u.get("filename", ""), u.get("text", ""))
        desired_rel = _stored_rel_for(u.get("category", "未分类"), year, doc_id, ext)
        cur = _resolve_binary_path(u)
        # 已归类到位
        if cur and os.path.relpath(cur, UPLOAD_DIR).replace("\\", "/") == desired_rel:
            continue
        # 寻找可迁移的源文件：当前 stored 或旧版扁平文件
        src = cur
        if not src:
            legacy = os.path.join(UPLOAD_DIR, "files", doc_id + ext)
            if os.path.exists(legacy):
                src = legacy
        if not src:
            continue
        dest = os.path.join(UPLOAD_DIR, desired_rel)
        if os.path.abspath(src) == os.path.abspath(dest):
            u["stored_path"] = desired_rel
            moved += 0
            continue
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            import shutil
            shutil.move(src, dest)
            u["stored_path"] = desired_rel
            moved += 1
        except Exception:
            pass
    if moved:
        _atomic_write_json(UPLOAD_FILE, ups)
    return moved


def check_upload_storage() -> list:
    """诊断：返回所有『原文件不存在』的上传文档（stored 标记但实际缺文件）。"""
    missing = []
    for u in _load_uploads():
        if not _resolve_binary_path(u):
            missing.append({
                "doc_id": u.get("doc_id"),
                "filename": u.get("filename", u.get("doc_id")),
                "category": u.get("category", "未分类"),
                "year": u.get("year"),
            })
    return missing


def dedupe_uploads():
    """清理 user_documents.json 中同名重复条目（每个文件名仅保留一条）。"""
    if not os.path.exists(UPLOAD_FILE):
        return 0
    ups = _load_uploads()
    seen = {}
    for u in ups:
        seen[u.get("filename")] = u  # 同名保留最后一条
    cleaned = list(seen.values())
    removed = len(ups) - len(cleaned)
    if removed:
        _atomic_write_json(UPLOAD_FILE, cleaned)
    return removed


def _now() -> str:
    """当前时间字符串（本地时区）。

    注意：早期实现用 datetime.now(timezone.utc)，写进去的是 UTC 时间，
    而展示界面按本地时区解读，导致「记录时间比实际慢/快 8 小时」
    （生产机时区 Asia/Shanghai，UTC+8）。这里统一改为本地时间，
    保证「写入时间」与「界面/系统时间」一致。
    """
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ============ 操作审计日志 ============
def _audit_conn():
    """复用管理库连接（与 admin.py 同库，审计表建在 kb_admin.db）。"""
    import admin
    conn = admin._conn()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS audit_log (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               ts TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now','localtime')),
               user_id INTEGER,
               username TEXT,
               action TEXT,
               target TEXT,
               detail TEXT
           )""")
    return conn


def audit_log(action: str, target: str = "", detail: str = "", user_id=None, username: str = ""):
    """记录一条操作审计（忽略异常，永不阻断主流程）。"""
    try:
        conn = _audit_conn()
        conn.execute(
            "INSERT INTO audit_log (user_id, username, action, target, detail) "
            "VALUES (?,?,?,?,?)",
            (user_id, username, action, target, detail))
        conn.commit()
        conn.close()
    except Exception:
        pass


def audit_current_user() -> tuple:
    """返回 (user_id, username)，供调用方在已登录上下文中记录审计。

    优先从 Flask session 读取；非请求上下文（如脚本）返回 (None, "")。
    """
    try:
        from flask import session
        uid = session.get("user_id")
        if uid:
            import admin
            row = admin._conn().execute(
                "SELECT username FROM users WHERE id=?", (uid,)).fetchone()
            return uid, (row["username"] if row else "")
    except Exception:
        pass
    return None, ""


def list_audit(page: int = 1, page_size: int = 50, action: str = None,
               q: str = None) -> dict:
    """分页查询审计日志，支持按动作类型/关键词过滤。"""
    conn = _audit_conn()
    where, params = [], []
    if action:
        where.append("action=?")
        params.append(action)
    if q:
        where.append("(target LIKE ? OR detail LIKE ? OR username LIKE ?)")
        params += ["%" + q + "%", "%" + q + "%", "%" + q + "%"]
    sql_where = (" WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute("SELECT COUNT(*) c FROM audit_log" + sql_where, params).fetchone()["c"]
    rows = conn.execute(
        "SELECT * FROM audit_log" + sql_where +
        " ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [page_size, (page - 1) * page_size]).fetchall()
    conn.close()
    return {"total": total, "page": page, "page_size": page_size,
            "items": [dict(r) for r in rows]}


def audit_actions() -> list:
    """返回全部不同的动作类型，供前端下拉过滤。"""
    conn = _audit_conn()
    rows = conn.execute(
        "SELECT action, COUNT(*) c FROM audit_log GROUP BY action ORDER BY c DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ============ 回收站（软删除） ============
def soft_delete_upload(doc_id: str) -> bool:
    """软删除：标记 deleted=1 并保留原文件与索引条目（仅从『活跃』列表隐藏）。

    真正从检索中剔除需在重建索引时排除 deleted 条目；为简单稳妥，
    软删除同时重建索引（排除被删条目），但原文件与 json 记录均保留可恢复。
    """
    ups = _load_uploads()
    found = False
    for u in ups:
        if u.get("doc_id") == doc_id:
            u["deleted"] = 1
            u["deleted_at"] = _now()
            found = True
            break
    if not found:
        return False
    _atomic_write_json(UPLOAD_FILE, ups)
    try:
        import rag_build_index
        rag_build_index.build_index()
        import vec_store
        vec_store.rebuild(iter_all_documents())
    except Exception:
        pass
    uid, uname = audit_current_user()
    audit_log("doc.delete", doc_id, "移入回收站（软删除）", uid, uname)
    return True


def soft_delete_uploads_batch(doc_ids: list) -> dict:
    doc_ids = [str(x) for x in (doc_ids or [])]
    if not doc_ids:
        return {"deleted": 0, "not_found": []}
    ups = _load_uploads()
    id_set = set(doc_ids)
    found = 0
    for u in ups:
        if u.get("doc_id") in id_set and not u.get("deleted"):
            u["deleted"] = 1
            u["deleted_at"] = _now()
            found += 1
    not_found = [d for d in doc_ids if not any(u.get("doc_id") == d for u in ups)]
    if found == 0:
        return {"deleted": 0, "not_found": not_found}
    _atomic_write_json(UPLOAD_FILE, ups)
    try:
        import rag_build_index
        rag_build_index.build_index()
        import vec_store
        vec_store.rebuild(iter_all_documents())
    except Exception:
        pass
    uid, uname = audit_current_user()
    audit_log("doc.delete.batch", ",".join(doc_ids), "批量移入回收站", uid, uname)
    return {"deleted": found, "not_found": not_found}


def restore_upload(doc_id: str) -> bool:
    """从回收站恢复：清除 deleted 标记并重建索引。"""
    ups = _load_uploads()
    found = False
    for u in ups:
        if u.get("doc_id") == doc_id and u.get("deleted"):
            u["deleted"] = 0
            u["deleted_at"] = None
            found = True
            break
    if not found:
        return False
    _atomic_write_json(UPLOAD_FILE, ups)
    try:
        import rag_build_index
        rag_build_index.build_index()
        import vec_store
        vec_store.rebuild(iter_all_documents())
    except Exception:
        pass
    uid, uname = audit_current_user()
    audit_log("doc.restore", doc_id, "从回收站恢复", uid, uname)
    return True


def purge_upload(doc_id: str) -> bool:
    """彻底删除：移除原文件 + json 条目 + 重建索引（不可恢复）。"""
    res = delete_upload(doc_id)
    if res:
        uid, uname = audit_current_user()
        audit_log("doc.purge", doc_id, "回收站彻底删除", uid, uname)
    return res


def purge_uploads_batch(doc_ids: list) -> dict:
    res = delete_uploads_batch(doc_ids)
    if res.get("deleted"):
        uid, uname = audit_current_user()
        audit_log("doc.purge.batch", ",".join(doc_ids), "回收站批量彻底删除", uid, uname)
    return res


def list_trash(page: int = 1, page_size: int = 50, q: str = None) -> dict:
    """回收站列表（仅 deleted=1 的上传文档）。"""
    ups = _load_uploads()
    items = []
    for u in ups:
        if not u.get("deleted"):
            continue
        p = _resolve_binary_path(u)
        items.append({
            "doc_id": u.get("doc_id"),
            "filename": u.get("filename", u.get("doc_id")),
            "category": u.get("category", "未分类"),
            "year": u.get("year"),
            "deleted_at": u.get("deleted_at", ""),
            "stored": bool(p),
        })
    items.sort(key=lambda d: str(d.get("deleted_at") or ""), reverse=True)
    q = q or ""
    if q:
        ql = str(q).lower()
        items = [d for d in items
                 if ql in str(d["filename"]).lower() or ql in str(d["category"]).lower()]
    total = len(items)
    start = (page - 1) * page_size
    return {"total": total, "page": page, "page_size": page_size,
            "items": items[start:start + page_size]}


def trash_count() -> int:
    return sum(1 for u in _load_uploads() if u.get("deleted"))


# ============ 标签系统 ============
def set_upload_tags(doc_id: str, tags: list) -> bool:
    """设置/覆盖某文档的标签列表（去重、去空、截断长度）。"""
    tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
    tags = tags[:20]
    ups = _load_uploads()
    found = False
    for u in ups:
        if u.get("doc_id") == doc_id:
            u["tags"] = tags
            found = True
            break
    if not found:
        return False
    _atomic_write_json(UPLOAD_FILE, ups)
    return True


def list_tags() -> list:
    """返回全部标签及文档数（标签云）。"""
    counts = {}
    for u in _load_uploads():
        if u.get("deleted"):
            continue
        for t in (u.get("tags") or []):
            counts[t] = counts.get(t, 0) + 1
    return [{"tag": t, "count": c} for t, c in sorted(counts.items(), key=lambda x: -x[1])]


def docs_by_tag(tag: str, page: int = 1, page_size: int = 50) -> dict:
    """按标签返回文档（含在分类浏览中复用）。"""
    ups = _load_uploads()
    items = []
    for u in ups:
        if u.get("deleted"):
            continue
        if tag in (u.get("tags") or []):
            items.append({
                "doc_id": u.get("doc_id"),
                "filename": u.get("filename", u.get("doc_id")),
                "category": u.get("category", "未分类"),
                "year": u.get("year"),
                "tags": u.get("tags", []),
            })
    items.sort(key=lambda d: d.get("filename") or "", reverse=True)
    total = len(items)
    start = (page - 1) * page_size
    return {"total": total, "page": page, "page_size": page_size,
            "items": items[start:start + page_size]}


# ============ 文档在线编辑（更新提取文本） ============
def update_upload_text(doc_id: str, text: str) -> bool:
    """更新文档提取文本（在线编辑），并重建索引。"""
    ups = _load_uploads()
    found = False
    for u in ups:
        if u.get("doc_id") == doc_id:
            u["text"] = text
            u["pages"] = max(1, text.count("\n") // 40 + 1)
            u["updated_at"] = _now()
            found = True
            break
    if not found:
        return False
    _atomic_write_json(UPLOAD_FILE, ups)
    try:
        import rag_build_index
        rag_build_index.build_index()
        import vec_store
        vec_store.rebuild(iter_all_documents())
    except Exception:
        pass
    uid, uname = audit_current_user()
    audit_log("doc.edit", doc_id, "在线编辑提取文本", uid, uname)
    return True


# ============ 统计概览（门户首页） ============
def kb_overview() -> dict:
    """门户首页统计：活跃文档数、分类数、标签数、最近更新、回收站数。"""
    ups = _load_uploads()
    active = [u for u in ups if not u.get("deleted")]
    cats = set(u.get("category", "未分类") for u in active)
    tags = set()
    for u in active:
        tags.update(u.get("tags") or [])
    recent = sorted(active, key=lambda d: d.get("updated_at") or d.get("created_at") or "",
                    reverse=True)[:8]
    recent_items = [{
        "doc_id": u.get("doc_id"),
        "filename": u.get("filename", u.get("doc_id")),
        "category": u.get("category", "未分类"),
        "updated_at": u.get("updated_at") or u.get("created_at") or "",
    } for u in recent]
    return {
        "doc_count": len(active),
        "total_count": len(ups),
        "category_count": len(cats),
        "tag_count": len(tags),
        "trash_count": trash_count(),
        "recent": recent_items,
    }


# ============ 系统初始化（清除文档 / 提取内容 / 重建索引） ============
def _rebuild_index() -> None:
    """统一重建 BM25 + 向量索引（封装多处重复逻辑）。"""
    import rag_build_index
    import vec_store
    rag_build_index.build_index()
    vec_store.rebuild(iter_all_documents())


def clear_all_documents(include_trash: bool = True) -> dict:
    """清除全部文档：删除原始二进制 + 清空 user_documents.json + 重建空索引。

    默认同时清空回收站（include_trash=True），使知识库回到完全空白状态。
    返回统计 {removed_files, removed_entries}。
    """
    # 全流程持锁：与后台 worker 的 update_upload_text_async / mark_extracting 互斥，
    # 防止「清除中 worker 用旧快照写回」导致已删条目复活（幽灵文档）。
    with _STORE_LOCK:
        ups = _load_uploads()
        if not isinstance(ups, list):
            ups = []
        ups = [u for u in ups if isinstance(u, dict)]
        removed_files = 0
        kept = []
        for u in ups:
            if not include_trash and u.get("deleted"):
                kept.append(u)
                continue
            p = _resolve_binary_path(u)
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                    removed_files += 1
                except Exception:
                    pass
        _atomic_write_json(UPLOAD_FILE, kept)
        removed_entries = len(ups) - len(kept)
    try:
        _rebuild_index()
    except Exception:
        pass
    uid, uname = audit_current_user()
    audit_log("doc.init.clear", "-", "清空全部文档(含回收站=%s) 删除文件%d条/条目%d条"
              % (include_trash, removed_files, removed_entries), uid, uname)
    return {"removed_files": removed_files, "removed_entries": removed_entries}


def reextract_all_documents() -> dict:
    """对所有活跃上传文档重新做文本提取（extract + 结构化），并重建索引。

    适用于提取规则升级后批量重抽。返回 {total, ok, failed, errors}。
    """
    ups = _load_uploads()
    active = [u for u in ups if not u.get("deleted")]
    total = len(active)
    ok = 0
    failed = 0
    errors = []
    for u in active:
        doc_id = u.get("doc_id")
        p = _resolve_binary_path(u)
        if not p or not os.path.exists(p):
            failed += 1
            errors.append({"doc_id": doc_id, "filename": u.get("filename"), "error": "原始文件缺失"})
            continue
        try:
            with open(p, "rb") as f:
                raw = f.read()
            text, warn = extract_text.extract(raw, u.get("filename", ""), u.get("category"))
            if warn:
                errors.append({"doc_id": doc_id, "filename": u.get("filename"), "warn": warn})
            update_upload_text_async(doc_id, text, u.get("category"), u.get("year"))
            ok += 1
        except Exception as e:
            failed += 1
            errors.append({"doc_id": doc_id, "filename": u.get("filename"), "error": str(e)})
    try:
        _rebuild_index()
    except Exception:
        pass
    uid, uname = audit_current_user()
    audit_log("doc.init.extract", "-", "重新提取全部文档 总%d/成功%d/失败%d" % (total, ok, failed), uid, uname)
    return {"total": total, "ok": ok, "failed": failed, "errors": errors[:50]}


def rebuild_index_only() -> dict:
    """仅重建 BM25 + 向量索引（不改动文档与文本）。返回 {docs} 已索引文档数。"""
    try:
        _rebuild_index()
    except Exception as e:
        return {"docs": 0, "error": str(e)}
    docs = len(iter_all_documents())
    uid, uname = audit_current_user()
    audit_log("doc.init.index", "-", "重建索引 文档数%d" % docs, uid, uname)
    return {"docs": docs}


def iter_active_uploads() -> list:
    """仅返回活跃上传文档的轻量列表 [{doc_id, filename, category}]（不读文本，
    避免重提取批量入队时把全部文档内容载入内存）。"""
    ups = _load_uploads()
    res = []
    for u in ups:
        if u.get("deleted"):
            continue
        res.append({
            "doc_id": u.get("doc_id"),
            "filename": u.get("filename", u.get("doc_id")),
            "category": u.get("category", "未分类"),
        })
    return res

