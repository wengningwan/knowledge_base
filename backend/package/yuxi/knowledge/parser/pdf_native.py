"""原生字符位置提取与有界原文件区域渲染。"""

from __future__ import annotations

import asyncio
import ctypes
import io
import math
from pathlib import Path
from statistics import median


def count_pdf_pages(file_path: str) -> int:
    """在共同 PDFium 锁内读取真实页数，不提取文字或预渲染页面。"""
    import pypdfium2 as pdfium

    from yuxi.knowledge.utils.pdf_utils import PDFIUM_LOCK

    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(str(file_path))
        try:
            count = len(document)
            if count < 1:
                raise ValueError("PDF 没有可读取的页面")
            return count
        finally:
            document.close()


def read_native_blocks(file_path: str, *, page_info: dict | None = None) -> list[dict]:
    """在共同 PDFium 锁内按行聚合原生字符，坐标匹配可见页面。"""
    import pypdfium2 as pdfium

    from yuxi.knowledge.utils.pdf_utils import PDFIUM_LOCK

    blocks = []
    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(str(file_path))
        try:
            if page_info is not None:
                page_info["page_count"] = len(document)
            for page_index in range(len(document)):
                page = document[page_index]
                try:
                    position, device_width, device_height = _page_position(pdfium, page)
                    textpage = page.get_textpage()
                    try:
                        if textpage.count_chars() > 2_000_000:
                            raise ValueError("PDF 单页原生字符数超过处理上限")
                        line = []
                        for index in range(textpage.count_chars()):
                            codepoint = pdfium.raw.FPDFText_GetUnicode(textpage, index)
                            char = chr(codepoint) if codepoint else ""
                            if char in {"\r", "\n"}:
                                _append_line(blocks, line, page_index + 1)
                                line = []
                                continue
                            if not char:
                                continue
                            left, bottom, right, top = textpage.get_charbox(index)
                            corners = [(x, y) for x in (left, right) for y in (bottom, top)]
                            bbox = _visible_bbox(position, device_width, device_height, corners)
                            angle = float(pdfium.raw.FPDFText_GetCharAngle(textpage, index))
                            # 在原始文字基线的法向上分行，页面 Rotate 不改变分行。
                            normal = -(left + right) / 2 * math.sin(angle) + (bottom + top) / 2 * math.cos(angle)
                            extent = abs(math.sin(angle)) * (right - left) + abs(math.cos(angle)) * (top - bottom)
                            if line and not char.isspace():
                                previous = next((item for item in reversed(line) if not item["text"].isspace()), None)
                                if previous is not None:
                                    old_normal, old_extent, old_angle = previous["baseline"]
                                    distance = abs(normal - old_normal)
                                    line_height = max(old_extent, extent, 0.001)
                                    if abs(math.sin(angle - old_angle)) > 0.25 or distance > line_height * 0.7:
                                        _append_line(blocks, line, page_index + 1)
                                        line = []
                            origin_x, origin_y = ctypes.c_double(), ctypes.c_double()
                            has_origin = pdfium.raw.FPDFText_GetCharOrigin(textpage, index, ctypes.byref(origin_x), ctypes.byref(origin_y))
                            origin_normal = -origin_x.value * math.sin(angle) + origin_y.value * math.cos(angle) if has_origin else None
                            font_size = float(pdfium.raw.FPDFText_GetFontSize(textpage, index))
                            line.append({
                                "index": index, "text": char, "bbox": bbox, "baseline": (normal, extent, angle),
                                "font_size": font_size if math.isfinite(font_size) and font_size > 0 else None,
                                "origin_normal": origin_normal if origin_normal is not None and math.isfinite(origin_normal) else None,
                            })
                        _append_line(blocks, line, page_index + 1)
                    finally:
                        textpage.close()
                finally:
                    page.close()
        finally:
            document.close()
    return blocks


def read_native_image_regions(file_path: str, *, diagnostics: list[dict] | None = None) -> list[dict]:
    """按 PDF 绘制实例返回原图区域，重复资源保留每次位置且不提取字节。"""
    return _read_native_images(file_path, diagnostics)


async def extract_native_figures(file_path: str, blocks: list[dict], diagnostics: list[dict]) -> dict:
    """只提取与模型图块匹配的 PDF 图像，取消时等待源文件释放。"""
    by_page = {}
    for block in blocks:
        if block["type"] in {"picture", "chemical_structure"}:
            by_page.setdefault(block["page"], []).append(block)
    records = await _run_pdfium(_read_native_images, file_path, diagnostics, by_page)
    return {record["id"]: record for record in records}


def _read_native_images(
    file_path: str, diagnostics: list[dict] | None, figure_pages: dict[int, list[dict]] | None = None
) -> list[dict]:
    """在页面关闭前读取图像位置及选中对象的像素。"""
    import pypdfium2 as pdfium

    from yuxi.knowledge.utils.pdf_utils import PDFIUM_LOCK

    regions = []
    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(str(file_path))
        try:
            for page_index in range(len(document)):
                if figure_pages is not None and page_index + 1 not in figure_pages:
                    continue
                page = document[page_index]
                try:
                    position, device_width, device_height = _page_position(pdfium, page)
                    page_images = []
                    regions_complete = True
                    for index, item in enumerate(page.get_objects(max_depth=16)):
                        if index >= 100_000:
                            message = "PDF 单页对象数超过区域遍历上限"
                            if diagnostics is None:
                                raise ValueError(message)
                            diagnostics.append({"kind": "native_image_object_limit", "page": page_index + 1, "message": message})
                            regions_complete = False
                            break
                        if item.type == pdfium.raw.FPDF_PAGEOBJ_FORM and item.level >= 15:
                            message = {
                                "kind": "native_image_depth_limit",
                                "page": page_index + 1,
                                "message": "Form 嵌套超过 16 层，不能保证图位完整",
                            }
                            if diagnostics is None:
                                raise ValueError(message["message"])
                            diagnostics.append(message)
                            regions_complete = False
                        if item.type != pdfium.raw.FPDF_PAGEOBJ_IMAGE:
                            continue
                        try:
                            corners = [(0, 0), (1, 0), (1, 1), (0, 1)]
                            current = item
                            while current is not None:
                                matrix = current.get_matrix()
                                corners = [matrix.on_point(x, y) for x, y in corners]
                                current = current.container
                            if not all(math.isfinite(value) for point in corners for value in point):
                                raise ValueError("图像矩阵包含非有限坐标")
                            bbox = _visible_bbox(position, device_width, device_height, corners)
                            if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
                                continue
                            if figure_pages is None:
                                regions.append({
                                    "id": f"native_image_p{page_index + 1}_o{index}",
                                    "page": page_index + 1,
                                    "order": len(regions),
                                    "bbox": bbox,
                                })
                            else:
                                page_images.append((item, bbox))
                        except (ValueError, pdfium.PdfiumError) as exc:
                            if diagnostics is None:
                                raise ValueError(f"第 {page_index + 1} 页图像 {index} 位置无法读取") from exc
                            diagnostics.append(
                                {
                                    "kind": "native_image_region_failed",
                                    "page": page_index + 1,
                                    "object_index": index,
                                    "message": "原图区域无法定位，未猜测替代位置",
                                }
                            )
                            regions_complete = False
                    if figure_pages is not None and regions_complete:
                        regions.extend(_extract_page_figures(figure_pages[page_index + 1], page_images))
                finally:
                    page.close()
        finally:
            document.close()
    return regions


def _extract_page_figures(blocks: list[dict], page_images: list) -> list[dict]:
    """为每个内嵌图像确定唯一的同页图块，保持子图的绘制顺序。"""
    from pypdfium2 import PdfiumError

    from yuxi.knowledge.parser.pdf_repair_rules import overlap_fraction

    images_by_block = {block["id"]: [] for block in blocks}
    ambiguous = set()
    unmatched = []
    for item, box in page_images:
        if box[2] - box[0] >= 0.9 and box[3] - box[1] >= 0.9:
            continue
        candidates = [block["id"] for block in blocks if overlap_fraction(box, block["bbox"]) >= 0.5]
        if len(candidates) == 1:
            images_by_block[candidates[0]].append((item, box))
        elif not candidates:
            unmatched.append((item, box))
        else:
            ambiguous.update(candidates)

    # 截偏的图框可能只碰到坐标轴。仅用双方唯一的同栏紧邻关系补配。
    nearby = {
        block["id"]: [] for block in blocks
        if not images_by_block[block["id"]] and block["id"] not in ambiguous
    }
    shared = set()
    for index, (_, box) in enumerate(unmatched):
        candidates = [
            block["id"] for block in blocks
            if block["id"] in nearby and _near_figure_box(box, block["bbox"])
        ]
        for block_id in candidates:
            nearby[block_id].append(index)
        if len(candidates) > 1:
            shared.add(index)

    sources = []
    for block in blocks:
        block_id = block["id"]
        images = images_by_block[block_id]
        neighbors = nearby.get(block_id, [])
        if not images and len(neighbors) == 1 and neighbors[0] not in shared:
            images = [unmatched[neighbors[0]]]
        if block_id in ambiguous or not images:
            continue
        boxes = [box for _, box in images]
        bounds = [
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        ]
        if overlap_fraction(block["bbox"], bounds) < 0.5 and not _near_figure_box(bounds, block["bbox"]):
            continue
        source = {"id": block_id, "bbox": bounds}
        try:
            source["data"] = _figure_png(images, bounds)
        except (PdfiumError, OSError, ValueError, ZeroDivisionError) as exc:
            source["reason"] = type(exc).__name__
        sources.append(source)
    return sources


def _near_figure_box(image: list[float], figure: list[float]) -> bool:
    """允许上下偏移的图框匹配同栏紧邻的完整内嵌图像。"""
    width = min(image[2], figure[2]) - max(image[0], figure[0])
    if width < 0.65 * max(image[2] - image[0], figure[2] - figure[0]):
        return False
    gap = max(image[1] - figure[3], figure[1] - image[3], 0)
    return gap <= min(0.025, (image[3] - image[1]) * 0.25)


def _figure_png(images: list, bounds: list[float]) -> bytes:
    """读取内嵌对象的像素与透明度，并按原页位置合成多张子图。"""
    from contextlib import ExitStack

    from PIL import Image

    max_pixels = 40_000_000
    with ExitStack() as stack:
        parts = []
        total_pixels = 0
        for item, box in images:
            width, height = item.get_px_size()
            left, bottom, right, top = item.get_bounds()
            object_width, object_height = right - left, top - bottom
            if min(object_width, object_height) <= 0 or not math.isfinite(object_width * object_height):
                raise ValueError("内嵌插图的绘制范围无效")
            scale = max(width, height) / min(object_width, object_height)
            if not math.isfinite(scale):
                raise ValueError("内嵌插图的缩放范围无效")
            total_pixels += math.ceil(object_width * scale) * math.ceil(object_height * scale)
            if total_pixels > max_pixels:
                raise ValueError("内嵌插图像素数过大")
            # 只渲染图像对象本身，保留透明遮罩，避免把周围正文截入。
            bitmap = item.get_bitmap(render=True)
            stack.callback(bitmap.close)
            image = stack.enter_context(bitmap.to_pil())
            parts.append((image, box))
        if len(parts) == 1:
            data = _png_bytes(parts[0][0])
        else:
            x0, y0, x1, y1 = bounds
            dx = max(image.width / (box[2] - box[0]) for image, box in parts)
            dy = max(image.height / (box[3] - box[1]) for image, box in parts)
            size = (math.ceil((x1 - x0) * dx), math.ceil((y1 - y0) * dy))
            if size[0] * size[1] > max_pixels:
                raise ValueError("合成插图像素数过大")
            with Image.new("RGBA", size, "white") as canvas:
                for image, (left, top, right, bottom) in parts:
                    pos = (round((left - x0) * dx), round((top - y0) * dy))
                    end = (round((right - x0) * dx), round((bottom - y0) * dy))
                    panel_size = (max(1, end[0] - pos[0]), max(1, end[1] - pos[1]))
                    with image.convert("RGBA") as rgba, rgba.resize(panel_size, Image.Resampling.LANCZOS) as panel:
                        canvas.alpha_composite(panel, dest=pos)
                data = _png_bytes(canvas)
        if len(data) > 32 * 1024 * 1024:
            raise ValueError("内嵌插图编码后超过大小上限")
        return data


def figure_crop_region(block: dict, blocks: list[dict], native_bbox: list[float] | None = None) -> list[float]:
    """无法直接提取时沿原图与模型框的范围渲染页面区域。"""
    box = block["bbox"]
    if native_bbox:
        box = [min(box[0], native_bbox[0]), min(box[1], native_bbox[1]),
               max(box[2], native_bbox[2]), max(box[3], native_bbox[3])]
    margin = min(0.018, max(0.006, (box[3] - box[1]) * 0.10))
    return _crop_with_boundaries(box, block, blocks, margin)


def review_crop_region(block: dict, targets: list[dict], native: list[dict], blocks: list[dict]) -> list[float]:
    """沿真实原生行补齐局部上下文，外扩边缘并在邻栏和图表边界止步。"""
    regions = [target["source_region"]["bbox"] for target in targets]
    box = [min(item[0] for item in regions), min(item[1] for item in regions),
           max(item[2] for item in regions), max(item[3] for item in regions)]
    lines = [line["bbox"] for line in native if line.get("page") == block["page"] and line.get("bbox")
             and line["bbox"][1] < box[3] and line["bbox"][3] > box[1]
             and line["bbox"][0] < box[2] and line["bbox"][2] > box[0]]
    if lines:
        lines = [line for line in lines if line[2] - line[0] <= (block["bbox"][2] - block["bbox"][0]) * 1.5 + .04]
    if lines:
        box = [min(box[0], min(line[0] for line in lines)), min(box[1], min(line[1] for line in lines)),
               max(box[2], max(line[2] for line in lines)), max(box[3], max(line[3] for line in lines))]
        height = median(line[3] - line[1] for line in lines)
    else:
        height = min(.025, max(.006, (box[3] - box[1]) / max(1, len(str(block.get("content", ""))) / 70)))
    return _crop_with_boundaries(box, block, blocks, height)


def wide_crop_region(block: dict, targets: list[dict], native: list[dict], blocks: list[dict],
                     *, extra_rows: int = 1) -> list[float]:
    """覆盖目标所在原生行并并入上下邻行，供裁图可读门槛的扩图重渲染。

    与 review_crop_region 的差异：不做跨栏宽度过滤，保证门槛场景一定能
    拿到含基线上下文的大图；原生行不可用时退化为保守放大。
    """
    regions = [target["source_region"]["bbox"] for target in targets]
    box = [min(item[0] for item in regions), min(item[1] for item in regions),
           max(item[2] for item in regions), max(item[3] for item in regions)]
    # 同页全部行参与排序；目标所在行靠相交精确判定，避免初筛提前排除邻行。
    lines = [line for line in native if line.get("page") == block["page"] and line.get("bbox")]
    if not lines:
        return _expand_box(review_crop_region(block, targets, native, blocks), 1.5, 2.0)
    lines.sort(key=lambda line: (line["bbox"][1] + line["bbox"][3]) / 2)
    covered = [index for index, line in enumerate(lines)
               if line["bbox"][1] < box[3] and line["bbox"][3] > box[1]
               and line["bbox"][0] < box[2] and line["bbox"][2] > box[0]]
    if not covered:
        return _expand_box(review_crop_region(block, targets, native, blocks), 1.5, 2.0)
    first = max(0, min(covered) - extra_rows)
    last = min(len(lines) - 1, max(covered) + extra_rows)
    window = [line["bbox"] for line in lines[first:last + 1]]
    box = [min(box[0], min(line[0] for line in window)), min(box[1], min(line[1] for line in window)),
           max(box[2], max(line[2] for line in window)), max(box[3], max(line[3] for line in window))]
    height = median(line["bbox"][3] - line["bbox"][1] for line in lines)
    return _crop_with_boundaries(box, block, blocks, height)


def _crop_with_boundaries(box: list[float], block: dict, blocks: list[dict], height: float) -> list[float]:
    """区域外扩半行距，并在同页邻栏与图表边界中点止步。"""
    crop = [max(0., box[0] - height * 1.5), max(0., box[1] - height * .6),
            min(1., box[2] + height * 1.5), min(1., box[3] + height * .6)]
    for other in blocks:
        boundary = other.get("bbox")
        if other["id"] == block["id"] or other["page"] != block["page"] or not boundary:
            continue
        if boundary[1] < box[3] and boundary[3] > box[1]:
            if boundary[2] <= box[0]:
                crop[0] = max(crop[0], (boundary[2] + box[0]) / 2)
            elif boundary[0] >= box[2]:
                crop[2] = min(crop[2], (box[2] + boundary[0]) / 2)
        if boundary[0] < box[2] and boundary[2] > box[0]:
            if boundary[3] <= box[1]:
                crop[1] = max(crop[1], (boundary[3] + box[1]) / 2)
            elif boundary[1] >= box[3]:
                crop[3] = min(crop[3], (box[3] + boundary[1]) / 2)
    return crop


def _expand_box(bbox: list[float], width_factor: float, height_factor: float) -> list[float]:
    """以区域中心按倍数放大并 clamp 到页面，用于无原生行时的保守扩图。"""
    center_x, center_y = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    half_w, half_h = (bbox[2] - bbox[0]) * width_factor / 2, (bbox[3] - bbox[1]) * height_factor / 2
    return [max(0., center_x - half_w), max(0., center_y - half_h),
            min(1., center_x + half_w), min(1., center_y + half_h)]


async def render_pdf_region_async(file_path: str, page: int, bbox: list, scale: float = 3) -> bytes:
    """取消时等待 PDFium 释放源文件，再交给调用方清理。"""
    return await _run_pdfium(render_pdf_region, file_path, page, bbox, scale)


async def _run_pdfium(operation, *args):
    """等待 PDFium 线程结束后再传播取消。"""
    rendering = asyncio.create_task(asyncio.to_thread(operation, *args))
    try:
        return await asyncio.shield(rendering)
    except asyncio.CancelledError:
        await asyncio.gather(rendering, return_exceptions=True)
        raise


def render_pdf_region(file_path: str, page: int, bbox: list, scale: float = 3) -> bytes:
    """渲染归一可见页面区域为 PNG，支持 PDF 与 PNG/JPEG 原图。"""
    from PIL import Image, ImageOps

    x0, y0, x1, y1 = _validated_region(page, bbox, scale)
    path = Path(file_path)
    if path.suffix.lower() in {".png", ".jpg", ".jpeg"}:
        if page != 1:
            raise ValueError("图片仅有第 1 页")
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source)
            try:
                width, height = image.size
                crop = (math.floor(x0 * width), math.floor(y0 * height), math.ceil(x1 * width), math.ceil(y1 * height))
                with image.crop(crop) as region:
                    factor = _bounded_scale(region.width, region.height, scale)
                    size = (max(1, math.ceil(region.width * factor)), max(1, math.ceil(region.height * factor)))
                    with region.resize(size) as resized:
                        return _png_bytes(resized)
            finally:
                if image is not source:
                    image.close()
    if path.suffix.lower() != ".pdf":
        raise ValueError("区域渲染仅支持 PDF、PNG 和 JPEG")

    import pypdfium2 as pdfium

    from yuxi.knowledge.utils.pdf_utils import PDFIUM_LOCK

    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(str(path))
        try:
            if page > len(document):
                raise ValueError("区域页码超出文档范围")
            pdf_page = document[page - 1]
            try:
                width, height = pdf_page.get_size()
                factor = _bounded_scale((x1 - x0) * width, (y1 - y0) * height, scale)
                # PDFium 的 render crop 位于旋转后的可见画布；外扩一像素避免向上取整切掉边缘。
                crop = tuple(
                    max(0, value - 1 / factor)
                    for value in (x0 * width, (1 - y1) * height, (1 - x1) * width, y0 * height)
                )
                bitmap = pdf_page.render(scale=factor, crop=crop)
                try:
                    image = bitmap.to_pil()
                    try:
                        return _png_bytes(image)
                    finally:
                        image.close()
                finally:
                    bitmap.close()
            finally:
                pdf_page.close()
        finally:
            document.close()


def _page_position(pdfium, page):
    """用 PDFium 自身映射处理固有旋转和 CropBox，避免重复手算变换。"""
    width, height = page.get_size()
    if width <= 0 or height <= 0 or not math.isfinite(width * height):
        raise ValueError("PDF 页面尺寸无效")
    device_width, device_height = math.ceil(width * 100), math.ceil(height * 100)
    if max(device_width, device_height) > 2_000_000_000:
        raise ValueError("PDF 页面尺寸超过设备坐标上限")
    return pdfium.PdfPosConv(page, (0, 0, device_width, device_height, 0)), device_width, device_height


def _visible_bbox(position, width, height, corners) -> list[float]:
    """把原生坐标角点包围盒映射到可见页的左上归一坐标。"""
    mapped = [position.to_bitmap(x, y) for x, y in corners]
    xs, ys = [point[0] / width for point in mapped], [point[1] / height for point in mapped]
    return [max(0.0, min(1.0, value)) for value in (min(xs), min(ys), max(xs), max(ys))]


def _append_line(blocks: list[dict], characters: list[dict], page: int) -> None:
    """保存一行文本和原始字符定位信息，不尝试修复字体编码。"""
    content = "".join(item["text"] for item in characters).strip()
    visible = [item for item in characters if not item["text"].isspace()]
    if not content or not visible:
        return
    bbox = [
        min(item["bbox"][0] for item in visible),
        min(item["bbox"][1] for item in visible),
        max(item["bbox"][2] for item in visible),
        max(item["bbox"][3] for item in visible),
    ]
    blocks.append(
        {
            "id": f"native_p{page}_c{characters[0]['index']}",
            "page": page,
            "bbox": bbox,
            "content": content,
            "chars": list(characters),
        }
    )


def _validated_region(page, bbox, scale) -> tuple[float, float, float, float]:
    """验证调用边界上的页码、区域和缩放倍数。"""
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError("page 必须为 1 基正整数")
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        raise ValueError("bbox 必须包含四个归一坐标")
    values = tuple(float(value) for value in bbox)
    if not all(math.isfinite(value) and 0 <= value <= 1 for value in values):
        raise ValueError("bbox 坐标必须在 0 到 1 之间")
    if values[2] <= values[0] or values[3] <= values[1]:
        raise ValueError("bbox 必须有正面积")
    if not math.isfinite(float(scale)) or not 0 < scale <= 10:
        raise ValueError("scale 必须在 0 到 10 之间")
    return values


def _bounded_scale(width: float, height: float, requested: float) -> float:
    """限制目标位图长边和面积，直接渲染区域以避免整页内存峰值。"""
    if width <= 0 or height <= 0 or not math.isfinite(width * height):
        raise ValueError("原区域尺寸无效")
    return min(float(requested), 4092 / width, 4092 / height, math.sqrt(11_950_000 / (width * height)))


def _png_bytes(image) -> bytes:
    """在原生缓冲区关闭前完成 PNG 编码。"""
    buffer = io.BytesIO()
    if image.mode not in {"RGB", "RGBA", "L", "LA", "P", "1"}:
        with image.convert("RGB") as converted:
            converted.save(buffer, format="PNG")
    else:
        image.save(buffer, format="PNG")
    return buffer.getvalue()
