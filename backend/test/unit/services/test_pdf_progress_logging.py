"""PDF 模型等待前有日志，超时单次调用且不泄露正文或凭据。"""

import asyncio
from types import SimpleNamespace

import pytest

from test.unit.services.pdf_model_fakes import configure_pdf_budget
from yuxi.knowledge.parser.result import ParsedDocument
from yuxi.services import pdf_figure_analysis_service as figures
from yuxi.services import pdf_review_service as review


def configure_case(monkeypatch, stage, call):
    """仅替换外部 PDF、模型和 Redis，保留真实规则、预算和验证链路。"""
    configure_pdf_budget(monkeypatch)
    logs = []
    logger = SimpleNamespace(info=logs.append, warning=logs.append)
    monkeypatch.setattr(figures, "logger", logger)
    monkeypatch.setattr(review, "logger", logger)
    def select_model(*args, **kwargs):
        """适配器自身也关闭自动重试，避免服务层单次调用仍重复发请求。"""
        assert kwargs == {"timeout": 90, "max_retries": 0, "safe_error_logging": True}
        return object()

    monkeypatch.setattr("yuxi.models.chat.select_model", select_model)
    monkeypatch.setattr(figures, "call_visual_text", call)
    block = {"id": "b1", "page": 1, "type": "text", "bbox": [.1, .2, .9, .4], "content": "PRIVATE SOURCE 12 mg"}
    result = ParsedDocument(markdown=block["content"], blocks=[block], metadata={"parsed_pages": [1]})
    if stage == "review":
        def native(path, *, page_info):
            """同区域数字差异走视觉路线，原生字符具有可检测质量。"""
            page_info["page_count"] = 1
            text = "PRIVATE SOURCE 13 mg"
            advance = .8 / len(text)
            return [{"id": "n1", "page": 1, "bbox": block["bbox"], "content": text, "chars": [
                {"text": char, "bbox": [.1 + index * advance, .2, .1 + (index + 1) * advance, .4], "baseline": (10., 10., 0.), "font_size": 10., "origin_normal": 10.}
                for index, char in enumerate(text)
            ]}]

        monkeypatch.setattr("yuxi.knowledge.parser.pdf_native.read_native_blocks", native)
        monkeypatch.setattr("yuxi.knowledge.parser.pdf_native.render_pdf_region", lambda *args: b"image")
    else:
        result.figures = [{"id": "f1", "page": 1, "asset_id": "a"}]
        result.assets = {"a": {"data": b"image", "mime": "image/png"}}
    return result, logs


async def run_case(stage, result, control):
    """从公开业务入口执行候选筛选与单次调用。"""
    if stage == "review":
        await review.review_pdf_document(result, "paper.pdf", {"document_review_model": "test:model"}, control)
    else:
        await figures.analyze_document_figures(result, {"image_analysis_model": "test:model"}, control)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["review", "figures"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_log_before_wait_and_after_finish_or_cancel(monkeypatch, stage, cancel):
    """请求挂起时已记录开始；取消传播且不伪报完成。"""
    started, release = asyncio.Event(), asyncio.Event()

    async def call(*args):
        started.set()
        await release.wait()
        return '{"edits": [], "unresolved": []}' if stage == "review" else "PRIVATE_MODEL_OUTPUT"

    async def check():
        return None

    result, logs = configure_case(monkeypatch, stage, call)
    control = SimpleNamespace(kb_id="kb", task_id="task123", file_id="file123", filename="LIT-00165.pdf", check_cancelled=check, remaining_seconds=lambda: 360)
    task = asyncio.create_task(run_case(stage, result, control))
    start_marker = "模型调用开始" if stage == "review" else "插图项开始"
    progress_marker = "completed_regions=1" if stage == "review" else "进度 1/1"
    end_marker = "正文检查结束" if stage == "review" else "插图解析阶段结束"
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert any(start_marker in line for line in logs)
        assert any("task_id=task123" in line and "LIT-00165.pdf" in line for line in logs)
        assert not any(progress_marker in line for line in logs)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert any("取消" in line for line in logs)
            assert not any(progress_marker in line or end_marker in line for line in logs)
        else:
            release.set()
            await asyncio.wait_for(task, 2)
            assert any(progress_marker in line and "elapsed_s=" in line for line in logs)
            assert any(end_marker in line for line in logs)
        assert result.markdown == "PRIVATE SOURCE 12 mg"
        assert "PRIVATE" not in "\n".join(logs)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["review", "figures"])
async def test_timeout_has_no_retry_and_logs_no_exception_credentials(monkeypatch, stage):
    """超时只发送一次，日志记录异常类型且不记录异常正文。"""
    attempts = []

    async def call(*args):
        attempts.append(True)
        raise TimeoutError("PRIVATE_TOKEN_IN_EXCEPTION")

    result, logs = configure_case(monkeypatch, stage, call)
    await run_case(stage, result, None)
    assert attempts == [True]
    assert any("error=TimeoutError" in line and "retry=False" in line for line in logs)
    progress_marker = "completed_regions=1" if stage == "review" else "进度 1/1"
    assert sum(progress_marker in line for line in logs) == 1
    assert "PRIVATE" not in "\n".join(logs)
    assert result.markdown == "PRIVATE SOURCE 12 mg"
