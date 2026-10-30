"""真实 Redis Lua 额度/owner 和真实 PostgreSQL 降额配置锁。"""

import asyncio
import json
import os
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from arq import create_pool
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from test.unit.services.pdf_model_fakes import configure_pdf_execution
from yuxi.config import options
from yuxi.services.pdf_model_execution import ACQUIRE, RENEW, PdfModelExecutor
from yuxi.storage.postgres.manager import PostgresManager
from yuxi.storage.postgres.models_business import ConfigOption
from yuxi.storage.redis import get_arq_redis_settings

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(scope="session", autouse=True)
def ensure_live_api_schema():
    """当前模块只使用隔离 Schema 与独立 Redis key，不要求运行 API。"""


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_knowledge_resources():
    """本模块不创建 HTTP 知识资源。"""
    yield


@pytest.fixture(scope="session", autouse=True)
def cleanup_test_sandboxes():
    """本模块不创建 Sandbox。"""
    yield


@pytest_asyncio.fixture
async def pdf_redis():
    """沿用 health_redis 的真实 ARQ 连接，只删除本测试唯一 key。"""
    redis = await create_pool(get_arq_redis_settings())
    keys = []
    try:
        yield redis, keys
    finally:
        if keys:
            await redis.delete(*keys)
        await redis.aclose()


@pytest_asyncio.fixture
async def concurrency_schema(monkeypatch):
    """沿用 figure_publication_schema 模式隔离真实配置行和事务锁。"""
    schema = f"pytest_pdf_concurrency_{uuid.uuid4().hex[:16]}"
    admin_engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True, connect_args={"server_settings": {"search_path": schema}})
    async with engine.begin() as connection:
        await connection.run_sync(ConfigOption.__table__.create)
    manager = object.__new__(PostgresManager)
    PostgresManager.__init__(manager)
    manager.async_engine = engine
    manager.AsyncSession = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    manager._initialized = True
    monkeypatch.setattr("yuxi.storage.postgres.manager.pg_manager", manager)
    try:
        yield manager
    finally:
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


@pytest.mark.parametrize("limit", [1, 3])
async def test_real_redis_shared_n_admission_waits_without_losing_requests(monkeypatch, pdf_redis, limit):
    """真实 Lua 跨执行器共用 N 个在途槽，释放后全部等待请求完成。"""
    redis, keys = pdf_redis
    _, info = configure_pdf_execution(monkeypatch, limit=limit, redis=redis)
    info.model_id = "pytest_pdf_" + uuid.uuid4().hex
    ready, release = asyncio.Event(), asyncio.Event()
    active, completed = set(), []

    async def call(messages, stream=False):
        """SDK 边界使用事件保持真实 Redis 租约在途。"""
        prompt = messages[0].content[0]["text"]
        active.add(prompt)
        if len(active) == limit:
            ready.set()
        try:
            await release.wait()
            completed.append(prompt)
            return SimpleNamespace(content="done")
        finally:
            active.remove(prompt)

    executors = [await PdfModelExecutor("p:test", model=SimpleNamespace(call=call)).initialize() for _ in range(2)]
    key = executors[0]._gate
    keys.append(key)
    assert executors[1]._gate == key
    tasks = [asyncio.create_task(executors[index % 2].request(str(index))) for index in range(9)]
    try:
        await asyncio.wait_for(ready.wait(), 5)
        assert len(active) == await redis.zcard(key) == limit
        assert completed == []
        assert 0 < await redis.ttl(key) <= 120
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 10)
        assert sorted(completed, key=int) == [str(index) for index in range(9)]
        assert await redis.zcard(key) == 0
        assert sum(executor.stats()["calls"] for executor in executors) == 9
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_real_lua_expires_stale_owner_renews_current_owner_and_keeps_limit(pdf_redis):
    """Redis 时钟清理死 owner；非 owner 不可续租也不增加额度。"""
    redis, keys = pdf_redis
    key = "pytest:pdf:slots:" + uuid.uuid4().hex
    keys.append(key)
    await redis.zadd(key, {"expired": 1})
    assert await redis.eval(ACQUIRE, 1, key, 2, "owner-a") == 1
    assert await redis.zscore(key, "expired") is None
    assert await redis.eval(ACQUIRE, 1, key, 2, "owner-b") == 1
    assert await redis.eval(ACQUIRE, 1, key, 2, "owner-c") == 0
    assert await redis.eval(RENEW, 1, key, "intruder") == 0
    before = await redis.zscore(key, "owner-a")
    assert await redis.eval(RENEW, 1, key, "owner-a") == 1
    assert await redis.zscore(key, "owner-a") >= before
    await redis.zrem(key, "owner-a")
    assert await redis.zrange(key, 0, -1) in (["owner-b"], [b"owner-b"])
    assert await redis.eval(ACQUIRE, 1, key, 1, "owner-c") == 0


async def test_pg_policy_holds_share_lock_and_ignores_stale_redis_after_reduction(monkeypatch, concurrency_schema, pdf_redis):
    """降额写入与准入读锁互斥；失效缓存留存也不能恢复旧额度。"""
    manager = concurrency_schema
    redis, keys = pdf_redis
    # 系统缓存键属于运行实例，隔离测试使用独立 Option key，避免覆盖线上配置缓存。
    unique = "pytest_pdf_policy_" + uuid.uuid4().hex
    option = options.Option(key=unique, name="isolated", description="test", params=options.system_options.params)
    async with manager.get_async_session_context() as session:
        session.add(ConfigOption(key=unique, name="test policy", params=option.params, value={"document_model_concurrency": 3}))
    old_option = options.system_options
    options.system_options = option
    key = options.OPTION_CACHE_PREFIX + unique
    keys.append(key)
    await redis.set(key, json.dumps({"document_model_concurrency": 99}), ex=60)

    async def connection():
        """旧配置读取路径连接相同的真实 Redis，确保陈旧缓存实际可读。"""
        return redis

    monkeypatch.setattr(options, "get_async_redis_client", connection)
    try:
        assert (await option.get())["document_model_concurrency"] == 99
        async with options.document_model_concurrency_policy() as limit:
            assert limit == 3
            async with manager.get_async_session_context() as writer:
                await writer.execute(text("SET LOCAL lock_timeout = '100ms'"))
                with pytest.raises(DBAPIError) as caught:
                    await writer.execute(update(ConfigOption).where(ConfigOption.key == unique).values(value={"document_model_concurrency": 1}))
                assert caught.value.orig.sqlstate == "55P03"
                await writer.rollback()
        async with manager.get_async_session_context() as writer:
            await writer.execute(update(ConfigOption).where(ConfigOption.key == unique).values(value={"document_model_concurrency": 1}))
        async with options.document_model_concurrency_policy() as reduced:
            assert reduced == 1
        assert json.loads(await redis.get(key))["document_model_concurrency"] == 99
        async with manager.get_async_session_context() as session:
            record = await session.scalar(select(ConfigOption).where(ConfigOption.key == unique))
            assert record.value == {"document_model_concurrency": 1}
    finally:
        options.system_options = old_option
