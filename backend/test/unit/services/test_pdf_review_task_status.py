"""主解析与模型复核分别对账，未完成复核仍保留且跳过自动入库。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from yuxi.services import knowledge_task_service as service


def task_context(payload):
    """提供只记录持久摘要的任务边界。"""
    return SimpleNamespace(payload=payload, task_id="task", worker_id="worker",
                           set_progress=AsyncMock(), set_result=AsyncMock(), raise_if_cancelled=AsyncMock())


@pytest.mark.asyncio
async def test_incomplete_review_skips_auto_index_but_other_file_still_indexes(monkeypatch):
    """已生成MD与复核完成分开，不让一个未完成文件阻塞独立成功文件。"""
    context = task_context({"kb_id": "kb", "operator_id": 1,
                            "items": [{"filename": "a.pdf"}, {"filename": "b.pdf"}],
                            "params": {"auto_index": True}})
    incomplete = {"file_id": "a", "status": "parsed", "review_status": "incomplete", "markdown_file": "a.md"}
    complete = {"file_id": "b", "status": "parsed", "review_status": "completed"}
    backend = SimpleNamespace(
        add_file_record=AsyncMock(side_effect=[{"file_id": "a"}, {"file_id": "b"}]),
        parse_file=AsyncMock(side_effect=[incomplete, complete]), update_file_params=AsyncMock(),
        index_file=AsyncMock(return_value={"file_id": "b", "status": "indexed"}),
    )
    monkeypatch.setattr(service, "knowledge_base", backend)
    monkeypatch.setattr(service, "params_for_uploaded_document", lambda item, params: params)
    returned = await service.run_knowledge_ingest(context)
    summary = context.set_result.await_args.args[0]
    assert summary["failed"] == 0
    assert summary["review_incomplete"] == 1
    assert returned == summary and summary["auto_index_skipped"] == 1
    assert "复核未完成 1 个" in context.set_progress.await_args.args[1]
    assert summary["items"] == [incomplete, {"file_id": "b", "status": "indexed"}]
    assert backend.index_file.await_count == 1
    assert backend.index_file.await_args.args == ("kb", "b")
    assert incomplete["markdown_file"] == "a.md"


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_status", ["needs_review", "incomplete"])
@pytest.mark.parametrize("auto_index", [True, False])
async def test_manual_or_unfinished_review_never_auto_indexes(monkeypatch, blocked_status, auto_index):
    """按每份文件的实际终态过滤，开关关闭时也不声称暂停了入库。"""
    context = task_context({"kb_id": "kb", "operator_id": 1,
                            "items": ["a.pdf", "b.pdf"], "params": {"auto_index": auto_index}})
    files = {
        "a": {"file_id": "a", "status": "parsed", "review_status": blocked_status, "markdown_file": "a.md"},
        "b": {"file_id": "b", "status": "parsed", "review_status": "completed"},
    }
    indexed = set()

    async def index_file(kb_id, file_id, **kwargs):
        """记录实际发生的索引状态，不能只依赖 mock 调用次数。"""
        indexed.add(file_id)
        files[file_id] = {**files[file_id], "status": "indexed"}
        return files[file_id]

    backend = SimpleNamespace(
        add_file_record=AsyncMock(side_effect=[{"file_id": "a"}, {"file_id": "b"}]),
        parse_file=AsyncMock(side_effect=[files["a"], files["b"]]),
        update_file_params=AsyncMock(), index_file=index_file,
    )
    monkeypatch.setattr(service, "knowledge_base", backend)
    monkeypatch.setattr(service, "params_for_uploaded_document", lambda item, params: params)
    summary = await service.run_knowledge_ingest(context)
    assert indexed == ({"b"} if auto_index else set())
    assert files["a"]["status"] == "parsed" and files["a"]["markdown_file"] == "a.md"
    assert summary["auto_index_skipped"] == int(auto_index)
    assert summary["review_needs_review"] == int(blocked_status == "needs_review")
    assert summary["review_incomplete"] == int(blocked_status == "incomplete")
    assert ("暂不进行自动入库操作" in context.set_progress.await_args.args[1]) is auto_index
    assert all(call.kwargs["auto_index_enabled"] is auto_index for call in backend.parse_file.await_args_list)


@pytest.mark.asyncio
async def test_explicit_parse_exposes_incomplete_in_task_summary(monkeypatch):
    """文件主解析成功也必须让任务明确表达复核未完成。"""
    context = task_context({"kb_id": "kb", "operator_id": 1, "file_ids": ["a"]})
    incomplete = {"file_id": "a", "status": "parsed", "review_status": "incomplete"}
    monkeypatch.setattr(service, "knowledge_base", SimpleNamespace(parse_file=AsyncMock(return_value=incomplete)))
    result = await service.run_knowledge_parse(context)
    assert result == context.set_result.await_args.args[0] == {"items": [incomplete], "processed": 1, "failed": 0, "review_incomplete": 1}
    assert "复核未完成 1 个" in context.set_progress.await_args.args[1]


@pytest.mark.asyncio
async def test_pending_parse_does_not_hide_incomplete_beyond_result_sample(monkeypatch):
    """摘要截断不应吞掉末尾文件的真实失败状态。"""
    context = task_context({"kb_id": "kb", "operator_id": 1, "scope": "pending", "statuses": ["uploaded"], "count": 2})
    monkeypatch.setattr(service, "DOCUMENT_ACTION_RESULT_ITEM_LIMIT", 1)
    backend = SimpleNamespace(
        list_document_file_ids_by_statuses=AsyncMock(side_effect=[["a", "b"], []]),
        parse_file=AsyncMock(side_effect=[{"file_id": "a", "status": "parsed", "review_status": "completed"},
                                         {"file_id": "b", "status": "parsed", "review_status": "incomplete"}]),
    )
    monkeypatch.setattr(service, "knowledge_base", backend)
    await service.run_knowledge_parse(context)
    summary = context.set_result.await_args.args[0]
    assert summary["processed"] == 2
    assert summary["failed"] == 0
    assert summary["review_incomplete"] == 1
    assert summary["result_truncated"] is True
    assert summary["items"][0]["file_id"] == "a"


@pytest.mark.asyncio
async def test_real_parse_failure_is_not_hidden_by_review_warning_handling(monkeypatch):
    """真正OCR或发布异常仍使上传任务失败，不能被警告语义吞掉。"""
    context = task_context({"kb_id": "kb", "operator_id": 1, "items": [{"filename": "a.pdf"}], "params": {"auto_index": True}})
    backend = SimpleNamespace(add_file_record=AsyncMock(return_value={"file_id": "a"}),
                              parse_file=AsyncMock(side_effect=RuntimeError("OCR publication failed")),
                              index_file=AsyncMock())
    monkeypatch.setattr(service, "knowledge_base", backend)
    monkeypatch.setattr(service, "params_for_uploaded_document", lambda item, params: params)
    messages = []
    sink_id = service.logger.add(lambda message: messages.append(message.record["message"]), level="ERROR")
    try:
        with pytest.raises(RuntimeError, match="失败 1 个"):
            await service.run_knowledge_ingest(context)
    finally:
        service.logger.remove(sink_id)
    summary = context.set_result.await_args.args[0]
    assert summary["failed"] == 1 and summary["review_incomplete"] == 0
    assert "OCR publication failed" in summary["items"][0]["error"]
    assert any(
        "a.pdf" in message and "file_id=a" in message and "OCR publication failed" in message
        for message in messages
    )
    backend.index_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_is_not_reported_as_a_completed_parse(monkeypatch):
    """任务取消继续传播，不因模型警告处理而变成成功。"""
    import asyncio

    context = task_context({"kb_id": "kb", "operator_id": 1, "file_ids": ["a"]})
    monkeypatch.setattr(service, "knowledge_base", SimpleNamespace(parse_file=AsyncMock(side_effect=asyncio.CancelledError())))
    with pytest.raises(asyncio.CancelledError):
        await service.run_knowledge_parse(context)
    context.set_result.assert_not_awaited()
