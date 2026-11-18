"""逐页原图模型识别，整份成功后才交回原有发布流程。"""

from __future__ import annotations

import asyncio
import time
import uuid

from yuxi.knowledge.parser.base import DocumentParserException
from yuxi.knowledge.parser.pdf_native import count_pdf_pages, render_pdf_region
from yuxi.knowledge.parser.pdf_recognition import (
    PROMPT_VERSION,
    RECOGNITION_PROMPT,
    InvalidRecognitionResponse,
    normalize_recognition_page,
)
from yuxi.knowledge.parser.result import ParsedDocument
from yuxi.models.chat import ModelRequestError
from yuxi.services.pdf_model_execution import (
    InvalidPdfModelResponse,
    PdfModelExecutor,
    PdfModelUnavailable,
    run_pdf_workers,
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
        rendering = asyncio.create_task(
            asyncio.to_thread(render_pdf_region, file_path, page, [0, 0, 1, 1])
        )
        try:
            image = await asyncio.shield(rendering)
        except asyncio.CancelledError:
            # 线程中的PDFium不能被取消，关闭临时PDF前等待已启动的渲染释放句柄。
            await asyncio.gather(rendering, return_exceptions=True)
            raise
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
                stops = [
                    str(value).lower().rsplit(".", 1)[-1]
                    for value in outcome["stop_reasons"].values()
                    if value is not None
                ]
                if any(
                    value in {
                        "length",
                        "max_tokens",
                        "max_output_tokens",
                        "max_completion_tokens",
                        "token_limit",
                        "model_length",
                    }
                    for value in stops
                ):
                    raise InvalidRecognitionResponse("response_truncated")
                page_result = normalize_recognition_page(response.text, page)
                if control:
                    await control.check_cancelled()
                outcome["status"] = "completed"
                record.update(
                    status="blank" if page_result.metadata["blank"] else "completed",
                    blank=page_result.metadata["blank"],
                )
                if not stops:
                    record["warnings"].append("stop_reason_not_reported")
                if page_result.diagnostics:
                    record["warnings"].append("unclear_regions")
                # 等上一条消息落库后再更新页数，避免独立会话乱序提交。
                async with progress_lock:
                    results[page] = page_result
                    if control and control.context is not None:
                        await control.context.set_message(f"PDF 模型识别 {len(results)}/{total} 页（当前第 {page} 页）")
                logger.info(
                    f"{prefix} page={page} status={record['status']} request_id={request_id}"
                    f" attempt={attempt} elapsed_s={time.monotonic() - started:.2f}"
                )
                return
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

    await run_pdf_workers(range(1, total + 1), recognize, executor.concurrency)
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
    for page in range(1, total + 1):
        current = results[page]
        for block in current.blocks:
            block["order"] = len(result.blocks)
            result.blocks.append(block)
        result.figures.extend(current.figures)
        result.diagnostics.extend(current.diagnostics)
        result.metadata["parsed_pages"].append(page)
    if result.diagnostics:
        result.metadata["review_status"] = "needs_review"
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
