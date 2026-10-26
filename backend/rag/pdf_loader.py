"""PDF 文本提取与清洗。

【零基础说明】
PDF 里存的是"画在页面上的字形"，不是纯文本——提取文字需要解析字体映射。
pypdf 能处理大多数文本型 PDF；但像 GB/T 国标这类用方正书造字体且缺少
ToUnicode 映射的 PDF，提取出来会是乱码（/G21 之类是字形编号，不是汉字）。

我们的策略：
1. 尽力提取（英文文档、部分结构化内容是好的），清洗后入库；
2. 提取质量差的（乱码比例高）自动丢弃，由 knowledge_seed.py 的人工条目补齐。

清洗规则：
- 制表符/多空格 -> 单空格（PDF 提取常出现大量 \t）
- 丢弃含 /G 字形码的行（乱码特征）
- 一行里"可读字符比例"太低也丢弃（防御其他乱码）
"""

import logging
import re
from pathlib import Path

from pypdf import PdfReader

log = logging.getLogger("rag.pdf")

# 乱码行特征：出现 /G21 这类字形编号
_GLYPH_LINE = re.compile(r"/G[0-9A-Fa-f]{2}")
# "可读字符"= 中英文、数字、常用标点；其余（控制符、私用区等）算不可读
# 注意：`-` 放在字符集末尾无需转义（写成 \- 会有 SyntaxWarning）
_READABLE = re.compile(r"[\u4e00-\u9fff\u3000-\u303fA-Za-z0-9\s，。；：、""''（）()《》—%/±.．,;:<>≤≥\[\]-]")


def clean_text(raw: str) -> str:
    """单页文本清洗：规范化空白 + 去乱码行。返回清洗后的文本（可能为空）。"""
    lines = []
    for line in raw.replace("\t", " ").splitlines():
        line = line.strip()
        if not line:
            continue
        if _GLYPH_LINE.search(line):
            continue
        # 可读字符占比 < 60% 视为乱码行
        if line and len(_READABLE.findall(line)) / len(line) < 0.6:
            continue
        lines.append(line)
    # 合并为一行（RAG 分块不太关心排版换行），页与页之间用换行分隔
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


# 高频汉字表（真实中文里这些字占比很高；乱码映射出的生僻字几乎不含它们）
_COMMON_HANZI = set(
    "的了在是有和人不这为我们中大来上国说地也得子时道出而要于就下可你年生自会那后能对着事其里所去行过之家十用发天如然作方成者多日都三小军二无同么经法当起与好看学进种将还分此心前面又定见只主没公从以回了很多要就把想在自己去到说他一二三四五六七八九十本条文标准该按应系统设备压力颜色质")


def _is_garbled(raw: str, cleaned: str) -> bool:
    """判断一页是否为乱码页（字体映射缺失，提取出来的是无意义字符）。

    三个启发式（任一命中即判乱码）：
    1. 原始文本里 /Gxx 字形码行占比高（本产线国标 PDF 的乱码特征）
    2. 清洗后文本的平均"词长"极短（乱码页特征：大量孤立单字）
    3. 含中文但高频汉字占比 < 8%（真中文里"的了在是"到处都是，
       字体映射错乱提取出的生僻 CJK 字符则几乎不含高频字）
    """
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    if lines:
        glyph_ratio = sum(1 for ln in lines if _GLYPH_LINE.search(ln)) / len(lines)
        if glyph_ratio > 0.10:
            return True
    words = [w for w in cleaned.split() if w]
    if not words:
        return True  # 清洗后什么都不剩
    avg_len = sum(len(w) for w in words) / len(words)
    if avg_len < 2.0:
        return True
    cjk = [ch for ch in cleaned if "\u4e00" <= ch <= "\u9fff"]
    if cjk and sum(1 for ch in cjk if ch in _COMMON_HANZI) / len(cjk) < 0.08:
        return True
    return False


def load_pdf_pages(path: Path) -> list[str]:
    """提取 PDF 每页文本并清洗。返回 [第1页文本, 第2页文本, ...]（乱码/空页已剔除）。"""
    try:
        reader = PdfReader(str(path))
    except Exception as exc:  # noqa: BLE001
        log.warning("cannot open %s: %r", path.name, exc)
        return []
    pages = []
    for idx, page in enumerate(reader.pages, start=1):
        try:
            raw = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001
            log.warning("extract %s page %d failed: %r", path.name, idx, exc)
            continue
        text = clean_text(raw)
        # 太短的页（<80 字符）通常是封面/目录，跳过；乱码页跳过
        if len(text) < 80 or _is_garbled(raw, text):
            continue
        pages.append(text)
    return pages
