"""只替换共享 Redis 和模型配置，不绕过真实预算与准入流程。"""

from types import SimpleNamespace

from yuxi.services import pdf_model_budget


class FakePdfRedis:
    """提供预算使用的异步 Redis 子集并记录成功缓存写入。"""

    def __init__(self):
        self.values = {}
        self.waiters = {}
        self.cache_writes = []
        self.gate_busy = False
        self.unavailable = False

    async def get(self, key):
        if self.unavailable:
            raise ConnectionError("redis unavailable")
        return self.values.get(key)

    async def set(self, key, value, *, nx=False, ex=None):
        if self.unavailable:
            raise ConnectionError("redis unavailable")
        if nx and (self.gate_busy or key in self.values):
            return False
        self.values[key] = value
        if key.startswith("pdf:validated:"):
            self.cache_writes.append((key, value))
        return True

    async def zadd(self, key, members):
        if self.unavailable:
            raise ConnectionError("redis unavailable")
        self.waiters.setdefault(key, {}).update(members)

    async def expire(self, key, seconds):
        return True

    async def zcount(self, key, minimum, maximum):
        return sum(score >= minimum for score in self.waiters.get(key, {}).values())

    async def zrem(self, key, member):
        self.waiters.get(key, {}).pop(member, None)

    async def eval(self, script, numkeys, key, token):
        if self.values.get(key) == token:
            del self.values[key]
            return 1
        return 0


def configure_pdf_budget(monkeypatch):
    """注入可控共享准入，调用方仍使用真实 PdfModelBudget。"""
    from yuxi.models.providers.cache import model_cache

    redis = FakePdfRedis()
    info = SimpleNamespace(provider_id="provider", model_id="model", base_url="https://model.invalid/v1", api_key="test-secret")

    async def connection(self):
        return redis

    monkeypatch.setattr(pdf_model_budget.PdfModelBudget, "_connection", connection)
    monkeypatch.setattr(model_cache, "get_model_info", lambda spec: info)
    return redis, info
