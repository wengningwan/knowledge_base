"""从 PDF 中提取内嵌插图，供 OCR 禁用（纯文字层提取）模式解析使用。"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

from PIL import Image
from pypdf import PdfReader

from yuxi.knowledge.parser.base import DocumentParserException

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
    """保留页面内图像身份，供视觉模型确认其实际位置。"""

    page: int  # 1-based 页码
    index: int  # 该页内已接受插图的序号，1-based
    name: str  # 形如 p3_1
    data: bytes
    mime: str

    @property
    def filename(self) -> str:
        """返回可上传的稳定插图文件名。"""
        return f"{self.name}.{_EXTENSION_BY_MIME.get(self.mime, 'png')}"


def extract_pdf_figures(
    file_path: str | Path,
    max_figures: int = MAX_FIGURES_PER_DOCUMENT,
    *,
    warnings: list[str] | None = None,
) -> list[PdfFigure]:
    """保留嵌入图片；提供诊断容器时保留可用图片并报告局部失败。"""
    if isinstance(file_path, str):
        file_path = Path(file_path)

    reader = PdfReader(str(file_path))
    figures: list[PdfFigure] = []

    for page_number, page in enumerate(reader.pages, start=1):
        figures_on_page: list[PdfFigure] = []
        try:
            page_images = page.images
            image_count = len(page_images)
        except Exception as exc:
            if warnings is not None:
                warnings.append(f"第 {page_number} 页插图读取失败，请从原 PDF 查看该页图片。")
                continue
            raise DocumentParserException(
                f"第 {page_number} 页插图读取失败", "pdf_figures", "figure_extraction_failed"
            ) from exc

        for image_index in range(image_count):
            if len(figures) >= max_figures:
                if warnings is not None:
                    warnings.append(f"插图达到 {max_figures} 张处理上限，其余图片未处理。")
                    return figures
                raise DocumentParserException(
                    f"PDF 插图超过 {max_figures} 张，请拆分文件或使用版面 OCR 引擎", "pdf_figures", "too_many_figures"
                )
            try:
                # pypdf 在读取单个索引时解码，不能先 list() 让坏图终止整页。
                image_file = page_images[image_index]
            except Exception as exc:
                message = f"第 {page_number} 页第 {image_index + 1} 个图片资源读取失败"
                if warnings is not None:
                    warnings.append(message)
                    continue
                raise DocumentParserException(message, "pdf_figures", "figure_extraction_failed") from exc
            try:
                figure = _build_figure(image_file, page_number, len(figures_on_page) + 1)
            except DocumentParserException as exc:
                if warnings is None:
                    raise
                warnings.append(exc.message)
                continue
            figures_on_page.append(figure)
            figures.append(figure)

    return figures


def _build_figure(image_file, page: int, index: int) -> PdfFigure:
    """解码图片或明确失败，不按压缩字节量过滤线图。"""
    try:
        raw = image_file.data
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception as exc:
        raise DocumentParserException(
            f"第 {page} 页插图无法解码", "pdf_figures", "figure_extraction_failed"
        ) from exc

    try:
        mime = _MIME_BY_FORMAT.get(image.format or "")
        if mime and image.mode in {"RGB", "RGBA", "L"}:
            data = raw
        else:
            buffer = io.BytesIO()
            with image.convert("RGB") as converted:
                converted.save(buffer, format="PNG")
            data, mime = buffer.getvalue(), "image/png"
    finally:
        image.close()

    return PdfFigure(page=page, index=index, name=f"p{page}_{index}", data=data, mime=mime)
