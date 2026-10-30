"""真实执行器的完整队列、取消收敛与服务报告 token 对账。"""

import asyncio
from types import SimpleNamespace

import pytest

from test.unit.services.pdf_model_fakes import FakePdfRedis, configure_pdf_execution
from yuxi.config.options import normalize_option_value, system_options
from yuxi.models import chat
from yuxi.services.pdf_model_execution import PdfModelExecutor, PdfModelUnavailable, run_pdf_workers


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 3])
async def test_shared_limit_is_simultaneously_inflight_and_waiting_items_are_not_lost(monkeypatch, limit):
    """事件证明 N 请求同时在途，等待超额请求最终全部完成。"""
    ready, denied, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Redis(FakePdfRedis):
        """把准入拒绝变为确定的事件证据。"""

        async def eval(self, script, *args):
            result = await super().eval(script, *args)
            if self.denied:
                denied.set()
            return result

    redis, _ = configure_pdf_execution(monkeypatch, limit=limit, redis=Redis())
    active, peak, completed = set(), 0, []

    async def call(messages, stream=False):
        """释放闸门前保留全部实际模型调用。"""
        nonlocal peak
        prompt = messages[0].content[0]["text"]
        active.add(prompt)
        peak = max(peak, len(active))
        if len(active) == limit:
            ready.set()
        try:
            await release.wait()
            completed.append(prompt)
            return SimpleNamespace(content="done")
        finally:
            active.remove(prompt)

    executors = [await PdfModelExecutor("p:model", model=SimpleNamespace(call=call)).initialize() for _ in range(2)]
    tasks = [asyncio.create_task(executors[index % 2].request(str(index))) for index in range(11)]
    try:
        await asyncio.wait_for(ready.wait(), 3)
        await asyncio.wait_for(denied.wait(), 3)
        assert len(active) == limit
        assert sum(len(tokens) for tokens in redis.slots.values()) == limit
        assert completed == []
        release.set()
        responses = await asyncio.wait_for(asyncio.gather(*tasks), 5)
        assert len(responses) == 11
        assert sorted(completed, key=int) == [str(index) for index in range(11)]
        assert peak == limit
        assert not any(redis.slots.values())
        assert sum(executor.stats()["calls"] for executor in executors) == 11
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_drains_sdk_call_and_releases_only_its_owner(monkeypatch):
    """取消返回前 SDK finally 已执行，同键后续请求可准入。"""
    redis, _ = configure_pdf_execution(monkeypatch, limit=1)
    started, drained = asyncio.Event(), asyncio.Event()

    async def call(messages, stream=False):
        """模拟供应商挂起并记录取消完成。"""
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    executor = await PdfModelExecutor("p:model", model=SimpleNamespace(call=call)).initialize()
    task = asyncio.create_task(executor.request("private source"))
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert drained.is_set()
    assert not any(redis.slots.values())
    assert len(redis.releases) == 1
    assert executor.stats()["unknown_usage_requests"] == 1

    async def success(messages, stream=False):
        """释放后新请求立即获得同一额度。"""
        return SimpleNamespace(content="next")

    next_executor = await PdfModelExecutor("p:model", model=SimpleNamespace(call=success)).initialize()
    assert (await asyncio.wait_for(next_executor.request("next"), 3)).text == "next"


@pytest.mark.asyncio
async def test_worker_failure_cancels_and_drains_sibling_calls():
    """消费队列的一个操作失败后，其余已启动操作必须完成取消清理。"""
    entered, drained = asyncio.Event(), asyncio.Event()

    async def operation(item):
        """让失败发生在兄弟已经在途之后。"""
        if item == 0:
            await entered.wait()
            raise ValueError("stop")
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    with pytest.raises(ValueError, match="stop"):
        await asyncio.wait_for(run_pdf_workers(list(range(10)), operation, 2), 3)
    assert drained.is_set()


@pytest.mark.asyncio
async def test_redis_unavailable_fails_closed_without_sdk_call(monkeypatch):
    """共享服务失败不能退化为不受控请求。"""
    redis, _ = configure_pdf_execution(monkeypatch)
    redis.unavailable = True

    async def forbidden(*args, **kwargs):
        """准入失败时不得碰供应商。"""
        pytest.fail("SDK called without admission")

    executor = await PdfModelExecutor("p:model", model=SimpleNamespace(call=forbidden)).initialize()
    with pytest.raises(PdfModelUnavailable):
        await executor.request("source")
    assert executor.stats()["calls"] == 0


@pytest.mark.asyncio
async def test_real_adapter_usage_routes_and_cache_do_not_double_count(monkeypatch):
    """保留 select_model/adapter/request，SDK 返回真实形状的 usage 字段。"""
    _, info = configure_pdf_execution(monkeypatch)
    sdk_options = []
    sdk_responses = [
        SimpleNamespace(text="not JSON", usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "input_token_details": {"cache_read": 4}, "output_token_details": {"reasoning": 2}}, response_metadata={}),
        SimpleNamespace(text="vision", usage_metadata=None, response_metadata={"token_usage": {"prompt_tokens": 20, "completion_tokens": 7, "total_tokens": 27, "prompt_tokens_details": {"cached_tokens": 3}, "completion_tokens_details": {"reasoning_tokens": 1}}}),
        SimpleNamespace(text="unknown", usage_metadata=None, response_metadata={}),
    ]

    class SDK:
        """仅替换供应商边界，不替换本地 adapter。"""

        async def ainvoke(self, messages):
            return sdk_responses.pop(0)

    def load(spec, **kwargs):
        """收集传给 SDK 的 timeout/retries。"""
        sdk_options.append(kwargs)
        return SDK()

    monkeypatch.setattr(chat, "_require_chat_model_info", lambda spec: info)
    monkeypatch.setattr(chat, "load_chat_model", load)
    executor = await PdfModelExecutor("p:model").initialize()
    text = await executor.request("text")
    await executor.request("vision", b"png")
    await executor.request("unknown")
    key = executor.cache_key({"kb_id": "kb", "file_id": "file"}, {"target": "one"})
    await executor.store_validated(key, {"description": "validated"})
    assert await executor.read_cache(key) == {"description": "validated"}
    executor.record_cache_hit("text")
    stats = executor.stats()
    assert sdk_options == [{"timeout": None, "max_retries": 0}]
    assert text.usage["source"] == "usage_metadata"
    assert stats["known_tokens"] == {"input_tokens": 30, "output_tokens": 12, "total_tokens": 42, "reasoning_tokens": 3, "provider_cached_input_tokens": 7}
    assert stats["unknown_usage_requests"] == 1
    assert stats["calls"] == stats["responses"] == 3
    assert stats["cache_hits"] == 1
    assert stats["routes"]["text"]["known_tokens"]["total_tokens"] == 15
    assert stats["routes"]["vision"]["known_tokens"]["total_tokens"] == 27
    assert stats["routes"]["text"]["cache_hits"] == 1


@pytest.mark.parametrize("value", [0, -1, True, False, 1.5, "1.5", "-2", "０", [], None])
def test_concurrency_rejects_non_positive_integer(value):
    """配置入口拒绝隐式截断、布尔值及零额度。"""
    field = next(field for field in system_options.fields if field["key"] == "document_model_concurrency")
    with pytest.raises(ValueError, match="正整数"):
        normalize_option_value(field, value)


@pytest.mark.parametrize("value,expected", [(1, 1), (12, 12), (" 3 ", 3)])
def test_concurrency_accepts_positive_integer_and_keeps_numeric_type(value, expected):
    """系统解析产物为整数，不保留字符串额度。"""
    resolved = system_options.resolve({"document_model_concurrency": value})
    assert resolved["document_model_concurrency"] == expected
    assert type(resolved["document_model_concurrency"]) is int


def test_cache_isolates_file_evidence_image_and_effective_model_configuration(monkeypatch):
    """结果缓存不跨文件、问题、图片和凭据配置重用。"""
    _, info = configure_pdf_execution(monkeypatch)
    scope = {"kb_id": "kb", "file_id": "file", "ocr": "ocr-a", "native": "native-a", "rule": "r1"}
    evidence = {"targets": ["one"], "image": "image-digest"}
    executor = PdfModelExecutor("p:model")
    original = executor.cache_key(scope, evidence)
    assert executor.cache_key(dict(reversed(list(scope.items()))), evidence) == original
    changed = [
        executor.cache_key({**scope, "file_id": "other"}, evidence),
        executor.cache_key({**scope, "ocr": "ocr-b"}, evidence),
        executor.cache_key({**scope, "native": "native-b"}, evidence),
        executor.cache_key({**scope, "rule": "r2"}, evidence),
        executor.cache_key(scope, {**evidence, "targets": ["two"]}),
        executor.cache_key(scope, {**evidence, "image": "different-digest"}),
    ]
    for field, value in [("api_key", "rotated-secret"), ("headers", {"route": "other"}), ("extra", {"temperature": .3}), ("request_body_overrides", {"seed": 9})]:
        setattr(info, field, value)
        changed.append(PdfModelExecutor("p:model").cache_key(scope, evidence))
    assert original and len({original, *changed}) == len(changed) + 1
    assert "test-secret" not in original
    assert executor.cache_key({"kb_id": "kb"}, evidence) is None
