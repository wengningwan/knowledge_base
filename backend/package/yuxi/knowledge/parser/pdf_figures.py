"""从 PDF 中提取内嵌插图，供 OCR 禁用（纯文字层提取）模式解析使用。"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

from PIL import Image
from pypdf import PdfReader

from yuxi.utils import logger

# 过小图片多为 logo、分隔线和项目符号，直接跳过
MIN_IMAGE_EDGE = 128
MIN_IMAGE_BYTES = 5 * 1024
MAX_FIGURES_PER_DOCUMENT = 50

_MIME_BY_FORMAT = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}
_EXTENSION_BY_MIME = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}


@dataclass
class PdfFigure:
    page: int  # 1-based 页码
    index: int  # 该页内已接受插图的序号，1-based
    name: str  # 形如 p3_1
    data: bytes
    mime: str

    @property
    def filename(self) -> str:
        return f"{self.name}.{_EXTENSION_BY_MIME.get(self.mime, 'png')}"


def extract_pdf_figures(
    file_path: str | Path,
    max_figures: int = MAX_FIGURES_PER_DOCUMENT,
) -> list[PdfFigure]:
    """提取 PDF 各页的嵌入插图，按内容哈希去重并过滤噪声图。"""
    if isinstance(file_path, str):
        file_path = Path(file_path)

    reader = PdfReader(str(file_path))
    figures: list[PdfFigure] = []
    seen_hashes: set[str] = set()

    for page_number, page in enumerate(reader.pages, start=1):
        figures_on_page: list[PdfFigure] = []
        try:
            page_images = list(page.images)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"读取第 {page_number} 页嵌入图片失败，已跳过: {e}")
            continue

        for image_file in page_images:
            if len(figures) >= max_figures:
                logger.info(f"插图数量达到上限 {max_figures}，停止提取: {file_path.name}")
                return figures
            figure = _build_figure(image_file, page_number, len(figures_on_page) + 1)
            if figure is None:
                continue
            digest = hashlib.sha256(figure.data).hexdigest()
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)
            figures_on_page.append(figure)
            figures.append(figure)

    return figures


def _build_figure(image_file, page: int, index: int) -> PdfFigure | None:
    try:
        raw = image_file.data
        if not raw or len(raw) < MIN_IMAGE_BYTES:
            return None

        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception as e:  # noqa: BLE001
        logger.debug(f"跳过第 {page} 页一张无法解码的嵌入图片: {e}")
        return None

    width, height = image.size
    if width < MIN_IMAGE_EDGE or height < MIN_IMAGE_EDGE:
        return None

    mime = _MIME_BY_FORMAT.get(image.format or "")
    if mime and image.mode in {"RGB", "RGBA", "L"}:
        data = raw
    else:
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="PNG")
        data, mime = buffer.getvalue(), "image/png"

    return PdfFigure(page=page, index=index, name=f"p{page}_{index}", data=data, mime=mime)
