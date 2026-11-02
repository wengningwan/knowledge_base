"""请求等待前可观察，结束后有真实 token，日志不包含论文或供应商错误正文。"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from test.unit.services.pdf_review_fakes import configure_review_case, correct_numeric_difference
from yuxi.services import pdf_figure_analysis_service as figures
from yuxi.services import pdf_model_execution as execution
from yuxi.services import pdf_review_service as review


def configure_case(monkeypatch, tmp_path, stage, call):
    """只替换外部 PDF/SDK，并收集实际业务及执行器日志。"""
    result, path, _, redis = configure_review_case(monkeypatch, tmp_path)
    result.markdown = "PRIVATE SOURCE 12 mg"
    logs = []
    logger = SimpleNamespace(info=logs.append, warning=logs.append)
    for module in (figures, review, execution):
        monkeypatch.setattr(module, "logger", logger)

    def select_model(*args, **kwargs):
        """SDK 不设置请求时限，并关闭自动重试。"""
        assert kwargs == {"timeout": None, "max_retries": 0, "safe_error_logging": True}
        return SimpleNamespace(call=call)

    monkeypatch.setattr("yuxi.models.chat.select_model", select_model)
    if stage == "figures":
        result.figures = [{"id": "f1", "page": 1, "asset_id": "a"}]
        result.assets = {"a": {"data": b"image", "mime": "image/png"}}
    return result, path, logs, redis


async def run_case(stage, result, path, control):
    """通过公开业务入口执行候选筛选、准入和请求。"""
    if stage == "review":
        await review.review_pdf_document(result, path, {"document_review_model": "test:model"}, control)
    else:
        await figures.analyze_document_figures(result, {"image_analysis_model": "test:model"}, control)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["review", "figures"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_request_logged_before_wait_and_drained_before_finish_or_cancel(monkeypatch, tmp_path, stage, cancel):
    """事件证明挂起时已经有请求开始日志；取消不会伪报业务完成。"""
    started, release, drained = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def call(messages, stream=False):
        """响应携带真实报告字段，取消清理也可观察。"""
        started.set()
        try:
            await release.wait()
            prompt = messages[0].content[0]["text"]
            content = json.dumps(correct_numeric_difference(json.loads(prompt.rsplit("\n", 1)[1]))) if stage == "review" else "PRIVATE MODEL OUTPUT"
            return SimpleNamespace(content=content, usage_metadata={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10})
        finally:
            drained.set()

    async def check():
        """保持现有取消检查入口可用。"""

    result, path, logs, redis = configure_case(monkeypatch, tmp_path, stage, call)
    control = SimpleNamespace(kb_id="kb", task_id="task123", file_id="file123", check_cancelled=check)
    task = asyncio.create_task(run_case(stage, result, path, control))
    progress_marker = "校对进度 processed=1/1" if stage == "review" else "插图解析进度 processed=1/1"
    end_marker = "正文检查结束" if stage == "review" else "插图解析阶段结束"
    try:
        await asyncio.wait_for(started.wait(), 3)
        assert any("request_start" in line and "task_id=task123" in line and "file_id=file123" in line for line in logs)
        assert not any(progress_marker in line or end_marker in line for line in logs)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not any(progress_marker in line or end_marker in line for line in logs)
            assert any("request_failed" in line and "CancelledError" in line for line in logs)
        else:
            release.set()
            await asyncio.wait_for(task, 3)
            assert any(progress_marker in line for line in logs)
            assert any(end_marker in line for line in logs)
            assert any("response_received" in line and '"total_tokens": 10' in line for line in logs)
        assert drained.is_set()
        assert not any(redis.slots.values())
        assert "PRIVATE" not in "\n".join(logs)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["review", "figures"])
async def test_remote_failure_has_no_retry_and_logs_no_exception_credentials(monkeypatch, tmp_path, stage):
    """供应商错误不自动重发，异常类型可查但正文和凭据不落日志。"""
    attempts = []

    async def call(*args, **kwargs):
        """模拟远端错误而非本地模型截止时间。"""
        attempts.append(True)
        raise ConnectionError("PRIVATE_TOKEN_IN_EXCEPTION")

    result, path, logs, redis = configure_case(monkeypatch, tmp_path, stage, call)
    await run_case(stage, result, path, None)
    assert attempts == [True]
    assert any("request_failed" in line and "error=ConnectionError" in line for line in logs)
    assert "PRIVATE" not in "\n".join(logs)
    assert redis.cache_writes == []
    assert result.blocks[0]["content"] == "The sample was 12 mg."
