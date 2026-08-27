#!/usr/bin/env python3
"""文档文本抽取层。

支持格式:
  - 纯文本类: .txt / .md / .csv  -> 标准库解码 (UTF-8 / GBK)
  - Office Open XML (标准库 zipfile + ElementTree 解析, 零三方依赖):
        .docx  Word
        .xlsx  Excel
        .pptx  PowerPoint
  - PDF: 依赖已安装的 pypdf (venv 内)
  - LLM 提取: 当 USE_LLM=true 时，使用大模型提取结构化内容

返回: (text: str, warn: str|None)
  text 为抽取出的纯文本（供索引与预览）；warn 为告警信息（如为空/部分失败）。
"""
import io
import os
import re
import zipfile
import json
import time
import xml.etree.ElementTree as ET
from pathlib import Path

# 尝试加载环境变量
try:
    from dotenv import load_dotenv
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
except ImportError:
    pass

import os

# 复用统一 LLM 调用层（MiniMax，OpenAI 兼容）
try:
    from .llm import structured_extract, is_configured
except ImportError:  # 直接以脚本运行时的回退
    from llm import structured_extract, is_configured

ALLOWED_EXT = {".txt", ".md", ".csv", ".docx", ".xlsx", ".pptx", ".pdf"}

# LLM 配置：默认关闭，可在 .env 中设置 USE_LLM=true 并配置 MINIMAX_API_KEY 启用
# 启用后，PDF/DOCX 会先用基础解析得到原始文本，再交由大模型做结构化排版
USE_LLM = os.environ.get("USE_LLM", "false").lower() in ("1", "true", "yes")


# ---------------- LLM 提取 ----------------
def _call_llm(raw_text: str, text: str) -> str:
    """调用 MiniMax API 进行结构化提取（委托给统一的 app/llm.py）。

    大模型只负责「重新排版」：把 PDF/Word 抽取出的、挤在一起的原始文本，
    按中文文档的自然结构（封面、章节、条款、表格）整理成干净的多行纯文本。
    严格忠实于原文，绝不增删内容。
    """
    return structured_extract(text)


def _clean_pdf_layout(text: str) -> str:
    """清理 PDF 抽取产生的版面噪声，喂给 LLM / 兜底回退前统一处理。

    仅删非语义的版面标记，绝不删正文内容：
      - 位置码：独立行的 `2-1-2-1` 这类「页码-栏-段-行」定位码（fitz 按阅读顺序
        拼页时插入），对读者无意义。
      - 页脚块：`集团公司YYYY-M-D\\n发布\\nYYYY-M-D\\n实施` 这类每页重复的发布实施注记。
      - 分页控制符 \\x0c 及紧随其后的 `Q/CT xxx.V0x` 文档编号标记行（下一行已是
        章节号，编号标记非正文）。
    """
    lines = text.split("\n")
    out = []
    # 位置码 + 文档编号：可能独立成行，也可能粘连为 `2-1-4-1Q/CT 303-2022.V01`
    _pos_doc_re = re.compile(r"^\s*\d-\d-\d-\d\s*Q/CT\s+[\w./-]+\.V0\d+\s*$")
    _pos_re = re.compile(r"^\s*\d-\d-\d-\d\s*$")          # 独立位置码行
    _docno_re = re.compile(r"^\s*\x0c?\s*Q/CT\s+[\w./-]+\.V0\d+\s*$")  # 独立文档号行
    for ln in lines:
        if _pos_doc_re.match(ln) or _pos_re.match(ln) or _docno_re.match(ln):
            continue  # 版面定位标记整行剔除
        out.append(ln)
    text = "\n".join(out)
    # 页脚发布/实施块（跨行，每页重复）：`XXX集团有限公司YYYY-M-D 发布 YYYY-M-D 实施`
    # 需整段删除（含单位名前缀，如「天水电气传动研究所集团有限公司2025-8-11 发布...」），
    # 否则只删「集团有限公司」起的后半段会残留单位名前半（如「天水电气传动研究所」）。
    text = re.sub(
        r"[一-龥（(][一-龥（）()\s]*?(?:集团有限公司|公司)\d{4}[-/]\d{1,2}[-/]\d{1,2}\s*发布\s*\d{4}[-/]\d{1,2}[-/]\d{1,2}\s*实施(?:\s*\x0c?\s*Q/[A-Z]{1,5}\s*[\w./-]+(?:\.V\d{1,2})?)?",
        "",
        text,
    )
    # 附录模板里的占位发布/实施行（如 `标准化委员会20XX-XX-XX 发布` / `20XX-XX-XX 实施`）
    text = re.sub(r"20XX-XX-XX\s*发布", "", text)
    text = re.sub(r"20XX-XX-XX\s*实施", "", text)
    # 残留分页符
    text = text.replace("\x0c", "")
    # 文档编号 Q/CT xxx.Vxx：仅删除「页眉/页脚」位置的编号——编号位于行首（前仅空白/
    # 分页符，非中文），避免误删正文中被引用的标准号（如「引用 Q/CT 304-2022.V01 的规定」）。
    # 版本号允许 .V1 / .V01（1-2 位）；允许编号前后粘连位置码/分页符/空白。MULTILINE 使 ^ 逐行。
    text = re.sub(
        r"(?m)^[\x0c\s]*Q/[A-Z]{1,5}\s*[\w./-]+(?:\.V\d{1,2})?[\x0c\s]*",
        "",
        text,
    )
    return text


def _extract_with_llm(raw: bytes, filename: str, category: str = None) -> str:
    """使用 LLM 提取文档内容。"""
    # 先用基础方法提取原始文本（兜底基准）；标准类走保守模式保留全部文字
    raw_text = _decode_pdf(raw, category=category) if filename.lower().endswith('.pdf') else _decode_text(raw)
    # 清理版面噪声（位置码/页脚/分页标记），让 LLM 聚焦正文、输出更干净
    raw_text = _clean_pdf_layout(raw_text)

    # 调用 LLM 进行结构化（严格忠实原文）
    structured_text = _call_llm(raw_text, raw_text)

    # 兜底：若 LLM 结果明显短于原始（可能丢内容），回退原始文本，保证不丢信息
    import re as _re
    def _norm(s):
        return _re.sub(r"\s+", "", s)
    if _norm(structured_text) and len(_norm(structured_text)) < 0.98 * len(_norm(raw_text)):
        # LLM 结果相对原始丢失超过 2%，视为不可靠，回退到保守原始提取，保证不丢内容
        return raw_text
    return structured_text


def _local(tag: str) -> str:
    """去掉 XML 命名空间前缀，取本地标签名。"""
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _decode_text(raw: bytes) -> str:
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


# ---------------- Office Open XML (docx/xlsx/pptx) ----------------
def _extract_docx(raw: bytes) -> str:
    z = zipfile.ZipFile(io.BytesIO(raw))
    root = ET.fromstring(z.read("word/document.xml"))
    body = None
    for el in root.iter():
        if _local(el.tag) == "body":
            body = el
            break
    out = []

    def collect_text(node) -> str:
        return "".join(t.text or "" for t in node.iter() if _local(t.tag) == "t")

    def walk(node):
        for child in node:
            tag = _local(child.tag)
            if tag == "p":
                out.append(collect_text(child))
            elif tag == "tbl":
                rows = []
                for tr in child:
                    if _local(tr.tag) != "tr":
                        continue
                    cells = []
                    for tc in tr:
                        if _local(tc.tag) != "tc":
                            continue
                        cells.append(collect_text(tc))
                    if cells:
                        rows.append("\t".join(cells))
                if rows:
                    out.append("\n".join(rows))
            else:
                walk(child)

    if body is not None:
        walk(body)
    return "\n".join(out)


def _extract_xlsx(raw: bytes) -> str:
    z = zipfile.ZipFile(io.BytesIO(raw))
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        sroot = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in sroot:
            shared.append("".join(t.text or "" for t in si.iter()
                                  if _local(t.tag) == "t"))
    sheets = sorted(
        [n for n in z.namelist()
         if re.match(r"xl/worksheets/sheet\d+\.xml$", n)],
        key=lambda n: int(re.search(r"(\d+)", n).group(1)),
    )
    out = []
    for sh in sheets:
        sroot = ET.fromstring(z.read(sh))
        for row in sroot.iter():
            if _local(row.tag) != "row":
                continue
            cells = []
            for c in row:
                if _local(c.tag) != "c":
                    continue
                t = c.get("t")
                v = None
                for sub in c:
                    lt = _local(sub.tag)
                    if lt == "v":
                        v = sub.text
                    elif lt == "is":
                        v = "".join(tt.text or "" for tt in sub.iter()
                                    if _local(tt.tag) == "t")
                if t == "s" and v is not None:
                    try:
                        v = shared[int(v)]
                    except (ValueError, IndexError):
                        v = ""
                if v:
                    cells.append(v)
            if cells:
                out.append("\t".join(cells))
        out.append("")  # 工作表之间空行
    return "\n".join(out)


def _extract_pptx(raw: bytes) -> str:
    z = zipfile.ZipFile(io.BytesIO(raw))
    slides = sorted(
        [n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)],
        key=lambda n: int(re.search(r"(\d+)", n).group(1)),
    )
    out = []
    for i, sl in enumerate(slides, 1):
        sroot = ET.fromstring(z.read(sl))
        txt = "".join(t.text or "" for t in sroot.iter()
                      if _local(t.tag) == "t")
        out.append("【幻灯片 %d】\n%s" % (i, txt))
    return "\n\n".join(out)


# ---------------- PDF (pypdf) ----------------
# 段落起始标记（条款序号 / 公文要素），命中则强制开启新段落
# 注意：「数字.」后面的 (?![\d]) 负向断言：防止「923.51」「1.5亿」这类
# 小数/金额被误判为「第923项议题」而错误断段（这是议题正文任意换行的主因之一）。
_PARAGRAPH_START = re.compile(
    r"^\s*(?:"
    r"[（(]?[一二三四五六七八九十百千零\d]+[、.)）](?![\d])\s*"   # 一、 （一） 1.
    r"|第[一二三四五六七八九十百千零\d]+[章节条]\s*"        # 第一条 / 第三章
    r"|(出席|列席|主持|审阅|记录|抄送|印发|主题词|报送)\s*[:：]?"
    r")"
)
# 句末标点（当前行以此结尾视为段落结束）
_END_PUNCT = set("。！？；：”’）】」…：…")

# 标准条款标题（管理标准 / 工作标准 / 国标 GB/T 1.1 风格）：
# 用于在无序号的短标题与正文之间断段，避免「范围本标准规定了…」「标准编写的
# 基本要求贯彻国家…」这类条款标题被拼进正文挤成一坨。
# 覆盖两类：
#   1) 编号式条款标题：『1 范围』『2 规范性引用文件』『3.1 管理标准』『4.2.1 职责』
#   2) 常见标准条款名（无编号）：范围/定义/术语和定义/职责/管理程序/要求/附录/前言…
_CLAUSE_TITLE_WORDS = (
    r"范围|规范性引用文件|引用文件|定义|术语和定义|术语|符号|符号和缩略语|缩略语"
    r"|职责|管理内容|管理程序|管理要求|工作要求|技术要求|一般要求|总则|基本要求"
    r"|方法|流程|检查与考核|考核|报告和记录|记录|附录|前言|引言|编制与解释|实施"
    r"|引用|标注|标志|包装|运输|贮存|安全|环境保护|培训|评审|监督|改进"
    r"|管理标准|工作标准|技术标准|程序|程序文件|标准编写的基本要求|编写规定|编写要求"
    r"|结构|起草|封面|目次|名称|文体|统一性|资料性概述要素|规范性一般要素|资料性补充要素"
)
# 编号式：数字/小数编号后接标题词（可无空格），如「1 范围」「3.1管理标准」
_CLAUSE_TITLE_RE = re.compile(
    r"^\s*(?:"
    r"\d+(?:\.\d+)*\s*" + _CLAUSE_TITLE_WORDS + r"?"   # 1 范围 / 3.1 管理标准
    r"|" + _CLAUSE_TITLE_WORDS + r"(?![:：])"            # 纯条款名（非以冒号收尾，避免误吞「定义：」正文）
    r")\s*$"
)
# 更宽松的编号式条款标题：仅「数字编号 + 短中文标题」亦可断段（兜底 GB/T 编号体系）
_CLAUSE_TITLE_NUM_RE = re.compile(r"^\s*\d+(?:\.\d+)*\s+[一-龥A-Za-z]{2,14}\s*$")
# 强结构名单行（出席/列席人员）：其后续行（可能因 PDF 块边界产生空行）应
# 直接拼接为单行姓名，不应断段——区域化排版的一部分（与 derived_store.py 的
# 出席/列席解析逻辑保持一致，把语义判断前置到提取阶段）。
_ATTEND_LINE_RE = re.compile(r"^\s*(出席|列席|参加|参会)人员\s*[:：]")


def _split_header_line(s: str) -> tuple:
    """把合并的红头行按关键字拆分成多个独立行，并保留尾部导语/正文。

    返回 (header_lines, tail)，其中 tail 是会议次数之后的剩余文本（通常是
    导语段落），若无剩余则为空字符串。

    以天传所总经理办公会纪要为例，合并行形如：
      天水电气传动研究所集团有限公司纪要天研司会议纪要〔2024〕59号
      天传所集团办公室2024年9月30日总经理办公会会议纪要（2024年第二十八次）导语...

    应拆为 5 行（顺序）：
      1. 天水电气传动研究所集团有限公司纪要      ← 单位名称（以"纪要"结尾）
      2. 天研司会议纪要〔2024〕59号              ← 部门纪要 + 文号
      3. 天传所集团办公室2024年9月30日          ← 落款办公室 + 日期
      4. 总经理办公会会议纪要                    ← 会议名称
      5. （2024年第二十八次）                    ← 会议次数

    实现策略：从右往左用稳定锚点切分——会议次数 → 落款日期 → 会议名称 →
    文号/部门纪要 → 单位名称，避免左往右启发式误吞（如把"天研司"并入单位名）。
    若无法稳定切分则退回整行，交由上层按整行处理。
    """
    if not s:
        return [], ""
    s = s.strip()
    # PDF 抽取常在数字与中文间插入空格（如 "2024 年"），整体规整便于按关键字定位
    sn = re.sub(r"\s+", "", s)

    result = []  # 从右往左收集，最后反转

    # 1) 会议次数：行尾的（…次）或(…次)
    m = re.search(r"[（(][^（）()]{0,40}次[）)]", sn)
    if m:
        result.append(m.group(0))
        sn = sn[:m.start()]
    else:
        # 无会议次数（极少见）直接退回整行，交由上层按整行处理
        return [s], ""

    # 在原始字符串 s 中定位会议次数，以便保留之后的导语/正文
    seq_in_s = re.search(r"[（(]\s*\d{4}\s*年[^（）()]{0,40}次\s*[）)]", s)
    tail = s[seq_in_s.end():].strip() if seq_in_s else ""

    # 2) 落款办公室 + 日期 + 会议名称：三条独立锚点，区间互斥分段
    #    支持：办公室在日期前/后、会议名称在日期前/后、无办公室简版。
    dm = re.search(r"\d{4}年\d{1,2}月\d{1,2}日", sn)
    okw = re.search(r"(?:办公室|综管办)", sn)
    if not dm:
        return [s], tail  # 无日期则无法稳定锚定落款，退回整行
    # 会议名称：取最后一个「会议纪要」，向前扩展直到遇到非会议修饰词的字符
    # （支持会议名称在日期前或后两种排版；无会议名时跳过，仅拆落款+次数）。
    meet_idx = sn.rfind("会议纪要")
    meeting_name = ""
    if meet_idx >= 0:
        _MEET_WORDS = set("会议纪要坚持办公总经理")  # 向左扩界用的会议修饰词（不含单位结尾词）
        ms = meet_idx
        while ms > 0 and sn[ms - 1] in _MEET_WORDS:
            ms -= 1
        meet_start, meet_end = ms, meet_idx + 4
        meeting_name = sn[meet_start: meet_end]
    else:
        # 无会议名：落款起点从日期起（单位行留空，避免日期被误当单位行重复）
        meet_start, meet_end = dm.start(), dm.start()

    # 落款区间：覆盖「办公室 + 日期」，但终点不含会议名称
    #   - 有办公室：起点 = 办公室前最近的单位名结尾词；终点 = max(办公室尾, 日期尾)
    #   - 无办公室：落款行 = 日期本身
    if okw:
        office_start = okw.start()
        search_seg = sn[max(0, office_start - 40):office_start]
        unit_end = -1
        for kw in ("号", "集团", "公司", "研究所", "研究院", "局", "院", "委员会", "部"):
            p = search_seg.rfind(kw)
            if p >= 0:
                unit_end = p + len(kw)
                break
        if unit_end >= 0:
            office_start = max(0, office_start - 40) + unit_end
        office_end = max(okw.end(), dm.end())
        # 若会议名称在日期之后（meet_start >= office_end），落款终点只到 office_end（不含会议名）
        if meet_start >= office_end:
            office_end = min(office_end, meet_start)
        office_line = sn[office_start: office_end]
    else:
        office_line = dm.group(0)  # 无办公室关键字，落款行仅含日期
        office_start = dm.start()
    # 若会议名称在落款之前（meet_end <= office_start），落款起点从日期起（会议名已单独成行）
    if meet_end <= office_start:
        office_line = sn[dm.start(): max(okw.end() if okw else dm.end(), dm.end())]

    # 单位/文号区间：sn 开头 到 min(落款起点, 会议名起点)
    unit_cut = min(office_start, meet_start)
    rest = sn[:unit_cut]
    docm = re.search(r"〔[^〕]{0,20}〕\d+号", rest)
    if docm:
        jiyao_idx = rest.rfind("会议纪要", 0, docm.start())
        if jiyao_idx < 0:
            return [s], tail
        unit_jiyao = rest.rfind("纪要", 0, jiyao_idx)
        if unit_jiyao < 0:
            return [s], tail
        dept_line = rest[unit_jiyao + 2: docm.end()]
        unit_line = rest[:unit_jiyao + 2]
        result.append(dept_line)
        result.append(unit_line)
    else:
        if rest.strip():
            result.append(rest.strip())
    # 收集顺序（reverse 前）：[次数, 会议名, 落款, 单位/文号]
    # 追加会议名与落款，使 reverse 后变为 [单位, 文号, 落款, 会议名, 次数]
    # （与 parse_minutes 第241-258行消费 hlines 的顺序一致）
    if meeting_name:
        result.append(meeting_name)
    result.append(office_line)

    result.reverse()
    final = [r for r in result if r.strip()]
    if len(final) < 2:
        return [s], tail
    return final, tail


def _clean_pdf_text(text: str) -> str:
    """把 PDF 逐行抽取产生的『行内换行』还原为段落。

    关键约束：红头区（单位名 / 文号 / 落款 / 会议名称 / 会议次数）各元素必须
    各自成行，**不能合并**——这些元素在源文件中本就是独立居中行，若被合并成
    一条，会让会议纪要结构化解析（parse_minutes）把整段误当成文号，导致红头与
    正文全部错位。因此本函数在首个议题 / 导语 / 出席列席标记之前（红头区）逐行
    保留，并在处理红头时按关键字拆分合并的段落。

    红头区判定：首个命中 _PARAGRAPH_START（议题序号 / 公文要素 / 会议-出席-列席）
    的行之前均视为红头；若无任何议题（如纯通知），则整篇按正文合并处理。
    """
    lines = text.split("\n")

    # 1) 定位红头区截止位置：连续命中「红头特征」的行归红头区，
    #    第一个未命中（导语/议题/出席列席）的行起为正文区。
    header_end = len(lines)
    for idx, ln in enumerate(lines):
        s = ln.strip()
        if not s:
            continue
        if _HEADER_LINE_RE.match(s):
            continue
        header_end = idx
        break

    def _strip_noise(s: str) -> str:
        # 中文 PDF 常用全角空格 \u3000 分隔，标准正则 \s 不匹配，统一归一为半角空格
        s = s.replace("\u3000", " ").strip()
        if not s:
            return ""
        # 丢弃疑似页码 / 页眉噪声行（整行仅数字或极短纯符号）。
        # 【关键】仅删除"数字 + 分隔符"类（页码/位置码），纯 1-3 位短数字（如顶层章节号
        # "1" "2" "3"）不能在此吞掉——会丢失章节编号，须交给 process_standard 切出。
        if re.fullmatch(r"[\d\s\-—/．.]+", s) and len(s) <= 6 \
                and not re.fullmatch(r"\d{1,3}", s.strip()):
            return ""
        # 去除行首粘连的页码数字（如抽取为 "1天水电气…" 的 "1"）。
        # 【关键】要求数字与中文之间【无空格】，否则会误删顶层章节号（"1 范围"→ "范围"）。
        # ——顶层章节号格式为"数字 + 空格 + 标题词"，须保留给 process_standard 切出。
        s = re.sub(r"^\d{1,3}(?!\s)(?=[\u4e00-\u9fff])", "", s)
        return s.strip()

    def _is_date_line(s: str) -> bool:
        """纯发文日期短行（如 2025 年10 月17 日），不含其它公文内容。"""
        if len(s) > 24 or "会议纪要" in s or "集团" in s or "公司" in s:
            return False
        return bool(re.fullmatch(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日", s))

    def _looks_like_org_tail(p: str) -> bool:
        """上一条红头是否是发文单位风格（用于单位 + 日期跨块合并）。"""
        return bool(re.search(r"(集团|公司|办公室|研究所|研究院|局|院|所|委员会|部|厅|处)$", p)) \
            or "办公室" in p or "集团" in p or "公司" in p

    paras = []

    # 2) 红头区：逐行保留（每个元素独立成段），但将「发文单位 + 紧随的日期短行」
    #    合并为同一行定义——解决 PyMuPDF 把『天传所集团办公室』与『2025 年10 月17 日』
    #    拆成两个文本块导致的红头定义被强行换行问题。
    #    对于合并的红头行（如"公司名纪要天研司会议纪要〔2024〕59号..."），
    #    调用 _split_header_line 拆分为多行，并把拆分后的尾部导语/正文插入正文区
    body_prefix = ""
    for ln in lines[:header_end]:
        s = _strip_noise(ln)
        if s:
            if ("纪要" in s and ("〔" in s or "办公室" in s or "会议纪要" in s or "（" in s)) or \
               (s.count("纪要") > 1):
                split_lines, tail = _split_header_line(s)
                for split_line in split_lines:
                    if split_line.strip():
                        paras.append(split_line.strip())
                if tail:
                    body_prefix = tail
            elif paras and _is_date_line(s) and _looks_like_org_tail(paras[-1]):
                # 单位行 + 日期行 -> 合并为同一红头定义
                paras[-1] = paras[-1] + " " + s
            else:
                paras.append(s)

    # 3) 正文区（导语+议题）：行内换行合并
    # 关键修复：不再以句末标点（。！？）断段——否则导语 / 议题内容会被“。”切碎成
    # 多个碎片段（非必要换行）。仅以下情形才断段：
    #   - 空行（PDF 文本块之间的间距，视为段落边界）；
    #   - 本行是议题序号 / 公文要素前缀（_PARAGRAPH_START，如 一、 出席：）。
    # 其余 PDF 自动折行一律直接拼接（中文行内连接不加空格），恢复“整段”阅读。
    # 若拆分红头时保留了尾部导语，先放入 cur 与后续正文合并。
    #
    # 换页续行（\f）：PyMuPDF 每页用 \f 连接，跨页相邻两行本属同一段落（如正文在
    # 页尾“破产程序，”换页后接“货款款项……”），若按空行断段会被错误切成两段。
    # 处理规则：换页首行（行首带 \f）若上一行 cur 不以句末标点 / 议题前缀结束，且
    # 本行也非议题 / 公文要素开头 → 视为同一段落跨页续行，直接拼接不断段；仅当 cur
    # 恰好以句末标点 / 议题前缀结束（段落刚好在页尾结束）才断段。
    expanded = []
    for ln in lines[header_end:]:
        if "\f" in ln:
            pre, post = ln.split("\f", 1)
            if pre.strip():
                expanded.append(pre)
            expanded.append("\f" + post)  # 标记：换页后的首行
        else:
            expanded.append(ln)

    cur = body_prefix
    for raw_ln in expanded:
        is_pagebreak = raw_ln.startswith("\f")
        ln = raw_ln[1:] if is_pagebreak else raw_ln
        s = _strip_noise(ln)
        if s == "":
            # 名单行（出席/列席人员：）未以句号结尾时，其后续姓名可能因 PDF
            # 块边界产生空行——此处不立即断段，保持 cur 等待续行拼接为单行姓名。
            if cur and _ATTEND_LINE_RE.match(cur) and not cur.rstrip().endswith("。"):
                continue
            if cur:
                paras.append(cur)
                cur = ""
            continue
        if cur == "":
            cur = s
        elif _PARAGRAPH_START.match(s) or _PARAGRAPH_START.match(cur):
            # 议题序号 / 公文要素前缀 -> 断段
            paras.append(cur)
            cur = s
        else:
            # 续行合并的判据（核心修复）：
            # 「上一行不以句末标点（。！？；：）结尾 且 本行不是议题/公文要素头」
            # 视为同一句的延续，直接拼接，绝不在句中断行——这避免了正文里
            # 「实际↵经济」「资金紧↵张」这类「无句号不应换行」的任意折行。
            # 仅当上一行恰好以句末标点收尾（段落结束）或本行是真正的议题头，
            # 才断段（断段条件仍仅由 空行 / 议题序号 / 公文要素 触发）。
            # 【章节号合并】cur 是纯数字 1-3 位（顶层章节号）→ 加空格拼成 "1 范围"
            if re.fullmatch(r"\d{1,3}", cur.strip()):
                cur = cur + " " + s   # "1" + "范围" → "1 范围"
            elif is_pagebreak and not (cur[-1:] in _END_PUNCT or _PARAGRAPH_START.match(cur)):
                cur = cur + s  # 跨页续行：上一行未结束
            else:
                cur = cur + s  # 行内连接（中文不加空格）
    if cur:
        paras.append(cur)

    # 数字跨行修复：PyMuPDF 常把「124,↵923.51」「2,↵548.33」这类含千分位的
    # 数字从逗号后切断。续行拼接后形态为「124, 923.51」（逗号后带空格），需
    # 还原为「124,923.51」。规则：逗号/顿号（含全角）前为数字、后为空格+数字
    # 时，去掉逗号后的空格；同时兜底处理「数字↵数字」直接粘连（无逗号）。
    def _fix_num_wrap(p: str) -> str:
        # 千分位逗号/顿号后紧跟空格+数字 -> 去掉逗号后的空格，还原「124,923.51」
        p = re.sub(r"(?<=\d)[,，、]\s+(?=\d)", ",", p)
        return p
    paras = [_fix_num_wrap(p) for p in paras]

    return "\n\n".join(p.strip() for p in paras if p.strip())


# 红头结构性行：命中则视为红头元素（独立成行，禁止与相邻行合并）
# 用于划分红头区 / 正文区：连续命中红头特征的行归红头区，第一个未命中行起为正文。
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
    r"|\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日\s*$"                          # 纯发文日期行（红头）
    r")"
)


def _merge_body_lines(lines) -> list:
    """正文区合并：仅在【空行（块间距）】或【议题序号 / 公文要素前缀】处断段，
    不以句末标点（。！？）断段——与 _clean_pdf_text 保持一致，避免对已结构化的
    整段文本（导语 / 议题内容）二次切碎。

    对未结构化的旧数据（逐行碎段、无空行），续行直接拼接即恢复整段。
    """
    paras = []
    cur = ""
    for ln in lines:
        s = (ln or "").replace("\u3000", " ").rstrip()
        if s == "":
            if cur:
                paras.append(cur)
                cur = ""
            continue
        if re.fullmatch(r"[\d\s\-—/．.]+", s) and len(s.strip()) <= 6:
            continue
        s = re.sub(r"^\d{1,3}(?=[\u4e00-\u9fff])", "", s).strip()
        if not s:
            continue
        if cur == "":
            cur = s
        elif _PARAGRAPH_START.match(s) or _PARAGRAPH_START.match(cur):
            # 议题序号 / 公文要素前缀 -> 断段
            paras.append(cur)
            cur = s
        else:
            # 续行合并（句末标点等中间/结尾标点均不断段）
            cur = cur + s
    if cur:
        paras.append(cur)
    return [p.strip() for p in paras if p.strip()]


def merge_lines_to_paragraphs(lines) -> list:
    """将一串文本行合并为逻辑段落列表（供渲染层兜底复用）。

    与 _clean_pdf_text 一致：红头区（首个议题/导语/出席列席标记之前）逐行保留，
    并对合并的红头行进行拆分；正文区按句末标点 / 条款序号合并行内换行。
    """
    lines = [ln for ln in (lines or [])]
    # 定位红头区截止：连续命中红头特征的行归红头区，第一个未命中行起为正文区
    header_end = len(lines)
    for idx, ln in enumerate(lines):
        s = (ln or "").strip()
        if not s:
            continue
        if _HEADER_LINE_RE.match(s):
            continue
        header_end = idx
        break
    # 红头区：逐行保留，不拆分
    header_paras = []
    for ln in lines[:header_end]:
        s = (ln or "").strip()
        if s:
            header_paras.append(s)
    body_paras = _merge_body_lines(lines[header_end:])
    return [p for p in header_paras + body_paras if p.strip()]


def _decode_pdf_pymupdf(raw: bytes, category: str = None) -> str:
    """PDF 底层解码（主引擎）：使用 PyMuPDF 按文本块提取，保留阅读顺序。

    PyMuPDF 的 get_text("blocks") 会按 (x0,y0,x1,y1, text, block_no, block_type)
    返回文本块，配合 sort=True 按阅读顺序（从上到下、从左到右）排序，对多栏
    / 表格 / 复杂排版远比逐行坐标提取鲁棒。图像型 PDF（无文本层）会返回空，
    由调用方给出扫描件告警。

    category 为「管理标准」类时，采用保守模式：保留所有文字块（含图注尺寸、
    页码、位置码等），仅跳过图片块，确保与 PDF 文字层差异最小、不丢内容。
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        raise RuntimeError("服务端未安装 PyMuPDF（pymupdf），无法解析 PDF")

    conservative = bool(category) and ("标准" in category or "standard" in (category or "").lower())
    try:
        doc = fitz.open(stream=raw, filetype="pdf")
    except Exception as e:
        raise RuntimeError(f"PDF 打开失败: {e}")

    parts = []
    for page in doc:
        # blocks=True 返回 (x0,y0,x1,y1, text, block_no, block_type)
        # 文本块 block_type=0；图片块 block_type=1 跳过（图片无文本）
        blocks = page.get_text("blocks", sort=True)
        page_lines = []
        for b in blocks:
            if len(b) < 5:
                continue
            txt = (b[4] or "").strip()
            if not txt:
                continue
            if conservative:
                # 保守模式：仅跳过图片块（block_type=1 已由 len(b)<5 之外处理），
                # 不过滤任何文字块，保留页码/位置码/图注尺寸等，保证零内容丢失
                page_lines.append(txt)
                continue
            # 常规模式：过滤位置码（格式如 2-1-5-1）与纯页码短行
            if re.fullmatch(r"\d+-\d+-\d+-\d+", txt):
                continue
            if re.fullmatch(r"[\d\s\-—/．.]+", txt) and len(txt) <= 6:
                continue
            page_lines.append(txt)
        if page_lines:
            parts.append("\n".join(page_lines))
    doc.close()

    # 页与页之间用 \f（form feed，PDF 语义即分页）连接，而非 \n\n。
    # 这样 _clean_pdf_text 能精准识别「换页导致的换行」：跨页相邻两行若本属
    # 同一段落（前一行未以句末标点 / 议题序号结束，后一行也非议题 / 公文要素
    # 开头），则应合并而非断段——否则正文跨页处会被错误地切成两个段落。
    text = "\f".join(parts)
    return _post_clean_pdf_text(text)


def _decode_pdf_pdfplumber(raw: bytes) -> str:
    """PDF 底层解码（回退引擎）：pdfplumber 按坐标逐行提取。"""
    try:
        import pdfplumber
    except ImportError:
        raise RuntimeError("服务端未安装 pdfplumber 也无法解析 PDF（且 PyMuPDF 缺失）")

    try:
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            parts = []
            for page in pdf.pages:
                txt = page.extract_text(x_tolerance=2, y_tolerance=2) or ""
                if txt.strip():
                    parts.append(txt)
    except Exception as e:
        raise RuntimeError(f"PDF 解析失败: {e}")

    lines = []
    for ln in "\n".join(parts).split("\n"):
        s = ln.strip()
        if re.fullmatch(r"\d+-\d+-\d+-\d+", s):
            continue
        if re.fullmatch(r"[\d\s\-—/．.]+", s) and len(s) <= 6:
            continue
        lines.append(ln)

    text = "\n".join(lines)
    return _post_clean_pdf_text(text)


def _post_clean_pdf_text(text: str) -> str:
    """PDF 文本公共后处理：把粘连的『发布/实施』日期拆成两行。"""
    def split_date_replace(m):
        return "\n".join(m.groups())

    text = re.sub(r"(\d{4}-\d+-\d+)\s*(发布)\s*(\d{4}-\d+-\d+)\s*(实施)", split_date_replace, text)
    text = re.sub(r"(\d{4}-\d+-\d+)(发布)(\d{4}-\d+-\d+)(实施)", split_date_replace, text)
    text = re.sub(r"([^\s]{10,})(\d{4}-\d+-\d+)\s*(发布)\s*(实施)", split_date_replace, text)
    text = re.sub(r"([^\s]{10,})(\d{4}-\d+-\d+)(发布)(\d{4}-\d+-\d+)(实施)", split_date_replace, text)
    text = re.sub(r"([^\s]{10,})(\d{4}-\d+-\d+)(发布)\n\s*(\d{4}-\d+-\d+)(实施)", split_date_replace, text)
    text = re.sub(r"(发布)\n\s*(\d{4}-\d+-\d+)(实施)", split_date_replace, text)
    text = re.sub(r"(发布)\n\s*(\d{4}-\d+-\d+)\s*(实施)", split_date_replace, text)
    text = re.sub(r"(发布)\s+(实施)", split_date_replace, text)
    text = re.sub(r"(发布)(实施)", split_date_replace, text)
    return text


def _decode_pdf(raw: bytes, category: str = None) -> str:
    """PDF 底层解码：优先 PyMuPDF（更强、已默认安装），缺失时回退 pdfplumber。"""
    try:
        return _decode_pdf_pymupdf(raw, category=category)
    except RuntimeError as e:
        # PyMuPDF 缺失或失败，尝试 pdfplumber
        return _decode_pdf_pdfplumber(raw)


# ============================================================================
# 语义后处理器注册表（按文档「类型」分发）
# ----------------------------------------------------------------------------
# 设计：底层按扩展名做二进制解码（txt/docx/xlsx/pptx/pdf），解码出的原始文本
# 再交由「语义后处理器」做结构化整理。不同文档类型（会议纪要 / 管理标准 / 其它）
# 采用不同策略；新增类型只需实现后处理器并用 @register 注册即可，无需改动分发逻辑。
# 分发键 (kind) 取值：minutes（会议纪要）/ standard（管理标准）/ default（其它）。
# ============================================================================
EXTRACTORS = {}


def register(kind):
    """装饰器：把后处理器注册到 EXTRACTORS[kind]。"""
    def deco(fn):
        EXTRACTORS[kind] = fn
        return fn
    return deco


# 分类名 -> 文档类型：会议纪要树与管理标准树各自绑定专属后处理器
_CATEGORY_KIND_MAP = {
    # 会议纪要树
    "会议纪要": "minutes", "总经理会议纪要": "minutes", "专项会议纪要": "minutes",
    # 管理标准树（顶级 + 16 个子类）
    "管理标准分类": "standard",
    "01.标准化类": "standard", "02.基础管理类": "standard", "03.规划管理类": "standard",
    "04.党群管理类": "standard", "05.风险管理类": "standard", "06.行政管理类": "standard",
    "07.科技管理类": "standard", "08.人力资源类": "standard", "09.财审类": "standard",
    "10.营销管理类": "standard", "11.采购管理类": "standard", "12.生产管理类": "standard",
    "13.质量管理类": "standard", "14.安全环保管理类": "standard", "15.安保物业类": "standard",
    # 16.合规管理类：多为「指引/办法/规定」类（章/条结构），非数字多级编号标准条款
    "16.合规管理类": "regulation",
}


def category_to_kind(category):
    """分类名 -> 文档类型；模糊兜底（含『纪要』→minutes，含『标准』→standard）。"""
    if not category:
        return None
    if category in _CATEGORY_KIND_MAP:
        return _CATEGORY_KIND_MAP[category]
    if "纪要" in category:
        return "minutes"
    if "标准" in category:
        return "standard"
    return None


def detect_kind(text):
    """无分类信息时，按内容嗅探文档类型（兜底）。

    关键：管理标准与会议纪要的判定互斥，绝不允许把标准当成会议纪要、或反之。
    """
    head = text[:2000]
    body = text[:4000]
    # 会议纪要优先：含『会议纪要』且含决议/出席要素 -> minutes
    if "会议纪要" in head and re.search(r"会议决定|出席人员|列席人员|主持人|会议时间", body):
        return "minutes"
    # 规章制度/指引：含『第X章』且『第X条』（章/条结构，区别于标准类的数字多级编号）
    if re.search(r"第[0-9零一二三四五六七八九十百千]+章", head) and re.search(r"第[0-9零一二三四五六七八九十百千]+条", body):
        return "regulation"
    # 管理标准：含『管理标准/工作标准/技术标准』字样（不强制 Q/ 编号，企标常无编号）
    # 或正文出现典型标准条款结构（范围/规范性引用文件/术语和定义 连续出现）
    if re.search(r"管理标准|工作标准|技术标准", head):
        return "standard"
    if re.search(r"Q/[A-Z]", head) and re.search(r"范围|规范性引用文件|术语和定义", body):
        return "standard"
    return "default"


# ---------------- 管理标准 后处理器 ----------------
# 页脚标准编号噪声：仅匹配「整行只有标准编号+版本号+页码」的纯噪声行
# 如 "Q/CT 300-2022.V022-1-1-1"，而不是 "公司标准结构和编写规则 Q/CT 304-2022.V01"
# 用 ^...$ 确保整行匹配，且中间不含汉字（封面标题行有汉字伴随）
_STD_FOOTER_RE = re.compile(r"^\s*Q/[A-Z]{1,5}\s*\d+[-—]\d{4}(?:\.\w+)?[-–—]\d+.*$")
# 条款序号：只匹配多级编号（3.1 / 1.1.1 / A.1），避免把正文 "1 次""6 个月内" 误当条款切开。
_STD_CLAUSE_RE = re.compile(r"^\s*(?:[A-Za-z]?\d+(?:\.\d+)+[\s、．.]\s*\S|[A-Za-z]\d+[\s、．.]\s*\S)")
# 顶层条款编号：如 "1 范围"、"2 总则"、"3 结构"（后面跟空格和中文标题）
_STD_TOPNUM_RE = re.compile(r"^\s*\d{1,3}\s+[\u4e00-\u9fff]")
# 表格标题：表 1 管理标准体系表
_STD_TABLE_CAP_RE = re.compile(r"^\s*表\s*\d+")
# 管理标准内部结构切分标记：用于把 PDF 抽到一行的长文本按章节/条款/表格边界切开
# 在匹配项前插入换行，使 "2025-09-29实施2第一章 总 则第一条..." 拆成多行。
# 注意：不预切分单独阿拉伯数字（"1 次" 等），避免正文数量词被误断；
# 第 X 条只在后面跟空格/行尾时才切，避免把 "《公司法》第三十九条、" 这类引用拆开。
_STD_SPLIT_MARKERS = re.compile(
    r"(?:"
    r"Q/[A-Z]{1,5}\s*\d+[-—]\d{4}(?:\.\w+)?"  # 标准编号，如 Q/CT 311-2022.V02
    r"|\d{4}[-—/年]\d{1,2}[-—/月]\d{1,2}[日]?\s*(?:发布|实施)"  # 发布/实施日期
    r"|第[一二三四五六七八九十百千零\d]+章(?=\s|$)"  # 第一章（后接空格/行尾）
    r"|第[一二三四五六七八九十百千零\d]+条(?=\s|$)"  # 第一条（后接空格/行尾）
    r"|[（(][一二三四五六七八九十百千零]+[）)]"  # （一）、（二）
    r"|[A-Za-z]?\d+(?:\.\d+)+[\s、．.]"  # 3.1 / 1.1.1 / A.1
    r"|表\s*\d+"  # 表 1
    r")"
)


def _segment_standard_lines(text: str) -> str:
    """把 PDF 抽到一行的长文本，按标准编号、发布日期、章节、条款、表格等边界切开。"""
    # 第一次切分：在标记前插入换行
    text = _STD_SPLIT_MARKERS.sub(lambda m: "\n" + m.group(0), text)

    # 第二次处理：封面标准编号后插入换行（Q/CT xxx.Vxx 后通常是单位名/日期行）
    # 匹配 Q/CT xxx 后面紧跟中文/日期的情况，包括版本号如 .V02
    text = re.sub(r"(Q/[A-Z]{1,5}\s*\d+[-—]\d{4}(?:\.\w+)?)(?=[\u4e00-\u9fff天水])", r"\1\n", text)

    # 处理标准编号后直接跟数字的情况（如 "Q/CT 300-2022.V021 范围"）
    text = re.sub(r"(Q/[A-Z]{1,5}\s*\d+[-—]\d{4}(?:\.\w+)?)(\d+\s+[\u4e00-\u9fff])", r"\1\n\2", text)

    # 注意：不再做章节编号切分，因为原始PDF抽取通常已有正确换行
    # 避免把日期年份"2022"误切成 "202\n2"
    return text


def _strip_std_tail_noise(s: str) -> str:
    """去除管理标准行尾常见的页码/图号噪声。"""
    # 行尾图号串：...-1-1-1（但跳过日期行，如 2022-11-28发布）
    # 只处理类似 "Q/CT 302-2022.V01-1-1-1" 这种格式
    if not re.search(r"\d{4}-\d+-\d+", s):  # 跳过包含日期的行
        s = re.sub(r"[-–—]\d+(?:[-–—]\d+)*\s*$", "", s).strip()
    # 行尾单独数字，且前面是发布/实施/中文标点：发布2、；2
    # 但如果是日期格式（如 2022-11-28），则跳过
    if not re.search(r"\d{4}-\d+-\d+$", s):
        s = re.sub(r"(?<=[实施发布；。：])\s*\d{1,3}\s*$", "", s).strip()
    return s


# 行内切分专用标记（用于管理标准逐行处理前）：把「多级编号 + 标题/正文」在行内切
# 成独立片段，使 3.1 / 4.1.1 这类条款号能独立成段，恢复章节层级。
# 注意：刻意【不含】Q/CT 标准编号分支——避免历史 bug（Q/CT 303-2022.V01 被切成
# V0 1）；标准编号整行保留。第X章/第X条/（一）不论前后是否有中文都切（管理标准的
# 章节号几乎总粘连在正文后，必须强切）；多级编号 A?\d+(\.\d+)+ 后接空白/标点/行尾都切
# （图片型 OCR 文本里编号常在行尾，如 '3.2.6'）。
_STD_INLINE_SPLIT = re.compile(
    r"(?:"
    r"第[一二三四五六七八九十百千零\d]+章"                  # 第一章
    r"|第[一二三四五六七八九十百千零\d]+条"                  # 第一条
    r"|[（(][一二三四五六七八九十百千零]+[）)]"            # （一）（二）
    r"|[A-Za-z]?\d+(?:\.\s*\d+)+"                          # 3.1 / 4.1.1 / A.1（OCR 多空格 5. 1.1）
    r"|表\s*\d+|图\s*\d+"                                  # 表 1 / 图 1
    r"|(?<=\s)[A-Za-z]?\d+(?:\.\s*\d+)+\s+[一-龥]{2,8}(?=\s|$|[，。；、])"  # 编号+短中文标题整体切出（3.1 标准体系、4.1 通则、5.1 管理标准体系表），让 process_standard 走 _CLAUSE_TITLE_NUM_RE 识别为标题独立成段
    r")"
)

# 管理标准顶层固定章节标题（图片型 OCR 常把它们粘连在正文句号之后，如
# '...GB/T 20000.2。 结构'）。这些词独立成行才能恢复章节层级；限定为
# 标准里高频出现的章名，避免误切普通正文。
_STD_TOP_TITLES = (
    "范围", "规范性引用文件", "术语和定义", "总则", "结构", "起草", "附录",
    "封面", "目次", "名称", "要求", "文体", "统一性", "资料性概述要素",
    "规范性一般要素", "资料性补充要素", "规范性引用文件",
)
_STD_TOP_TITLE_RE = re.compile(
    r"(?<=[。；；])(\s*(?:" + "|".join(_STD_TOP_TITLES) + r")\s*)(?=[\u4e00-\u9fff（(\s]|$)"
)


def _split_std_line(ln: str) -> list:
    """把一行内连续多个条款（如 '3.1 标准体系... 3.2 标准体系表...'）切成多段。

    在『编号』这类稳定的条款起始处插入换行（编号可出现在行内任意位置，因为图片型
    OCR / 文本型 PDF 抽取的编号常粘连在前文之后）；编号前的普通文字（如
    '本标准采用下列定义。'）保持原样作为前导段落。同时去除 PDF 页脚位置码
    （如 '2-1-1-1'），避免噪声段落。
    """
    # 去除页脚位置码（四段式数字，如 2-1-1-1，可能含空格 2- 1- 1- 1），
    # 不影响标准编号 Q/CT（非四段 -）
    ln = re.sub(r"\d+(?:\s*[-–—]\s*\d+){3}", " ", ln)
    # 顶层固定章名粘连在句号后时，独立成行（图片型 OCR 修复）
    ln = _STD_TOP_TITLE_RE.sub(lambda m: "\n" + m.group(0).strip() + "\n", ln)
    # 在编号前插入换行（编号可紧跟中文，如 '...条文。3.2.3 条'）
    parts = _STD_INLINE_SPLIT.sub(lambda m: "\n" + m.group(0), ln)
    # 二次清理：编号后若紧跟中文但被 OCR 多空格分隔（如 '5. 1.1' 已是单编号），不再处理
    return [p.strip() for p in parts.split("\n") if p.strip()]


@register("standard")
def process_standard(text):
    """管理标准：与会议纪要提取方式严格区分——按「标准条款结构」断段，但基础合并
    仍遵循「句末标点」原则（与会议纪要的合并逻辑同源，仅断段判据不同：会议纪要用
    议题序号，标准用条款标题）。

    关键修复：不再调用 _segment_standard_lines 做激进切行（会把 Q/CT 标准编号切碎）。
    改为：① 逐行先做【行内切分】——把一行内连续多个多级条款号（3.1 / 4.1.1 / (一) /
    第一章 / 第一条）切成独立片段，恢复章节层级；② 再用条款标题判据让『范围/定义/
    规范性引用文件』等独立成段；③ 其余正文按句末标点合并，保持连贯。

    严格保留全部文字（标准编号/页码/位置码），与 PDF 文字层一致；仅跳过纯页码行。
    """
    out = []
    cur = ""

    def _is_std_title(s: str) -> bool:
        """判断是否为标准条款标题（独立成段的依据）。"""
        if not s:
            return False
        if _CLAUSE_TITLE_RE.match(s):
            return True
        if _CLAUSE_TITLE_NUM_RE.match(s):
            return True
        if _STD_TOPNUM_RE.match(s):
            return True
        return False

    def flush():
        nonlocal cur
        if cur.strip():
            out.append(cur.strip())
        cur = ""

    for raw_ln in text.split("\n"):
        # 行内切分：把一行内多个条款号拆成独立片段（保留顺序）
        # 一次性切出本行所有 part，看是否有『纯数字编号孤行』在尾（如 "图表。 3.3"）
        # 若尾 part 是编号孤行且 cur 末以句号收尾，先 flush cur 让编号独立成段
        line_parts = _split_std_line(raw_ln)
        # 检测：最后一个非空 part 是纯数字编号 → 后面编号要独立成段，cur 应先 flush
        last_is_number = False
        for s in line_parts[::-1]:
            if s.strip() == "": continue
            last_is_number = bool(re.fullmatch(r"\d{1,3}(?:\.\d+)+", s.strip()))
            break
        if last_is_number and cur and re.search(r"[。；]$", cur):
            # 上一句以句号收尾，遇到独立编号孤行：先把上一段 flush
            flush()
        for s in line_parts:
            # OCR 同行粘连『正文。 3.3』：cur 末以句号收尾，遇到下一 part 是纯数字编号孤行
            # （如 '3.3'），先把 cur flush 让编号独立成段；否则会与"图表。 3.3"被合到一段
            if cur and re.fullmatch(r"\d{1,3}(?:\.\d+)+", s.strip()) and re.search(r"[。；]$", cur):
                flush()
            # OCR 同行粘连『多级编号 + 中文（标题或完整句子）+ 后续正文』（如
            # "3.1 标准体系 标准按其..." 或 "3.2.1 公司标准应严于国家标准和行业标准。 3.2.2..."）：
            # 把 "编号 + 首个中文片段" 作为条款独立成段（标题或完整短句），后续正文作为新段开头。
            # 该模式由 _STD_INLINE_SPLIT 预切成独立 part，此处按「编号 + 中文开头」识别为条款起点。
            # 注意：编号后中文不限字数（完整句子也算一条独立条款），只取编号到第一个句末标点/行尾作为本条。
            m = re.match(r"^(\d+(?:\.\d+)+\s+[一-龥])", s)
            if m:
                # 切出"编号 + 到第一个句末标点为止"作为一条独立条款；剩余（下一条编号及之后）留给后续 part
                seg = re.match(r"^(\d+(?:\.\d+)+\s+[^。；]*[。；]?)", s)
                head = seg.group(1).strip() if seg else s
                tail = s[seg.end():].strip() if seg else ""
                if cur:
                    flush()
                out.append(head)   # "3.2.1 公司标准应严于国家标准和行业标准。" 独立成段
                cur = tail         # 剩余正文（若含下一条编号，下个 part 继续切）
                continue
            # 顶层编号孤行（如 "1"）位于段首时：暂存，等下一行标题词来合并为
            # "1 范围"，避免编号被当作页码跳过、或编号与标题断裂成两段。
            # （必须放在页码跳过之前，否则纯数字章节号会被误删）
            if re.fullmatch(r"\d{1,3}", s.strip()) and cur == "":
                cur = s
                continue
            # 跳过纯页码行（如独立一行的 "12"，且不在段首、不可能是章节号）。
            # 【关键】cur 非空 + 纯 1-3 位数字 → 不能直接 continue 跳过（否则会丢章节号）；
            # 应先把旧 cur flush 出去，再把数字暂存为新 cur，等下一 part（标题）合并为 "1 范围"。
            if re.fullmatch(r"\d{1,4}", s) or re.fullmatch(r"\d{1,3}(?:\.\d+){1,3}", s):
                if re.fullmatch(r"\d{1,3}(?:\.\d+)*", s.strip()):
                    # 短数字（含多级编号 1.1/3.1.1）：作为章节号。cur 非空先断段，数字暂存等下个标题词
                    if cur:
                        flush()
                    cur = s
                    continue
                # 4 位及以上的纯数字（页码 1000+）才视为页码跳过
                continue
            # 跳过 PDF 页脚位置码（如 "2-1-1-1"，可能含空格）
            if re.fullmatch(r"\d+(?:\s*[-–—]\s*\d+){3}", s):
                continue
            if s == "":
                flush()
                continue
            if cur and re.fullmatch(r"\d{1,3}(?:\.\d+)*", cur.strip()) and (
                _is_std_title(s) or re.fullmatch(r"[一-龥]{2,8}", s.strip())
            ):
                cur = cur + " " + s
                continue
            # 条款标题（无编号纯条款名 / 编号式 / 顶层章节编号 / 行内切出的多级编号）
            # 独立成段；但若当前 cur 是【纯数字编号】孤行（如 "3.1"）、本行是标题词，
            # 则合并为 "3.1 标准的分类"（而非断段）。若 cur 已经是 "3.1 标准的分类"（含中文），
            # 则内层 re.fullmatch 不命中，走 else 把 cur 独立 flush、s 独立成段。
            if _is_std_title(s) or _is_std_title(cur) or _CLAUSE_TITLE_NUM_RE.match(s) or _CLAUSE_TITLE_NUM_RE.match(cur):
                if cur and re.fullmatch(r"\d{1,3}(?:\.\d+)*", cur.strip()):
                    cur = cur + " " + s
                else:
                    flush()
                    out.append(s)
                    cur = ""
                continue
            # 其余：按句末标点合并（不以句末标点结尾则并入当前段，保持正文连贯）
            cur = (cur + " " + s).strip() if cur else s
    flush()
    return "\n\n".join(out)


# ---------------- 规章制度/指引 后处理器（章、条、项结构） ----------------
# 适用于「二级文件/指引/制度/规定/办法/规程」类：层级为 第X章 / 第X条 / （一）（二） / 一、二、
_REG_CHAP_RE = re.compile(r"^\s*第[0-9零一二三四五六七八九十百千]+章")
_REG_ART_RE = re.compile(r"^\s*第[0-9零一二三四五六七八九十百千]+条")
_REG_ITEM_RE = re.compile(r"^\s*[（(][0-9零一二三四五六七八九十]+[)）]")
_REG_SUB_RE = re.compile(r"^\s*[一二三四五六七八九十]+、")
# 流内文档编号噪声（如 2022GLBF-HGGL / 2022GLBF-HGGL 出现在正文流里）
_REG_DOCNO_INLINE_RE = re.compile(r"\b\d{4}[A-Z]{2,}-[A-Z]{2,}\b")
# 开头单位名 + 文件类型 + 编号 行（封面副标题噪声）
_REG_COVER_RE = re.compile(r"^天水电气传动研究所集团有限公司[^\n]*?(指引|制度|规定|办法|规程|标准)[^\n]*$")


def _split_regulation_line(s):
    """把一行内粘连的多个『章/条/项/子项』切分为独立片段（OCR 同行粘连）。"""
    parts = []
    # 以 第X章 / 第X条 / （一） / 一、 为边界切分（章也作为边界，避免章与条粘连同一段）
    for seg in re.split(r"(?=(?:第[0-9零一二三四五六七八九十百千]+[章条])|(?:[（(][0-9零一二三四五六七八九十]+[)）])|(?:[一二三四五六七八九十]+、))", s):
        seg = seg.strip()
        if seg:
            parts.append(seg)
    return parts


@register("regulation")
def process_regulation(text):
    """规章制度/指引：章、条、项（一）、子项一、 分层独立成段，清除流内编号噪声。"""
    # 清流内文档编号噪声（页眉页脚已在 _clean_pdf_layout 处理，此处处理正文流中的编号串）
    text = _REG_DOCNO_INLINE_RE.sub("", text)
    paras = []
    cur_block = None  # 当前条款/项缓冲（用于把条款正文合并到条号下）

    def flush_block():
        nonlocal cur_block
        if cur_block:
            paras.append(cur_block)
            cur_block = None

    for raw in text.split("\n"):
        ln = raw.strip()
        if not ln:
            # 空行：若当前有缓冲且以句末标点结尾则断段，否则继续合并
            continue
        # 封面单位名行跳过
        if _REG_COVER_RE.match(ln):
            continue
        # 行内粘连切分（OCR 把多章/条/项挤在一行）；单行无多层级时切分返回原样，无副作用
        segs = _split_regulation_line(ln)
        for s in segs:
            if not s:
                continue
            if _REG_CHAP_RE.match(s):
                # 章标题独立成段
                cur_block = None
                paras.append(s)
            elif _REG_ART_RE.match(s):
                # 条：独立成段（条号 + 正文）
                flush_block()
                cur_block = s
            elif _REG_ITEM_RE.match(s):
                # 项（一）：作为当前条下的子段落，独立成段
                if cur_block:
                    flush_block()
                paras.append(s)
            elif _REG_SUB_RE.match(s):
                # 子项 一、：独立成段
                if cur_block:
                    flush_block()
                paras.append(s)
            else:
                # 普通正文：并入当前条/项缓冲（合并为连贯段落）
                if cur_block:
                    cur_block = cur_block + " " + s
                else:
                    cur_block = s
    flush_block()
    return "\n\n".join(paras)


# ---------------- 会议纪要 后处理器（复用既有红头/议题结构化逻辑） ----------------
@register("minutes")
def process_minutes(text):
    """会议纪要：红头逐行保留并拆分，导语/议题按句末标点与序号合并为段落。"""
    return _clean_pdf_text(text)


# ---------------- 默认 后处理器 ----------------
@register("default")
def process_default(text):
    """其它文档：去除空行与纯页码，行内换行合并为自然段落。"""
    paras = []
    cur = ""
    for ln in text.split("\n"):
        s = ln.strip()
        if not s:
            if cur:
                paras.append(cur)
                cur = ""
            continue
        if re.fullmatch(r"[\d\s\-—/．.]+", s) and len(s) <= 6:
            continue
        s = re.sub(r"^\d{1,3}(?=[\u4e00-\u9fff])", "", s).strip()
        if not s:
            continue
        if cur:
            cur = cur + s
        else:
            cur = s
    if cur:
        paras.append(cur)
    return "\n\n".join(paras)


def post_process(text: str, category: str = None):
    """按分类（或内容嗅探）选择语义后处理器，返回结构化文本。"""
    # 所有路径（标准/纪要/默认/LLM 回退）先统一清理页眉页脚噪声与版面伪换行，
    # 否则标准类走规则后处理时会漏掉 _clean_pdf_layout（该函数在 LLM 路径才调）。
    text = _clean_pdf_layout(text)
    kind = category_to_kind(category) or detect_kind(text) or "default"
    fn = EXTRACTORS.get(kind, EXTRACTORS["default"])
    return fn(text)


# ---------------- 统一入口 ----------------
def extract(raw: bytes, filename: str, category: str = None):
    """统一入口。

    1) 按扩展名做底层二进制解码；
    2) 若提供 category，按分类选择语义后处理器；否则按内容嗅探；
    3) 返回 (text, warn)。text 为已结构化的纯文本（供索引与预览）。

    当 USE_LLM=true 时，使用大模型进行结构化提取。

    新增文档类型时：在 EXTRACTORS 注册对应后处理器，并在 _CATEGORY_KIND_MAP
    中把分类名映射到该类型即可，无需改动本函数。
    """
    # 按文档类型分流排版通道：
    #   - 会议纪要(minutes)：保留规则后处理（process_minutes，红头拆分 + 议题合并），
    #     不使用 LLM——规则对纪要结构（红头/出席列席/议题序号）已足够稳定且零成本。
    #   - 管理标准(standard) / 其它：PDF/DOCX 统一交由大模型做结构化排版（忠实原文
    #     重排：章节/条款/表格独立成行、自然段落换行）。模型排版是既定方案，规则后
    #     处理器仅作为 LLM 失败时的兜底，避免标准类复杂条款层级被规则切坏。
    kind = category_to_kind(category)
    if kind is None:
        # 无分类时，先用底层解码出原始文本再嗅探（仅判断类型用，不污染最终输出）
        _ext0 = os.path.splitext(filename)[1].lower()
        if _ext0 == ".pdf":
            _raw0 = _decode_pdf(raw, category=category)
        elif _ext0 == ".docx":
            _raw0 = _extract_docx(raw)
        else:
            _raw0 = None
        if _raw0 is not None:
            kind = detect_kind(_raw0)

    # 会议纪要 / 管理标准 / 规章制度(合规指引) 均走规则后处理，不使用 LLM。
    # 实证：本库管理标准 / 合规指引 PDF 经 LLM 排版后，表格/章节号/段落号常被打乱、
    # 挤成一坨，质量不如规则版；规则后处理按「标准/规章条款结构」断段，能稳定还原
    # 章节层级、条款编号、表格标题，且不丢内容、零成本、无幻觉、无联网延迟。
    # 其它文档（无分类或非标准/纪要/规章）才按 .env 的 USE_LLM 决定走 LLM 还是规则兜底。
    # 注：USE_LLM 走 MiniMax 接口单次联网耗时高达数十秒（实测 94s/篇），是「提取慢」的
    # 根因，故凡能走规则后处理的类型一律强制走规则，绝不联网。
    use_llm = (
        USE_LLM
        and filename.lower().endswith(('.pdf', '.docx'))
        and kind not in ("minutes", "standard", "regulation")  # 纪要/标准/规章强制走规则
    )
    if use_llm:
        try:
            text = _extract_with_llm(raw, filename, category)
            return text, None
        except Exception as e:
            warn = f"LLM 提取失败，回退到基础提取: {e}"
            # 回退到基础提取（标准类走保守模式，保留全部文字，避免丢内容）

    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXT:
        raise ValueError(
            "不支持的文件格式: %s（支持 txt/md/csv/docx/xlsx/pptx/pdf）" % ext)
    if ext in (".txt", ".md", ".csv"):
        text = _decode_text(raw)
        warn = None
    elif ext == ".docx":
        text = _extract_docx(raw)
        warn = None
    elif ext == ".xlsx":
        text = _extract_xlsx(raw)
        warn = None
    elif ext == ".pptx":
        text = _extract_pptx(raw)
        warn = None
    elif ext == ".pdf":
        text = _decode_pdf(raw, category=category)
        warn = None
        if not text.strip():
            warn = "PDF 未提取到文本（可能为扫描件/图片型 PDF）"
    else:
        raise ValueError("不支持的文件格式: %s" % ext)
    # 语义后处理（会议纪要 / 管理标准 / 默认）
    text = post_process(text, category)
    return text, warn
