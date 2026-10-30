"""真实 PostgreSQL 证明正文发布与图片 intent 原子性及过期写保护。"""

import asyncio
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from yuxi.repositories import knowledge_file_repository as files_module
from yuxi.repositories import task_repository as tasks_module
from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository
from yuxi.repositories.task_repository import TaskRepository
from yuxi.services.knowledge_figure_analysis_service import figure_task_intent
from yuxi.services.knowledge_parse_service import FileParseControl
from yuxi.services.task_service import TaskContext
from yuxi.storage.postgres.manager import PostgresManager
from yuxi.storage.postgres.models_business import TaskRecord
from yuxi.storage.postgres.models_knowledge import KnowledgeBase, KnowledgeFile

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(scope="session", autouse=True)
def ensure_live_api_schema():
    """沿用 durable_task_repository 模式，以隔离 Schema 代替运行 API。"""


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_knowledge_resources():
    """本文件没有创建 HTTP 知识资源。"""
    yield


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_sandboxes():
    """本文件没有创建 Sandbox 资源。"""
    yield


@pytest.fixture
async def figure_publication_schema(monkeypatch):
    """复用现有真实 PG 隔离 Schema 的建表、注入和销毁方式。"""
    schema = f"pytest_pdf_publication_{uuid.uuid4().hex[:16]}"
    admin_engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True, connect_args={"server_settings": {"search_path": schema}})
    async with engine.begin() as connection:
        await connection.run_sync(TaskRecord.__table__.create)
        await connection.run_sync(KnowledgeBase.__table__.create)
        await connection.run_sync(KnowledgeFile.__table__.create)
    manager = object.__new__(PostgresManager)
    PostgresManager.__init__(manager)
    manager.async_engine = engine
    manager.AsyncSession = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    manager._initialized = True
    monkeypatch.setattr(files_module, "pg_manager", manager)
    monkeypatch.setattr(tasks_module, "pg_manager", manager)
    try:
        yield manager
    finally:
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


async def prepare_publication(manager):
    """创建真实 parsing 文件、持 lease 解析任务与待提交图片 intent。"""
    suffix = uuid.uuid4().hex[:12]
    kb_id, file_id, parse_id = f"kb_{suffix}", f"file_{suffix}", f"parse_{suffix}"
    intent = figure_task_intent(f"figure_{suffix}", kb_id=kb_id, file_id=file_id, attempt_id="attempt", markdown_file="new.md", assets_file="new.json", model_spec="test:model", filename="paper.pdf")
    parse_data = {**intent["data"], "type": "knowledge_parse", "payload": {"kb_id": kb_id, "file_id": file_id}}
    await TaskRepository().create(parse_id, parse_data)
    _, claimed = await TaskRepository().claim(parse_id, worker_id="owner", lease_seconds=60)
    assert claimed is True
    async with manager.get_async_session_context() as session:
        session.add(KnowledgeBase(kb_id=kb_id, name="pytest publication", kb_type="milvus"))
        await session.flush()
        session.add(KnowledgeFile(kb_id=kb_id, file_id=file_id, filename="paper.pdf", status="parsing", markdown_file="old.md", parse_assets_file="old.json", processing_task_id=parse_id, processing_owner="owner"))
    context = TaskContext(parse_id, "owner")
    control = FileParseControl(kb_id, file_id, "attempt", task_context=context)
    return control, intent


@pytest.mark.parametrize("duplicate_intent", [False, True])
async def test_publication_and_figure_intent_commit_or_rollback_together(figure_publication_schema, duplicate_intent):
    """intent 插入冲突时，先刷新的正文指针也必须随 owning transaction 回滚。"""
    control, intent = await prepare_publication(figure_publication_schema)
    if duplicate_intent:
        await TaskRepository().create(intent["id"], {**intent["data"], "message": "existing intent"})
    publication = {"status": "parsed", "markdown_file": "new.md", "parse_assets_file": "new.json", "processing_task_id": None, "processing_owner": None}
    if duplicate_intent:
        with pytest.raises(IntegrityError):
            await control.write_fields(publication, figure_task=intent)
    else:
        await control.write_fields(publication, figure_task=intent)

    persisted = await KnowledgeFileRepository().get_by_file_id(control.file_id)
    queued = await TaskRepository().get_by_id(intent["id"])
    if duplicate_intent:
        assert (persisted.status, persisted.markdown_file, persisted.parse_assets_file) == ("parsing", "old.md", "old.json")
        assert (persisted.processing_task_id, persisted.processing_owner) == (control.task_id, "owner")
        assert queued.message == "existing intent"
    else:
        assert (persisted.status, persisted.markdown_file, persisted.parse_assets_file) == ("parsed", "new.md", "new.json")
        assert queued.status == "pending"
        assert queued.payload["markdown_file"] == persisted.markdown_file
        assert queued.payload["attempt_id"] == "attempt"


@pytest.mark.parametrize("replacement", ["unchanged", "reparsed", "deleted"])
async def test_sidecar_cas_preserves_reparsed_or_deleted_file(figure_publication_schema, replacement):
    """图片任务只更新仍引用原正文和原侧录的文件，重解析和删除不会被复活。"""
    control, _ = await prepare_publication(figure_publication_schema)
    repo = KnowledgeFileRepository()
    if replacement == "reparsed":
        await repo.update_fields(file_id=control.file_id, data={"markdown_file": "reparsed.md", "parse_assets_file": "reparsed.json"})
    elif replacement == "deleted":
        await repo.delete(control.file_id)
    async with figure_publication_schema.get_async_session_context() as session:
        published = await repo.publish_parse_assets_in_session(session, kb_id=control.kb_id, file_id=control.file_id, markdown_file="old.md", previous_assets_file="old.json", assets_file="figures.json")
    persisted = await repo.get_by_file_id(control.file_id)
    assert published is (replacement == "unchanged")
    if replacement == "deleted":
        assert persisted is None
    elif replacement == "reparsed":
        assert (persisted.markdown_file, persisted.parse_assets_file) == ("reparsed.md", "reparsed.json")
    else:
        assert (persisted.markdown_file, persisted.parse_assets_file) == ("old.md", "figures.json")


@pytest.mark.parametrize("rejection", ["old_file_owner", "cancel_after_precheck", "old_task_owner"])
async def test_old_owner_or_cancelled_parse_cannot_publish_or_create_intent(figure_publication_schema, monkeypatch, rejection):
    """文件 owner、Task lease 和事务内取消各自拒绝发布且没有遗留图片任务。"""
    control, intent = await prepare_publication(figure_publication_schema)
    repo = KnowledgeFileRepository()
    if rejection == "old_file_owner":
        await repo.update_fields(file_id=control.file_id, data={"processing_owner": "new-owner"})
    elif rejection == "old_task_owner":
        control.context.worker_id = "stale-owner"
    else:
        original_check = control.context.raise_if_cancelled

        async def cancel_after_check():
            await original_check()
            await TaskRepository().request_cancel(control.task_id)

        monkeypatch.setattr(control.context, "raise_if_cancelled", cancel_after_check)
    with pytest.raises(asyncio.CancelledError):
        await control.write_fields({"status": "parsed", "markdown_file": "new.md", "parse_assets_file": "new.json"}, figure_task=intent)
    persisted = await repo.get_by_file_id(control.file_id)
    assert (persisted.status, persisted.markdown_file, persisted.parse_assets_file) == ("parsing", "old.md", "old.json")
    assert await TaskRepository().get_by_id(intent["id"]) is None
