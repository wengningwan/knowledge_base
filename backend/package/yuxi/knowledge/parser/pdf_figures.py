"""PDF 布局提取：按页面位置产出文本段、插图与公式区域（基于 pypdfium2）。

OCR 禁用模式使用本模块代替整页纯文本提取，解决两个问题：
- 插图按其页面坐标插入相邻文本之间，而不是堆在页尾；
- 数学公式在文字层中通常是乱码（数学字体无 Unicode 映射），检测后渲染为
  区域图片，交由视觉模型转写 LaTeX。
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from PIL import Image

from yuxi.utils import logger

# 图片对象在 PDFium 对象类型常量中的编号（FPDF_PAGEOBJ_IMAGE）
_PAGEOBJ_IMAGE = 3

# 显示尺寸过小的图片多为 logo、分隔线和项目符号
MIN_FIGURE_DISPLAY_PT = 80
# 渲染后仍小于该边长的图片跳过
MIN_RENDER_PIXEL = 100
# 过大的渲染图等比缩放到该边长，控制存储与模型输入
MAX_IMAGE_EDGE = 1600
# 单页插图上限
MAX_FIGURES_PER_DOCUMENT = 50
# 占页面超过该比例的图片视为背景/水印，跳过
FULLPAGE_AREA_RATIO = 0.9
FIGURE_RENDER_SCALE = 2.0
FORMULA_RENDER_SCALE = 3.0


@dataclass
class LayoutBlock:
    kind: str  # "text" | "figure" | "formula"
    page: int  # 1-based 页码
    left: float = 0.0
    bottom: float = 0.0
    right: float = 0.0
    top: float = 0.0
    text: str = ""
    image: bytes | None = None
    mime: str = "image/png"
    url: str = ""
    description: str = ""
    seq: int = 0  # 页内媒体块序号，用于命名

    @property
    def name(self) -> str:
        return f"{self.kind}_p{self.page}_{self.seq}"

    @property
    def filename(self) -> str:
        return f"{self.name}.png"


def looks_like_formula(text: str) -> bool:
    """启发式判断一行文本是否是公式在文字层中的乱码表现。

    数学字体（Mathematical Pi、CMMI 等）缺乏 Unicode 映射时，文字层会输出
    私有区字符、替换符或零散数学算符。仅当这类字符在行内占比足够高时判定，
    避免误伤含少量符号的普通文本。
    """
    if len(text) < 2:
        return False
    special = sum(1 for char in text if _is_math_like_char(char))
    return special / len(text) >= 0.5


def _is_math_like_char(char: str) -> bool:
    """数学字体乱码常见字符：私有区、替换符、数学算符、间距修饰与几何符号。"""
    code = ord(char)
    return (
        char == "\ufffd"
        or 0xE000 <= code <= 0xF8FF
        or 0x2200 <= code <= 0x22FF
        or 0x2000 <= code <= 0x200F
        or 0x2060 <= code <= 0x2064
        or 0x25A0 <= code <= 0x25FF
    )


# 公式变量字体：低频且只承载拉丁字母（CMMI、MathType 斜体等），正文字体必然高频
_MAX_FORMULA_FONT_CHARS = 60


def _collect_page_chars(textpage) -> list[tuple[str, str, tuple[float, float, float, float]]]:
    """逐字符收集 (字体, 字符, 包围盒)。"""
    import ctypes

    handle = textpage.raw
    char_count = pdfium_c.FPDFText_CountChars(handle)
    if char_count <= 0:
        return []

    buffer = ctypes.create_string_buffer(128)
    flags = ctypes.c_int()
    left = ctypes.c_double()
    right = ctypes.c_double()
    bottom = ctypes.c_double()
    top = ctypes.c_double()
    items: list[tuple[str, str, tuple[float, float, float, float]]] = []
    for index in range(char_count):
        code = pdfium_c.FPDFText_GetUnicode(handle, index)
        char = chr(code) if 0 <= code < 0x110000 else "\ufffd"
        length = pdfium_c.FPDFText_GetFontInfo(handle, index, buffer, 128, ctypes.byref(flags))
        font = buffer.value.decode("utf-8", "ignore") if length else "?"
        # FPDFText_GetCharBox 的输出参数顺序是 left, right, bottom, top
        box_ready = pdfium_c.FPDFText_GetCharBox(
            handle, index, ctypes.byref(left), ctypes.byref(right), ctypes.byref(bottom), ctypes.byref(top)
        )
        if box_ready:
            if right.value > left.value and top.value > bottom.value:
                items.append((font, char, (left.value, bottom.value, right.value, top.value)))
    return items


def _formula_variable_fonts(items: list[tuple[str, str, tuple[float, float, float, float]]]) -> set[str]:
    """识别"公式变量字体"：页内低频且只承载拉丁字母的字体。

    正文与标题字体必然高频承载内容；斜体变量、数学专用字体只出现在公式里，
    数量少且不含 CJK 与标点。连字（fi/fl/ff 等排版字形）字体同样低频且
    全字母，需显式排除；参考文献的斜体期刊名会被误认，配合行级
    "多种字体混排"条件排除。
    """
    font_chars: dict[str, list[str]] = {}
    for font, char, _ in items:
        font_chars.setdefault(font, []).append(char)

    formula_fonts = set()
    for font, char_list in font_chars.items():
        if not (3 <= len(char_list) <= _MAX_FORMULA_FONT_CHARS):
            continue
        meaningful = [c for c in char_list if not c.isspace()]
        if not meaningful:
            continue
        if all(c in _LIGATURE_CHARS for c in meaningful):
            continue  # 连字字形字体（fi/fl/ff），不是公式变量
        if all(("a" <= c <= "z") or ("A" <= c <= "Z") for c in meaningful):
            formula_fonts.add(font)
    return formula_fonts


# 连字排版字形及其字母组成（fi、fl、ff、ffi、ffl）
_LIGATURE_CHARS = set("fil\uFB00\uFB01\uFB02\uFB03\uFB04")


def _box_overlap_ratio(
    box: tuple[float, float, float, float], region: tuple[float, float, float, float]
) -> float:
    """box 与 region 的交集面积占 box 面积的比例。"""
    overlap_left = max(box[0], region[0])
    overlap_bottom = max(box[1], region[1])
    overlap_right = min(box[2], region[2])
    overlap_top = min(box[3], region[3])
    if overlap_right <= overlap_left or overlap_top <= overlap_bottom:
        return 0.0
    box_area = max(box[2] - box[0], 0.01) * max(box[3] - box[1], 0.01)
    return (overlap_right - overlap_left) * (overlap_top - overlap_bottom) / box_area


def _merge_rects(
    rects: list[tuple[float, float, float, float]], gap: float = 0.0
) -> list[tuple[float, float, float, float]]:
    """把空间相交（或间距小于 gap）的矩形迭代合并。"""
    if not rects:
        return []
    merged = [list(rect) for rect in rects]
    changed = True
    while changed:
        changed = False
        output: list[list[float]] = []
        for rect in merged:
            for index, kept in enumerate(output):
                close = (
                    rect[0] <= kept[2] + gap
                    and kept[0] <= rect[2] + gap
                    and rect[1] <= kept[3] + gap
                    and kept[1] <= rect[3] + gap
                )
                if close:
                    kept[0] = min(kept[0], rect[0])
                    kept[1] = min(kept[1], rect[1])
                    kept[2] = max(kept[2], rect[2])
                    kept[3] = max(kept[3], rect[3])
                    changed = True
                    break
            else:
                output.append(list(rect))
        merged = output
    return [(rect[0], rect[1], rect[2], rect[3]) for rect in merged]


def _extract_formula_regions(page, page_size) -> list[LayoutBlock]:
    """由公式变量字体定位公式行，渲染为公式图片块。

    公式变量（低频斜体字体）按视觉行分组；行内变量 ≥2 且几乎没有 CJK
    字符时视为独立公式行，整行渲染。含较多中文的混排行只渲染相邻变量簇
    的局部区域，避免把正文吞进公式图。行内单个变量引用保留原文本。
    """
    textpage = page.get_textpage()
    try:
        items = _collect_page_chars(textpage)
        formula_fonts = _formula_variable_fonts(items)
        if not formula_fonts:
            return []
        formula_boxes = [box for font, _, box in items if font in formula_fonts]
        if not formula_boxes:
            return []

        page_area = max(page_size[0] * page_size[1], 1.0)
        blocks: list[LayoutBlock] = []
        for row in _group_formula_rows(formula_boxes):
            if len(row) < 2:
                continue
            region = _formula_row_region(row, items, formula_fonts)
            if region is None:
                continue
            if (region[2] - region[0]) * (region[3] - region[1]) > page_area * 0.9:
                continue
            image = _render_rect(page, region, FORMULA_RENDER_SCALE, min_pixel=12)
            if image is None:
                continue
            blocks.append(
                LayoutBlock(
                    kind="formula",
                    page=0,  # 由调用方按页填写
                    left=region[0],
                    bottom=region[1],
                    right=region[2],
                    top=region[3],
                    image=image,
                )
            )
        return blocks
    finally:
        textpage.close()


def _group_formula_rows(
    boxes: list[tuple[float, float, float, float]],
    y_tolerance: float = 6.0,
) -> list[list[tuple[float, float, float, float]]]:
    """把公式字符按视觉行（y 中心邻近）分组。"""
    rows: list[list[tuple[float, float, float, float]]] = []
    for box in sorted(boxes, key=lambda b: (b[1] + b[3]) / 2):
        center_y = (box[1] + box[3]) / 2
        placed = False
        for row in rows:
            ys = [(b[1] + b[3]) / 2 for b in row]
            if min(ys) - y_tolerance <= center_y <= max(ys) + y_tolerance:
                row.append(box)
                placed = True
                break
        if not placed:
            rows.append([box])
    return rows


def _formula_row_region(
    row: list[tuple[float, float, float, float]],
    items: list[tuple[str, str, tuple[float, float, float, float]]],
    formula_fonts: set[str],
) -> tuple[float, float, float, float] | None:
    """由公式变量行推导渲染区域。

    独立公式行（行内 CJK ≤ 2）取行内全部字符包围盒的并集，等号、括号、
    数字等乱码字符自然被覆盖；混排行只取相邻变量簇并略外扩，簇宽不足
    30pt 时视为行内符号引用，返回 None 保留原文本。
    """
    row_bottom = min(box[1] for box in row) - 3.0
    row_top = max(box[3] for box in row) + 3.0
    row_chars = [
        (font, char, box)
        for font, char, box in items
        if row_bottom <= (box[1] + box[3]) / 2 <= row_top
    ]
    cjk_count = sum(1 for _, char, _ in row_chars if "\u4e00" <= char <= "\u9fff")
    # 公式行是变量、数字、运算符多字体混排；英文正文行通常只有正文字体 + 斜体两种
    row_font_count = len({font for font, _, _ in row_chars})
    # 公式行的变量字符密度高；正文行内变量引用（at T = 298 K）只占零星几个字符
    variable_count = sum(1 for font, _, _ in row_chars if font in formula_fonts)
    variable_ratio = variable_count / max(len(row_chars), 1)

    if cjk_count <= 2 and row_font_count >= 3 and variable_ratio >= 0.25:
        left = min(box[0] for _, _, box in row_chars)
        bottom = min(box[1] for _, _, box in row_chars)
        right = max(box[2] for _, _, box in row_chars)
        top = max(box[3] for _, _, box in row_chars)
        return (left, bottom, right, top)

    # 局部公式只适用于中文正文与变量混排的行；不满足整行条件的无中文行保留原文本，
    # 避免把纯英文正文误判为公式
    if cjk_count <= 2:
        return None

    ordered = sorted(row, key=lambda box: box[0])
    clusters: list[list[tuple[float, float, float, float]]] = []
    for box in ordered:
        if clusters and box[0] - max(b[2] for b in clusters[-1]) <= 12.0:
            clusters[-1].append(box)
        else:
            clusters.append([box])
    cluster = max(clusters, key=len) if clusters else row
    left = min(box[0] for box in cluster) - 2.0
    right = max(box[2] for box in cluster) + 2.0
    if right - left < 30.0:
        return None
    return (
        left,
        min(box[1] for box in cluster) - 1.0,
        right,
        max(box[3] for box in cluster) + 1.0,
    )


def keep_figure_by_bounds(bounds: tuple[float, float, float, float], page_size: tuple[float, float]) -> bool:
    """按显示矩形过滤噪声图：过小的图标与占满整页的背景图都跳过。"""
    left, bottom, right, top = bounds
    width, height = right - left, top - bottom
    if width <= 0 or height <= 0:
        return False
    if width < MIN_FIGURE_DISPLAY_PT and height < MIN_FIGURE_DISPLAY_PT:
        return False
    page_width, page_height = page_size
    if page_width > 0 and page_height > 0:
        if (width * height) / (page_width * page_height) > FULLPAGE_AREA_RATIO:
            return False
    return True


def extract_pdf_layout(
    file_path: str | Path,
    max_figures: int = MAX_FIGURES_PER_DOCUMENT,
) -> list[list[LayoutBlock]]:
    """提取每页的有序布局块。

    文本块保持 PDF 文本流顺序（不重排，双栏阅读顺序依赖生成器的流序），
    插图按其垂直位置插入到水平重叠的相邻文本之间，公式行在原位替换为公式图片块。
    """
    if isinstance(file_path, str):
        file_path = Path(file_path)

    document = pdfium.PdfDocument(str(file_path))
    try:
        pages: list[list[LayoutBlock]] = []
        seen_hashes: set[str] = set()
        figure_total = 0

        for page_number, page in enumerate(document, start=1):
            page_size = page.get_size()
            formula_regions = _extract_formula_regions(page, page_size)
            for block in formula_regions:
                block.page = page_number
            text_blocks = _extract_text_blocks(page, page_number, formula_regions)
            figures, figure_total = _extract_figures(
                page, page_number, page_size, seen_hashes, figure_total, max_figures
            )
            blocks = _merge_blocks(text_blocks, formula_regions + figures)
            _assign_media_sequence(blocks)
            pages.append(blocks)
        return pages
    finally:
        document.close()


def _assign_media_sequence(blocks: list[LayoutBlock]) -> None:
    """为页内 figure/formula 块按输出顺序编号，用于命名。"""
    sequence = 0
    for block in blocks:
        if block.kind != "text":
            sequence += 1
            block.seq = sequence


def _extract_text_blocks(
    page,
    page_number: int,
    formula_regions: list[LayoutBlock] | None = None,
) -> list[LayoutBlock]:
    textpage = page.get_textpage()
    try:
        count = textpage.count_rects()
        blocks: list[LayoutBlock] = []
        pending_formula: list[tuple[tuple[float, float, float, float], str]] = []

        def flush_pending_formula() -> None:
            if not pending_formula:
                return
            merged = pending_formula[:]
            pending_formula.clear()
            block = _build_formula_block(page, page_number, merged)
            if block is not None:
                blocks.append(block)

        for index in range(count):
            rect = textpage.get_rect(index)
            if rect is None:
                continue
            left, bottom, right, top = rect
            text = textpage.get_text_bounded(left=left, bottom=bottom, right=right, top=top)
            text = (text or "").strip()
            if not text:
                continue
            # 落在公式区域内的行是公式在文字层的乱码表现，剔除后由公式图片块取代
            if formula_regions and any(
                _box_overlap_ratio((left, bottom, right, top), (r.left, r.bottom, r.right, r.top)) >= 0.6
                for r in formula_regions
            ):
                continue
            if looks_like_formula(text):
                # 相邻公式行合并渲染，避免一个公式被拆成多张图
                if pending_formula:
                    last_rect = pending_formula[-1][0]
                    gap = last_rect[1] - top
                    horizontally_overlapping = min(last_rect[2], right) - max(last_rect[0], left) > 0
                    if gap < 3.0 and horizontally_overlapping:
                        pending_formula.append((rect, text))
                        continue
                    flush_pending_formula()
                pending_formula.append((rect, text))
            else:
                flush_pending_formula()
                blocks.append(
                    LayoutBlock(
                        kind="text", page=page_number, left=left, bottom=bottom, right=right, top=top, text=text
                    )
                )
        flush_pending_formula()

        return _merge_adjacent_text(blocks)
    finally:
        textpage.close()


def _merge_adjacent_text(blocks: list[LayoutBlock]) -> list[LayoutBlock]:
    """把同一行水平相邻、或垂直间距很小的连续文本块合并为段落块，段间保留空行。"""
    merged: list[LayoutBlock] = []
    for block in blocks:
        if not merged:
            merged.append(block)
            continue
        previous = merged[-1]
        vertical_gap = previous.bottom - block.top
        line_height = max(previous.top - previous.bottom, block.top - block.bottom, 1.0)
        horizontal_gap = max(previous.left - block.right, block.left - previous.right)
        same_line = horizontal_gap < line_height * 1.5 and vertical_gap < line_height * 0.8
        next_line = (
            vertical_gap < line_height * 1.2
            and min(previous.right, block.right) - max(previous.left, block.left) > 0
        )
        if same_line or next_line:
            previous.text = f"{previous.text}{'' if same_line else chr(10)}{block.text}"
            previous.bottom = min(previous.bottom, block.bottom)
            previous.left = min(previous.left, block.left)
            previous.right = max(previous.right, block.right)
        else:
            merged.append(block)
    return merged


def _extract_figures(
    page,
    page_number: int,
    page_size: tuple[float, float],
    seen_hashes: set[str],
    figure_total: int,
    max_figures: int,
) -> tuple[list[LayoutBlock], int]:
    figures: list[LayoutBlock] = []
    for obj in page.get_objects():
        if figure_total >= max_figures:
            logger.info(f"插图数量达到上限 {max_figures}，停止提取")
            break
        if obj.type != _PAGEOBJ_IMAGE:
            continue
        try:
            bounds = obj.get_bounds()
        except Exception as e:  # noqa: BLE001
            logger.debug(f"跳过第 {page_number} 页一个无法定位的图片对象: {e}")
            continue
        if not keep_figure_by_bounds(bounds, page_size):
            continue
        image = _render_rect(page, bounds, FIGURE_RENDER_SCALE)
        if image is None:
            continue
        digest = hashlib.sha256(image).hexdigest()
        if digest in seen_hashes:
            continue
        seen_hashes.add(digest)
        figures.append(
            LayoutBlock(
                kind="figure",
                page=page_number,
                left=bounds[0],
                bottom=bounds[1],
                right=bounds[2],
                top=bounds[3],
                image=image,
            )
        )
        figure_total += 1
    return figures, figure_total


def _build_formula_block(
    page,
    page_number: int,
    formula_lines: list[tuple[tuple[float, float, float, float], str]],
) -> LayoutBlock | None:
    left = min(rect[0] for rect, _ in formula_lines)
    bottom = min(rect[1] for rect, _ in formula_lines)
    right = max(rect[2] for rect, _ in formula_lines)
    top = max(rect[3] for rect, _ in formula_lines)
    # 区域略外扩，避免字形边缘被裁
    pad = 2.0
    image = _render_rect(
        page, (left - pad, bottom - pad, right + pad, top + pad), FORMULA_RENDER_SCALE, min_pixel=12
    )
    if image is None:
        return None
    return LayoutBlock(
        kind="formula",
        page=page_number,
        left=left,
        bottom=bottom,
        right=right,
        top=top,
        text="\n".join(text for _, text in formula_lines),
        image=image,
    )


def _render_rect(
    page,
    rect: tuple[float, float, float, float],
    scale: float,
    min_pixel: int = MIN_RENDER_PIXEL,
) -> bytes | None:
    """把页面坐标矩形 (left, bottom, right, top) 渲染为 PNG 字节。

    pypdfium2 的 render crop 是"从各侧裁掉的坐标量"，需按页面尺寸换算。
    """
    left, bottom, right, top = rect
    if right <= left or top <= bottom:
        return None
    page_width, page_height = page.get_size()
    crop = (left, bottom, page_width - right, page_height - top)
    try:
        bitmap = page.render(scale=scale, crop=crop, may_draw_forms=False)
        pil = bitmap.to_pil()
    except Exception as e:  # noqa: BLE001
        logger.debug(f"渲染页面区域失败: {e}")
        return None
    width, height = pil.size
    if width < min_pixel or height < min_pixel:
        return None
    if pil.mode != "RGB":
        pil = pil.convert("RGB")
    if max(width, height) > MAX_IMAGE_EDGE:
        ratio = MAX_IMAGE_EDGE / max(width, height)
        pil = pil.resize((max(1, int(width * ratio)), max(1, int(height * ratio))), Image.LANCZOS)
    buffer = io.BytesIO()
    pil.save(buffer, format="PNG")
    return buffer.getvalue()


def _merge_blocks(text_blocks: list[LayoutBlock], figures: list[LayoutBlock]) -> list[LayoutBlock]:
    """把插图插入文本流：保持文本顺序，图片落在与其水平重叠的首个下方文本之前。"""
    if not figures:
        return list(text_blocks)
    if not text_blocks:
        return list(figures)

    blocks: list[LayoutBlock] = []
    remaining_figures = sorted(figures, key=lambda block: -block.top)  # 页面上方的图先插入
    for text_block in text_blocks:
        while remaining_figures:
            figure = remaining_figures[0]
            overlaps_horizontally = min(figure.right, text_block.right) - max(figure.left, text_block.left) > 0
            if overlaps_horizontally and figure.top > text_block.top:
                blocks.append(remaining_figures.pop(0))
            else:
                break
        blocks.append(text_block)
    blocks.extend(remaining_figures)
    return blocks
