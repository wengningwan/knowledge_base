"""真实 PostgreSQL 证明论文补取的发布身份、范围和并发边界。"""

import asyncio
import copy
import os
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import delete, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from yuxi.knowledge.chunking.paper import chunk_paper
from yuxi.knowledge.chunking.source import source_sha256
from yuxi.knowledge.implementations.milvus import MilvusKB
from yuxi.knowledge.related_evidence import public_source_metadata
from yuxi.repositories import knowledge_chunk_repository as module
from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository
from yuxi.storage.postgres.models_knowledge import KnowledgeBase, KnowledgeChunk, KnowledgeFile

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(scope="session", autouse=True)
def ensure_live_api_schema():
    """本文件只使用独立 Schema。"""


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_knowledge_resources():
    """资源由局部 fixture 清理。"""
    yield


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_sandboxes():
    """本文件不创建沙盒。"""
    yield


@pytest.fixture
async def paper_store(monkeypatch):
    """建立含同名文件、同名章节和不同原文版本的独立 PG 事实。"""
    schema = f"pytest_paper_{uuid.uuid4().hex}"
    admin = create_async_engine(os.environ["POSTGRES_URL"])
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(os.environ["POSTGRES_URL"], connect_args={"server_settings": {"search_path": schema}})
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def context():
        """事务退出即释放论文发布锁。"""
        async with sessions.begin() as session:
            yield session

    monkeypatch.setattr(module.pg_manager, "get_async_session_context", context)
    document = (
        "# Study\n\n## Same\n\nAnchorunique refers to Figure 1 and Fig. S4."
        "\n\n$$\nx=a+b\n$$\n\n## Same\n\nWrong repeated section."
        "\n\n## Figures\n\n![plot](plot.png)\n\nFigure 1. Exact caption."
    )
    chunks = chunk_paper(document, "paper", "same.md", {"chunk_token_num": 1})
    for chunk in chunks:
        chunk["source_metadata"]["_publication_id"] = source_sha256("attempt-one/document.md")
    try:
        async with engine.begin() as connection:
            for table in (KnowledgeBase.__table__, KnowledgeFile.__table__, KnowledgeChunk.__table__):
                await connection.run_sync(table.create)
        async with sessions.begin() as session:
            session.add_all([KnowledgeBase(kb_id=kb, name=kb, kb_type="milvus") for kb in ("kb", "other-kb")])
            await session.flush()
            session.add_all(
                [
                    KnowledgeFile(
                        file_id=file,
                        kb_id=kb,
                        filename="same.md",
                        status="indexed",
                        markdown_file="attempt-one/document.md",
                    )
                    for file, kb in (("paper", "kb"), ("other-file", "kb"), ("foreign", "other-kb"))
                ]
            )
            await session.flush()
            for chunk in chunks:
                session.add(
                    KnowledgeChunk(
                        chunk_id=chunk["chunk_id"],
                        file_id="paper",
                        kb_id="kb",
                        chunk_index=chunk["chunk_index"],
                        content=chunk["content"],
                        source_metadata=chunk["source_metadata"],
                    )
                )
            for index, (file, kb, changes) in enumerate(
                (
                    ("other-file", "kb", {}),
                    ("foreign", "other-kb", {}),
                    ("paper", "kb", {"source_sha256": "old-version"}),
                ),
                start=10,
            ):
                session.add(
                    KnowledgeChunk(
                        chunk_id=f"wrong-{index}",
                        file_id=file,
                        kb_id=kb,
                        chunk_index=index,
                        content="WRONG SOURCE",
                        source_metadata={**chunks[0]["source_metadata"], **changes},
                    )
                )
        anchor = {
            "content": chunks[0]["content"],
            "metadata": {
                "chunk_id": chunks[0]["chunk_id"],
                "file_id": "paper",
                "chunk_index": 0,
                "source_metadata": public_source_metadata(chunks[0]["source_metadata"]),
            },
        }
        yield sessions, anchor, chunks
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def test_current_paper_supplements_exclude_other_section_file_kb_and_version(paper_store):
    """JSON 查询真实排除跨身份伪造块，保留同节公式与跨节明确图片。"""
    sessions, anchor, chunks = paper_store
    repository = module.KnowledgeChunkRepository()
    related, status = await repository.get_paper_related_evidence(kb_id="kb", anchor=anchor, limit=25)
    assert [record["metadata"]["chunk_id"] for record in related] == [chunks[1]["chunk_id"], chunks[3]["chunk_id"]]
    assert [record["evidence_role"] for record in related] == ["section_context", "figure_reference"]
    assert status["figures"] == [{"figure_id": "1", "status": "linked"}, {"figure_id": "S4", "status": "missing"}]
    assert "WRONG SOURCE" not in str(related)
    forged = copy.deepcopy(anchor)
    forged["metadata"]["source_metadata"]["figure_references"][0]["target_chunk_id"] = "wrong-10"
    assert (await repository.get_paper_related_evidence(kb_id="kb", anchor=forged, limit=25))[1][
        "scope"
    ] == "unavailable"
    assert (await repository.get_paper_related_evidence(kb_id="other-kb", anchor=anchor, limit=25))[0] == []
    async with sessions.begin() as session:
        await session.execute(
            update(KnowledgeFile)
            .where(KnowledgeFile.file_id == "paper")
            .values(markdown_file="attempt-two/document.md", status="indexed")
        )
    assert (await repository.get_paper_related_evidence(kb_id="kb", anchor=anchor, limit=25))[1][
        "reason"
    ] == "anchor_not_current"
    assert await MilvusKB.__new__(MilvusKB)._hydrate_chunk_sources("kb", [copy.deepcopy(anchor)]) == []


async def test_chunk_deletion_waits_for_related_evidence_file_operation(paper_store, monkeypatch):
    """两次读取间先删块再更新文件的真实写入必须等待文件操作锁。"""
    sessions, anchor, _chunks = paper_store
    anchored, release = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def paused_context():
        """拿到真实主块后暂停，允许并发删除尝试。"""
        async with sessions.begin() as session:
            original_execute = session.execute
            first = True

            async def execute(statement):
                """只暂停第一次真实 SQL 返回。"""
                nonlocal first
                result = await original_execute(statement)
                if first:
                    first = False
                    anchored.set()
                    await release.wait()
                return result

            monkeypatch.setattr(session, "execute", execute)
            yield session

    monkeypatch.setattr(module.pg_manager, "get_async_session_context", paused_context)
    reading = asyncio.create_task(
        module.KnowledgeChunkRepository().get_paper_related_evidence(kb_id="kb", anchor=anchor, limit=25)
    )
    await asyncio.wait_for(anchored.wait(), timeout=5)

    async def publish():
        """复用生产文件锁，按删除入口的顺序先删块再更新文件。"""
        async with KnowledgeFileRepository().lock_file_operation("kb", "paper"):
            async with sessions.begin() as session:
                await session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.file_id == "paper"))
            async with sessions.begin() as session:
                await session.execute(
                    update(KnowledgeFile).where(KnowledgeFile.file_id == "paper").values(status="parsed", chunk_count=0)
                )

    publishing = asyncio.create_task(publish())
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(publishing), timeout=0.2)
    finally:
        release.set()
    related, _status = await asyncio.wait_for(reading, timeout=5)
    assert len(related) == 2
    await asyncio.wait_for(publishing, timeout=5)


async def test_missing_or_wrong_figure_target_is_observable(paper_store):
    """目标块存在但图号不匹配时，不能宣称明确关联已补齐。"""
    sessions, anchor, chunks = paper_store
    async with sessions.begin() as session:
        source = {**chunks[3]["source_metadata"], "figure_ids": ["9"]}
        await session.execute(
            update(KnowledgeChunk)
            .where(KnowledgeChunk.chunk_id == chunks[3]["chunk_id"])
            .values(source_metadata=source)
        )
    related, status = await module.KnowledgeChunkRepository().get_paper_related_evidence(
        kb_id="kb", anchor=anchor, limit=25
    )
    assert [item["evidence_role"] for item in related] == ["section_context"]
    assert status["figures"][0]["status"] == "unavailable"


async def test_busy_reader_uses_one_nonwaiting_lock_transaction(paper_store):
    """写者持锁时真实读者快速报告未就绪，避免排队占满连接池。"""
    _sessions, anchor, _chunks = paper_store
    async with KnowledgeFileRepository().lock_file_operation("kb", "paper"):
        related, status = await asyncio.wait_for(
            module.KnowledgeChunkRepository().get_paper_related_evidence(kb_id="kb", anchor=anchor, limit=25), timeout=1
        )
        assert related == [] and status == {"scope": "unavailable", "reason": "file_operation_in_progress"}
