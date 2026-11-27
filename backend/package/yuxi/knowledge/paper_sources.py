"""冻结正文的可引用范围与原 PDF 的局部证据。"""

from __future__ import annotations

import math
import re
import unicodedata
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterable
from html import unescape
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from pypdf import PdfReader

from yuxi.knowledge.chunking.source import SourceDocument
from yuxi.utils.logging_config import logger

_HEADING = re.compile(r"^(#{1,6})[^\S\r\n]+([^\n\r]+)")
_BIBLIOGRAPHY_HEADINGS = {"references", "bibliography", "literature cited", "参考文献", "引用文献"}
_EXCLUDED_SECTIONS = {
    *_BIBLIOGRAPHY_HEADINGS,
    "author information", "acknowledgments", "acknowledgements", "associated content",
    "corresponding authors", "authors", "author contributions", "致谢",
}
_CAPTION = re.compile(
    r"^(?:\*\*|__)?(?P<kind>table|fig(?:ure)?\.?|图|表)\s*"
    r"(?P<number>(?:[a-z]?\d+(?:[.-]\d+)*[a-z]?|[ivxlcdm]+)(?:\s*\([a-z]\))?)(?=[\s.(：:、。*_-]|$)", re.I,
)
_CAPTION_PROSE = re.compile(
    r"^(?:shows?|provides?|lists?|compares?|presents?|indicates?|illustrates?|depicts?|"
    r"reveals?|displays?|demonstrates?|summari[sz]es|is|was)\b|^显示|^表明|^给出|^中|^为", re.I,
)
_IMAGE = re.compile(r"^(?:!\[|<img\b)", re.I)
_PIPE_DIVIDER = re.compile(r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*")
_DISPLAY_ENV = re.compile(r"\\begin\{((?:equation|align|gather|displaymath)\*?)\}")


def read_paper_sources(
    markdown: str, pdf_content: bytes, parse_assets: dict | None,
) -> tuple[dict[str, dict], list[str], dict[str, int], bool, int]:
    """将正文、表、图和公式分别定位到同次解析块或原 PDF 文本。"""
    pdf = PdfReader(BytesIO(pdf_content))
    pages = [page.extract_text() for page in pdf.pages]
    if not pages:
        raise ValueError("论文 PDF 没有可读取的页面")
    labels = {
        str(index): int(label) for index, label in enumerate(pdf.page_labels, 1)
        if re.fullmatch(r"[1-9][0-9]*", label)
    } if pdf.root_object.get("/PageLabels") else {}
    assets = parse_assets or {}
    blocks, figures = assets.get("blocks", []), assets.get("figures", [])
    if any(not isinstance(items, list) or not all(isinstance(item, dict) for item in items)
           for items in (blocks, figures)) or any(not isinstance(block.get("type"), str) for block in blocks):
        raise ValueError("冻结正文的解析块或图表附属数据格式无效")
    spans, excluded = _source_sections(markdown)
    parsed = _parsed_spans(markdown, blocks, figures, len(pages))
    starts = [left for left, _, _ in parsed]
    native_pages = [compact_source(page) for page in pages]
    document = SourceDocument(markdown)
    entries = []
    partial = False
    for start, end, section in spans:
        anchored = [span for span in parsed[bisect_left(starts, start):bisect_left(starts, end)] if span[1] <= end]
        cursor = start
        for left, right, block in anchored:
            if left < cursor:
                continue
            partial |= _append_native_span(entries, document, cursor, left, section, native_pages)
            entries.append(_source_entry(document, left, right, section, block["page"], block))
            cursor = right
        partial |= _append_native_span(entries, document, cursor, end, section, native_pages)

    _link_visual_sources(markdown, entries, figures)
    sources = {}
    for entry in entries:
        for fragment in _attribution_fragments(document, entry):
            sources[f"ev:source-{len(sources) + 1}"] = fragment
    if not sources:
        raise ValueError("冻结正文没有可定位到原 PDF 页的证据，无法生成规范 JSON")
    return sources, pages, labels, partial, excluded


def compact_source(text: str) -> str:
    """折叠题录核对所需的排版空白与连字符。"""
    folded = re.sub(r"[\u2010\u2011\u2012\u2013\u2014\u2212]", "-", text)
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", folded).replace("\u00ad", "")).casefold()


def printed_page_map(document: dict, pages: list[str], labels: dict[str, int], notices: list) -> dict[str, int]:
    """保留可核对的数字印刷页码，报告冲突或缺失。"""
    page_range = re.fullmatch(r"\s*(\d+)\s*[-–—−]\s*(\d+)\s*", document.get("pages") or "")
    if page_range and pages:
        first, last = map(int, page_range.groups())
        title = compact_source(document.get("title") or "")
        if (
            first > 0 and last - first + 1 == len(pages) and title and title in compact_source(pages[0])
            and re.search(rf"(?<!\d){first}\s*[-–—−]\s*{last}(?!\d)", pages[0])
        ):
            inferred = {str(index): first + index - 1 for index in range(1, len(pages) + 1)}
            if not any(inferred.get(key) != value for key, value in labels.items()):
                return inferred
            notices.append((
                "printed_page_conflict", "PDF 页标与首页期刊页范围不一致，已保留明确页标。",
            ))
    if missing := len(pages) - len(labels):
        notices.append((
            "unverified_printed_pages",
            f"原 PDF 有 {missing} 页缺少可核对的数字印刷页码，相关 printed_page 为 null，请按 PDF 页码核对出处。",
        ))
    return labels


async def render_paper_evidence(
    pdf_content: bytes, sources: dict[str, dict], refs: Iterable[str],
) -> tuple[list[bytes], list[dict]]:
    """按所选证据裁图，返回成功渲染的 PNG 和对应的位置及证据号。"""
    from pypdfium2 import PdfiumError

    from yuxi.knowledge.parser.pdf_native import render_pdf_region_async

    visuals = defaultdict(list)
    for source in sources.values():
        if source["origin"] in {"figure", "table"} and source.get("bbox") and not _is_visual_caption(source):
            visuals[(source["page"], source["origin"], source["location"])].append(source["bbox"])
    regions = {}
    for ref in dict.fromkeys(refs):
        if ref not in sources:
            continue
        source = sources[ref]
        page, origin, location = source["page"], source["origin"], source["location"]
        if _is_visual_caption(source):
            unique = {tuple(box) for box in visuals.get((page, origin, location), [])}
            if len(unique) != 1:
                continue
            bbox = next(iter(unique))
        elif source.get("bbox"):
            bbox = tuple(source["bbox"])
        else:
            continue
        regions.setdefault((page, bbox), []).append(ref)
    if not regions:
        return [], []

    images = []
    descriptors = []
    available_bytes = 12 * 1024 * 1024
    with TemporaryDirectory(prefix="yuxi-paper-evidence-") as directory:
        path = Path(directory) / "source.pdf"
        path.write_bytes(pdf_content)
        for (page, bbox), evidence_refs in list(regions.items())[:8]:
            x0, y0, x1, y1 = bbox
            padded = [max(0, x0 - 0.008), max(0, y0 - 0.008), min(1, x1 + 0.008), min(1, y1 + 0.008)]
            try:
                image = await render_pdf_region_async(str(path), page, padded)
            except (OSError, ValueError, PdfiumError) as error:
                logger.warning("[paper_structure] PDF 局部裁图失败 page={} error={}", page, type(error).__name__)
                continue
            if len(image) > available_bytes:
                continue
            available_bytes -= len(image)
            images.append(image)
            descriptors.append({"pdf_page": page, "bbox": padded, "evidence_refs": evidence_refs})
    return images, descriptors


def _parsed_spans(
    markdown: str, blocks: list[dict], figures: list[dict], page_count: int,
) -> list[tuple[int, int, dict]]:
    """用解析渲染器对齐版面块，重文仅在完整重现正文时按块顺序配对。"""
    from yuxi.knowledge.parser.pdf_structure import render_document

    urls = {
        figure["asset_id"]: figure["image_url"] for figure in figures
        if figure.get("asset_id") and figure.get("image_url")
    }
    by_rendered_text = defaultdict(list)
    for block in blocks:
        page = block.get("page")
        if type(page) is not int or not 1 <= page <= page_count:
            continue
        if block.get("type") in {"page_header", "page_footer"}:
            continue
        rendered = render_document([block], urls)
        original = str(block.get("content") or "").strip()
        if not rendered or rendered not in markdown:
            rendered = original
        if rendered:
            by_rendered_text[rendered].append(block)

    exact_order = bool(blocks) and render_document(blocks, urls) == markdown
    positions = []
    for rendered, matches in by_rendered_text.items():
        locations = list(re.finditer(re.escape(rendered), markdown))
        if len(locations) != len(matches):
            continue
        if len(matches) > 1 and (not exact_order or any(block.get("recognition_join") for block in matches)):
            continue
        positions.extend(
            (located.start(), located.end(), block) for located, block in zip(locations, matches, strict=True)
        )
    return sorted(positions, key=lambda item: (item[0], -item[1]))


def _append_native_span(
    entries: list[dict], document: SourceDocument, start: int, end: int, section: str, pages: list[str],
) -> bool:
    """追加原件中可唯一确认的正文范围，报告是否仍有未定位文字。"""
    original = document.text[start:end]
    if not original.strip():
        return False
    start += len(original) - len(original.lstrip())
    quote = original.strip()
    if page := _native_page(quote, pages):
        entries.append(_source_entry(document, start, start + len(quote), section, page))
        return False
    if quote.startswith(("#", "<", "|", "![", "$$", "```", "~~~", r"\[")) or _DISPLAY_ENV.match(quote):
        return True
    if any(_PIPE_DIVIDER.fullmatch(line) for line in quote.splitlines()[1:2]):
        return True

    partial = False
    for left, right in _sentence_ranges(quote):
        sentence = quote[left:right]
        page = _native_page(sentence, pages) if len(compact_source(sentence)) >= 16 else None
        if page:
            entries.append(_source_entry(document, start + left, start + right, section, page))
        else:
            partial = True
    return partial


def _native_page(quote: str, pages: list[str]) -> int | None:
    """仅在规范化原文匹配唯一物理页时返回页码。"""
    key = _native_key(quote)
    matching = [page for page, text in enumerate(pages, 1) if key and key in text]
    return matching[0] if len(matching) == 1 else None


def _source_entry(
    document: SourceDocument, start: int, end: int, section: str, page: int, block: dict | None = None,
) -> dict:
    """创建字面原文、偏移量和可用的原件位置。"""
    quote = document.text[start:end]
    span = document.span(start, end)
    kind = (block or {}).get("type", "")
    caption = _caption_location(quote, explicit=kind == "caption") if kind in {"caption", ""} else None
    if kind == "table" or re.match(r"<table\b", quote, re.I) or any(
        _PIPE_DIVIDER.fullmatch(line) for line in quote.splitlines()[1:2]
    ):
        origin = "table"
    elif kind in {"formula", "chemical_equation"} or quote.startswith(("$$", r"\[")) or _DISPLAY_ENV.match(quote):
        origin = "formula"
    elif kind in {"picture", "chemical_structure"} or _IMAGE.match(quote):
        origin = "figure"
    else:
        origin = caption[0] if caption else "text"
    entry = {
        **span, "page": page, "quote": quote, "section": section or "article", "origin": origin,
        "location": caption[1] if caption else f"Markdown lines {span['line_start']}-{span['line_end']}",
    }
    if block:
        if block_id := block.get("id") or block.get("block_id"):
            entry["block_id"] = block_id
        if bbox := _normalized_bbox(block.get("bbox")):
            entry["bbox"] = bbox
        if figure_text := _image_ocr(block):
            entry["figure_text"] = figure_text
    return entry


def _link_visual_sources(markdown: str, entries: list[dict], figures: list[dict]) -> None:
    """保留图号和同次解析图内文字，按唯一来源给无标题的图表补充定位。"""
    figures_by_id = {figure["id"]: figure for figure in figures if figure.get("id")}
    figures_by_caption = defaultdict(list)
    figures_by_caption_id = defaultdict(list)
    for figure in figures_by_id.values():
        if caption := figure.get("caption"):
            figures_by_caption[(figure.get("page"), _native_key(caption))].append(figure)
        if caption_block_id := figure.get("caption_block_id"):
            figures_by_caption_id[caption_block_id].append(figure)

    for entry in entries:
        if entry["origin"] not in {"table", "figure"}:
            continue
        figure = figures_by_id.get(entry.get("block_id"))
        if figure and figure.get("page") == entry["page"]:
            caption = _caption_location(figure.get("caption") or "", explicit=True)
            if caption and caption[0] == entry["origin"]:
                entry["location"] = caption[1]
            if figure_text := _image_ocr(figure):
                entry["figure_text"] = figure_text
            if not entry.get("bbox") and (bbox := _normalized_bbox(figure.get("bbox"))):
                entry["bbox"] = bbox
        elif _is_visual_caption(entry):
            matching = figures_by_caption_id.get(entry.get("block_id")) or figures_by_caption.get(
                (entry["page"], _native_key(entry["quote"])), [],
            )
            if len(matching) == 1 and matching[0].get("page") == entry["page"]:
                if figure_text := _image_ocr(matching[0]):
                    entry["figure_text"] = figure_text

    nearby_bodies = defaultdict(list)
    for index, entry in enumerate(entries):
        if entry["origin"] not in {"table", "figure"} or not entry["location"].startswith("Markdown lines "):
            continue
        nearby = entries[max(index - 1, 0):index] + entries[index + 1:index + 2]
        captions = []
        for item in nearby:
            left = min(entry["end_char"], item["end_char"])
            right = max(entry["start_char"], item["start_char"])
            if (
                item["page"] == entry["page"] and item["origin"] == entry["origin"]
                and _is_visual_caption(item) and not markdown[left:right].strip()
                and _same_visual_column(entry.get("bbox"), item.get("bbox"))
            ):
                captions.append(item)
        if len(captions) == 1:
            nearby_bodies[captions[0]["start_char"]].append((entry, captions[0]["location"]))
    for candidates in nearby_bodies.values():
        if len(candidates) == 1:
            entry, location = candidates[0]
            entry["location"] = location


def _same_visual_column(body: list[float] | None, caption: list[float] | None) -> bool:
    """对有版面坐标的相邻块核对栏位和垂直距离。"""
    if not body or not caption:
        return True
    overlap = min(body[2], caption[2]) - max(body[0], caption[0])
    distance = max(body[1] - caption[3], caption[1] - body[3], 0)
    return overlap > 0 and distance <= 0.12


def _source_sections(markdown: str) -> tuple[list[tuple[int, int, str]], int]:
    """跟踪正文标题与不可拆分的表格、公式和围栏，剔除参考文献等栏目。"""
    blocks = []
    offset = start = 0
    fence = ""
    closing = ""
    closed_block = False
    for line in markdown.splitlines(keepends=True):
        value = line.strip()
        end = offset + len(line)
        if not fence and not closing:
            if _section_heading(line):
                blocks.extend(((start, offset), (offset, end)))
                start = end
                offset = end
                continue
            if marker := re.match(r"^(`{3,}|~{3,})", value):
                fence = marker[0]
            elif re.match(r"<table\b", value, re.I):
                closing = "</table>"
            elif value.startswith(("$$", r"\[")):
                closing = "$$" if value.startswith("$$") else r"\]"
            elif environment := _DISPLAY_ENV.match(value):
                closing = rf"\end{{{environment[1]}}}"
            if fence or closing or _IMAGE.match(value):
                blocks.append((start, offset))
                start = offset
                closed_block = not (fence or closing)
        elif fence and value.startswith(fence) and not value.strip(fence[0]):
            fence, closed_block = "", True
        if closing == "$$" and (offset > start and "$$" in value or "$$" in value[2:]):
            closing, closed_block = "", True
        elif closing and closing != "$$" and closing in value.casefold():
            closed_block = True
            closing = ""
        if closed_block or (not value and not fence and not closing):
            blocks.append((start, end))
            start = end
            closed_block = False
        offset = end
    blocks.append((start, len(markdown)))

    spans = []
    section = ""
    excluded_level = None
    excluded = 0
    for start, end in blocks:
        block = markdown[start:end]
        heading = _section_heading(block)
        if heading:
            level, section = heading
            if excluded_level is None or level <= excluded_level:
                excluded_level = level if section.casefold() in _EXCLUDED_SECTIONS else None
        if excluded_level is not None:
            excluded += end - start
        elif block.strip():
            spans.append((start + len(block) - len(block.lstrip()), start + len(block.rstrip()), section))
    return spans, excluded


def _section_heading(line: str) -> tuple[int, str] | None:
    """同时识别 Markdown 章节与纯 PDF 文本的参考文献标题。"""
    marked = _HEADING.match(line)
    title = re.sub(r"^\d+(?:\.\d+)*[.)、]?\s+", "", marked[2] if marked else line.strip()).strip(" #.：:")
    if marked:
        return len(marked[1]), title
    if title.casefold() in _BIBLIOGRAPHY_HEADINGS:
        return 2, title
    return None


def _native_key(text: str) -> str:
    """移除不会出现在 PDF 文本层的 Markdown 和表格标记。"""
    text = _HEADING.sub(r"\2", text)
    if _IMAGE.match(text):
        return ""
    if text.startswith("$$") and text.endswith("$$"):
        text = text[2:-2]
    elif text.startswith(r"\[") and text.endswith(r"\]"):
        text = text[2:-2]
    elif environment := _DISPLAY_ENV.match(text):
        closing = rf"\end{{{environment[1]}}}"
        if text.endswith(closing):
            text = text[environment.end():-len(closing)]
    if re.match(r"<table\b", text, re.I):
        text = re.sub(r"</?[a-z][^>]*>", " ", text, flags=re.I)
    elif any(_PIPE_DIVIDER.fullmatch(line) for line in text.splitlines()[1:2]):
        text = "\n".join(line.replace("|", " ") for line in text.splitlines() if not _PIPE_DIVIDER.fullmatch(line))
    text = re.sub(r"</?(?:sup|sub)\b[^>]*>|\*\*|__", "", text, flags=re.I)
    return compact_source(unescape(text))


def _is_visual_caption(source: dict) -> bool:
    """判断已定位的图表证据是题注还是本体。"""
    location = _caption_location(source["quote"], explicit=True)
    return location == (source["origin"], source["location"])


def _caption_location(text: str, *, explicit: bool = False) -> tuple[str, str] | None:
    """保留题注编号，跳过以编号起句的常见正文表达。"""
    match = _CAPTION.match(text)
    if not match or (not explicit and _CAPTION_PROSE.match(text[match.end():].lstrip(" *_(:："))):
        return None
    kind = "table" if match["kind"].casefold() in {"table", "表"} else "figure"
    number = re.sub(r"\s+", "", match["number"])
    return kind, f"{'Table' if kind == 'table' else 'Figure'} {number}"


def _normalized_bbox(value: object) -> list[float] | None:
    """只传递解析侧车中有正面积的归一页面区域。"""
    if not isinstance(value, (list, tuple)) or len(value) != 4 or not all(
        type(item) in {int, float} and math.isfinite(item) and 0 <= item <= 1 for item in value
    ):
        return None
    x0, y0, x1, y1 = value
    return list(value) if x0 < x1 and y0 < y1 else None


def _image_ocr(block: dict) -> str:
    """只收录图中实际识别的文字。"""
    return "\n".join(item for item in block.get("image_ocr") or [] if isinstance(item, str) and item.strip())


def _attribution_fragments(document: SourceDocument, entry: dict) -> list[dict]:
    """在带引文的段落另外给出句级出处，保留完整段落作上下文。"""
    quote = entry["quote"]
    if entry["origin"] != "text" or quote.startswith(("|", "<", "#")) or not re.search(
        r"\[\s*\d+(?:[\d, −–-]*\d)?\s*\]|\bet al\.", unicodedata.normalize("NFKC", quote),
    ):
        return [entry]
    offset = entry["start_char"]
    fragments = [entry]
    for start, end in _sentence_ranges(quote):
        if start or end < len(quote):
            span = document.span(offset + start, offset + end)
            fragments.append({
                **entry, **span, "quote": quote[start:end],
                "location": f"Markdown lines {span['line_start']}-{span['line_end']}",
            })
    return fragments


def _sentence_ranges(quote: str) -> Iterable[tuple[int, int]]:
    """按句末标点切分，保留 Markdown 句子的原文偏移。"""
    edges = [0, *(match.end() for match in re.finditer(r"[。！？]|[.!?](?=\s+[A-Z])", quote)), len(quote)]
    for left, right in zip(edges, edges[1:]):
        fragment = quote[left:right]
        start = left + len(fragment) - len(fragment.lstrip())
        end = left + len(fragment.rstrip())
        if start < end:
            yield start, end
