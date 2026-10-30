"""原生字符位置提取与有界原文件区域渲染。"""

from __future__ import annotations

import ctypes
import io
import math
from pathlib import Path


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
    import pypdfium2 as pdfium

    from yuxi.knowledge.utils.pdf_utils import PDFIUM_LOCK

    regions = []
    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(str(file_path))
        try:
            for page_index in range(len(document)):
                page = document[page_index]
                try:
                    position, device_width, device_height = _page_position(pdfium, page)
                    for index, item in enumerate(page.get_objects(max_depth=16)):
                        if index >= 100_000:
                            raise ValueError("PDF 单页对象数超过区域遍历上限")
                        if item.type == pdfium.raw.FPDF_PAGEOBJ_FORM and item.level >= 15:
                            message = {
                                "kind": "native_image_depth_limit",
                                "page": page_index + 1,
                                "message": "Form 嵌套超过 16 层，不能保证图位完整",
                            }
                            if diagnostics is None:
                                raise ValueError(message["message"])
                            diagnostics.append(message)
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
                            regions.append(
                                {
                                    "id": f"native_image_p{page_index + 1}_o{index}",
                                    "page": page_index + 1,
                                    "order": len(regions),
                                    "bbox": bbox,
                                }
                            )
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
                finally:
                    page.close()
        finally:
            document.close()
    return regions


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
