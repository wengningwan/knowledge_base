"""来源回填的身份、正文及容量负控。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import yuxi.knowledge.implementations.milvus as module
from yuxi.knowledge.implementations.milvus import MILVUS_CONTENT_MAX_BYTES, MilvusKB


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", [None, "kb", "file", "body", "index", "missing"])
async def test_source_hydration_rejects_stale_or_cross_identity_hits(monkeypatch, mismatch):
    """向量命中不能借用另一版本或另一身份的 PG 来源。"""
    source = {"source_sha256": "a" * 64, "source_spans": [{"line_start": 3, "line_end": 4}]}
    record = SimpleNamespace(
        chunk_id="c", kb_id="kb", file_id="file", chunk_index=0, content="new body", source_metadata=source
    )
    if mismatch == "kb":
        record.kb_id = "other-kb"
    elif mismatch == "file":
        record.file_id = "other-file"
    elif mismatch == "body":
        record.content = "old body"
    elif mismatch == "index":
        record.chunk_index = 1
    monkeypatch.setattr(
        module,
        "KnowledgeFileRepository",
        lambda: SimpleNamespace(
            get_chunk_sources_by_file_ids=AsyncMock(return_value={"file": {"source": "paper.md"}}),
        ),
    )
    monkeypatch.setattr(
        module,
        "KnowledgeChunkRepository",
        lambda: SimpleNamespace(
            list_by_chunk_ids=AsyncMock(return_value=[] if mismatch == "missing" else [record]),
        ),
    )
    kb = MilvusKB.__new__(MilvusKB)
    result = await kb._hydrate_chunk_sources(
        "kb",
        [
            {
                "content": "new body",
                "metadata": {
                    "chunk_id": "c",
                    "file_id": "file",
                    "chunk_index": 0,
                },
            }
        ],
    )
    if mismatch is None:
        assert result[0]["metadata"]["source_metadata"] == source
    else:
        assert result == []


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["current", "new_parse", "parsing", "unbound"])
async def test_paper_hydration_requires_same_published_markdown(monkeypatch, state):
    """旧块即使正文相同，也不能在新解析发布后借用当前文件身份。"""
    source = {"chunker_version": "paper_rules_v3", "source_sha256": "digest", "_publication_id": "current"}
    if state == "unbound":
        source.pop("_publication_id")
    record = SimpleNamespace(
        chunk_id="c", kb_id="kb", file_id="file", chunk_index=0, content="body", source_metadata=source
    )
    publication = {
        "source": "paper.md",
        "_publication_id": "new" if state == "new_parse" else "current",
        "_file_status": "parsing" if state == "parsing" else "indexed",
    }
    monkeypatch.setattr(
        module,
        "KnowledgeFileRepository",
        lambda: SimpleNamespace(get_chunk_sources_by_file_ids=AsyncMock(return_value={"file": publication})),
    )
    monkeypatch.setattr(
        module, "KnowledgeChunkRepository", lambda: SimpleNamespace(list_by_chunk_ids=AsyncMock(return_value=[record]))
    )
    result = await MilvusKB.__new__(MilvusKB)._hydrate_chunk_sources(
        "kb", [{"content": "body", "metadata": {"chunk_id": "c", "file_id": "file", "chunk_index": 0}}]
    )
    if state == "current":
        assert len(result) == 1 and "_publication_id" not in str(result)
    else:
        assert result == []


@pytest.mark.asyncio
async def test_over_capacity_evidence_is_rejected_before_pg_or_vector_writes(monkeypatch):
    """UTF-8 字节容量超限显式失败，不截断完整公式。"""
    repo = SimpleNamespace(batch_upsert=AsyncMock())
    monkeypatch.setattr(module, "KnowledgeChunkRepository", lambda: repo)
    collection = SimpleNamespace(upsert=AsyncMock())
    content = "$$\n" + "中" * (MILVUS_CONTENT_MAX_BYTES // 3 + 1) + "\n$$"
    chunk = {"chunk_id": "formula", "content": content}
    with pytest.raises(ValueError, match="UTF-8 字节"):
        await MilvusKB.__new__(MilvusKB)._insert_chunks_to_stores("kb", "file", collection, [chunk], [])
    assert chunk["content"] == content
    repo.batch_upsert.assert_not_awaited()
    collection.upsert.assert_not_called()


@pytest.mark.asyncio
async def test_preview_returns_saved_sources_and_current_markdown_digest(monkeypatch):
    """预览分别返回块来源版本与当前原文版本，允许识别失效来源。"""
    metadata = {"source_sha256": "old"}
    record = SimpleNamespace(
        chunk_id="c",
        chunk_index=0,
        content="body",
        source_metadata=metadata,
        start_char_pos=None,
        end_char_pos=None,
        start_token_pos=None,
        end_token_pos=None,
        graph_indexed=False,
        ent_ids=None,
        tags=None,
        extraction_result=None,
    )
    monkeypatch.setattr(
        module,
        "KnowledgeChunkRepository",
        lambda: SimpleNamespace(
            list_by_file_id=AsyncMock(return_value=[record]),
        ),
    )
    kb = MilvusKB.__new__(MilvusKB)
    kb._read_markdown_from_minio = AsyncMock(return_value="# Current\n\nNew body")
    response = await kb._get_file_content_from_meta("file", {"markdown_file": "current.md"})
    assert response["lines"][0]["source_metadata"] == metadata
    assert response["source_sha256"] == module.source_sha256(response["content"])
    assert response["source_sha256"] != metadata["source_sha256"]


def test_comparison_connection_never_creates_or_switches_database(monkeypatch):
    """对照只连接指定已有数据库，不进入默认创建及静默回退。"""
    connect = Mock()
    writes = SimpleNamespace(create_database=Mock(), using_database=Mock(), list_database=Mock())
    monkeypatch.setattr(module, "connections", SimpleNamespace(connect=connect))
    monkeypatch.setattr(module, "db", writes)
    kb = MilvusKB.__new__(MilvusKB)
    kb.require_existing_index = True
    kb.connection_alias, kb.milvus_uri, kb.milvus_token, kb.milvus_db = (
        "comparison",
        "http://milvus:19530",
        "",
        "existing",
    )
    kb._init_connection()
    assert connect.call_args.kwargs == {
        "alias": "comparison",
        "uri": "http://milvus:19530",
        "token": "",
        "db_name": "existing",
    }
    assert not any((writes.create_database.called, writes.using_database.called, writes.list_database.called))
    connect.side_effect = RuntimeError("database does not exist")
    with pytest.raises(RuntimeError, match="database does not exist"):
        kb._init_connection()


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", [None, "missing", "model", "dimension", "schema"])
async def test_comparison_only_loads_existing_compatible_collection(monkeypatch, mismatch):
    """索引缺失或不兼容时拒绝对照，不创建、删除或重建集合。"""
    info = SimpleNamespace(model_id="embedding", model_type="embedding", dimension=12)
    monkeypatch.setattr(module.model_cache, "get_model_info", lambda _spec: info)
    collection = SimpleNamespace(
        description="Knowledge base collection for kb using embedding",
        schema=SimpleNamespace(
            fields=[
                SimpleNamespace(name="embedding", dtype=module.DataType.FLOAT_VECTOR, params={"dim": 12}),
            ]
        ),
        load=Mock(),
    )
    if mismatch == "model":
        collection.description = "different model"
    elif mismatch == "dimension":
        collection.schema.fields[0].params["dim"] = 24
    kb = MilvusKB.__new__(MilvusKB)
    kb.require_existing_index = True
    kb._get_existing_milvus_collection = AsyncMock(return_value=None if mismatch == "missing" else collection)
    kb._get_or_create_milvus_collection = AsyncMock(side_effect=AssertionError("must not create"))
    kb._collection_supports_bm25 = lambda _collection: mismatch != "schema"
    if mismatch is None:
        result = await kb._load_query_collection("kb", "provider:embedding")
        assert result is collection
        collection.load.assert_called_once()
    else:
        with pytest.raises(ValueError, match="不存在|不兼容"):
            await kb._load_query_collection("kb", "provider:embedding")
        collection.load.assert_not_called()
    kb._get_or_create_milvus_collection.assert_not_awaited()
