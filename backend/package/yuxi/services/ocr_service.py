"""OCR 方法选择、运行时配置和健康检测。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from yuxi.config.options import (
    mineru_ocr_host_opts,
    mineru_official_api_opts,
    oxr_api_opts,
    paddleocr_api_opts,
    pp_structure_v3_ocr_host_opts,
    system_options,
)
from yuxi.knowledge.parser.capabilities import OCR_FILE_EXTENSIONS, PARSER_CAPABILITIES, get_parser_capability
from yuxi.knowledge.parser.factory import DocumentProcessorFactory
from yuxi.models.providers.service import get_model_provider_by_id, resolve_api_key


async def get_ocr_options(db: AsyncSession | None = None) -> dict[str, Any]:
    options = await system_options.get(db)
    return {
        "default_engine": options["default_ocr_engine"],
        "engines": [
            {
                "engine_id": engine_id,
                "service_name": capability.service_name,
                "display_name": capability.display_name,
                "supported_extensions": list(capability.supported_extensions),
            }
            for engine_id, capability in PARSER_CAPABILITIES.items()
        ],
    }


def resolve_ocr_engine_id(engine_id: str | None, default_engine: str) -> str:
    resolved = str(engine_id or default_engine).strip() or default_engine
    if resolved == "disable":
        return resolved
    if resolved not in PARSER_CAPABILITIES:
        raise ValueError(f"不支持的 OCR 引擎: {resolved}")
    return resolved


async def resolve_ocr_task_params(
    params: dict[str, Any] | None = None,
    db: AsyncSession | None = None,
) -> dict[str, Any]:
    resolved = dict(params or {})
    configured_engine = resolved.get("ocr_engine")
    if configured_engine is None:
        default_engine = (await system_options.get(db))["default_ocr_engine"]
    else:
        default_engine = str(configured_engine)
    engine_id = resolve_ocr_engine_id(configured_engine, default_engine)
    resolved["ocr_engine"] = engine_id
    resolved.pop("ocr_engine_config", None)

    # 知识库任务有明确图片归属；API 调用方也能复用默认视觉模型。
    # 临时附件没有该归属时保留纯文字能力，不向 unknown 前缀写入图片。
    image_analysis_model = str(resolved.get("image_analysis_model") or "").strip()
    enrich_pdf = engine_id in {"disable", "oxr_ocr"} and bool(resolved.get("image_prefix"))
    needs_defaults = enrich_pdf and (
        "image_analysis_model" not in resolved
        or engine_id == "oxr_ocr"
        and ("document_review_model" not in resolved or "document_review_vision_final_authority" not in resolved)
    )
    defaults = await system_options.get(db) if needs_defaults else {}
    if enrich_pdf and "image_analysis_model" not in resolved:
        image_analysis_model = str(defaults.get("default_image_analysis_model") or "").strip()
    if enrich_pdf:
        resolved["image_analysis_model"] = image_analysis_model
    else:
        resolved.pop("image_analysis_model", None)
    if engine_id == "oxr_ocr" and enrich_pdf:
        if "document_review_model" not in resolved:
            resolved["document_review_model"] = str(defaults.get("default_document_review_model") or "").strip()
        if "document_review_vision_final_authority" not in resolved:
            resolved["document_review_vision_final_authority"] = bool(defaults.get("default_document_review_vision_final_authority"))
    else:
        resolved.pop("document_review_model", None)
        resolved.pop("document_review_vision_final_authority", None)

    if engine_id == "disable":
        kwargs = {}
    elif db is None:
        from yuxi.storage.postgres.manager import pg_manager

        async with pg_manager.get_async_session_context() as session:
            kwargs = await _build_processor_kwargs(session, engine_id)
    else:
        kwargs = await _build_processor_kwargs(db, engine_id)

    resolved["_ocr_processor_kwargs"] = kwargs
    return resolved


async def parse_document_result(
    source: str,
    params: dict[str, Any] | None = None,
    db: AsyncSession | None = None,
    control=None,
):
    """使用当前运行时配置将文件解析为 Markdown。

    这是业务代码唯一应调用的文档解析入口。PDF 的模型模式先验证显式选择的
    聊天模型，不读取 OCR 配置；其余 PDF 与图片沿用 OCR 配置解析。
    普通文本、Office、表格等文件继续交给既有格式解析器。

    底层 parser 只接收已经准备好的模型或 OCR 参数，不查询数据库。调用方
    不应直接调用 ``yuxi.knowledge.parser.unified`` 中的内部解析入口，否则会
    绕过数据库配置、环境变量回退和默认 OCR 引擎解析。

    Args:
        source: 本地文件路径或系统支持的 MinIO 文件地址。
        params: 文件解析参数。可以包含 ``ocr_engine``、图片存储位置和各解析器
            支持的业务参数；PDF 的 pdf_parse_mode 缺失时为 ocr，模型模式必须
            显式传入 document_recognition_model。未指定 OCR 引擎时使用系统默认值。
        db: 可选的异步数据库会话。已有事务的调用方可以传入以复用会话；未传入
            时仅在解析配置或默认图片模型需要查询数据库时创建独立会话。

    Returns:
        解析后的 ParsedDocument，包含 Markdown 与独立附属结果。

    Raises:
        ValueError: OCR 引擎无效、图片禁用 OCR 或文件类型不受支持。
        DocumentProcessorException: OCR 或文档解析器执行失败。
        StorageError: MinIO 文件读取失败。
    """

    resolved_params = params
    suffix = Path(source.split("?", 1)[0]).suffix.lower()
    if suffix != ".pdf":
        resolved_params = dict(params or {})
        resolved_params.pop("pdf_parse_mode", None)
        resolved_params.pop("document_recognition_model", None)
    mode = (resolved_params or {}).get("pdf_parse_mode", "ocr")
    if mode not in ("ocr", "model"):
        raise ValueError("pdf_parse_mode 必须为 ocr 或 model")
    if mode == "model":
        resolved_params = await _resolve_pdf_model_params(resolved_params, db)
    elif suffix in OCR_FILE_EXTENSIONS:
        resolved_params = await resolve_ocr_task_params(resolved_params, db)
        engine_id = resolved_params["ocr_engine"]
        if engine_id != "disable" and suffix not in get_parser_capability(engine_id).supported_extensions:
            raise ValueError(f"OCR 引擎 {engine_id} 不支持文件类型 {suffix}")

    from yuxi.knowledge.parser.unified import parse_resolved_document_result

    return await parse_resolved_document_result(source=source, params=resolved_params, control=control)


async def parse_document(source: str, params=None, db=None) -> str:
    """保持附件与工具调用的字符串契约，复用唯一解析流程。"""
    return (await parse_document_result(source, params, db)).markdown


async def check_all_ocr_health(db: AsyncSession) -> dict[str, Any]:
    """使用当前有效配置并行检查所有 OCR 方法。"""

    configured = []
    results = {}
    for engine_id in PARSER_CAPABILITIES:
        try:
            kwargs = await _build_processor_kwargs(db, engine_id)
            configured.append((engine_id, kwargs))
        except Exception as exc:
            results[engine_id] = {"status": "error", "message": str(exc), "details": {}}

    async def check(engine_id: str, kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        try:
            result = await asyncio.to_thread(DocumentProcessorFactory.check_health, engine_id, **kwargs)
        except Exception as exc:
            result = {"status": "error", "message": str(exc), "details": {}}
        return engine_id, result

    checked = await asyncio.gather(*(check(engine_id, kwargs) for engine_id, kwargs in configured))
    results.update(checked)
    return results


async def _build_processor_kwargs(db: AsyncSession, engine_id: str) -> dict[str, Any]:
    if engine_id == "oxr_ocr":
        opts = await oxr_api_opts.get(db)
        return {key: value for key, value in opts.items() if value}
    if engine_id == "mineru_ocr":
        opts = await mineru_ocr_host_opts.get(db)
        return {"server_url": opts["server_url"]} if opts["server_url"] else {}
    if engine_id == "mineru_official":
        opts = await mineru_official_api_opts.get(db)
        return {"api_key": opts["api_key"]} if opts["api_key"] else {}
    if engine_id == "pp_structure_v3_ocr":
        opts = await pp_structure_v3_ocr_host_opts.get(db)
        return {"server_url": opts["server_url"]} if opts["server_url"] else {}
    if engine_id == "deepseek_ocr":
        provider = await get_model_provider_by_id(db, "siliconflow-cn")
        api_key = resolve_api_key(provider) if provider and provider.is_enabled else None
        if not api_key:
            raise ValueError("siliconflow-cn 模型供应商凭证不可用")
        return {
            "api_key": api_key,
            "api_url": f"{provider.base_url.rstrip('/')}/chat/completions",
        }
    if engine_id in {"paddleocr_vl_1_6", "paddleocr_pp_ocrv6"}:
        opts = await paddleocr_api_opts.get(db)
        return {key: value for key, value in opts.items() if value}
    return {}


async def _resolve_pdf_model_params(params: dict | None, db: AsyncSession | None) -> dict:
    """保留原图识别和校对模型，清理 OCR 专用参数。"""
    from yuxi.models.providers.cache import model_cache

    resolved = dict(params or {})
    model_spec = resolved.get("document_recognition_model")
    if not isinstance(model_spec, str) or not model_spec.strip():
        raise ValueError("模型识别模式必须显式选择文档识别模型")
    model_spec = model_spec.strip()
    info = await asyncio.to_thread(model_cache.get_model_info, model_spec)
    if info is None or info.model_type != "chat":
        raise ValueError("文档识别模型必须是已配置并启用的聊天模型")
    resolved.update(pdf_parse_mode="model", document_recognition_model=model_spec)
    for key in (
        "ocr_engine",
        "ocr_engine_config",
        "_ocr_processor_kwargs",
        "enable_ocr",
        "document_review_vision_final_authority",
    ):
        resolved.pop(key, None)
    if resolved.get("image_prefix"):
        if "image_analysis_model" not in resolved:
            resolved["image_analysis_model"] = (await system_options.get(db)).get("default_image_analysis_model")
        resolved["image_analysis_model"] = str(resolved.get("image_analysis_model") or "").strip()
    else:
        resolved.pop("image_analysis_model", None)
    return resolved
