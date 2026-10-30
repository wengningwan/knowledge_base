"""复核技术失败保留已生成正文，但不得自动入库或宣称任务成功。"""

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
    with pytest.raises(RuntimeError, match="失败 1 个"):
        await service.run_knowledge_ingest(context)
    summary = context.set_result.await_args.args[0]
    assert summary["failed"] == 1
    assert summary["items"] == [incomplete, {"file_id": "b", "status": "indexed"}]
    assert backend.index_file.await_count == 1
    assert backend.index_file.await_args.args == ("kb", "b")
    assert incomplete["markdown_file"] == "a.md"


@pytest.mark.asyncio
async def test_explicit_parse_exposes_incomplete_in_task_summary(monkeypatch):
    """文件主解析成功也必须让任务明确表达复核未完成。"""
    context = task_context({"kb_id": "kb", "operator_id": 1, "file_ids": ["a"]})
    incomplete = {"file_id": "a", "status": "parsed", "review_status": "incomplete"}
    monkeypatch.setattr(service, "knowledge_base", SimpleNamespace(parse_file=AsyncMock(return_value=incomplete)))
    with pytest.raises(RuntimeError, match="复核尚未完成"):
        await service.run_knowledge_parse(context)
    assert context.set_result.await_args.args[0] == {"items": [incomplete], "processed": 1, "failed": 1}


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
    with pytest.raises(RuntimeError, match="部分模型复核未完成"):
        await service.run_knowledge_parse(context)
    summary = context.set_result.await_args.args[0]
    assert summary["processed"] == 2
    assert summary["failed"] == 1
    assert summary["result_truncated"] is True
    assert summary["items"][0]["file_id"] == "a"
