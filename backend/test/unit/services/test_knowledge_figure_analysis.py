"""图片任务的对象归属、侧录状态和正文隔离边界。"""

import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from test.unit.services.pdf_model_fakes import configure_pdf_execution
from yuxi.knowledge.parser.pdf_structure import finalize_document
from yuxi.knowledge.parser.result import ParsedDocument
from yuxi.services import knowledge_figure_analysis_service as tasks
from yuxi.services import pdf_figure_analysis_service as figures


@pytest.mark.asyncio
async def test_complete_figure_queue_and_second_pass_cache_have_separate_token_accounting(monkeypatch):
    """九张图都完成；重新读取成功缓存没有重复请求或 token 消耗。"""
    configure_pdf_execution(monkeypatch, limit=3)
    calls = []

    async def call(messages, stream=False):
        """供应商边界返回报告用量，执行与缓存走真实链路。"""
        calls.append(messages)
        return SimpleNamespace(content="Visible axes and legend.", usage_metadata={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14})

    monkeypatch.setattr("yuxi.models.chat.select_model", lambda *args, **kwargs: SimpleNamespace(call=call))

    async def check():
        """保持原有任务取消接口。"""

    control = SimpleNamespace(kb_id="kb", file_id="file", task_id="task", check_cancelled=check)
    assets = {f"a{index}": {"data": f"unique image {index}".encode()} for index in range(9)}
    source_figures = [{"id": f"f{index}", "asset_id": f"a{index}", "caption": "Original caption"} for index in range(9)]
    first = ParsedDocument(markdown="Original body.", figures=deepcopy(source_figures), assets=deepcopy(assets))
    await figures.analyze_document_figures(first, {"image_analysis_model": "p:model"}, control)
    assert len(calls) == 9
    assert all(figure["analysis_status"] == "completed" for figure in first.figures)
    assert first.markdown == "Original body."
    assert first.assets == assets
    execution = first.metadata["figure_analysis"]["execution"]
    assert execution["calls"] == execution["vision_calls"] == 9
    assert execution["routes"]["figures"]["known_tokens"]["total_tokens"] == 126

    second = ParsedDocument(markdown="Original body.", figures=deepcopy(source_figures), assets=deepcopy(assets))
    await figures.analyze_document_figures(second, {"image_analysis_model": "p:model"}, control)
    assert len(calls) == 9
    assert all(figure["analysis_status"] == "completed" for figure in second.figures)
    second_execution = second.metadata["figure_analysis"]["execution"]
    assert second_execution["cache_hits"] == 9
    assert second_execution["calls"] == 0
    assert second_execution["known_tokens"]["total_tokens"] == 0


@pytest.mark.parametrize("url", [
    "https://evil.invalid/api/knowledge/databases/kb/images/kb-images/file/attempt/a.png",
    "//evil.invalid/api/knowledge/databases/kb/images/kb-images/file/attempt/a.png",
    "/api/knowledge/databases/other/images/kb-images/file/attempt/a.png",
    "/api/knowledge/databases/kb/images/kb-images/other/attempt/a.png",
    "/api/knowledge/databases/kb/images/kb-images/file/old-attempt/a.png",
    "/api/knowledge/databases/kb/images/kb-images/file/attempt/../secret.png",
    "/api/knowledge/databases/kb/images/kb-images/file/attempt/%2e%2e/secret.png",
    "/api/knowledge/databases/kb/images/kb-images/file/attempt/%5csecret.png",
    "/api/knowledge/databases/kb/images/kb-images/file/attempt/a.png?token=bad",
])
def test_image_proxy_rejects_external_cross_file_and_traversal_urls(url):
    """独立图片任务只可读取当前文件当前解析尝试的原图。"""
    with pytest.raises(ValueError):
        tasks.figure_image_object(url, kb_id="kb", file_id="file", attempt_id="attempt")


def test_image_proxy_accepts_exact_current_attempt():
    """合法鉴权代理 URL 还原为当前知识库下的对象名。"""
    assert tasks.figure_image_object(
        "/api/knowledge/databases/kb/images/kb-images/file/attempt/a.png",
        kb_id="kb", file_id="file", attempt_id="attempt",
    ) == "kb/kb-images/file/attempt/a.png"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "cancelled", "success"])
async def test_terminal_task_projection_only_updates_figure_sidecar(monkeypatch, status):
    """任务失败或取消会解除等待，正文块和审阅记录保持原值。"""
    data = {
        "attempt_id": "attempt", "markdown": "source text", "blocks": [{"id": "b1", "content": "source text"}],
        "reviews": [{"id": "r1", "status": "unresolved"}],
        "figures": [{"id": "f1", "analysis_status": "pending"}],
        "metadata": {"figure_analysis": {"status": "pending", "task_id": "figure-task"}},
    }
    before = deepcopy(data)
    requested = []

    async def get(self, task_id):
        requested.append(task_id)
        return SimpleNamespace(type=tasks.TASK_TYPE, status=status, payload={"kb_id": "kb", "file_id": "file", "attempt_id": "attempt"})

    monkeypatch.setattr(tasks.TaskRepository, "get_by_id", get)
    await tasks.project_figure_task_status(data, kb_id="kb", file_id="file")
    assert requested == ["figure-task"]
    assert data["metadata"]["figure_analysis"]["status"] == ("needs_review" if status == "success" else status)
    assert data["figures"][0]["analysis_status"] == "not_checked"
    for key in ("markdown", "blocks", "reviews"):
        assert data[key] == before[key]


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("kb_id", "other"), ("file_id", "other"), ("attempt_id", "old"), ("type", "knowledge_parse")])
async def test_projection_rejects_task_from_another_document_version(monkeypatch, field, value):
    """引用存在的任务 ID 也不能把另一文件或旧尝试状态投影进来。"""
    data = {"attempt_id": "attempt", "figures": [{"id": "f1", "analysis_status": "pending"}], "metadata": {"figure_analysis": {"task_id": "task", "status": "pending"}}}
    before = deepcopy(data)

    async def get(self, task_id):
        return SimpleNamespace(type=value if field == "type" else tasks.TASK_TYPE, status="failed", payload={"kb_id": "kb", "file_id": "file", "attempt_id": "attempt", field: value})

    monkeypatch.setattr(tasks.TaskRepository, "get_by_id", get)
    with pytest.raises(ValueError, match="文档版本不匹配"):
        await tasks.project_figure_task_status(data, kb_id="kb", file_id="file")
    assert data == before


@pytest.mark.asyncio
@pytest.mark.parametrize("description", ["", "bad\ufffdglyph"])
async def test_invalid_model_description_is_failed_and_never_cached(monkeypatch, description):
    """无效解释只进入失败状态，不进入成功缓存和正文。"""
    redis, _ = configure_pdf_execution(monkeypatch)

    async def model(*args, **kwargs):
        return SimpleNamespace(content=description)

    monkeypatch.setattr("yuxi.models.chat.select_model", lambda *args, **kwargs: SimpleNamespace(call=model))
    result = ParsedDocument(markdown="source", figures=[{"id": "f1", "asset_id": "image"}], assets={"image": {"data": b"image"}})
    control = SimpleNamespace(kb_id="kb", file_id="file", task_id="task", filename="paper.pdf", remaining_seconds=lambda: 360)

    async def check():
        return None

    control.check_cancelled = check
    await figures.analyze_document_figures(result, {"image_analysis_model": "test:model"}, control)
    assert result.figures[0]["analysis_status"] == "failed"
    assert redis.cache_writes == []
    assert result.markdown == "source"


@pytest.mark.asyncio
async def test_model_figure_explanation_is_sidecar_only_even_after_final_render(monkeypatch):
    """模型解释可侧录下载，但再次渲染后的 Markdown 只包含原文图注。"""
    redis, _ = configure_pdf_execution(monkeypatch)

    async def model(*args, **kwargs):
        return SimpleNamespace(content="MODEL_EXPLANATION_MUST_STAY_IN_SIDECAR")

    monkeypatch.setattr("yuxi.models.chat.select_model", lambda *args, **kwargs: SimpleNamespace(call=model))
    result = ParsedDocument(
        blocks=[{"id": "b1", "type": "text", "content": "Author paragraph."}, {"id": "b2", "type": "picture", "content": "![Original caption](asset://image)"}],
        figures=[{"id": "f1", "asset_id": "image", "caption": "Original caption"}], assets={"image": {"data": b"image"}},
    )
    await figures.analyze_document_figures(result, {"image_analysis_model": "test:model"})
    finalize_document(result, {"image": "/api/knowledge/image.png"})
    assert result.figures[0]["analysis_status"] == "completed"
    assert result.figures[0]["analysis"] == "MODEL_EXPLANATION_MUST_STAY_IN_SIDECAR"
    assert result.markdown == "Author paragraph.\n\n![Original caption](/api/knowledge/image.png)"
    assert "MODEL_EXPLANATION" not in result.markdown


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("human_changed", [False, True])
async def test_commit_ack_failure_cleans_only_unreferenced_uploaded_sidecar(
    monkeypatch, committed, failure, human_changed,
):
    """提交成功后 close/取消抛错也保留已引用侧录，未提交对象才可删除。"""
    payload = {"kb_id": "kb", "file_id": "file", "attempt_id": "attempt", "markdown_file": "document.md", "assets_file": "old.json", "model_spec": "test:model"}
    old_pointer = "kb/parsed/file/attempt/old.json"
    record = SimpleNamespace(kb_id="kb", markdown_file="document.md", parse_assets_file=old_pointer)
    sidecar = {"schema_version": 1, "kb_id": "kb", "file_id": "file", "attempt_id": "attempt", "blocks": [{"id": "b1", "content": "source"}], "reviews": [{"id": "r1"}], "figures": [], "metadata": {"figure_analysis": {"task_id": "figure-task", "status": "pending"}}}
    deleted, uploaded = [], []

    class Repository:
        """模拟提交后的事实可重读，独立于调用栈是否收到成功确认。"""

        async def get_by_file_id(self, file_id):
            return record

        @asynccontextmanager
        async def lock_file_operation(self, *args):
            yield

        async def publish_parse_assets_in_session(self, session, **values):
            if committed:
                record.parse_assets_file = values["assets_file"]
            return committed

    class Client:
        """记录上传和精确对象删除，不执行任何存储 I/O。"""

        KB_BUCKETS = {"parsed": "parsed", "images": "images"}

        async def aupload_file(self, bucket, name, data):
            uploaded.append((bucket, name, data))
            return SimpleNamespace(url="uploaded.json")

        async def adelete_file(self, bucket, name):
            deleted.append((bucket, name))

    async def read_sidecar(*args):
        return deepcopy(sidecar)

    async def analyze(result, params, control):
        result.metadata["figure_analysis"]["status"] = "completed"
        if human_changed:
            record.markdown_file = "document.review.md"
            sidecar["blocks"][0]["content"] = "Human corrected source"
            sidecar["reviews"][0]["human_review"] = {"confirmed_by": "human"}
            sidecar["metadata"]["human_review_revision"] = "manual-revision"

    async def noop(*args):
        return None

    async def transaction(operation):
        await operation(None, SimpleNamespace(cancel_requested=0))
        raise failure("commit acknowledgment interrupted")

    client = Client()
    context = SimpleNamespace(payload=payload, task_id="figure-task", deadline=None, raise_if_cancelled=noop, set_progress=noop, run_owned_transaction=transaction, set_result=noop)
    monkeypatch.setattr(tasks, "KnowledgeFileRepository", Repository)
    monkeypatch.setattr(tasks, "_read_sidecar", read_sidecar)
    monkeypatch.setattr("yuxi.storage.minio.get_minio_client", lambda: client)
    monkeypatch.setattr(figures, "analyze_document_figures", analyze)
    with pytest.raises(failure):
        await tasks.run_knowledge_figure_analysis(context)
    assert len(uploaded) == 1
    assert record.markdown_file == ("document.review.md" if human_changed else "document.md")
    assert record.parse_assets_file == ("uploaded.json" if committed else old_pointer)
    assert deleted == ([] if committed else [(uploaded[0][0], uploaded[0][1])])
    assert uploaded[0][1].startswith("kb/parsed/file/attempt/assets.figures.figure-task.")
    saved = json.loads(uploaded[0][2])
    assert saved["blocks"] == sidecar["blocks"]
    assert saved["reviews"] == sidecar["reviews"]
    if human_changed:
        assert saved["metadata"]["human_review_revision"] == "manual-revision"


@pytest.mark.asyncio
async def test_figure_control_rejects_different_parse_attempt():
    """允许同次人工正文变更，重新解析后的旧图片任务必须停止。"""
    async def noop():
        """保持任务自身有效。"""

    context = SimpleNamespace(task_id="figures", deadline=None, raise_if_cancelled=noop, payload={
        "kb_id": "kb", "file_id": "file", "attempt_id": "old", "markdown_file": "old.md",
    })
    from unittest.mock import patch

    async def get(self, file_id):
        """返回已经切换的发布事实。"""
        return SimpleNamespace(kb_id="kb", parse_assets_file="kb/parsed/file/new/assets.json")

    with patch.object(tasks.KnowledgeFileRepository, "get_by_file_id", get):
        with pytest.raises(asyncio.CancelledError):
            await tasks.FigureTaskControl(context).check_cancelled()
