"""补取只能使用当前 PG anchor、发布文件和同版来源。"""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from yuxi.knowledge.chunking.source import source_sha256
from yuxi.knowledge.related_evidence import public_source_metadata
from yuxi.repositories import knowledge_chunk_repository as module


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch", [None, "missing", "status", "pointer", "body", "index", "version", "section", "targets"]
)
async def test_repository_revalidates_anchor_before_scoped_query(monkeypatch, mismatch):
    """陈旧正文、版本或伪造关联 ID 不能触发第二次查询。"""
    source = {
        "chunker_version": "paper_rules_v3",
        "source_sha256": "digest",
        "section_id": "section",
        "_publication_id": source_sha256("current.md"),
        "figure_references": [],
    }
    current = SimpleNamespace(content="body", chunk_index=0, chunk_id="c0", file_id="file", source_metadata=source)
    file = SimpleNamespace(status="indexed", markdown_file="current.md", filename="paper.md")
    anchor = {
        "content": "body",
        "metadata": {
            "chunk_id": "c0",
            "file_id": "file",
            "chunk_index": 0,
            "source_metadata": public_source_metadata(source),
        },
    }
    if mismatch in {"version", "section", "targets"}:
        anchor["metadata"]["source_metadata"][
            {"version": "source_sha256", "section": "section_id", "targets": "figure_references"}[mismatch]
        ] = "forged"
    elif mismatch == "status":
        file.status = "parsed"
    elif mismatch == "pointer":
        file.markdown_file = "new-attempt.md"
    elif mismatch == "body":
        anchor["content"] = "old body"
    elif mismatch == "index":
        anchor["metadata"]["chunk_index"] = 1
    calls = []

    class Session:
        """只返回准备好的 PG 事实，保留真实 SQL 以检查边界。"""

        async def scalar(self, _statement):
            """允许获取现有文件操作锁。"""
            return True

        async def execute(self, statement):
            """每次查询读取预设的独立结果。"""
            calls.append(statement)
            if len(calls) == 1:
                return SimpleNamespace(one_or_none=lambda: None if mismatch == "missing" else (current, file))
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    @asynccontextmanager
    async def context():
        """提供受控数据库事务。"""
        yield Session()

    monkeypatch.setattr(module.pg_manager, "get_async_session_context", context)
    candidates, status = await module.KnowledgeChunkRepository().get_paper_related_evidence(
        kb_id="kb", anchor=anchor, limit=25
    )
    assert candidates == []
    if mismatch is not None:
        assert len(calls) == 1 and status == {"scope": "unavailable", "reason": "anchor_not_current"}
    else:
        assert len(calls) == 2 and status["section"] == "complete"
        values = list(calls[1].compile().params.values())
        assert all(value in values for value in ("kb", "file", "digest", "section", source_sha256("current.md"), 26))


@pytest.mark.asyncio
async def test_repository_untitled_paper_has_no_broad_file_join(monkeypatch):
    """没有小节和明确图号时只校验主块，不扫描全文。"""
    source = {
        "chunker_version": "paper_rules_v3",
        "source_sha256": "digest",
        "section_id": None,
        "_publication_id": source_sha256("current.md"),
        "figure_references": [],
    }
    current = SimpleNamespace(content="body", chunk_index=0, chunk_id="c0", file_id="file", source_metadata=source)
    file = SimpleNamespace(status="indexed", markdown_file="current.md", filename="paper.md")
    calls = []

    class Session:
        """禁止执行第二次全文查询。"""

        async def scalar(self, _statement):
            """允许获取现有文件操作锁。"""
            return True

        async def execute(self, statement):
            """返回当前主块。"""
            calls.append(statement)
            assert len(calls) == 1
            return SimpleNamespace(one_or_none=lambda: (current, file))

    @asynccontextmanager
    async def context():
        """提供当前主块事务。"""
        yield Session()

    monkeypatch.setattr(module.pg_manager, "get_async_session_context", context)
    candidates, status = await module.KnowledgeChunkRepository().get_paper_related_evidence(
        kb_id="kb",
        anchor={
            "content": "body",
            "metadata": {
                "chunk_id": "c0",
                "file_id": "file",
                "chunk_index": 0,
                "source_metadata": public_source_metadata(source),
            },
        },
        limit=25,
    )
    assert candidates == [] and status["section"] == "not_applicable"


@pytest.mark.asyncio
@pytest.mark.parametrize("database_busy", [False, True])
async def test_repository_busy_file_operation_returns_observable_unavailable(monkeypatch, database_busy):
    """读者抢不到当前操作锁时立即返回，不等待或继续读取半成品。"""
    lock_attempts = []

    class Session:
        """禁止忙状态执行任何证据读取。"""

        async def scalar(self, statement):
            """分别模拟整库维护和同文件写入占锁。"""
            lock_attempts.append(statement)
            return False if database_busy else len(lock_attempts) == 1

        async def execute(self, _statement):
            """忙状态不得继续读取。"""
            raise AssertionError("busy reader queried incomplete chunks")

    @asynccontextmanager
    async def context():
        """只提供唯一的锁事务。"""
        yield Session()

    monkeypatch.setattr(module.pg_manager, "get_async_session_context", context)
    candidates, status = await module.KnowledgeChunkRepository().get_paper_related_evidence(
        kb_id="kb", anchor={"metadata": {"file_id": "file"}}, limit=25
    )
    assert candidates == [] and status == {"scope": "unavailable", "reason": "file_operation_in_progress"}
    assert len(lock_attempts) == (1 if database_busy else 2)
