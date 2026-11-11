"""文档模型请求的共享并发、PDF 成功缓存及真实用量。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
import uuid
from collections import deque
from dataclasses import dataclass

from yuxi.utils.logging_config import logger


class PdfModelUnavailable(Exception):
    """配置或共享准入不可用，调用方保留未完成状态。"""


class InvalidPdfModelResponse(ValueError):
    """供应商返回了不可作为文本读取的响应。"""


@dataclass(frozen=True)
class PdfModelResponse:
    """模型文本和服务报告用量，内容校验之前即可计量。"""

    text: str
    usage: dict
    request_id: str
    queue_seconds: float
    call_seconds: float


def content_digest(value) -> str:
    """以摘要限定输入、证据及配置，不将原文放入缓存键。"""
    data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
    return hashlib.sha256(data).hexdigest()


def normalize_usage(response) -> dict:
    """归一真实服务字段，缺失或冲突不伪造为零。"""
    standard = getattr(response, "usage_metadata", None)
    legacy = getattr(response, "token_usage", None)
    source = standard if isinstance(standard, dict) and standard else legacy
    standard_source = source is standard and isinstance(standard, dict) and bool(standard)
    result = {key: None for key in ("input_tokens", "output_tokens", "total_tokens", "reasoning_tokens", "provider_cached_input_tokens")}
    result.update(status="unknown", source="none")
    if not isinstance(source, dict) or not source:
        return result
    names = {"input_tokens": "input_tokens" if standard_source else "prompt_tokens", "output_tokens": "output_tokens" if standard_source else "completion_tokens", "total_tokens": "total_tokens"}
    values = {key: source.get(name) for key, name in names.items()}
    input_details = source.get("input_token_details" if standard_source else "prompt_tokens_details") or {}
    output_details = source.get("output_token_details" if standard_source else "completion_tokens_details") or {}
    values["reasoning_tokens"] = output_details.get("reasoning" if standard_source else "reasoning_tokens") if isinstance(output_details, dict) else None
    values["provider_cached_input_tokens"] = input_details.get("cache_read" if standard_source else "cached_tokens") if isinstance(input_details, dict) else None
    malformed = any(value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0) for value in values.values())
    if malformed:
        result["status"] = "inconsistent"
        return result
    result.update(values, source="usage_metadata" if standard_source else "token_usage")
    if values["input_tokens"] is not None and values["output_tokens"] is not None:
        total = values["input_tokens"] + values["output_tokens"]
        if values["total_tokens"] is not None and values["total_tokens"] != total:
            return {**{key: None for key in values}, "status": "inconsistent", "source": result["source"]}
        result["total_tokens"] = total
        result["status"] = "reported" if values["total_tokens"] is not None else "reported_parts"
    elif any(value is not None for value in values.values()):
        result["status"] = "partial"
    return result


ACQUIRE = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2])/1000000
redis.call('ZREMRANGEBYSCORE',KEYS[1],'-inf',now)
if redis.call('ZCARD',KEYS[1]) >= tonumber(ARGV[1]) then return 0 end
redis.call('ZADD',KEYS[1],now+60,ARGV[2])
redis.call('EXPIRE',KEYS[1],120)
return 1
"""
RENEW = """
if not redis.call('ZSCORE',KEYS[1],ARGV[1]) then return 0 end
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2])/1000000
redis.call('ZADD',KEYS[1],now+60,ARGV[1])
redis.call('EXPIRE',KEYS[1],120)
return 1
"""


class PdfModelExecutor:
    """同端点模型跨文件共享额度；请求持续期间续租，不设置请求时限。"""

    def __init__(self, model_spec, *, control=None, stage="review", model=None):
        self.model_spec = model_spec
        self.control = control
        self.stage = stage
        self.model = model
        self.concurrency = 2
        self.calls = self.vision_calls = self.cache_hits = self.responses = self.failed_requests = 0
        self.queue_seconds = self.call_seconds = 0.0
        self.usage_totals = {key: 0 for key in ("input_tokens", "output_tokens", "total_tokens", "reasoning_tokens", "provider_cached_input_tokens")}
        self.unknown_usage_requests = 0
        self.routes = {}
        self._redis = None
        self._fingerprint = self._gate = None

    async def initialize(self):
        """读取有效并发配置，PDF请求显式关闭SDK固定超时与自动重试。"""
        from yuxi.config.options import document_model_concurrency_policy
        from yuxi.models.chat import select_model

        async with document_model_concurrency_policy() as limit:
            self.concurrency = limit
        if self.model is None:
            self.model = select_model(self.model_spec, timeout=None, max_retries=0, safe_error_logging=True)
        self._configuration()
        return self

    async def request(self, prompt, images=(), *, mime="image/png", batch_id="-", attempt=1, system_prompt=None):
        """只记安全规模和真实用量，取消时停止子请求并释放当前owner。"""
        await self._check()
        client = await self._connection()
        self._configuration()
        token = uuid.uuid4().hex
        image_list = [images] if isinstance(images, bytes) else list(images)
        route = "figures" if self.stage == "figures" else "vision" if image_list else "text"
        route_stats = self._route(route)
        acquired = False
        queued = time.monotonic()
        queue_recorded = False
        operation = monitor = None
        request_id = uuid.uuid4().hex
        prefix = f"[pdf_model] task_id={getattr(self.control, 'task_id', None) or '-'} file_id={getattr(self.control, 'file_id', None) or '-'} stage={self.stage} route={route} batch_id={batch_id} request_id={request_id} attempt={attempt}"
        try:
            while not acquired:
                await self._check()
                try:
                    from yuxi.config.options import document_model_concurrency_policy

                    async with document_model_concurrency_policy() as limit:
                        async with asyncio.timeout(3):
                            status = await client.eval(ACQUIRE, 1, self._gate, limit, token)
                            acquired = status == 1
                except Exception as exc:
                    raise PdfModelUnavailable("共享模型准入不可用") from exc
                if not acquired:
                    await asyncio.sleep(0.2)
            queue_s = time.monotonic() - queued
            self.queue_seconds += queue_s
            route_stats["queue_seconds"] += queue_s
            queue_recorded = True
            await self._check()
            self.calls += 1
            route_stats["calls"] += 1
            self.vision_calls += int(bool(image_list))
            logger.info(f"{prefix} request_start images={len(image_list)} input_chars={len(prompt) + len(system_prompt or '')} input_image_bytes={sum(len(data) for data in image_list)} queue_s={queue_s:.2f} model={json.dumps(self.model_spec)}")
            started = time.monotonic()
            received = False
            try:
                operation = asyncio.create_task(self._invoke(prompt, image_list, mime, system_prompt))
                monitor = asyncio.create_task(self._renew(client, token))
                done, _ = await asyncio.wait({operation, monitor}, return_when=asyncio.FIRST_COMPLETED)
                if monitor in done:
                    await monitor
                    raise PdfModelUnavailable("共享模型准入已丢失")
                response = await operation
                received = True
                usage = normalize_usage(response)
                self.responses += 1
                route_stats["responses"] += 1
                self._record_usage(usage, route_stats)
                content = getattr(response, "content", None)
                if isinstance(content, list):
                    content = "\n".join(item["text"] for item in content if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str))
                call_s = time.monotonic() - started
                logger.info(f"{prefix} response_received output_chars={len(content) if isinstance(content, str) else 0} usage={json.dumps(usage, sort_keys=True)} call_s={call_s:.2f}")
                if not isinstance(content, str):
                    raise InvalidPdfModelResponse("invalid_response_content")
                return PdfModelResponse(content, usage, request_id, queue_s, call_s)
            except BaseException as exc:
                self.failed_requests += 1
                route_stats["failed_requests"] += 1
                if not received:
                    self.unknown_usage_requests += 1
                    route_stats["unknown_usage_requests"] += 1
                logger.warning(f"{prefix} request_failed error={getattr(exc, 'error_type', type(exc).__name__)} usage_status={'recorded' if received else 'unknown'} call_s={time.monotonic() - started:.2f}")
                raise
            finally:
                call_s = time.monotonic() - started
                self.call_seconds += call_s
                route_stats["call_seconds"] += call_s
                for task in (operation, monitor):
                    if task is not None and not task.done():
                        task.cancel()
                await asyncio.gather(*(task for task in (operation, monitor) if task is not None), return_exceptions=True)
        finally:
            if not queue_recorded:
                queue_s = time.monotonic() - queued
                self.queue_seconds += queue_s
                route_stats["queue_seconds"] += queue_s
            if acquired:
                try:
                    async with asyncio.timeout(3):
                        await client.zrem(self._gate, token)
                except Exception:
                    # 不明远端/进程中断仍由带owner的租约收敛，不删除其他请求。
                    pass

    def cache_key(self, scope, evidence):
        """成功缓存按问题/证据隔离，不受并发和批次编号改变影响。"""
        if not scope.get("kb_id") or not scope.get("file_id"):
            return None
        try:
            config = self._configuration()
        except Exception:
            return None
        return "pdf:validated:v2:" + content_digest({"scope": scope, "model_config": config, "evidence": evidence, "contract": 2})

    async def read_cache(self, key):
        """只读短期结果，复核调用方仍需验证目标和证据。"""
        if not key:
            return None
        try:
            client = await self._connection()
            async with asyncio.timeout(2):
                raw = await client.get(key)
            return json.loads(raw) if raw else None
        except Exception:
            return None

    async def store_validated(self, key, result):
        """只有调用方已验证的独立结果才能写入缓存。"""
        if not key:
            return
        try:
            client = await self._connection()
            async with asyncio.timeout(2):
                await client.set(key, json.dumps(result, ensure_ascii=False), ex=86400)
        except Exception:
            pass

    def stats(self):
        """已知token与未知请求分开，不把未知量宣称为完整消耗。"""
        return {"calls": self.calls, "vision_calls": self.vision_calls, "responses": self.responses,
                "failed_requests": self.failed_requests, "cache_hits": self.cache_hits,
                "queue_seconds": round(self.queue_seconds, 2), "call_seconds": round(self.call_seconds, 2),
                "known_tokens": dict(self.usage_totals), "unknown_usage_requests": self.unknown_usage_requests,
                "routes": json.loads(json.dumps(self.routes))}

    def record_cache_hit(self, route):
        """本地结果复用没有新请求或新token，只计缓存命中。"""
        self.cache_hits += 1
        self._route(route)["cache_hits"] += 1

    async def _check(self):
        """服从原有任务取消和owner，不自行设置额外模型截止时间。"""
        if self.control:
            await self.control.check_cancelled()

    async def _invoke(self, prompt, images, mime, system_prompt):
        """经现有适配器发送文本与有序图像。"""
        from langchain_core.messages import HumanMessage, SystemMessage

        parts = [{"type": "text", "text": prompt}]
        parts.extend({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"}} for data in images)
        user_message = HumanMessage(content=parts if images or system_prompt is None else prompt)
        messages = [SystemMessage(content=system_prompt), user_message] if system_prompt is not None else [user_message]
        return await self.model.call(messages, stream=False)

    async def _connection(self):
        """共享准入不可用时拒绝新增请求，不退化为无约束并发。"""
        if self._redis is None:
            try:
                from yuxi.storage.redis import get_async_redis_client

                async with asyncio.timeout(3):
                    self._redis = await get_async_redis_client()
            except Exception as exc:
                raise PdfModelUnavailable("共享模型准入不可用") from exc
        return self._redis

    async def _renew(self, client, token):
        """等待中的请求定期续租，失权时取消本地调用并停止发布。"""
        while True:
            await asyncio.sleep(10)
            await self._check()
            try:
                async with asyncio.timeout(3):
                    live = await client.eval(RENEW, 1, self._gate, token)
            except Exception as exc:
                raise PdfModelUnavailable("共享模型租约续期失败") from exc
            if not live:
                raise PdfModelUnavailable("共享模型租约已丢失")

    def _configuration(self):
        """模型完整配置仅存摘要，端点模型共同限定并发键。"""
        if self._fingerprint is None:
            from yuxi.models.providers.cache import model_cache

            info = model_cache.get_model_info(self.model_spec)
            if info is None:
                raise PdfModelUnavailable("模型未配置")
            config = {name: getattr(info, name, None) for name in ("provider_id", "model_id", "base_url", "provider_type", "api_key", "headers", "extra", "request_body_overrides")}
            self._fingerprint = content_digest(config)
            self._gate = "pdf:model:slots:v2:" + content_digest([str(config["base_url"] or "").rstrip("/"), config["model_id"]])
        return self._fingerprint

    def _route(self, name):
        """文字、视觉与插图各自对账，保持同一请求只有一条归属。"""
        return self.routes.setdefault(name, {
            "calls": 0, "responses": 0, "failed_requests": 0, "cache_hits": 0,
            "unknown_usage_requests": 0, "queue_seconds": 0., "call_seconds": 0.,
            "known_tokens": {key: 0 for key in self.usage_totals},
        })

    def _record_usage(self, usage, route_stats):
        """每次实际响应仅累加一次，推理/缓存子项不再加进total。"""
        for key in self.usage_totals:
            if usage.get(key) is not None:
                self.usage_totals[key] += usage[key]
                route_stats["known_tokens"][key] += usage[key]
        if usage["status"] not in {"reported", "reported_parts"}:
            self.unknown_usage_requests += 1
            route_stats["unknown_usage_requests"] += 1



async def run_pdf_workers(items, operation, concurrency):
    """有界消费完整队列，取消或异常时等待所有已启动调用收敛。"""
    queue = deque(items)

    async def consume():
        """每项只取一次，不因批次数量或时长丢弃待办。"""
        while queue:
            await operation(queue.popleft())

    tasks = [asyncio.create_task(consume()) for _ in range(min(len(queue), concurrency))]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
