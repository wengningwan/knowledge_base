"""只替换共享 Redis、配置和 SDK，不绕过真实 Executor.request。"""

from contextlib import asynccontextmanager
from types import SimpleNamespace

from yuxi.services import pdf_model_execution


class FakePdfRedis:
    """实现执行器的 Redis 子集，Lua 语义另由真实 Redis 集成测试证明。"""

    def __init__(self):
        self.values = {}
        self.slots = {}
        self.cache_writes = []
        self.acquisitions = []
        self.releases = []
        self.denied = 0
        self.unavailable = False
        self.renew_live = True

    async def get(self, key):
        """返回缓存原值。"""
        self._check()
        return self.values.get(key)

    async def set(self, key, value, *, nx=False, ex=None):
        """记录成功结果缓存，避免失败响应污染。"""
        self._check()
        if nx and key in self.values:
            return False
        self.values[key] = value
        if key.startswith("pdf:validated:"):
            self.cache_writes.append((key, value))
        return True

    async def eval(self, script, numkeys, key, *args):
        """不含 await 的状态变更模拟 Redis 原子 Lua 调用。"""
        self._check()
        assert numkeys == 1
        active = self.slots.setdefault(key, set())
        if script == pdf_model_execution.ACQUIRE:
            limit, token = args
            if len(active) >= limit:
                self.denied += 1
                return 0
            active.add(token)
            self.acquisitions.append((key, token, len(active)))
            return 1
        assert script == pdf_model_execution.RENEW
        return int(self.renew_live and args[0] in active)

    async def zrem(self, key, token):
        """只释放当前 owner，保留同键其他在途请求。"""
        self._check()
        active = self.slots.setdefault(key, set())
        removed = token in active
        active.discard(token)
        self.releases.append((key, token))
        return int(removed)

    def _check(self):
        """按需模拟共享服务不可用。"""
        if self.unavailable:
            raise ConnectionError("redis unavailable")


def configure_pdf_execution(monkeypatch, *, limit=2, redis=None):
    """注入配置 Owner 和共享连接，保留初始化及真实请求链路。"""
    from yuxi.config import options
    from yuxi.models.providers.cache import model_cache

    redis = redis or FakePdfRedis()
    info = SimpleNamespace(provider_id="provider", model_id="model", provider_type="openai", base_url="https://model.invalid/v1", api_key="test-secret")

    @asynccontextmanager
    async def policy():
        """测试配置与 Redis准入使用同一个显式额度。"""
        yield limit

    async def connection(self):
        """所有执行器共享同一个可观察准入实例。"""
        return redis

    monkeypatch.setattr(options, "document_model_concurrency_policy", policy)
    monkeypatch.setattr(pdf_model_execution.PdfModelExecutor, "_connection", connection)
    monkeypatch.setattr(model_cache, "get_model_info", lambda spec: info)
    return redis, info
