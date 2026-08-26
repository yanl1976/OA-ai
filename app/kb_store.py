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

from extract_text import merge_lines_to_paragraphs as _merge_lines

KB_ROOT = os.environ.get("KB_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KB_DIR = os.path.join(KB_ROOT, "knowledge_base")
RAW_DATA = os.path.join(KB_DIR, "raw_data_full.json")
UPLOAD_DIR = os.path.join(KB_DIR, "uploads")
UPLOAD_FILES_DIR = os.path.join(UPLOAD_DIR, "files")  # 上传原始二进制存放目录
UPLOAD_FILE = os.path.join(UPLOAD_DIR, "user_documents.json")
MANIFEST = os.path.join(KB_DIR, "bm25_index", "documents_manifest.json")

_YEAR_RE = re.compile(r"(?:18|19|20)\d{2}")

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


def _load_raw() -> dict:
    if not os.path.exists(RAW_DATA):
        return {"categories": {}, "documents": []}
    with open(RAW_DATA, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_uploads() -> list:
    if not os.path.exists(UPLOAD_FILE):
        return []
    with open(UPLOAD_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_manifest() -> dict:
    """manifest: {doc_id: {filename, category, pages, label, source}}"""
    if not os.path.exists(MANIFEST):
        return {}
    with open(MANIFEST, "r", encoding="utf-8") as f:
        return json.load(f)


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
        if u["doc_id"] not in seen:
            docs.append(_upload_to_doc(u))
            seen.add(u["doc_id"])
    return docs


def list_documents(category: str = None, q: str = None, year: int = None,
                   page: int = 1, page_size: int = 20) -> dict:
    """返回分页文档列表 + 总数 + 可选年份 facet。

    年份 facet(years): 当前筛选条件下出现的全部年份，供前端按年代出按钮。
    """
    docs = _all_browse_docs()
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
    entry = {"doc_id": doc_id, "filename": filename, "category": category,
             "pages": max(1, text.count("\n") // 40 + 1), "label": filename,
             "text": text, "created_at": _now(),
             "stored_path": stored_rel,
             "mimetype": mimetype,
             "ext": ext, "year": year}
    for u in ups:
        if u.get("filename") == filename and u.get("category") == category:
            u.update(entry)
            break
    else:
        ups.append(entry)
    with open(UPLOAD_FILE, "w", encoding="utf-8") as f:
        json.dump(ups, f, ensure_ascii=False, indent=2)
    return doc_id


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


def list_uploads(q: str = None, page: int = 1, page_size: int = 50) -> dict:
    """返回上传文档管理列表（含归类/年代/存储路径/原文件状态），支持关键词与分页。"""
    ups = _load_uploads()
    items = []
    for u in ups:
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
            "source": "upload",
            "stored": bool(p),
            "storage_path": u.get("stored_path"),
            "mimetype": u.get("mimetype"),
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
    """删除一条上传文档（移除原始二进制 + user_documents.json 条目并重建索引）。"""
    ups = _load_uploads()
    new_ups = [u for u in ups if u.get("doc_id") != doc_id]
    if len(new_ups) == len(ups):
        return False
    delete_upload_binary(doc_id)
    with open(UPLOAD_FILE, "w", encoding="utf-8") as f:
        json.dump(new_ups, f, ensure_ascii=False, indent=2)
    try:
        import rag_build_index
        rag_build_index.build_index()
        import vec_store
        vec_store.rebuild(iter_all_documents())
    except Exception:
        pass
    return True


def reclassify_upload(doc_id: str, category: str) -> bool:
    """调整上传文档的归类分类，并同步移动原始二进制到新归类目录。"""
    if not category:
        return False
    ups = _load_uploads()
    found = False
    for u in ups:
        if u.get("doc_id") == doc_id:
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
    with open(UPLOAD_FILE, "w", encoding="utf-8") as f:
        json.dump(ups, f, ensure_ascii=False, indent=2)
    # 归类影响检索分类，重建索引
    try:
        import rag_build_index
        rag_build_index.build_index()
        import vec_store
        vec_store.rebuild(iter_all_documents())
    except Exception:
        pass
    return True


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
        with open(UPLOAD_FILE, "w", encoding="utf-8") as f:
            json.dump(ups, f, ensure_ascii=False, indent=2)
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
        with open(UPLOAD_FILE, "w", encoding="utf-8") as f:
            json.dump(cleaned, f, ensure_ascii=False, indent=2)
    return removed


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
