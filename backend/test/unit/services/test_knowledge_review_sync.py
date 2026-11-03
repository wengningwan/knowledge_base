"""人工确认后的正文、回执、版本拒绝与失败清理。"""

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from yuxi.knowledge.parser.pdf_structure import render_document
from yuxi.services import knowledge_review_service as reviews


@pytest.fixture
def document(monkeypatch):
    """外部存储替身保存真实字节与最终指针，不以调用次数作为结果。"""
    prefix = "kb/parsed/file/attempt/"
    url = "http://minio.test/parsed/"
    blocks = [
        {"id": "title", "type": "title", "content": "Example", "level": 1, "page": 1},
        {"id": "b1", "type": "text", "content": "The measured value is 10 A.", "page": 1},
    ]
    markdown = render_document(blocks)
    sidecar = {
        "schema_version": 1,
        "kb_id": "kb",
        "file_id": "file",
        "attempt_id": "attempt",
        "blocks": blocks,
        "figures": [{"analysis": "MODEL_ONLY"}],
        "diagnostics": [{"reason": "DIAGNOSTIC_ONLY"}],
        "metadata": {"auto_index_enabled": True, "review_status": "incomplete", "pdf_repair": {}},
        "reviews": [
            {
                "id": "r1",
                "block_id": "b1",
                "status": "unresolved",
                "candidates": {"native": "The measured value is 1.0 A."},
            },
            {"id": "r2", "block_id": "b1", "status": "technical_failed", "protocol_errors": ["old error"]},
            {"id": "r3", "block_id": None, "kind": "missing_region", "status": "unresolved", "page": 1},
        ],
    }
    objects = {prefix + "document.md": markdown.encode(), prefix + "assets.json": json.dumps(sidecar).encode()}
    record = SimpleNamespace(
        kb_id="kb",
        status="indexed",
        markdown_file=url + prefix + "document.md",
        parse_assets_file=url + prefix + "assets.json",
        chunk_count=1,
        token_count=10,
    )
    state = SimpleNamespace(
        record=record,
        objects=objects,
        chunks={"old"},
        vectors={"old"},
        upload_error=None,
        cleanup_error=False,
        cas=False,
        ack_error=False,
    )
    lock = asyncio.Lock()

    class Repository:
        """通过同文件锁和最终状态模拟 publication boundary。"""

        async def get_by_file_id(self, file_id):
            return record

        @asynccontextmanager
        async def lock_file_operation(self, *args):
            async with lock:
                yield

        async def publish_review(self, **values):
            if state.cas:
                return False
            assert record.markdown_file == values["previous_markdown"]
            assert record.parse_assets_file == values["previous_assets"]
            assert record.status in values["allowed_statuses"]
            assert values["markdown_file"].removeprefix(url) in objects
            assert values["assets_file"].removeprefix(url) in objects
            record.markdown_file = values["markdown_file"]
            record.parse_assets_file = values["assets_file"]
            if values["content_changed"]:
                record.status, record.chunk_count, record.token_count = "parsed", 0, 0
            if state.ack_error:
                raise RuntimeError("commit ACK lost")
            return True

    class Client:
        """保留旧版对象用于证明未原位覆写。"""

        KB_BUCKETS = {"parsed": "parsed"}

        async def adownload_file(self, bucket, name):
            return objects[name]

        async def aupload_file(self, bucket, name, data):
            if state.upload_error and name.endswith(state.upload_error):
                raise RuntimeError("upload failed")
            objects[name] = data
            return SimpleNamespace(url=url + name)

        async def adelete_file(self, bucket, name):
            objects.pop(name, None)

    async def cleanup(kb_id, file_id):
        """最终派生集合独立于回执声明。"""
        if state.cleanup_error:
            raise RuntimeError("index deletion failed")
        state.chunks.clear()
        state.vectors.clear()

    monkeypatch.setattr(reviews, "KnowledgeFileRepository", Repository)
    monkeypatch.setattr("yuxi.storage.minio.get_minio_client", lambda: Client())
    state.executor = SimpleNamespace(delete_file_chunks_only=cleanup)
    state.changes = dict(
        operator_id="human",
        attempt_id="attempt",
        review_revision=None,
        source_sha256=hashlib.sha256(markdown.encode()).hexdigest(),
        review_index=0,
        action="replace",
        content="The measured value is 1.0 A.",
        anchor_block_id=None,
    )
    state.original = deepcopy(objects)
    state.read = lambda: (
        objects[record.markdown_file.removeprefix(url)].decode(),
        json.loads(objects[record.parse_assets_file.removeprefix(url)]),
    )
    return state


@pytest.mark.asyncio
async def test_confirmation_publishes_corrected_md_and_all_same_block_receipts(document):
    """整段确认与真实正文同版，附属解释不混入 MD，旧索引消失。"""
    doc = document
    result = await reviews.confirm_document_review(doc.executor, "kb", "file", **doc.changes)
    markdown, sidecar = doc.read()
    assert markdown == "# Example\n\nThe measured value is 1.0 A."
    assert render_document(sidecar["blocks"]) == markdown
    assert sidecar["figures"] == [{"analysis": "MODEL_ONLY"}]
    assert "MODEL_ONLY" not in markdown and "DIAGNOSTIC_ONLY" not in markdown
    assert [bool(row.get("human_review")) for row in sidecar["reviews"]] == [True, True, False]
    assert sidecar["reviews"][1]["status"] == "technical_failed"
    assert sidecar["metadata"]["review_status"] == "needs_review"
    assert sidecar["metadata"]["human_review_revision"] == result["review_revision"]
    assert sidecar["reviews"][0]["human_review"]["confirmed_by"] == "human"
    assert doc.record.status == "parsed" and doc.record.chunk_count == 0
    assert doc.chunks == doc.vectors == set()
    assert all(doc.objects[name] == value for name, value in doc.original.items())
    assert result["content_changed"] and result["index_invalidated"] and result["confirmed_count"] == 2


@pytest.mark.asyncio
async def test_keep_and_second_confirmation_preserve_md_and_complete_review(document):
    """保留正文不清索引；最新版本可逐项确认，旧版本不可重复发布。"""
    doc = document
    first = await reviews.confirm_document_review(doc.executor, "kb", "file", **{**doc.changes, "action": "keep"})
    markdown_pointer = doc.record.markdown_file
    assert doc.chunks == doc.vectors == {"old"}
    assert doc.record.status == "indexed" and not first["content_changed"]
    second = await reviews.confirm_document_review(
        doc.executor,
        "kb",
        "file",
        **{
            **doc.changes,
            "action": "keep",
            "review_index": 2,
            "review_revision": first["review_revision"],
        },
    )
    assert doc.record.markdown_file == markdown_pointer
    assert doc.read()[1]["metadata"]["review_status"] == "completed"
    assert second["confirmed_count"] == 1
    before = deepcopy(doc.objects)
    with pytest.raises(reviews.ReviewConflictError):
        await reviews.confirm_document_review(doc.executor, "kb", "file", **doc.changes)
    assert doc.objects == before


@pytest.mark.asyncio
@pytest.mark.parametrize("anchor", ["__start__", "b1"])
async def test_missing_region_uses_explicit_human_insertion_position(document, anchor):
    """遗漏段落插在人工选择的位置，不通过正文字符串猜测。"""
    doc = document
    await reviews.confirm_document_review(
        doc.executor,
        "kb",
        "file",
        **{
            **doc.changes,
            "review_index": 2,
            "action": "insert_after",
            "anchor_block_id": anchor,
            "content": "Added paragraph.",
        },
    )
    markdown, sidecar = doc.read()
    if anchor == "__start__":
        assert markdown.startswith("Added paragraph.\n\n# Example")
    else:
        assert markdown.endswith("10 A.\n\nAdded paragraph.")
    assert render_document(sidecar["blocks"]) == markdown
    assert "human_review" in sidecar["reviews"][2]
    assert "human_review" not in sidecar["reviews"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value,error",
    [
        ("source_sha256", "0" * 64, "已变化"),
        ("review_revision", "stale", "已变化"),
        ("attempt_id", "old", "版本已变化"),
        ("attempt_id", "../attempt", "无效"),
        ("review_index", 999, "未找到"),
        ("action", "auto", "无效"),
    ],
)
async def test_invalid_snapshot_never_changes_published_objects(document, field, value, error):
    """版本、范围与协议拒绝前不得上传对象或清理旧索引。"""
    doc = document
    with pytest.raises(ValueError, match=error):
        await reviews.confirm_document_review(doc.executor, "kb", "file", **{**doc.changes, field: value})
    assert doc.objects == doc.original
    assert doc.chunks == doc.vectors == {"old"}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["scope", "status", "sidecar", "structure", "anchor", "replace_missing"])
async def test_wrong_scope_busy_and_ambiguous_structure_are_rejected(document, case):
    """跨库、处理中、外来侧录与缺失锚点均不能改写正文。"""
    doc = document
    changes = doc.changes.copy()
    if case == "scope":
        doc.record.kb_id = "other"
    elif case == "status":
        doc.record.status = "parsing"
    elif case in {"sidecar", "structure"}:
        name = "kb/parsed/file/attempt/assets.json"
        data = json.loads(doc.objects[name])
        if case == "sidecar":
            data["file_id"] = "foreign"
        else:
            data["blocks"][1]["content"] = "stale structure"
        doc.objects[name] = json.dumps(data).encode()
    elif case == "anchor":
        changes.update(review_index=2, action="insert_after", anchor_block_id="missing")
    else:
        changes["review_index"] = 2
    before = deepcopy(doc.objects)
    with pytest.raises(ValueError):
        await reviews.confirm_document_review(doc.executor, "kb", "file", **changes)
    assert doc.objects == before
    assert doc.chunks == doc.vectors == {"old"}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["md", "json", "cleanup", "cas", "ack"])
async def test_failed_save_only_retains_committed_objects(document, failure):
    """上传、清索引或 CAS 失败保留旧指针；commit ACK 丢失不得删除已发布字节。"""
    doc = document
    original_pointers = (doc.record.markdown_file, doc.record.parse_assets_file)
    doc.upload_error = failure if failure in {"md", "json"} else None
    doc.cleanup_error, doc.cas, doc.ack_error = failure == "cleanup", failure == "cas", failure == "ack"
    with pytest.raises((RuntimeError, reviews.ReviewConflictError)):
        await reviews.confirm_document_review(doc.executor, "kb", "file", **doc.changes)
    if failure == "ack":
        assert "1.0 A." in doc.read()[0]
        assert doc.read()[1]["reviews"][0]["human_review"]
    else:
        assert (doc.record.markdown_file, doc.record.parse_assets_file) == original_pointers
        assert doc.objects == doc.original


@pytest.mark.asyncio
async def test_concurrent_confirmations_reject_second_stale_request(document):
    """同文件两次保存只有首个版本可发布。"""
    doc = document
    results = await asyncio.gather(
        *(reviews.confirm_document_review(doc.executor, "kb", "file", **doc.changes) for _ in range(2)),
        return_exceptions=True,
    )
    assert sum(isinstance(value, dict) for value in results) == 1
    assert sum(isinstance(value, reviews.ReviewConflictError) for value in results) == 1
    assert "1.0 A." in doc.read()[0]


@pytest.mark.asyncio
async def test_inserted_paragraph_can_be_edited_without_adding_duplicate(document):
    """补入回执绑定新块身份，再次核对修改同一块。"""
    doc = document
    first = await reviews.confirm_document_review(
        doc.executor,
        "kb",
        "file",
        **{
            **doc.changes,
            "review_index": 2,
            "action": "insert_after",
            "anchor_block_id": "b1",
            "content": "Added paragraph.",
        },
    )
    markdown, sidecar = doc.read()
    inserted_id = sidecar["reviews"][2]["human_review"]["block_id"]
    await reviews.confirm_document_review(
        doc.executor,
        "kb",
        "file",
        **{
            **doc.changes,
            "review_index": 2,
            "review_revision": first["review_revision"],
            "source_sha256": hashlib.sha256(markdown.encode()).hexdigest(),
            "content": "Corrected addition.",
        },
    )
    markdown, sidecar = doc.read()
    assert markdown.endswith("10 A.\n\nCorrected addition.")
    assert len(sidecar["blocks"]) == 3
    assert sidecar["blocks"][-1]["id"] == inserted_id
    assert "Added paragraph." not in markdown


@pytest.mark.asyncio
@pytest.mark.parametrize("content,changed", [("", True), ("The measured value is 10 A.", False)])
async def test_empty_replacement_is_deletion_and_unchanged_confirmation_keeps_indexes(document, content, changed):
    """明确空串删除正文；确认原段落不清理已有检索数据。"""
    doc = document
    result = await reviews.confirm_document_review(doc.executor, "kb", "file", **{**doc.changes, "content": content})
    assert result["content_changed"] is changed
    assert doc.read()[0] == ("# Example" if changed else "# Example\n\nThe measured value is 10 A.")
    assert doc.chunks == (set() if changed else {"old"})
