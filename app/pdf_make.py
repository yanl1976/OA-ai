"""
会议纪要「二次生成」PDF 生成模块。

业务诉求:
  二次生成的会议纪要必须产出一份**真正的 PDF 文件**（而非纯文本/TXT），
  并且版式要**忠实还原标准会议纪要红头文件**（单位名称/文号/落款/会议标题/
  议题），字体、字号、颜色、对齐方式与源文件保持一致。

实现要点:
  - 使用 reportlab 排版，按「角色」注册多套中文字体（解决宋体/仿宋/黑体/
    红头无衬线并用的真实公文版式）：
      redhead 红头单位名  → 无衬线黑体（对应源 WenQuanYiMicroHei）
      hei     标题/议题标题 → 无衬线黑体（对应源 FZXBSJW/SimHei）
      song    文号/次数    → 宋体（对应源 SimSun）
      fangsong 落款/导语/正文/会议决定 → 仿宋（对应源 FangSong）
  - 各角色字体文件路径位于 KB_FONT_DIR 目录（默认 /opt/OA-ai/fonts），
    可用环境变量覆盖；缺失时回退到默认字体。
  - 中文渲染：必须嵌入真实 CJK 字体（TrueType/glyf 轮廓），reportlab 在 build
    时会自动对所用字形做子集化，体积保持小巧。
"""
import os
import io
import re

# 字体目录与默认字体（可用环境变量覆盖）
DEFAULT_FONT = os.environ.get("KB_PDF_FONT",
                              os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                           "..", "fonts", "SHSC.ttf"))
DEFAULT_FONT = os.path.abspath(DEFAULT_FONT)
FONT_DIR = os.environ.get("KB_FONT_DIR", os.path.dirname(DEFAULT_FONT))

# 角色 → 字体文件名。缺失则回退 DEFAULT_FONT。
# 角色语义与源模板对应：
#   redhead/hei → 黑体（红头单位名、会议标题、议题标题）
#   song        → 宋体（文号、次数）
#   fangsong    → 仿宋（落款、导语、正文、会议决定）
_ROLE_FILES = {
    "redhead": "FandolHei-Regular.ttf",
    "hei": "FandolHei-Regular.ttf",
    "song": "FandolSong-Regular.ttf",
    "fangsong": "FandolFang-Regular.ttf",
}

_REGISTERED = {}


def _font(role: str) -> str:
    """返回 reportlab 中该角色字体的注册名（按路径懒注册，自动子集化）。"""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont as RLTTFont
    fname = _ROLE_FILES.get(role, "SHSC.ttf")
    path = os.path.join(FONT_DIR, fname)
    if not os.path.exists(path):
        path = DEFAULT_FONT
    name = "f_" + role
    if name not in _REGISTERED:
        pdfmetrics.registerFont(RLTTFont(name, path))
        _REGISTERED[name] = True
    return name


def _esc(s: str) -> str:
    """转义 XML 特殊字符，供 reportlab Paragraph 使用。"""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _esc_br(s: str) -> str:
    """转义并保留原始换行（\n → <br/>），使导语/正文排版忠实于原文。"""
    return _esc(s).replace("\n", "<br/>")


def _fit_size(text: str, fontname: str, base: float, max_w: float) -> float:
    """若文本宽度超过 max_w，则按比例缩小字号以保证单行显示（红头不允许换行）。"""
    from reportlab.pdfbase import pdfmetrics
    try:
        w = pdfmetrics.stringWidth(text, fontname, base)
    except Exception:  # noqa: BLE001
        return base
    if w <= max_w:
        return base
    return max(8.0, base * max_w / w)


def build_derived_pdf(meta: dict) -> bytes:
    """根据衍生版本记录生成 PDF 字节流。

    meta 需包含: title, source_title, requirement, destination, version,
                 created_at, content（截取后的纯文本）, parent_id(可选),
                 以及可选 template（按标准模板解析的结构化字段）。

    若 meta 含结构化 template，则按正式会议纪要红头版式排版；否则回退到
    「二次生成」说明式排版。
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                    Table, TableStyle, HRFlowable)

    title = (meta.get("title") or "未命名二次纪要").strip() or "未命名二次纪要"
    source_title = meta.get("source_title") or "—"
    requirement = meta.get("requirement") or "—"
    destination = meta.get("destination") or "—"
    version = meta.get("version") or 1
    created_at = meta.get("created_at") or "—"
    content = meta.get("content") or ""
    parent_id = meta.get("parent_id")
    tpl = meta.get("template")
    is_original = bool(meta.get("is_original"))

    structured = bool(tpl and isinstance(tpl, dict) and tpl.get("structured"))

    # 源模板实测排版参数（来自正式会议纪要 PDF 的精确测量）
    RED = colors.Color(1, 0, 0)                      # 红头纯红 0xff0000
    LEFT = 79.4                                      # 正文左界（pt）
    RIGHT = 67.9                                     # 正文右界（pt）
    OFFICE_INDENT = 90.8 - LEFT                      # 落款左缩进（使单位名与源对齐）
    OFFICE_TAB = 494.9 - 90.8                        # 落款右对齐截止（使日期与源对齐）
    FRAME_W = A4[0] - LEFT - RIGHT                   # 正文可用宽度（pt）

    # 样式（字号/字体/颜色/对齐严格对齐源文件）
    red_font = _font("redhead")
    red_size = _fit_size(((tpl or {}).get("org") or "").strip() or " ", red_font, 39.6,
                         FRAME_W * 0.98)
    s_red_org = ParagraphStyle("redorg", fontName=red_font, fontSize=red_size,
                               leading=red_size * 1.12, alignment=TA_CENTER,
                               textColor=RED, spaceAfter=4, splitLongWords=0,
                               wordWrap=None)
    s_docno = ParagraphStyle("docno", fontName=_font("song"), fontSize=15.9,
                             leading=22, alignment=TA_CENTER, textColor=colors.black,
                             spaceAfter=6)
    s_office = ParagraphStyle("office", fontName=_font("fangsong"), fontSize=15.9,
                              leading=22, alignment=TA_LEFT, spaceAfter=8)
    s_office_r = ParagraphStyle("office_r", fontName=_font("fangsong"), fontSize=15.9,
                                leading=22, alignment=TA_RIGHT, spaceAfter=8)
    s_mname = ParagraphStyle("mname", fontName=_font("hei"), fontSize=22,
                             leading=30, alignment=TA_CENTER, textColor=colors.black,
                             spaceBefore=8, spaceAfter=2)
    s_seq = ParagraphStyle("seq", fontName=_font("song"), fontSize=15.9,
                           leading=22, alignment=TA_CENTER, textColor=colors.black,
                           spaceAfter=8)
    # 正文段落：首行缩进 2 字符，按 CJK 规则整行填满换行（不随意断行）
    FIRST_INDENT = 15.9 * 2
    s_intro = ParagraphStyle("intro", fontName=_font("fangsong"), fontSize=15.9,
                             leading=24, alignment=TA_LEFT, textColor=colors.black,
                             firstLineIndent=FIRST_INDENT, wordWrap="CJK", spaceAfter=6)
    s_item_title = ParagraphStyle("ititle", fontName=_font("hei"), fontSize=15.9,
                                  leading=22, alignment=TA_LEFT, spaceBefore=10,
                                  spaceAfter=4, textColor=colors.black)
    s_item_body = ParagraphStyle("ibody", fontName=_font("fangsong"), fontSize=15.9,
                                 leading=24, alignment=TA_LEFT, spaceAfter=4,
                                 firstLineIndent=FIRST_INDENT, wordWrap="CJK",
                                 textColor=colors.black)
    s_attend = ParagraphStyle("att", fontName=_font("fangsong"), fontSize=15.9,
                              leading=24, alignment=TA_LEFT, spaceBefore=6,
                              firstLineIndent=FIRST_INDENT, wordWrap="CJK",
                              textColor=colors.black)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=LEFT, rightMargin=RIGHT,
                            topMargin=100, bottomMargin=80,
                            title=title, author="知识库系统")

    story = []

    # ===== 模板版式：忠实还原标准会议纪要红头文件 =====
    if structured:
        # 优先使用 header_lines（按行解析的红头），直接按行渲染
        header_lines = tpl.get("header_lines") or []
        if header_lines:
            # 第1行：单位名（红头，居中红色）
            if len(header_lines) > 0:
                story.append(Paragraph(_esc(header_lines[0]), s_red_org))
            # 第2行：文号（居中黑色）
            if len(header_lines) > 1:
                story.append(Paragraph(_esc(header_lines[1]), s_docno))
            # 第3行：落款（办公室+日期，左/右分开）
            if len(header_lines) > 2:
                ol = header_lines[2]
                dm = re.search(r"\d{4}\s*年", ol)
                if dm:
                    office_name = ol[:dm.start()].strip()
                    office_date = re.sub(r"\s+", "", ol[dm.start():])
                else:
                    office_name = ol
                    office_date = ""
                if office_name or office_date:
                    off_tbl = Table([[Paragraph(_esc(office_name), s_office),
                                      Paragraph(_esc(office_date), s_office_r)]],
                                   colWidths=[FRAME_W * 0.45, FRAME_W * 0.55])
                    off_tbl.setStyle(TableStyle([
                        ("ALIGN", (0, 0), (0, 0), "LEFT"),
                        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 0),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                        ("TOPPADDING", (0, 0), (-1, -1), 0),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                    ]))
                    story.append(off_tbl)
            # 红色分隔线
            story.append(HRFlowable(width="100%", thickness=2.0, color=RED,
                                    spaceBefore=4, spaceAfter=12))
            # 第4行：会议名称
            if len(header_lines) > 3:
                story.append(Paragraph(_esc(header_lines[3]), s_mname))
            # 第5行：会议次数
            if len(header_lines) > 4:
                story.append(Paragraph(_esc(header_lines[4]), s_seq))
        else:
            # 兼容旧逻辑：无 header_lines 时回退到字段渲染
            if (tpl.get("org") or "").strip():
                story.append(Paragraph(_esc(tpl["org"]), s_red_org))
            if (tpl.get("doc_no") or "").strip():
                story.append(Paragraph(_esc(tpl["doc_no"]), s_docno))
            office_name = (tpl.get("office_name") or "").strip()
            office_date = (tpl.get("office_date") or "").strip()
            if (not office_name and not office_date) and (tpl.get("office_line") or "").strip():
                ol = tpl["office_line"].strip()
                dm = re.search(r"\d{4}\s*年", ol)
                if dm:
                    office_name = ol[:dm.start()].strip()
                    office_date = ol[dm.start():].strip()
                else:
                    office_name = ol
            if office_name or office_date:
                off_tbl = Table([[Paragraph(_esc(office_name), s_office),
                                  Paragraph(_esc(office_date), s_office_r)]],
                               colWidths=[FRAME_W * 0.45, FRAME_W * 0.55])
                off_tbl.setStyle(TableStyle([
                    ("ALIGN", (0, 0), (0, 0), "LEFT"),
                    ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                    ("TOPPADDING", (0, 0), (-1, -1), 0),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]))
                story.append(off_tbl)
            story.append(HRFlowable(width="100%", thickness=2.0, color=RED,
                                    spaceBefore=4, spaceAfter=12))
            if (tpl.get("meeting_name") or "").strip():
                story.append(Paragraph(_esc(tpl["meeting_name"]), s_mname))
            if (tpl.get("meeting_seq") or "").strip():
                story.append(Paragraph(_esc(tpl["meeting_seq"]), s_seq))
        # 导语/议题/出席列席
        if (tpl.get("intro") or "").strip():
            story.append(Paragraph(_esc_br(tpl["intro"]), s_intro))
        items = tpl.get("items", [])
        single_item = len(items) == 1  # 单议题：原文无章节号，渲染时去掉可能的前导序号
        for it in items:
            title = (it.get("title") or "").strip()
            if title:
                if single_item:
                    # 去掉前导章节号（如「一、」「1.」），保留议题标题本身加粗显示
                    title = re.sub(r"^\s*(?:[一二三四五六七八九十]+、|\d+[.．、])\s*", "", title)
                story.append(Paragraph(_esc(title), s_item_title))
            if (it.get("body") or "").strip():
                story.append(Paragraph(_esc_br(it["body"]), s_item_body))
            if (it.get("decision") or "").strip():
                story.append(Paragraph(_esc_br(it["decision"]), s_item_body))
        if (tpl.get("present") or "").strip():
            story.append(Paragraph(_esc_br(tpl["present"]), s_attend))
        if (tpl.get("absent") or "").strip():
            story.append(Paragraph(_esc_br(tpl["absent"]), s_attend))

        doc.build(story)
        return buf.getvalue()

    # ===== 回退版式：二次生成说明式 =====
    s_title = ParagraphStyle("t", fontName=_font("hei"), fontSize=18, leading=26,
                            alignment=TA_CENTER, spaceAfter=2)
    s_sub = ParagraphStyle("s", fontName=_font("fangsong"), fontSize=9.5, leading=14,
                           alignment=TA_CENTER, textColor=colors.HexColor("#666666"))
    s_lbl = ParagraphStyle("lbl", fontName=_font("fangsong"), fontSize=9.5, leading=15,
                          textColor=colors.HexColor("#333333"))
    s_val = ParagraphStyle("val", fontName=_font("fangsong"), fontSize=9.5, leading=15)
    s_h2 = ParagraphStyle("h2", fontName=_font("hei"), fontSize=12.5, leading=18,
                         spaceBefore=8, spaceAfter=4)
    s_body = ParagraphStyle("body", fontName=_font("fangsong"), fontSize=10.5, leading=19,
                           alignment=TA_LEFT, spaceAfter=6)

    story.append(Paragraph("二次生成会议纪要" if not is_original else (title or "文档"), s_title))
    story.append(Paragraph("（基于原始会议纪要截取生成）" if not is_original else "（知识库原始文档打印件）", s_sub))
    story.append(Spacer(1, 8))

    info_rows = [
        ("标题", title),
        ("来源纪要", source_title),
        ("文件需求", requirement),
        ("文件去向", destination),
        ("版本", "v" + str(version)),
        ("生成时间", created_at),
    ]
    if parent_id:
        info_rows.append(("衍生链", "基于上级衍生版本再生成"))
    info_rows.append(("正文篇幅", "%d 字" % len(re.sub(r"\s+", "", content))))

    table_data = [[Paragraph(_esc(k), s_lbl), Paragraph(_esc(v), s_val)] for k, v in info_rows]
    t = Table(table_data, colWidths=[28 * mm, 128 * mm])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f2f4f8")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dddddd")),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(t)
    story.append(HRFlowable(width="100%", thickness=0.8,
                            color=colors.HexColor("#cccccc"),
                            spaceBefore=10, spaceAfter=8))
    story.append(Paragraph("正文", s_h2))

    paras = _split_body_paragraphs(content)
    if not paras:
        paras = ["（无正文内容）"]
    for p in paras:
        story.append(Paragraph(_esc(p), s_body))

    doc.build(story)
    return buf.getvalue()


def build_source_pdf(doc: dict) -> bytes:
    """将原版知识库文档生成 PDF 字节流（供「原版 PDF 预览」使用）。

    doc 需含: filename/label, full_text/text, category。
    若正文可识别为标准会议纪要模板则按红头版式渲染，否则按纯文本版式渲染。
    标记 is_original=True，页脚说明为『原始文件打印件』而非『二次生成』。
    """
    text = (doc.get("full_text") or doc.get("text") or "").strip()
    title = (doc.get("label") or doc.get("filename") or "文档").strip() or "文档"
    meta = {
        "title": title,
        "source_title": title,
        "requirement": "",
        "destination": "",
        "version": 1,
        "created_at": doc.get("created_at") or "",
        "content": text,
        "parent_id": None,
        "is_original": True,
    }
    if text:
        try:
            from derived_store import parse_minutes
            st = parse_minutes(text)
            if st and st.get("structured"):
                meta["template"] = st
        except Exception:  # noqa: BLE001
            pass
    return build_derived_pdf(meta)


# ---------------- 正文段落拆分（换行归一化兜底） ----------------
_PARAGRAPH_START_RE = re.compile(
    r"^\s*(?:"
    r"[（(]?[一二三四五六七八九十百千零\d]+[、.)）]\s"   # 一、 （一） 1.
    r"|第[一二三四五六七八九十百千零\d]+[章节条]\s"        # 第一条 / 第三章
    r"|(会议|出席|列席|主持|审阅|记录|抄送|印发|主题词|报送)\s*[:：]?"
    r")"
)
_END_PUNCT_SET = set("。！？；：”’）】」……")


def _split_body_paragraphs(text):
    """把正文文本拆分为逻辑段落列表（换行归一化）。

    兼容两种来源：
      - 已归一化文本（段落间以空行分隔）：按空行分段，段内残留换行再行内合并；
      - 未经归一化的逐行抽取文本：逐行合并，仅在句末标点 / 条款序号 / 空行处断段。
    避免把 PDF 逐行抽取产生的『行内换行』误当成段落，从而导致每段一行、版面混乱。
    """
    if not text:
        return []
    out = []
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        if "\n" not in block:
            out.append(block)
            continue
        cur = ""
        for s in block.split("\n"):
            s = s.rstrip()
            if s == "":
                if cur:
                    out.append(cur)
                    cur = ""
                continue
            if cur == "":
                cur = s
            elif _PARAGRAPH_START_RE.match(s) or cur[-1:] in _END_PUNCT_SET:
                out.append(cur)
                cur = s
            else:
                cur = cur + s
        if cur:
            out.append(cur)
    return [p.strip() for p in out if p.strip()]
