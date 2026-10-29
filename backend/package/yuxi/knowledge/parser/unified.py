"""Unified parser module for markdown conversion."""

from __future__ import annotations

import asyncio
import base64
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import aiofiles
from docling.backend.msexcel_backend import MsExcelDocumentBackend
from docling.backend.mspowerpoint_backend import MsPowerpointDocumentBackend
from docling.backend.msword_backend import MsWordDocumentBackend
from docling.datamodel.base_models import InputFormat
from docling.datamodel.document import InputDocument
from docling_core.types.doc import DoclingDocument
from markdownify import markdownify as md_convert
from pypdf import PdfReader

from yuxi.knowledge.parser.capabilities import (
    IMAGE_FILE_EXTENSIONS,
    get_ocr_engine_ids,
)
from yuxi.knowledge.parser.zip_utils import process_zip_file as _process_zip_file
from yuxi.knowledge.utils.pdf_utils import validate_pdf_page_tree_loadable
from yuxi.storage.minio import get_minio_client
from yuxi.utils import logger

_OFFICE_BACKENDS = {
    ".docx": (InputFormat.DOCX, MsWordDocumentBackend),
    ".pptx": (InputFormat.PPTX, MsPowerpointDocumentBackend),
    ".xlsx": (InputFormat.XLSX, MsExcelDocumentBackend),
    ".xls": (InputFormat.XLS, MsExcelDocumentBackend),
}
_docling_office_lock = threading.Lock()


def extract_pdf_page_texts(file_path) -> list[str]:
    """按页提取 PDF 文字层，返回每页文本列表。"""
    if isinstance(file_path, str):
        file_path = Path(file_path)

    assert file_path.exists(), "File not found"
    assert file_path.suffix.lower() == ".pdf", "File format not supported"

    with file_path.open("rb") as pdf_file:
        reader = PdfReader(pdf_file)
        return [page.extract_text(extraction_mode="plain").strip() for page in reader.pages]


def pdfreader(file_path, params=None):
    """读取 PDF 文件并返回 text 文本。"""
    return "\n\n".join(extract_pdf_page_texts(file_path))


def parse_pdf(file, params=None):
    """解析 PDF 文件，支持多种 OCR 方式。"""
    from yuxi.knowledge.parser.base import DocumentProcessorException
    from yuxi.knowledge.parser.factory import DocumentProcessorFactory

    opt_ocr, processor_params = _resolve_ocr_engine_params(params)

    if opt_ocr == "disable":
        return pdfreader(file, params=processor_params)

    image_bucket, image_prefix = _resolve_image_storage_params(processor_params)
    processor_params.setdefault("image_bucket", image_bucket)
    processor_params.setdefault("image_prefix", image_prefix)
    processor_kwargs = processor_params.pop("_ocr_processor_kwargs", {})

    try:
        return DocumentProcessorFactory.process_file(opt_ocr, file, processor_params, processor_kwargs)
    except DocumentProcessorException as e:
        logger.error(f"文档处理失败: {e.service_name} - {str(e)}")
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(f"PDF 解析失败: {str(e)}")
        raise DocumentProcessorException(f"PDF解析失败: {str(e)}", opt_ocr, "parsing_failed")


def parse_image(file, params=None):
    """解析图像文件，支持多种 OCR 方式。"""
    from yuxi.knowledge.parser.base import DocumentProcessorException
    from yuxi.knowledge.parser.factory import DocumentProcessorFactory

    opt_ocr, processor_params = _resolve_ocr_engine_params(params)

    if opt_ocr == "disable":
        raise ValueError(
            f"图像文件必须启用OCR才能提取文本内容。请选择OCR方式 ({'/'.join(get_ocr_engine_ids())}) 或移除该文件。"
        )

    image_bucket, image_prefix = _resolve_image_storage_params(processor_params)
    processor_params.setdefault("image_bucket", image_bucket)
    processor_params.setdefault("image_prefix", image_prefix)
    processor_kwargs = processor_params.pop("_ocr_processor_kwargs", {})

    try:
        return DocumentProcessorFactory.process_file(opt_ocr, file, processor_params, processor_kwargs)
    except DocumentProcessorException as e:
        logger.error(f"图像处理失败: {e.service_name} - {str(e)}")
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(f"图像解析失败: {str(e)}")
        raise DocumentProcessorException(f"图像解析失败: {str(e)}", opt_ocr, "parsing_failed")


async def parse_pdf_async(file, params=None):
    return await asyncio.to_thread(parse_pdf, file, params=params)


FIGURE_ANALYSIS_PROMPT = (
    "你是科研文档插图解析助手。请解析这张从文档中提取的插图：\n"
    "1) 判断图的类型（如实验流程、数据图表、显微/电镜照片、装置示意图、分子结构等）；\n"
    "2) 提取图中可读的文字、坐标轴、图例、数值和趋势；\n"
    "3) 总结图传递的关键信息。\n"
    "只描述图中真实存在的内容，不要编造图中没有的信息。使用中文回答，不超过 300 字。"
)

FORMULA_TRANSCRIBE_PROMPT = (
    "你是科研文档公式转写助手。请把这张公式图片精确转写为 LaTeX：\n"
    "- 只输出 LaTeX 代码本身，独立公式用 $$...$$ 包裹，多行公式用 \\begin{aligned}...\\end{aligned}；\n"
    "- 不要输出任何解释、编号或多余文字；\n"
    "- 保留上下标、希腊字母、分式、根号、求和、积分、箭头和化学反应条件等结构。"
)

_FIGURE_ANALYSIS_CONCURRENCY = 4


async def parse_pdf_disable(file, params=None) -> str:
    """OCR 禁用模式：按页面布局提取文本，插图与公式区域在原位嵌入并可交视觉模型解析。

    params 可包含 ``image_analysis_model``（模型 spec）与 ``image_bucket``/``image_prefix``。
    未配置模型时插图与公式仅嵌入图片不生成解析文字；模型 spec 无效时抛 ValueError，
    让解析失败以暴露配置错误。
    """
    from yuxi.knowledge.parser.pdf_figures import LayoutBlock, extract_pdf_layout
    from yuxi.models.chat import select_model

    params = params or {}
    pages = await asyncio.to_thread(extract_pdf_layout, file)
    if not any(blocks for blocks in pages):
        return "\n\n".join(await asyncio.to_thread(extract_pdf_page_texts, file))

    media_blocks = [block for blocks in pages for block in blocks if block.kind != "text"]
    image_bucket, image_prefix = _resolve_image_storage_params(params)
    model_spec = str(params.get("image_analysis_model") or "").strip()
    model = None
    if model_spec:
        model = select_model(model_spec)
    elif media_blocks:
        logger.info("未配置插图解析模型，插图与公式仅嵌入原文不做模型解析")

    semaphore = asyncio.Semaphore(_FIGURE_ANALYSIS_CONCURRENCY)
    failed_blocks: set[int] = set()

    async def upload_and_analyze(block: LayoutBlock) -> None:
        try:
            block.url = await asyncio.to_thread(
                _upload_image_to_minio, block.image, block.filename, image_bucket, image_prefix
            )
        except Exception as e:  # noqa: BLE001
            # 正文是解析主体，单张图片上传失败时跳过该图而不是让整个解析失败
            logger.error(f"图片上传失败，已跳过（{block.name}）: {e}")
            failed_blocks.add(id(block))
            return
        if model is None:
            return
        prompt = FORMULA_TRANSCRIBE_PROMPT if block.kind == "formula" else FIGURE_ANALYSIS_PROMPT
        async with semaphore:
            block.description = await _analyze_image(model, block, prompt, model_spec)

    await asyncio.gather(*(upload_and_analyze(block) for block in media_blocks))

    page_parts: list[str] = []
    for blocks in pages:
        parts: list[str] = []
        for block in blocks:
            if id(block) in failed_blocks:
                continue
            if block.kind == "text":
                parts.append(block.text)
            elif block.kind == "formula":
                formula_parts = []
                if block.description:
                    formula_parts.append(block.description)
                formula_parts.append(f"![{block.name}]({block.url})")
                parts.append("\n\n".join(formula_parts))
            else:
                figure_parts = [f"![{block.name}]({block.url})"]
                if block.description:
                    figure_parts.append(f"**插图解析**：{block.description}")
                parts.append("\n\n".join(figure_parts))
        page_parts.append("\n\n".join(part for part in parts if part))
    return "\n\n".join(part for part in page_parts if part)


async def _analyze_image(model, block, prompt: str, model_spec: str) -> str:
    """调用视觉模型解析单张图片（插图或公式），失败时降级为空描述。"""
    from langchain_core.messages import HumanMessage

    data_url = f"data:{block.mime};base64,{base64.b64encode(block.image).decode('ascii')}"
    # 适配器通过 convert_to_messages 归一输入，多模态消息需以消息列表形式传入
    message = [
        HumanMessage(
            content=[
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]
        )
    ]
    try:
        response = await model.call(message, stream=False)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"图片解析失败，保留图片不生成描述（{block.name}，模型 {model_spec}）: {e}")
        return ""
    return str(getattr(response, "content", "") or "").strip()


async def parse_image_async(file, params=None):
    return await asyncio.to_thread(parse_image, file, params=params)


async def parse_resolved_document(source: str, params: dict | None = None) -> str:
    """使用已解析的运行时参数，将本地或 MinIO 文件转换为 Markdown。"""
    from yuxi.knowledge.utils.kb_utils import is_minio_url, parse_minio_url
    from yuxi.storage.minio.client import get_minio_client

    # 1. 如果是 MinIO URL，下载文件到临时路径
    if is_minio_url(source):
        logger.debug(f"Downloading file from MinIO: {source}")

        if "?" in source:
            source_clean = source.split("?")[0]
        else:
            source_clean = source

        original_filename = source_clean.split("/")[-1]

        with tempfile.NamedTemporaryFile(delete=False, suffix=Path(original_filename).suffix) as temp_file:
            temp_path = temp_file.name

        try:
            bucket_name, object_name = parse_minio_url(source)
            minio_client = get_minio_client()
            file_content = await minio_client.adownload_file(bucket_name, object_name)

            async with aiofiles.open(temp_path, "wb") as f:
                await f.write(file_content)

            logger.debug(f"File downloaded to temp path: {temp_path}")
            actual_file_path = temp_path

        except Exception as e:  # noqa: BLE001
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            logger.error(f"Failed to download file from MinIO: {e}")
            raise ValueError(f"无法从MinIO下载文件: {e}")
    else:
        actual_file_path = source

    # 2. 根据文件类型调用不同的解析器
    try:
        file_path_obj = Path(actual_file_path)
        file_ext = file_path_obj.suffix.lower()

        if file_ext == ".pdf":
            validate_pdf_page_tree_loadable(file_path_obj)
            if str((params or {}).get("ocr_engine") or "").strip() == "disable":
                result = await parse_pdf_disable(str(file_path_obj), params=params)
            else:
                result = await parse_pdf_async(str(file_path_obj), params=params)

        elif file_ext in [".txt", ".md"]:
            async with aiofiles.open(file_path_obj, encoding="utf-8") as f:
                content = await f.read()
            result = content

        elif file_ext == ".docx":
            try:
                result = await asyncio.to_thread(_convert_with_docling, file_path_obj, params=params)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Docling 解析 DOCX 失败，回退到 python-docx: {file_path_obj.name}, {e}")
                result = await asyncio.to_thread(_convert_docx_with_python_docx, file_path_obj)

        elif file_ext == ".pptx":
            result = await asyncio.to_thread(_convert_with_docling, file_path_obj, params=params)

        elif file_ext in IMAGE_FILE_EXTENSIONS:
            result = await parse_image_async(str(file_path_obj), params=params)

        elif file_ext in [".html", ".htm"]:
            async with aiofiles.open(file_path_obj, encoding="utf-8") as f:
                content = await f.read()
            text = await asyncio.to_thread(md_convert, content, heading_style="ATX")
            result = f"{text}"

        elif file_ext == ".csv":
            result = await asyncio.to_thread(_convert_csv_to_markdown, file_path_obj)

        elif file_ext == ".xls":
            result = await asyncio.to_thread(_convert_xls_to_markdown, file_path_obj)

        elif file_ext == ".xlsx":
            result = await asyncio.to_thread(_convert_with_docling, file_path_obj, params=params)

        elif file_ext == ".json":
            import json

            async with aiofiles.open(file_path_obj, encoding="utf-8") as f:
                content = await f.read()
            data = json.loads(content)
            json_str = json.dumps(data, ensure_ascii=False, indent=2)
            result = f"```json\n{json_str}\n```"

        elif file_ext == ".zip":
            image_bucket, image_prefix = _resolve_image_storage_params(params)
            result = await _process_zip_file(
                str(file_path_obj),
                image_bucket=image_bucket,
                image_prefix=image_prefix,
            )

        else:
            raise ValueError(f"Unsupported file type: {file_ext}")

    except Exception:
        if is_minio_url(source) and os.path.exists(actual_file_path):
            try:
                os.unlink(actual_file_path)
                logger.debug(f"Cleaned up temp file: {actual_file_path}")
            except Exception as cleanup_e:  # noqa: BLE001
                logger.warning(f"Failed to clean up temp file {actual_file_path}: {cleanup_e}")
        raise

    finally:
        if is_minio_url(source) and os.path.exists(actual_file_path):
            try:
                os.unlink(actual_file_path)
                logger.debug(f"Cleaned up temp file: {actual_file_path}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Failed to clean up temp file {actual_file_path}: {e}")

    return result


def _convert_office_document(file_path: Path) -> DoclingDocument:
    """用 Docling Slim 的格式 backend 转换一个 Office 文件。"""
    try:
        input_format, backend_type = _OFFICE_BACKENDS[file_path.suffix.lower()]
    except KeyError as exc:
        raise ValueError(f"Docling 不支持该 Office 格式: {file_path.suffix}") from exc

    input_document = InputDocument(file_path, input_format, backend_type)
    backend = getattr(input_document, "_backend", None)
    if backend is None:
        raise RuntimeError(f"Docling 无法读取 Office 文件: {file_path.name}")

    try:
        if not input_document.valid:
            raise RuntimeError(f"Docling 无法读取 Office 文件: {file_path.name}")
        return backend.convert()
    finally:
        backend.unload()


def _resolve_image_storage_params(params: dict | None) -> tuple[str, str]:
    params = params or {}

    image_bucket = params.get("image_bucket") or get_minio_client().KB_BUCKETS["images"]
    image_prefix = params.get("image_prefix")
    if image_prefix:
        normalized_prefix = str(image_prefix).strip("/")
        if normalized_prefix:
            return image_bucket, normalized_prefix

    return image_bucket, "unknown/kb-images"


def _resolve_ocr_engine_params(params: dict | None) -> tuple[str, dict[str, Any]]:
    params = params or {}
    engine = str(params.get("ocr_engine") or "").strip()
    if not engine:
        raise ValueError("OCR 文件缺少已解析的 ocr_engine，请通过 parse_document() 解析")

    processor_params = dict(params)
    processor_params.pop("ocr_engine_config", None)
    return engine, processor_params


def _upload_image_to_minio(image_data: bytes, filename: str, bucket_name: str, object_prefix: str) -> str:
    """上传图片到 MinIO，返回经后端鉴权代理访问的 URL。"""
    from yuxi.knowledge.utils.kb_utils import build_kb_image_proxy_url

    minio_client = get_minio_client()
    minio_client.ensure_bucket_exists(bucket_name)

    normalized_prefix = object_prefix.strip("/") or "unknown/kb-images"
    timestamp = int(time.time() * 1000000)
    object_name = f"{normalized_prefix}/{timestamp}_{Path(filename).name}"

    minio_client.upload_file(
        bucket_name=bucket_name,
        object_name=object_name,
        data=image_data,
    )
    return build_kb_image_proxy_url(object_name)


def _parse_data_uri(data_uri: str) -> tuple[bytes, str]:
    """解析 data URI，返回 (image_data, mime_type)。"""
    header, base64_data = data_uri.split(",", 1)
    mime_type = header.split(":")[1].split(";")[0]
    image_data = base64.b64decode(base64_data)
    return image_data, mime_type


def _convert_with_docling(file_path: Path, params: dict | None = None) -> str:
    """使用 Docling Slim 将 Office 文件转换为 Markdown。"""
    params = params or {}
    image_bucket, image_prefix = _resolve_image_storage_params(params)

    with _docling_office_lock:
        doc = _convert_office_document(file_path)

    if hasattr(doc, "pictures") and doc.pictures:
        replacements: list[str] = []
        for pic in doc.pictures:
            uri = str(pic.image.uri) if hasattr(pic, "image") and hasattr(pic.image, "uri") else ""
            if uri.startswith("data:"):
                filename = "image"
                try:
                    image_data, mime_type = _parse_data_uri(uri)
                    filename = f"image_{int(time.time() * 1000000)}.{mime_type.split('/')[-1]}"
                    url = _upload_image_to_minio(image_data, filename, image_bucket, image_prefix)
                    replacements.append(f"![{filename}]({url})")
                except Exception as e:  # noqa: BLE001
                    logger.error(f"上传图片失败 {filename}: {e}")
                    replacements.append(f"[图片: {filename}]")
            else:
                replacements.append("")

        markdown = doc.export_to_markdown()
        for replacement in replacements:
            markdown = re.sub(r"<!--\s*image\s*-->", replacement, markdown, count=1)
        return markdown

    return doc.export_to_markdown()


def _convert_docx_with_python_docx(file_path: Path) -> str:
    """使用 python-docx 解析 DOCX（Docling 失败时兜底）。"""
    from docx import Document

    document = Document(str(file_path))
    blocks: list[str] = []

    for para in document.paragraphs:
        text = para.text.strip()
        if text:
            blocks.append(text)

    for table in document.tables:
        rows: list[list[str]] = []
        for row in table.rows:
            cells = [cell.text.strip().replace("\n", " ") for cell in row.cells]
            if any(cells):
                rows.append(cells)

        if not rows:
            continue

        header = rows[0]
        blocks.append(f"| {' | '.join(header)} |")
        blocks.append(f"| {' | '.join(['---'] * len(header))} |")

        for row in rows[1:]:
            normalized_row = row + [""] * (len(header) - len(row))
            blocks.append(f"| {' | '.join(normalized_row[: len(header)])} |")

        blocks.append("")

    return "\n\n".join(blocks).strip()


def _convert_csv_to_markdown(file_path: Path) -> str:
    import pandas as pd

    dataframe = pd.read_csv(file_path)
    tables: list[str] = []
    for i in range(len(dataframe)):
        row_dataframe = dataframe.iloc[[i]]
        tables.append(row_dataframe.to_markdown(index=False))
    return "\n\n".join(tables)


def _convert_xls_to_markdown(file_path: Path) -> str:
    """使用 pandas + xlrd 解析旧版 .xls 文件并转为 Markdown（Docling 不支持该格式）。"""
    import pandas as pd

    # 文档提取不假定首行是表头，也不把文本编号和 NA 等字面值转换成分析数据。
    sheet_map = pd.read_excel(
        file_path, engine="xlrd", sheet_name=None, header=None, dtype=object, keep_default_na=False
    )
    blocks: list[str] = []
    for sheet_name, dataframe in sheet_map.items():
        if dataframe.empty:
            continue
        if len(sheet_map) > 1:
            blocks.append(f"## {sheet_name}")
        blocks.append(dataframe.to_markdown(index=False, headers=[""] * len(dataframe.columns), disable_numparse=True))
    return "\n\n".join(blocks)
