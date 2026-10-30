"""图片任务的对象归属、侧录状态和正文隔离边界。"""

import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from test.unit.services.pdf_model_fakes import configure_pdf_budget
from yuxi.knowledge.parser.pdf_structure import finalize_document
from yuxi.knowledge.parser.result import ParsedDocument
from yuxi.services import knowledge_figure_analysis_service as tasks
from yuxi.services import pdf_figure_analysis_service as figures


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
    redis, _ = configure_pdf_budget(monkeypatch)
    monkeypatch.setattr("yuxi.models.chat.select_model", lambda *args, **kwargs: object())

    async def model(*args):
        return description

    monkeypatch.setattr(figures, "call_visual_text", model)
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
    redis, _ = configure_pdf_budget(monkeypatch)
    monkeypatch.setattr("yuxi.models.chat.select_model", lambda *args, **kwargs: object())

    async def model(*args):
        return "MODEL_EXPLANATION_MUST_STAY_IN_SIDECAR"

    monkeypatch.setattr(figures, "call_visual_text", model)
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
async def test_commit_ack_failure_cleans_only_unreferenced_uploaded_sidecar(monkeypatch, committed, failure):
    """提交成功后 close/取消抛错也保留已引用侧录，未提交对象才可删除。"""
    payload = {"kb_id": "kb", "file_id": "file", "attempt_id": "attempt", "markdown_file": "document.md", "assets_file": "old.json", "model_spec": "test:model"}
    record = SimpleNamespace(kb_id="kb", markdown_file="document.md", parse_assets_file="old.json")
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
    assert record.markdown_file == "document.md"
    assert record.parse_assets_file == ("uploaded.json" if committed else "old.json")
    assert deleted == ([] if committed else [(uploaded[0][0], uploaded[0][1])])
    assert uploaded[0][1].startswith("kb/parsed/file/attempt/assets.figures.figure-task.")
    saved = json.loads(uploaded[0][2])
    assert saved["blocks"] == [{"id": "b1", "content": "source"}]
    assert saved["reviews"] == [{"id": "r1"}]
