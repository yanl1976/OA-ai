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


def _extract_with_llm(raw: bytes, filename: str, category: str = None) -> str:
    """使用 LLM 提取文档内容。"""
    # 先用基础方法提取原始文本（兜底基准）；标准类走保守模式保留全部文字
    raw_text = _decode_pdf(raw, category=category) if filename.lower().endswith('.pdf') else _decode_text(raw)

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

    # 2) 落款办公室 + 日期：…办公室/综管办 … 年…月…日
    dm = re.search(r"\d{4}年\d{1,2}月\d{1,2}日", sn)
    okw = re.search(r"(?:办公室|综管办)", sn)
    if not (dm and okw and okw.start() < dm.start()):
        return [s], tail
    # 会议名称位于日期之后、会议次数之前（先于 office_line 入栈，反转后落在 office 之后）
    meet_idx = sn.rfind("会议纪要")
    if meet_idx < 0 or meet_idx < dm.end():
        return [s], tail
    meeting_name = sn[dm.end(): meet_idx + 4]
    result.append(meeting_name)

    # 落款办公室名应包含前缀（如"天传所集团综管办"），向前扩展直到"号"等文号边界
    office_start = okw.start()
    search_seg = sn[max(0, office_start - 30):office_start]
    hao_pos = search_seg.rfind("号")
    if hao_pos >= 0:
        office_start = max(0, office_start - 30) + hao_pos + 1
    office_line = sn[office_start: dm.end()]
    result.append(office_line)

    # 3) 单位名 + 部门纪要 + 文号：日期之前的部分
    #    形如 …集团有限公司纪要天研司会议纪要〔2024〕59号
    rest = sn[:office_start]
    docm = re.search(r"〔[^〕]{0,20}〕\d+号", rest)
    if not docm:
        return [s], tail
    jiyao_idx = rest.rfind("会议纪要", 0, docm.start())  # 部门"会议纪要"起始
    if jiyao_idx < 0:
        return [s], tail
    unit_jiyao = rest.rfind("纪要", 0, jiyao_idx)        # 单位名末尾"纪要"
    if unit_jiyao < 0:
        return [s], tail
    dept_line = rest[unit_jiyao + 2: docm.end()]   # 天研司会议纪要〔…〕号
    unit_line = rest[:unit_jiyao + 2]              # 天水电气传动研究所集团有限公司纪要
    result.append(dept_line)
    result.append(unit_line)

    result.reverse()
    final = [r for r in result if r.strip()]
    if len(final) < 4:
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
        # 丢弃疑似页码 / 页眉噪声行（整行仅数字或极短纯符号）
        if re.fullmatch(r"[\d\s\-—/．.]+", s) and len(s) <= 6:
            return ""
        # 去除行首粘连的页码数字（如抽取为 "1天水电气…" 的 "1"）
        s = re.sub(r"^\d{1,3}(?=[\u4e00-\u9fff])", "", s)
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
            if is_pagebreak and not (cur[-1:] in _END_PUNCT or _PARAGRAPH_START.match(cur)):
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
    "16.合规管理类": "standard",
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
    """无分类信息时，按内容嗅探文档类型（兜底）。"""
    head = text[:2000]
    body = text[:4000]
    # 管理标准：含『管理标准/工作标准/技术标准』且有标准编号（Q/ 开头）
    if re.search(r"管理标准|工作标准|技术标准", head) and re.search(r"Q/[A-Z]", head):
        return "standard"
    # 会议纪要：含『会议纪要』且含决议/出席要素
    if "会议纪要" in head and re.search(r"会议决定|出席人员|列席人员|主持人", body):
        return "minutes"
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


@register("standard")
def process_standard(text):
    """管理标准：仅做换行切分与轻微去噪，严格保留全部文字内容（含图注尺寸、页码、
    位置码、标准编号行），确保与 PDF 文字层差异最小、不丢内容。
    """
    # 先把 PDF 抽到一行的长文本切开（标准编号/日期/章节/条款/表格），再逐行处理
    text = _segment_standard_lines(text)
    out = []
    cur = ""

    def flush():
        nonlocal cur
        if cur.strip():
            out.append(cur.strip())
        cur = ""

    for ln in text.split("\n"):
        s = ln.strip()
        if not s:
            continue
        # 仅去除行首粘连的页码数字（如抽取为 "1天水电气…"），其余文字完整保留
        s = re.sub(r"^\d{1,3}(?=[\u4e00-\u9fff])", "", s).strip()
        # 不删除任何内容行（位置码/图注坐标/标准编号/页码均保留，保证与原文一致）
        if re.match(r"^\s*\d{1,3}\s+[\u4e00-\u9fff]", s):
            # 顶层章节编号（如 "1 范围"）：断段，章节标题独立成段
            flush()
            out.append(s)
            continue
        # 其他行并入当前段
        cur = (cur + " " + s).strip() if cur else s
    flush()
    return "\n\n".join(out)


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
    # LLM 提取模式
    # 会议纪要要求确定性结构化（红头每个定义换行、导语/议题整段），强制走规则化
    # 提取，不使用 LLM——LLM 提取对会议纪要易出现非必要换行/强行换行，已实测需
    # 人工修正。其余类型仍可走 LLM 以提升质量。
    _force_rule = bool(category) and ("纪要" in category)
    if USE_LLM and filename.lower().endswith(('.pdf', '.docx')) and not _force_rule:
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
