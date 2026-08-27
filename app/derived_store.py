"""
会议纪要「二次生成」衍生版本存储层

业务背景:
  会议纪要含公司核心决策，必要时需对原文进行「截取」——除必要格式外，只保留
  需要对外传达的内容，形成一份「二次生成」的纪要(衍生版本)。本模块负责对这些
  衍生版本进行持久化管理，并跟踪每份衍生版本的:
    - 来源纪要 (source_doc_id / source_title)
    - 截取后的内容 (content) 与被选中的原文段落序号 (selected_blocks)
    - 文件需求 (requirement): 该衍生文件为何而生 / 需要体现什么
    - 文件去向 (destination): 该衍生文件分发 / 报送给谁
    - 版本号 (version): 同一来源可产生多个衍生版本
    - 衍生链 (parent_id): 可基于某衍生版本再次生成

数据落盘位置: knowledge_base/derived_minutes.json
"""
import os
import json
import re
import uuid

KB_ROOT = os.environ.get("KB_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KB_DIR = os.path.join(KB_ROOT, "knowledge_base")
STORE_FILE = os.path.join(KB_DIR, "derived_minutes.json")

# 段落切分：优先按空行切分；若全文无空行则退化为按行切分
_BLOCK_SPLIT_RE = re.compile(r"\n[ \t]*\n")

# ---- 红头行识别正则（与 extract_text.py 保持一致）--------------------
# 在 derived_store.py 中重复定义，避免循环导入
_HEADER_LINE_RE = re.compile(
    r"^\s*(?:"
    r"〔"                                    # 文号
    r"|.*办公室"                             # 落款（含"办公室"）
    r"|.*综管办"                             # 落款（含"综管办"）
    r"|（[^）]*次）"                         # 会议次数（中文括号）
    r"|\([^）]*次\)"                         # 会议次数（圆括号）
    r"|.*会议纪要"                           # 会议名称
    r"|.*纪要$"                              # 单位名（以「纪要」结尾）
    r"|(?:(?!纪要)[\u4e00-\u9fff])*(?:公司|集团|研究所|研究院|局|部|委员会)$"  # 单位名称
    r")"
)

# ---- 会议纪要模板解析 ----------------------------------------------------
# 标准会议纪要版式（以用户提供的正式模板为准）：
#   公司全称 + 纪要          ← org（单位名称行）
#   天研司会议纪要〔2024〕59 号   ← doc_no（文号）
#   天传所集团办公室 2024 年9 月 30 日  ← office_line（落款办公室+日期）
#   总经理办公会会议纪要      ← meeting_name（会议名称）
#   （2024 年第二十八次）     ← meeting_seq（会议次数）
#   <导语段落>               ← intro（时间/地点/主持/参会/审议事项概述）
#   一、审议关于…的议案      ← 议题（标题）
#   <议题正文…>             ← body
#   会议决定：会议一致通过…  ← decision
#   出席人员：… / 列席人员：…  ← present / absent
_DOCNO_RE = re.compile(r"〔\s*\d{4}\s*〕\s*\d+\s*号")
_OFFICE_RE = re.compile(r"办公室.+?\d{4}\s*年")
_DATE_RE = re.compile(r"\d{4}\s*年.*?日")
_SEQ_RE = re.compile(r"（.+?次）")
_ATTEND_RE = re.compile(r"^\s*(出\s*席|列\s*席)\s*人员\s*[:：]?")
_ITEM_HEAD_RE = re.compile(r"^\s*((?:[0-9]{1,2}(?![\d]))|[一二三四五六七八九十百零]+)\s*[\.、．]")


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def split_blocks(text: str) -> list:
    """将文本切分为可选中的「段落块」，用于前端逐段截取。"""
    if not text:
        return []
    blocks = [b for b in _BLOCK_SPLIT_RE.split(text) if b.strip()]
    if len(blocks) <= 1:
        # 退化为按行切分（合并连续空行）
        lines = [ln.rstrip() for ln in text.split("\n")]
        blocks = []
        buf = []
        for ln in lines:
            if ln.strip() == "":
                if buf:
                    blocks.append("\n".join(buf))
                    buf = []
            else:
                buf.append(ln)
        if buf:
            blocks.append("\n".join(buf))
    return [b for b in blocks if b.strip()]


# 中文数字（用于二次生成时章节号自动重编号）
_CN_DIGITS = "零一二三四五六七八九"


def num2cn(n: int) -> str:
    """将正整数转为中文数字（支持 1–99，覆盖纪要议题数量）。"""
    if n <= 0:
        return str(n)
    if n < 10:
        return _CN_DIGITS[n]
    if n < 20:
        return "十" + (_CN_DIGITS[n - 10] if n % 10 else "")
    if n < 100:
        t, o = n // 10, n % 10
        return _CN_DIGITS[t] + "十" + (_CN_DIGITS[o] if o else "")
    return str(n)


def _strip_item_prefix(title: str) -> str:
    """去掉议题标题前的序号与分隔符（如『一、』『3.』），保留正文。"""
    m = _ITEM_HEAD_RE.match(title or "")
    if m:
        return (title or "")[m.end():].lstrip()
    return (title or "").lstrip()


def renumber_items(items: list, first: int = 1) -> list:
    """对选中的议题重新编号：从 first 开始顺延（一、二、三…）。

    用于二次生成时，被截取的若干议题自动从「一、」重新编号，
    而不沿用原文中的全局序号。
    """
    out = []
    for k, it in enumerate(items or []):
        it = dict(it)
        body = _strip_item_prefix(it.get("title", ""))
        it["title"] = num2cn(first + k) + "、" + body
        out.append(it)
    return out


def _summary(d: dict) -> dict:
    return {
        "id": d.get("id"), "title": d.get("title"), "version": d.get("version"),
        "parent_id": d.get("parent_id"), "source_doc_id": d.get("source_doc_id"),
        "source_title": d.get("source_title"), "created_at": d.get("created_at"),
    }


def get_source_summary(doc_id: str) -> dict or None:
    """返回来源文档的摘要（供血缘顺查/倒查跳转使用）。"""
    if not doc_id:
        return None
    try:
        from kb_store import get_document
    except Exception:  # noqa: BLE001
        return None
    doc = get_document(doc_id)
    if not doc:
        return None
    return {"doc_id": doc_id, "title": doc.get("label") or doc.get("filename"),
            "category": doc.get("category"), "year": doc.get("year"),
            "source": doc.get("source")}


def lineage(derived_id: str) -> dict or None:
    """返回某衍生版本的父子血缘关系：
      - source   : 根源纪要（原版）摘要（顺查终点 / 倒查起点）
      - ancestors: 通过 parent_id 链接的上级衍生版本链（倒查）
      - children : 以本版本为上级的下游衍生版本（顺查）
    """
    items = _load()
    target = next((d for d in items if d.get("id") == derived_id), None)
    if not target:
        return None
    source = get_source_summary(target.get("source_doc_id"))
    # 沿 parent_id 向上回溯祖先链
    ancestors, seen, pid = [], set(), target.get("parent_id")
    while pid and pid not in seen:
        seen.add(pid)
        p = next((d for d in items if d.get("id") == pid), None)
        if not p:
            break
        ancestors.append(_summary(p))
        pid = p.get("parent_id")
    ancestors.reverse()
    children = [_summary(d) for d in items if d.get("parent_id") == derived_id]
    return {"source": source, "ancestors": ancestors, "children": children,
            "source_doc_id": target.get("source_doc_id")}


def parse_minutes(text: str) -> dict:
    """按标准会议纪要模板解析文本，返回结构化字段。

    返回 dict：
      structured : 是否成功识别出模板要素（文号/会议名称/议题 任一即可）
      org / doc_no / office_line / meeting_name / meeting_seq : 文头元信息
      intro      : 导语段落
      items      : [{title, body, decision}]  每个议题
      present / absent : 出席/列席人员行
    若无法识别任何模板要素，structured=False，items 为空，由上层回退块选择。
    """
    if not text or not text.strip():
        return {"structured": False, "items": []}

    # 先调用 _clean_pdf_text 进行预处理：拆分合并的红头、合并行内换行
    try:
        from app.extract_text import _clean_pdf_text
        text = _clean_pdf_text(text)
    except Exception:
        pass  # 如果导入失败，使用原始文本

    # 剔除 PDF 抽取产生的孤立短数字页码行（如页面顶部的 "1"）
    lines = [ln.strip() for ln in text.split("\n")
             if ln.strip() and not re.fullmatch(r"\d{1,3}", ln.strip())]

    header = {}

    # 红头按行解析：使用与 extract_text.py 一致的 _HEADER_LINE_RE 识别红头行
    # 与 _clean_pdf_text 保持一致：连续命中红头特征的行归红头区，第一个未命中行起为正文
    header_end = len(lines)
    for idx, ln in enumerate(lines):
        s = ln.strip()
        if not s:
            continue
        if _HEADER_LINE_RE.match(s):
            continue
        header_end = idx
        break

    # 红头区：逐行保留（与 extract_text.py 一致）
    hlines = []
    for ln in lines[:header_end]:
        s = ln.strip()
        if s:
            # 去除行首粘连的页码数字（如 "1天水…"）
            s = re.sub(r"^\d{1,3}\s*", "", s).strip()
            if s:
                hlines.append(s)

    # 正文区
    body_lines = []
    for ln in lines[header_end:]:
        body_lines.append(ln)

    # 直接把红头行保存为 header_lines，供 PDF 生成时按行取
    header["header_lines"] = hlines

    # 兼容旧逻辑：尝试从行中提取字段
    if hlines:
        header["org"] = hlines[0] if len(hlines) > 0 else ""
        header["doc_no"] = hlines[1] if len(hlines) > 1 else ""
        if len(hlines) > 2:
            office_line = hlines[2]
            dm = re.search(r"\d{4}\s*年", office_line)
            if dm:
                header["office_name"] = office_line[:dm.start()].strip()
                header["office_date"] = re.sub(r"\s+", "", office_line[dm.start():])
            else:
                header["office_name"] = office_line
                header["office_date"] = ""
            header["office_line"] = office_line
        else:
            header["office_name"] = ""
            header["office_date"] = ""
            header["office_line"] = ""
        header["meeting_name"] = hlines[3] if len(hlines) > 3 else ""
        header["meeting_seq"] = hlines[4] if len(hlines) > 4 else ""

    body = body_lines
    # 红头已按行清晰解析（>=4 行）时，直接信任 header_lines 映射结果，
    # 跳过下方旧版「合并启发式」，避免字段错位。仅在红头行不足时回退。
    if len(hlines) < 4:
        # 兼容旧逻辑：合并红头行为单行用于提取字段
        hparts = []
        for ln in lines[:header_end]:
            s = ln.strip()
            if s:
                s = re.sub(r"^\d{1,3}\s*", "", s).strip()
                if s:
                    hparts.append(s)
        htext = re.sub(r"\s+", " ", " ".join(hparts)).strip()
        htext = re.sub(r"^\d{1,3}\s*", "", htext)  # 去行首粘连页码（如 "1天水…"）

        # 1) 单位名（红头最前的机构全称：以 公司/集团/研究所/院/局/部/委员会 结尾）
        m = re.match(r"[\u4e00-\u9fff]*(?:公司|集团|研究院|研究所|局|部|委员会|办公室)", htext)
        if m:
            header["org"] = m.group(0).strip()
            htext = htext[m.end():].lstrip()

        # 2) 文号：〔年份〕序号号 + 紧邻其前的发文机关（连续中文，并去掉单位名后孤立"纪要"）
        m = re.search(r"〔\s*\d{4}\s*〕\s*\d+\s*号", htext)
        if m:
            j = m.start() - 1
            while j >= 0 and re.match(r"[\u4e00-\u9fff]", htext[j]):
                j -= 1
            prefix = htext[j + 1:m.start()]
            prefix = re.sub(r"^纪要", "", prefix)
            header["doc_no"] = re.sub(r"\s+", "", prefix + m.group(0))
            htext = htext[:j + 1].rstrip() + " " + htext[m.end():].lstrip()
        # 3) 会议次数（…次）
        m = re.search(r"[（(][^（）()]*?次[）)]", htext)
        if m:
            header["meeting_seq"] = m.group(0).strip()
            htext = htext[:m.start()].rstrip() + " " + htext[m.end():].lstrip()
        # 4) 落款：办公室 … 年…月…日
        m = re.search(r"[\u4e00-\u9fff]*办公室\s*\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日", htext)
        if m:
            ol = m.group(0).strip()
            dm = re.search(r"\d{4}\s*年", ol)
            if dm:
                header["office_name"] = ol[:dm.start()].strip()
                header["office_date"] = re.sub(r"\s+", "", ol[dm.start():])
            else:
                header["office_name"] = ol
                header["office_date"] = ""
            header["office_line"] = ol
            htext = htext[:m.start()].rstrip() + " " + htext[m.end():].lstrip()
        # 5) 会议名称（含"会议纪要"的连续段）；若单位名尚未识别，则从会议名前兜底取
        htext = htext.strip()
        if "会议纪要" in htext:
            m = re.search(r"[^\s]*会议纪要", htext)
            if m:
                header["meeting_name"] = m.group(0).strip()
                if not header.get("org"):
                    org = htext[:m.start()].strip()
                    org = re.sub(r"[、，。\s]+$", "", org)
                    if org.endswith("纪要"):
                        org = org[:-2]
                    if org.endswith("会议"):
                        org = org[:-2]
                    header["org"] = org.strip()
            else:
                if not header.get("org"):
                    org = htext.strip()
                    if org.endswith("纪要"):
                        org = org[:-2]
                    header["org"] = org
                header["meeting_name"] = ""

    # 剩余行 = 正文区（导语/议题/出席列席）
    body = body_lines

    # 6) 取出席/列席人员（支持姓名跨多行续写）
    # 续行截止条件：遇到议题头、或另一个出席/列席头。
    # 注意：名单本身可能整行以句号结尾（如「…牛晓滨  。」），不能仅凭句号截断，
    # 否则会丢失最后一行姓名。仅当续行含会议类动词（会议/听取/报告/审议等）时，
    # 才视为名单已结束、该行为独立正文。
    _MEETING_VERB_RE = re.compile(r"(会议|听取|报告|审议|研究|讨论|决定|通过|形成|指出|强调|要求|部署|安排)")
    present, absent = [], []
    cleaned = []
    i = 0
    while i < len(body):
        ln = body[i]
        m = _ATTEND_RE.match(ln)
        if m:
            buf = [ln]
            j = i + 1
            while j < len(body):
                nxt = body[j]
                if _ITEM_HEAD_RE.match(nxt) or _ATTEND_RE.match(nxt):
                    break
                # 句号结尾：若含会议类动词则视为正文（截止），否则仍是姓名列表续接
                if nxt.endswith("。") and _MEETING_VERB_RE.search(nxt):
                    break
                buf.append(nxt)
                j += 1
            (present if m.group(1).strip().startswith("出") else absent).append(
                "".join(x for x in buf if x.strip())
            )
            i = j
            continue
        cleaned.append(ln)
        i += 1
    body = cleaned

    # 7) 议题切分（以"一、/1."开头的行）
    item_starts = [i for i, ln in enumerate(body) if _ITEM_HEAD_RE.match(ln)]
    items = []
    intro = ""
    if item_starts:
        intro = "\n".join(body[:item_starts[0]]).strip()
        for j, start in enumerate(item_starts):
            end = item_starts[j + 1] if j + 1 < len(item_starts) else len(body)
            seg = body[start:end]
            title = seg[0]
            seg_text = "\n".join(seg[1:]).strip()
            decision, bodytext = "", seg_text
            dm = re.search(r"会议决定[：:].*", seg_text, re.DOTALL)
            if dm:
                decision = dm.group(0).strip()
                bodytext = seg_text[:dm.start()].strip()
            items.append({"title": title, "body": bodytext, "decision": decision})
    else:
        # 无章节号（如仅一项议题的纪要）：识别「现将……纪要如下」引导句，
        # 引导句之后整段即为唯一议题，不应丢进 intro。
        full = "\n".join(body).strip()
        m = re.search(r"纪要如下[：:]\s*", full)
        if m:
            intro = full[:m.end()].strip()
            topic = full[m.end():].strip()
        else:
            intro = ""
            topic = full
        if topic:
            decision, bodytext = "", topic
            dm = re.search(r"会议决定[：:].*", topic, re.DOTALL)
            if dm:
                decision = dm.group(0).strip()
                bodytext = topic[:dm.start()].strip()
            # 单议题无序号：标题取「议案/事项/议题」等议案结束词之后截断，
            # 避免把整段正文（首句号在段尾）误当标题。
            mt = re.search(r"(?:议案|事项|议题)[：:。]?", bodytext)
            if mt:
                title = bodytext[:mt.end()].strip()
            elif "。" in bodytext:
                title = bodytext.split("。")[0].strip()
            else:
                title = bodytext[:30]
            items.append({"title": title, "body": bodytext, "decision": decision})

    structured = bool(header.get("doc_no") or header.get("meeting_name") or items)
    return {
        "structured": structured,
        "org": header.get("org", ""),
        "doc_no": header.get("doc_no", ""),
        "office_line": header.get("office_line", ""),
        "office_name": header.get("office_name", ""),
        "office_date": header.get("office_date", ""),
        "meeting_name": header.get("meeting_name", ""),
        "meeting_seq": header.get("meeting_seq", ""),
        "header_lines": header.get("header_lines", []),  # 红头原始行列表
        "intro": intro,
        "items": items,
        "present": "\n".join(present),
        "absent": "\n".join(absent),
    }


def _indent_after_lead(text: str) -> str:
    """规范「如下：」引导句的排版。

    会议纪要中「具体分配如下：」「内容如下：」这类引导句后，紧接的
    实质性内容（如「直接责任人……承担10%……」）应另起一段并首行缩进
    2 个全角空格，而非直接接在冒号后成为同一段的续写。

    本函数把「如下：」后非空白、且当前未另起段落的内容，拆分为
    「引导句尾行」+「换行 + 两个全角空格 + 续写内容」，使前端
    （pre-wrap）与 PDF（<br/>）均呈现正确缩进。

    注意：仅当「如下：」后还有内容且其后未以换行开头时才处理；若原
    文本已是「如下：\\n　　内容」则不重复添加。
    """
    if not text:
        return text
    # 找到所有「如下：/如下:」
    out_parts = []
    last = 0
    for m in re.finditer(r"如下[：:]", text):
        end = m.end()
        out_parts.append(text[last:end])
        last = end
        rest = text[end:]
        # 去掉开头空白（若已换行则保留换行，仅补缩进）
        if rest.startswith("\n"):
            # 已是换行，确保换行后有两空格缩进
            stripped = rest.lstrip("\n")
            if not stripped.startswith("　　"):
                out_parts.append("\n　　")
                out_parts.append(stripped)
            else:
                out_parts.append(rest)
        else:
            # 直接接内容：插入换行 + 两空格缩进
            out_parts.append("\n　　" + rest)
            last = len(text)
            break
    if last < len(text):
        out_parts.append(text[last:])
    return "".join(out_parts)


def render_minutes(struct: dict) -> str:
    """将模板结构化字段重排为标准纪要纯文本（用于存储/检索/回退展示）。"""
    if not struct:
        return ""
    parts = []
    # 优先使用 header_lines（按行解析的红头）
    hlines = struct.get("header_lines") or []
    if hlines:
        parts.extend(hlines)
    else:
        # 兼容旧逻辑
        for key in ("org", "doc_no", "office_line", "meeting_name", "meeting_seq"):
            v = (struct.get(key) or "").strip()
            if v:
                parts.append(v)
    intro = (struct.get("intro") or "").strip()
    if intro:
        parts.append(intro)
    for it in struct.get("items", []):
        t = (it.get("title") or "").strip()
        if t:
            parts.append(t)
        b = (it.get("body") or "").strip()
        if b:
            parts.append(_indent_after_lead(b))
        d = (it.get("decision") or "").strip()
        if d:
            parts.append(d)
    p = (struct.get("present") or "").strip()
    if p:
        parts.append(p)
    a = (struct.get("absent") or "").strip()
    if a:
        parts.append(a)
    return "\n".join(parts)


def _load() -> list:
    if not os.path.exists(STORE_FILE):
        return []
    try:
        with open(STORE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(items: list):
    os.makedirs(KB_DIR, exist_ok=True)
    with open(STORE_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


# ---- 二次生成 PDF 的持久化（与原始 PDF 分别管理、可关联） ----
# 存储布局: knowledge_base/uploads/derived/<年代>/<id>.pdf
# 与「原始上传 PDF」物理隔离，互不直接覆盖；通过 source_doc_id 与来源原文件关联。
PDF_ROOT = os.path.join(KB_DIR, "uploads", "derived")


def _derived_year(d: dict):
    """二次生成 PDF 的年代：优先取 created_at 的年份，其次正文/标题中的年份。"""
    ca = (d.get("created_at") or "")[:4]
    if ca.isdigit() and 1980 <= int(ca) <= 2100:
        return int(ca)
    y = _extract_year((d.get("title") or ""), d.get("content") or "")
    return y


def _derived_pdf_abspath(d: dict) -> str:
    year = _derived_year(d) or "unknown"
    return os.path.join(PDF_ROOT, str(year), (d.get("id") or "derived") + ".pdf")


def save_derived_pdf(d: dict, data: bytes) -> str:
    """把二次生成 PDF 字节落盘，并在记录中登记 pdf_path/pdf_updated_at，返回绝对路径。"""
    import shutil
    path = _derived_pdf_abspath(d)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    items = _load()
    for x in items:
        if x.get("id") == d.get("id"):
            x["pdf_path"] = os.path.relpath(path, KB_DIR).replace("\\", "/")
            x["pdf_updated_at"] = _now()
            break
    _save(items)
    return path


def get_cached_pdf(d: dict):
    """返回已缓存的二次生成 PDF 字节；若不存在或内容已变更（记录比缓存新）则返回 None。"""
    rel = d.get("pdf_path")
    if not rel:
        return None
    p = os.path.join(KB_DIR, rel)
    if not os.path.exists(p):
        return None
    # 内容变更（updated_at 晚于 pdf 生成时间）则缓存失效
    if d.get("pdf_updated_at") and d.get("updated_at") and d["pdf_updated_at"] < d["updated_at"]:
        return None
    try:
        with open(p, "rb") as f:
            return f.read()
    except Exception:
        return None


def delete_derived_pdf(did: str):
    """删除某衍生版本关联的 PDF 文件（内容更新/版本删除时清理）。"""
    for d in _load():
        if d.get("id") == did:
            rel = d.get("pdf_path")
            if rel:
                p = os.path.join(KB_DIR, rel)
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass
                try:
                    od = os.path.dirname(p)
                    if od.startswith(PDF_ROOT) and not os.listdir(od):
                        os.rmdir(od)
                except Exception:
                    pass
            return


def _extract_year(filename: str, content: str):
    """年代提取（与 kb_store 同语义，避免反向依赖）：文件名优先，其次正文年份。"""
    if not filename and not content:
        return None
    for src in ((filename or "")[:200], (content or "")[:200]):
        m = re.findall(r"(?:18|19|20)\d{2}", src or "")
        for y in m:
            yi = int(y)
            if 1980 <= yi <= 2100:
                return yi
    return None


def list_derived(source_doc_id: str = None) -> list:
    """列出衍生版本。可按来源纪要过滤；按创建时间倒序。"""
    items = _load()
    if source_doc_id:
        items = [d for d in items if d.get("source_doc_id") == source_doc_id]
    items.sort(key=lambda d: (d.get("source_doc_id", ""), -(d.get("version", 0) or 0),
                              d.get("created_at", "")))
    return items


def get_derived(derived_id: str) -> dict or None:
    for d in _load():
        if d.get("id") == derived_id:
            return d
    return None


def next_version(source_doc_id: str) -> int:
    """同一来源纪要的下一个衍生版本号。"""
    if not source_doc_id:
        return 1
    versions = [d.get("version", 0) or 0 for d in _load()
                if d.get("source_doc_id") == source_doc_id]
    return (max(versions) + 1) if versions else 1


def create_derived(data: dict) -> dict:
    """创建一份衍生版本。

    data 可含：
      - source_doc_id / source_title / title / requirement / destination /
        parent_id / version / created_by
      - template : 按模板解析后的结构化字段（含 items 等），若存在则用
                   render_minutes 重算 content，保证存储正文与模板一致
      - content  : 若未提供 template，则直接用此纯文本（块选择回退模式）
      - selected_blocks : 被保留的段落/议题序号
    """
    items = _load()
    source_doc_id = data.get("source_doc_id", "")
    tpl = data.get("template")
    renumber = bool(data.get("renumber"))
    # 有模板则按模板重排正文，保证模板模式下存储正文始终符合版式
    if tpl and isinstance(tpl, dict) and tpl.get("structured"):
        if renumber:
            tpl = dict(tpl)
            tpl["items"] = renumber_items(tpl.get("items", []))
        content = render_minutes(tpl)
    else:
        content = (data.get("content") or "").strip()
    derived = {
        "id": "dm_" + uuid.uuid4().hex[:12],
        "title": (data.get("title") or "").strip() or "未命名二次纪要",
        "source_doc_id": source_doc_id,
        "source_title": data.get("source_title", ""),
        "content": content,
        "selected_blocks": data.get("selected_blocks", []) or [],
        "template": tpl if (tpl and isinstance(tpl, dict)) else None,
        "requirement": data.get("requirement", "") or "",
        "destination": data.get("destination", "") or "",
        "version": data.get("version") or next_version(source_doc_id),
        "parent_id": data.get("parent_id") or None,
        "created_by": data.get("created_by", ""),
        "created_at": _now(),
        "updated_at": _now(),
    }
    items.append(derived)
    _save(items)
    return derived


def update_derived(derived_id: str, data: dict) -> dict or None:
    items = _load()
    for d in items:
        if d.get("id") == derived_id:
            for k in ("title", "content", "selected_blocks", "requirement",
                      "destination", "source_title"):
                if k in data:
                    d[k] = data[k]
            if "template" in data:
                tpl = data["template"]
                if tpl and isinstance(tpl, dict) and tpl.get("structured") and data.get("renumber"):
                    tpl = dict(tpl)
                    tpl["items"] = renumber_items(tpl.get("items", []))
                d["template"] = tpl if (tpl and isinstance(tpl, dict)) else None
                if tpl and isinstance(tpl, dict) and tpl.get("structured"):
                    d["content"] = render_minutes(tpl)
            d["updated_at"] = _now()
            _save(items)
            return d
    return None


def delete_derived(derived_id: str) -> bool:
    items = _load()
    target = next((d for d in items if d.get("id") == derived_id), None)
    if not target:
        return False
    pdf_path = target.get("pdf_path")   # 先取出 pdf_path，再删记录（否则 _save 后读不到）
    new_items = [d for d in items if d.get("id") != derived_id]
    _save(new_items)
    # 一并清理关联的二次生成 PDF 文件
    if pdf_path:
        p = os.path.join(KB_DIR, pdf_path)
        try:
            if os.path.exists(p):
                os.remove(p)
            od = os.path.dirname(p)
            if od.startswith(PDF_ROOT) and os.path.isdir(od) and not os.listdir(od):
                os.rmdir(od)
        except Exception:
            pass
    return True
