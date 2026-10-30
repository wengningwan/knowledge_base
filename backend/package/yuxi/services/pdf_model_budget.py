"""为可选 PDF 模型阶段提供总预算、跨 worker 排队和验证后缓存。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid


class PdfBudgetExhausted(Exception):
    """可选复核已用完时间或实际调用额度。"""


class PdfModelUnavailable(Exception):
    """共享准入不可用时停止可选调用，保留主解析结果。"""


def content_digest(value) -> str:
    """对快照或图像计算稳定摘要，不向缓存键暴露正文。"""
    data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
    return hashlib.sha256(data).hexdigest()


class PdfModelBudget:
    """时间包含排队；只在实际发出请求时消耗调用数，超时不重试。"""

    def __init__(self, model_spec, *, control=None, seconds=180, max_calls=6, max_vision_calls=3, background=False):
        self.model_spec = model_spec
        self.control = control
        self.started = time.monotonic()
        available = control.remaining_seconds() - 45 if control else seconds
        self.deadline = self.started + max(0, min(seconds, available))
        self.max_calls = max_calls
        self.max_vision_calls = max_vision_calls
        self.background = background
        self.calls = self.vision_calls = self.cache_hits = 0
        self.queue_seconds = self.call_seconds = 0.0
        self._redis = None
        self._fingerprint = None
        self._gate = None

    def remaining(self):
        """返回整个阶段尚可花费的秒数。"""
        return max(0.0, self.deadline - time.monotonic())

    async def check(self, *, vision=False):
        """在每次等待和调用前服从任务取消与阶段预算。"""
        if self.control:
            await self.control.check_cancelled()
        if self.remaining() <= 0.5 or self.calls >= self.max_calls or (vision and self.vision_calls >= self.max_vision_calls):
            raise PdfBudgetExhausted()



    def cache_key(self, scope, prompt, images=()):
        """文件、输入快照、提示、图片和模型参数共同限定缓存。"""
        if not scope.get("kb_id") or not scope.get("file_id"):
            return None
        try:
            config = self._configuration()
        except Exception:
            return None
        return "pdf:validated:" + content_digest({
            "scope": scope, "config": config, "prompt": prompt,
            "images": [content_digest(data) for data in images],
            "parameters": {"timeout": 90, "max_retries": 0},
        })

    async def read_cache(self, key):
        """缓存读取失败不影响主解析，也不把失败结果当成成功。"""
        if not key or self.remaining() <= 0.5:
            return None
        try:
            client = await self._connection()
            async with asyncio.timeout(min(1, self.remaining())):
                value = await client.get(key)
            return value.decode() if isinstance(value, bytes) else value
        except Exception:
            return None

    async def store_validated(self, key, value):
        """仅由通过证据校验的调用方写入短期成功结果。"""
        if not key or self.remaining() <= 0.5:
            return
        try:
            client = await self._connection()
            async with asyncio.timeout(min(1, self.remaining())):
                await client.set(key, value, ex=86400)
        except Exception:
            pass

    async def call(self, operation, *, vision=False):
        """相同端点/模型一次只准入一个 PDF 请求，前台先于未开始的图片任务。"""
        await self.check(vision=vision)
        try:
            self._configuration()
            client = await self._connection()
        except Exception as exc:
            raise PdfModelUnavailable() from exc
        token = uuid.uuid4().hex
        waiters = self._gate + ":foreground"
        acquired = False
        queue_recorded = False
        queued = time.monotonic()
        try:
            if not self.background:
                try:
                    async with asyncio.timeout(min(2, self.remaining())):
                        await client.zadd(waiters, {token: time.time() + self.remaining() + 5})
                        await client.expire(waiters, 240)
                except Exception as exc:
                    raise PdfModelUnavailable() from exc
            while not acquired:
                await self.check(vision=vision)
                try:
                    async with asyncio.timeout(min(2, self.remaining())):
                        if self.background and await client.zcount(waiters, time.time(), "+inf"):
                            acquired = False
                        else:
                            acquired = bool(await client.set(self._gate, token, nx=True, ex=120))
                except Exception as exc:
                    raise PdfModelUnavailable() from exc
                if not acquired:
                    await asyncio.sleep(min(0.25, self.remaining()))
            self.queue_seconds += time.monotonic() - queued
            queue_recorded = True
            await self.check(vision=vision)
            self.calls += 1
            self.vision_calls += int(vision)
            started = time.monotonic()
            try:
                async with asyncio.timeout(min(90, self.remaining())):
                    return await operation()
            finally:
                self.call_seconds += time.monotonic() - started
        finally:
            if not queue_recorded:
                self.queue_seconds += time.monotonic() - queued
            # token 比较防止旧请求释放后来持有者；TTL 覆盖进程崩溃和硬取消。
            try:
                async with asyncio.timeout(2):
                    if acquired:
                        await client.eval("if redis.call('get',KEYS[1]) == ARGV[1] then return redis.call('del',KEYS[1]) end return 0", 1, self._gate, token)
                    if not self.background:
                        await client.zrem(waiters, token)
            except Exception:
                pass

    def stats(self):
        """输出不含原文和凭据的预算统计。"""
        return {"calls": self.calls, "vision_calls": self.vision_calls, "cache_hits": self.cache_hits,
                "queue_seconds": round(self.queue_seconds, 2), "call_seconds": round(self.call_seconds, 2),
                "elapsed_seconds": round(time.monotonic() - self.started, 2), "limit_seconds": round(self.deadline - self.started, 2)}


    async def _connection(self):
        """Redis 只负责短期准入；不可用时不退化为无限本地并发。"""
        if self._redis is None:
            try:
                from yuxi.storage.redis import get_async_redis_client

                async with asyncio.timeout(min(3, max(0.1, self.remaining()))):
                    self._redis = await get_async_redis_client()
            except Exception as exc:
                raise PdfModelUnavailable() from exc
        return self._redis


    def _configuration(self):
        """缓存绑定完整有效配置摘要；准入按相同端点和模型共享。"""
        if self._fingerprint is None:
            from yuxi.models.providers.cache import model_cache

            info = model_cache.get_model_info(self.model_spec)
            if info is None:
                raise PdfModelUnavailable()
            config = {name: getattr(info, name, None) for name in (
                "provider_id", "model_id", "base_url", "provider_type", "api_key", "headers", "extra", "request_body_overrides",
            )}
            self._fingerprint = content_digest(config)
            self._gate = "pdf:model:gate:" + content_digest([config["base_url"], config["model_id"]])
        return self._fingerprint

