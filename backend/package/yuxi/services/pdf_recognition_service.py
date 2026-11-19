"""逐页原图模型识别，整份成功后才交回原有发布流程。"""

from __future__ import annotations

import asyncio
import time
import uuid

from yuxi.knowledge.parser.base import DocumentParserException
from yuxi.knowledge.parser.pdf_native import count_pdf_pages, read_native_page_async, render_pdf_region_async
from yuxi.knowledge.parser.pdf_recognition import (
    PROMPT_VERSION,
    RECOGNITION_PROMPT,
    InvalidRecognitionResponse,
    normalize_recognition_page,
)
from yuxi.knowledge.parser.pdf_recognition_source import check_recognition_source
from yuxi.knowledge.parser.result import ParsedDocument
from yuxi.models.chat import ModelRequestError
from yuxi.services.pdf_model_execution import (
    InvalidPdfModelResponse,
    PdfModelExecutor,
    PdfModelUnavailable,
    run_pdf_workers,
)
from yuxi.services.pdf_recognition_review_service import (
    attach_recognition_review_images,
    collect_recognition_uncertainties,
    review_recognition_page,
    review_recognition_seam,
)
from yuxi.utils.logging_config import logger


class PdfRecognitionError(DocumentParserException):
    """只公开页号、安全原因和请求身份，不携带文档或供应商原文。"""

    def __init__(self, page: int | None, reason: str, request_id: str | None = None):
        """把失败定位绑定到当前识别页和请求。"""
        self.page, self.reason, self.request_id = page, reason, request_id
        location = f"第 {page} 页" if page is not None else "读取页数"
        explanation = {
            "incomplete_page": "模型未完整识别本页",
            "response_truncated": "模型输出达到长度上限，内容不完整",
            "invalid_formula_delimiters": "模型返回的独立公式含无效或不配对的定界符",
            "credentials_rejected": "模型供应商凭据被拒绝",
            "rate_limited": "模型供应商限流，请稍后重试",
            "image_not_supported": "所选模型不支持当前图像输入",
            "connection_failed": "模型服务连接失败",
            "provider_timeout": "模型服务响应超时",
            "provider_unavailable": "模型服务暂时不可用",
            "provider_error": "模型服务请求失败",
            "pdf_page_count_failed": "无法读取PDF页数",
            "page_render_failed": "无法渲染原PDF页面",
            "shared_model_unavailable": "共享模型并发服务不可用",
            "recognition_model_required": "请显式选择文档识别模型",
            "recognition_model_unavailable": "文档识别模型配置或共享服务不可用",
            "task_deadline_exceeded": "解析任务超过剩余执行时间",
            "recognition_failed": "文档模型识别未完成",
        }.get(reason, "模型返回内容不符合识别格式")
        super().__init__(
            f"PDF 模型识别失败：{location}，{explanation}（reason={reason}，request_id={request_id or '-'}）",
            "pdf_model_recognition",
            reason,
        )


async def recognize_pdf_document(file_path: str, params: dict, control=None) -> ParsedDocument:
    """有界按需渲染并发识别全部页，失败收敛在途且不发布部分结果。"""
    from pypdfium2 import PdfiumError

    from yuxi.services.pdf_document_service import enrich_document

    model_spec = params.get("document_recognition_model")
    if not isinstance(model_spec, str) or not model_spec.strip():
        raise PdfRecognitionError(1, "recognition_model_required")
    model_spec = model_spec.strip()
    if control:
        await control.check_cancelled()
    try:
        total = await asyncio.to_thread(count_pdf_pages, file_path)
    except Exception:
        raise PdfRecognitionError(None, "pdf_page_count_failed") from None
    executor = PdfModelExecutor(model_spec, control=control, stage="recognition")
    try:
        await executor.initialize()
    except Exception:
        raise PdfRecognitionError(1, "recognition_model_unavailable") from None

    stage = {
        "status": "running",
        "model": model_spec,
        "prompt_version": PROMPT_VERSION,
        "total_pages": total,
        "pages": [],
        "warnings": [],
    }
    results = {}
    failure = None
    progress_lock = asyncio.Lock()
    prefix = (
        f"[pdf_recognition] task_id={getattr(control, 'task_id', None) or '-'}"
        f" file_id={getattr(control, 'file_id', None) or '-'}"
        f" attempt_id={getattr(control, 'attempt_id', None) or '-'}"
    )
    review_spec = str(params.get("document_review_model") or "").strip()
    review_executor = None
    review_stage = {"model": review_spec, "corrected": 0, "failed_checks": []}
    if review_spec:
        try:
            review_executor = await PdfModelExecutor(
                review_spec, control=control, stage="recognition_review"
            ).initialize()
        except (ValueError, PdfModelUnavailable) as exc:
            review_stage["failed_checks"].append("setup")
            logger.warning(f"{prefix} review=setup error={type(exc).__name__}")

    async def check_review(label, operation) -> None:
        """独立记录可选校对失败，保留本次已识别正文。"""
        try:
            changes = await operation
        except (ValueError, OSError, PdfiumError, ModelRequestError, PdfModelUnavailable) as exc:
            review_stage["failed_checks"].append(label)
            reason = exc.reason if isinstance(exc, (InvalidRecognitionResponse, ModelRequestError)) else type(exc).__name__
            logger.warning(f"{prefix} review={label} status=failed error={type(exc).__name__} reason={reason}")
        else:
            review_stage["corrected"] += changes

    async def recognize(page: int) -> None:
        """单个worker持有一页原图，最多重试一次且绝不切换解析模式。"""
        nonlocal failure
        if failure is not None:
            raise failure
        if control:
            await control.check_cancelled()
        record = {
            "page": page,
            "status": "running",
            "blank": None,
            "request_ids": [],
            "attempts": [],
            "warnings": [],
        }
        stage["pages"].append(record)
        try:
            image = await render_pdf_region_async(file_path, page, [0, 0, 1, 1])
        except Exception:
            failure = PdfRecognitionError(page, "page_render_failed")
            raise failure from None
        for attempt in (1, 2):
            if failure is not None:
                raise failure
            if control:
                await control.check_cancelled()
            request_id = uuid.uuid4().hex
            record["request_ids"].append(request_id)
            outcome = {
                "request_id": request_id,
                "status": "running",
                "reason": None,
                "stop_reasons": {"finish_reason": None, "stop_reason": None},
                "usage": _recognition_usage(None),
                "queue_seconds": None,
                "call_seconds": None,
            }
            record["attempts"].append(outcome)
            started = time.monotonic()
            try:
                response = await executor.request(
                    "完整转写随附的当前一页原图，严格返回协议JSON。",
                    image,
                    system_prompt=RECOGNITION_PROMPT,
                    batch_id=f"page-{page}",
                    attempt=attempt,
                    request_id=request_id,
                )
                outcome.update(
                    usage=_recognition_usage(response.usage),
                    stop_reasons={
                        "finish_reason": response.finish_reason,
                        "stop_reason": response.stop_reason,
                    },
                    queue_seconds=round(response.queue_seconds, 3),
                    call_seconds=round(response.call_seconds, 3),
                )
                if response.truncated:
                    raise InvalidRecognitionResponse("response_truncated")
                page_result = normalize_recognition_page(response.text, page)
                if control:
                    await control.check_cancelled()
                outcome["status"] = "completed"
                record.update(
                    status="blank" if page_result.metadata["blank"] else "completed",
                    blank=page_result.metadata["blank"],
                )
                if not any(value is not None for value in outcome["stop_reasons"].values()):
                    record["warnings"].append("stop_reason_not_reported")
                if page_result.diagnostics:
                    record["warnings"].append("unclear_regions")
                ignored_captions = page_result.metadata.get("ignored_caption_references")
                if ignored_captions:
                    record["warnings"].append("invalid_caption_reference")
                    record["ignored_caption_references"] = ignored_captions
                    logger.warning(
                        f"{prefix} page={page} request_id={request_id} ignored_caption_references={ignored_captions}"
                    )
                logger.info(
                    f"{prefix} page={page} status={record['status']} request_id={request_id}"
                    f" attempt={attempt} elapsed_s={time.monotonic() - started:.2f}"
                )
                break
            except asyncio.CancelledError:
                outcome["status"] = record["status"] = "cancelled"
                raise
            except Exception as exc:
                reason, retryable = _recognition_failure(exc)
                outcome.update(status="failed", reason=reason)
                logger.warning(
                    f"{prefix} page={page} status=failed request_id={request_id} attempt={attempt}"
                    f" reason={reason} elapsed_s={time.monotonic() - started:.2f}"
                )
                if not retryable or attempt == 2:
                    record["status"] = "failed"
                    failure = PdfRecognitionError(page, reason, request_id)
                    raise failure from None
        if review_executor is not None:
            await check_review(
                f"page-{page}", review_recognition_page(file_path, page, image, page_result, review_executor)
            )
        # 等上一条消息落库后再更新页数，避免独立会话乱序提交。
        async with progress_lock:
            results[page] = page_result
            if control and control.context is not None:
                await control.context.set_message(f"PDF 已处理 {len(results)}/{total} 页（当前第 {page} 页）")

    await run_pdf_workers(range(1, total + 1), recognize, executor.concurrency)
    if review_executor is not None and total > 1:
        if control and control.context is not None:
            await control.context.set_message("PDF 校对跨页续接")

        async def review_seam(page: int) -> None:
            """只在相邻两页已完成各自校对后检查接续。"""
            await check_review(
                f"seam-{page}",
                review_recognition_seam(file_path, page, results[page], results[page + 1], review_executor),
            )

        await run_pdf_workers(range(1, total), review_seam, review_executor.concurrency)
    if control:
        await control.check_cancelled()
    result = ParsedDocument(
        metadata={
            "engine": "model",
            "pdf_parse_mode": "model",
            "parsed_pages": [],
            "review_status": "not_requested",
        },
    )
    source_check = {"native_pages": 0, "eligible_blocks": 0, "compared_blocks": 0, "flagged_blocks": 0}
    failed_source_pages = []
    for page in range(1, total + 1):
        if control:
            await control.check_cancelled()
        current = results[page]
        collect_recognition_uncertainties(current)
        try:
            native = await read_native_page_async(file_path, page)
        except (PdfiumError, OSError, ValueError) as exc:
            failed_source_pages.append(page)
            logger.warning(f"{prefix} source_check=unavailable page={page} error={type(exc).__name__}")
            native = []
        coverage = await asyncio.to_thread(check_recognition_source, current, native)
        del native
        for name, count in coverage.items():
            source_check[name] += count
        for block in current.blocks:
            block["order"] = len(result.blocks)
            result.blocks.append(block)
        result.figures.extend(current.figures)
        result.reviews.extend(current.reviews)
        result.diagnostics.extend(current.diagnostics)
        result.metadata["parsed_pages"].append(page)
    if failed_source_pages:
        source_status = "partial" if source_check["compared_blocks"] else "unavailable"
    elif source_check["compared_blocks"]:
        source_status = "compared"
    else:
        source_status = "no_comparable_text"
    result.metadata["recognition_source_check"] = {
        "status": source_status, **source_check, "failed_pages": failed_source_pages,
    }
    logger.info(f"{prefix} source_check={source_status} coverage={source_check}")
    if review_spec:
        result.metadata["recognition_review"] = review_stage
    if review_stage["failed_checks"]:
        result.metadata["review_status"] = "incomplete"
    elif result.reviews:
        result.metadata["review_status"] = "needs_review"
    elif review_spec:
        result.metadata["review_status"] = "completed"
    await attach_recognition_review_images(result, file_path, control)
    stage["pages"].sort(key=lambda item: item["page"])
    stage["status"] = "completed"
    attempts = [outcome for page in stage["pages"] for outcome in page["attempts"]]
    stage["usage"] = {
        key: (
            sum(outcome["usage"][key] for outcome in attempts)
            if all(outcome["usage"][key] is not None for outcome in attempts)
            else None
        )
        for key in ("input", "output", "total", "reasoning", "cached_input")
    }
    stage["usage"]["unit"] = "token"
    if any(stage["usage"][key] is None for key in ("input", "output", "total")):
        stage["warnings"].append("incomplete_usage")
    result.metadata["model_recognition"] = stage
    result = await enrich_document(result, file_path, params, control)
    if not result.markdown.strip():
        raise DocumentParserException("模型未识别到可用正文", "pdf_model_recognition", "empty_recognition")
    return result


def _recognition_usage(usage: dict | None) -> dict:
    """避开凭据过滤关键字，未知计量保留为空且不伪造零消耗。"""
    source = usage or {}
    names = {
        "input": "input_tokens",
        "output": "output_tokens",
        "total": "total_tokens",
        "reasoning": "reasoning_tokens",
        "cached_input": "provider_cached_input_tokens",
    }
    return {
        **{key: source.get(name) for key, name in names.items()},
        "unit": "token",
        "status": source.get("status", "unknown"),
        "source": source.get("source", "none"),
    }


def _recognition_failure(exc: Exception) -> tuple[str, bool]:
    """仅暂时连接、供应商超时和页协议错误允许一次重试。"""
    if isinstance(exc, InvalidRecognitionResponse):
        return exc.reason, True
    if isinstance(exc, InvalidPdfModelResponse):
        return "invalid_response_content", True
    if isinstance(exc, ModelRequestError):
        return exc.reason, exc.reason in {"provider_timeout", "connection_failed", "provider_unavailable"}
    if isinstance(exc, PdfModelUnavailable):
        return "shared_model_unavailable", False
    if isinstance(exc, TimeoutError):
        return "task_deadline_exceeded", False
    return "recognition_failed", False
