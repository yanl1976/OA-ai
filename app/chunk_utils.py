"""统一文本切分工具。

供 rag_build_index.py / vec_store.py 共用，保证 BM25 索引与向量索引的 chunk
边界完全一致；同时针对会议纪要等结构化文档做「议题/章节边界感知」切分，
避免一个 chunk 把多个议题糊在一起，导致检索摘要驴头不对马嘴。
"""
import re

# 议题 / 章节 / 公文要素起始标记（与 extract_text.py 的 _PARAGRAPH_START 对齐）
_ITEM_START_RE = re.compile(
    r"^\s*(?:"
    r"[（(]?[一二三四五六七八九十百千零\d]+[、.)）](?![\d])\s*"   # 一、 （一） 1.
    r"|第[一二三四五六七八九十百千零\d]+[章节条]\s*"        # 第一条 / 第三章
    r")"
)

# 强段落边界：空行视为段落边界
# 出席/列席人员名单等也视为独立段落，但不强制在此处断 chunk


def _is_item_boundary(line: str) -> bool:
    """判断行是否是议题、条款、章节等结构边界。"""
    return bool(_ITEM_START_RE.match(line))


def chunk_text(text: str, chunk_size: int = 1200):
    """将长文本切分为小块。

    策略：
      1. 以段落为基本单元；
      2. 遇到议题/章节边界（一、二、三... / 第X条 / 第X章）时，
         若当前缓冲区已有内容，先结束当前 chunk，保证同一 chunk 内
         尽量不跨多个议题；
      3. 在议题内部仍按 chunk_size 上限切分，防止超长议题无限膨胀；
      4. 记录每个 chunk 在原文中的绝对字符偏移 [char_start, char_end)。

    这样检索摘要展示的是单个议题或章节，避免把「李小宝任免」和
    「光伏项目借款」糊在同一个 snippet 里。
    """
    paragraphs = text.split("\n")
    chunks = []
    buf = ""
    buf_start = None
    cursor = 0

    for para in paragraphs:
        p = para.rstrip("\n")
        if not p:
            cursor += 1
            continue

        is_boundary = _is_item_boundary(p)

        # 议题/章节边界：先 flush 已有内容，确保新议题从新的 chunk 开始
        if is_boundary and buf.strip():
            chunks.append((buf, buf_start, buf_start + len(buf)))
            buf = ""
            buf_start = None

        # 普通大小上限切分（非边界行）
        if buf and len(buf) + len(p) + 1 > chunk_size and not is_boundary:
            chunks.append((buf, buf_start, buf_start + len(buf)))
            buf = ""
            buf_start = None

        if buf_start is None:
            buf_start = cursor
        buf = (buf + "\n" + p) if buf else p
        cursor += len(p) + 1

    if buf.strip():
        chunks.append((buf, buf_start, buf_start + len(buf)))

    return chunks or [(text, 0, len(text))]
