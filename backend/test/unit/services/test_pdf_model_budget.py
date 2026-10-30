"""预算覆盖实际总调用、视觉额度、共享排队和成功缓存隔离。"""

from types import SimpleNamespace

import pytest

from test.unit.services.pdf_model_fakes import configure_pdf_budget
from yuxi.services import pdf_model_budget as budgets


@pytest.mark.asyncio
async def test_total_and_vision_limits_prevent_further_model_calls(monkeypatch):
    """视觉与总调用各自有硬上限，拒绝不消耗实际调用额度。"""
    configure_pdf_budget(monkeypatch)
    budget = budgets.PdfModelBudget("test:model", max_calls=2, max_vision_calls=1)
    requests = []

    async def model():
        requests.append("sent")
        return "accepted"

    assert await budget.call(model, vision=True) == "accepted"
    with pytest.raises(budgets.PdfBudgetExhausted):
        await budget.call(model, vision=True)
    assert await budget.call(model) == "accepted"
    with pytest.raises(budgets.PdfBudgetExhausted):
        await budget.call(model)
    assert requests == ["sent", "sent"]
    assert (budget.calls, budget.vision_calls) == (2, 1)


@pytest.mark.asyncio
async def test_queue_wait_spends_same_deadline_without_sending_request(monkeypatch):
    """共享槽持续繁忙时排队耗尽整个阶段，不再获得新调用窗口。"""
    redis, _ = configure_pdf_budget(monkeypatch)
    redis.gate_busy = True
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(budgets, "time", SimpleNamespace(monotonic=lambda: clock.now, time=lambda: clock.now))

    async def advance(seconds):
        clock.now += seconds

    monkeypatch.setattr(budgets.asyncio, "sleep", advance)
    budget = budgets.PdfModelBudget("test:model", seconds=2)
    sent = []

    async def model():
        sent.append(True)

    with pytest.raises(budgets.PdfBudgetExhausted):
        await budget.call(model)
    assert budget.remaining() <= 0.5
    assert clock.now - budget.started >= 1.5
    assert sent == []
    assert budget.calls == 0
    assert not any(redis.waiters.values())
    assert budget.queue_seconds == pytest.approx(clock.now - budget.started)


@pytest.mark.asyncio
async def test_timeout_is_single_request_and_releases_shared_gate(monkeypatch):
    """远端超时不重发，后续其他请求仍能取得释放后的共享槽。"""
    redis, _ = configure_pdf_budget(monkeypatch)
    budget = budgets.PdfModelBudget("test:model")
    sent = []

    async def timeout():
        sent.append(True)
        raise TimeoutError("remote timeout")

    with pytest.raises(TimeoutError):
        await budget.call(timeout, vision=True)
    assert sent == [True]
    assert (budget.calls, budget.vision_calls) == (1, 1)
    assert not any(key.startswith("pdf:model:gate:") for key in redis.values)


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_unavailable_shared_admission_never_sends_model_request(monkeypatch, background):
    """Redis 准入失败不能降级为无约束本地模型调用。"""
    redis, _ = configure_pdf_budget(monkeypatch)
    redis.unavailable = True
    budget = budgets.PdfModelBudget("test:model", background=background)
    sent = []

    async def model():
        sent.append(True)

    with pytest.raises(budgets.PdfModelUnavailable):
        await budget.call(model)
    assert sent == []
    assert budget.calls == 0
    assert redis.cache_writes == []


def test_cache_key_isolates_file_evidence_image_and_effective_configuration(monkeypatch):
    """成功缓存不跨文件、证据版本、图片或完整有效模型配置复用。"""
    _, info = configure_pdf_budget(monkeypatch)
    scope = {"kb_id": "kb", "file_id": "file", "ocr": "snapshot-a", "native": "native-a", "rule": "r1"}
    budget = budgets.PdfModelBudget("test:model")
    original = budget.cache_key(scope, "prompt", [b"image"])
    assert budget.cache_key(dict(reversed(list(scope.items()))), "prompt", [b"image"]) == original
    changes = [
        budget.cache_key({**scope, "file_id": "other-file"}, "prompt", [b"image"]),
        budget.cache_key({**scope, "ocr": "snapshot-b"}, "prompt", [b"image"]),
        budget.cache_key({**scope, "native": "native-b"}, "prompt", [b"image"]),
        budget.cache_key({**scope, "rule": "r2"}, "prompt", [b"image"]),
        budget.cache_key(scope, "changed-prompt", [b"image"]),
        budget.cache_key(scope, "prompt", [b"different-image"]),
    ]
    for field, value in [("api_key", "rotated-secret"), ("headers", {"route": "other"}), ("extra", {"temperature": 0.3}), ("request_body_overrides", {"seed": 9})]:
        setattr(info, field, value)
        changes.append(budgets.PdfModelBudget("test:model").cache_key(scope, "prompt", [b"image"]))
    assert original and len({original, *changes}) == len(changes) + 1
    assert "test-secret" not in original and "snapshot-a" not in original
    assert budget.cache_key({"kb_id": "kb"}, "prompt") is None
