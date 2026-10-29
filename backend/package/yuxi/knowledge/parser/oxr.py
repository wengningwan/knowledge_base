"""OXR 异步表单协议适配器，不在健康检查中提交解析。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from yuxi.knowledge.parser.base import BaseDocumentProcessor, DocumentParserException
from yuxi.knowledge.parser.pdf_structure import normalize_oxr_result
from yuxi.knowledge.parser.result import ParsedDocument


class OXRParser(BaseDocumentProcessor):
    """以任务局部状态执行 OXR 上传、查询和恢复。"""

    service_name = "oxr_ocr"
    display_name = "OXR"
    supported_extensions = [".pdf", ".png", ".jpg", ".jpeg"]

    def __init__(self, server_url: str | None = None, api_key: str | None = None):
        """保存服务配置，免费接口使用占位密钥。"""
        self.server_url = str(server_url if server_url is not None else os.getenv("OXR_API_URI") or "").strip().rstrip("/")
        parsed = urlsplit(self.server_url)
        if self.server_url and (parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password):
            raise ValueError("OXR 地址必须是无内嵌凭据的 HTTP(S) URL")
        if parsed.query or parsed.fragment:
            raise ValueError("OXR 基地址不能包含查询参数或片段")
        self.api_key = api_key or os.getenv("OXR_API_KEY") or "dummy"

    def check_health(self) -> dict:
        """仅说明配置已提供，不宣称服务或解析能力经过实测。"""
        if not self.server_url:
            return {"status": "unconfigured", "message": "请在 OCR 配置中填写 OXR API Base URL，或设置 OXR_API_URI", "details": {"tested": False}}
        return {"status": "configured", "message": "OXR 地址已配置，尚未实测", "details": {"tested": False}}

    def process_file(self, file_path: str, params: dict | None = None) -> str:
        """为既有同步入口返回 Markdown，异步调用者应使用结果接口。"""
        return asyncio.run(self.process_file_result(file_path, params)).markdown

    async def process_file_result(self, file_path: str, params: dict | None = None, control=None) -> ParsedDocument:
        """在有界期限内解析，并通过控制对象恢复同一源文件的远端任务。"""
        if not self.server_url:
            raise self._error("请在 OCR 配置中填写 OXR API Base URL，或设置 OXR_API_URI", "missing_server_url")
        path = Path(file_path)
        if not self.supports_file_type(path.suffix):
            raise self._error("不支持的文件类型", "unsupported_file_type")
        options = params or {}
        max_wait = float(options.get("max_wait_seconds", 1800))
        if not 0 < max_wait <= 1800:
            raise self._error("max_wait_seconds 必须在 0 到 1800 秒之间", "invalid_params")
        if control is not None:
            await control.check_cancelled()
            max_wait = min(max_wait, float(control.remaining_seconds()) - 30)
        if max_wait <= 0:
            raise self._error("任务剩余时间不足，需保留 30 秒处理结果", "deadline_exceeded")
        deadline = time.monotonic() + max_wait
        try:
            async with asyncio.timeout(max_wait):
                await self._check(control)
                try:
                    source_hash = await asyncio.to_thread(self._hash_source, path)
                except OSError as exc:
                    raise self._error("源文件无法读取", "file_unreadable") from exc
                finally:
                    await self._check(control)

                fingerprint = hashlib.sha256(self.server_url.encode()).hexdigest()
                checkpoint = await self._checkpoint(control)
                remote = checkpoint.get("oxr", {})
                if not isinstance(remote, dict):
                    remote = {}
                remote_task_id = remote.get("remote_task_id")
                can_resume = (
                    isinstance(remote_task_id, str)
                    and bool(remote_task_id)
                    and remote.get("terminal_status") != "failed"
                    and remote.get("source_hash") == source_hash
                    and remote.get("server_url_fingerprint") == fingerprint
                )
                async with httpx.AsyncClient(follow_redirects=False) as client:
                    if not can_resume:
                        await self._check(control)
                        with path.open("rb") as source:
                            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                            files = [("file", (path.name, source, mime))]
                            data = {
                                "api_key": self.api_key,
                                "output_formats": ["markdown", "json"],
                                "element_formats": json.dumps(
                                    {"image": "base64", "formula": "latex", "table": "html", "cs": "image"}
                                ),
                                "feature_config": json.dumps({"keep_header_footer": False}),
                            }
                            body = await self._request(client, "/parse/async", data, files, deadline, control)
                        submitted = body.get("data")
                        remote_task_id = submitted.get("task_id") if isinstance(submitted, dict) else None
                        if not isinstance(remote_task_id, str) or not remote_task_id or len(remote_task_id) > 512:
                            raise self._error("OXR 提交响应缺少有效 task_id", "invalid_response")
                        checkpoint["oxr"] = {
                            "remote_task_id": remote_task_id,
                            "source_hash": source_hash,
                            "server_url_fingerprint": fingerprint,
                        }
                        if control is not None:
                            # 已知提交编号先落地；取消只阻止后续查询和发布。
                            save = asyncio.create_task(control.save_checkpoint(checkpoint))
                            try:
                                await asyncio.wait_for(asyncio.shield(save), timeout=10)
                            except asyncio.CancelledError:
                                # worker.cancel()也必须等待已知提交编号的短事务，再传播取消。
                                await asyncio.wait_for(asyncio.shield(save), timeout=10)
                                raise
                            finally:
                                if not save.done():
                                    save.cancel()
                                    await asyncio.gather(save, return_exceptions=True)
                            await self._check(control)

                    failures = 0
                    while True:
                        try:
                            body = await self._request(
                                client,
                                "/parse/async_check",
                                {"task_id": remote_task_id, "api_key": self.api_key},
                                None,
                                deadline,
                                control,
                            )
                            failures = 0
                        except httpx.HTTPStatusError as exc:
                            failures += 1
                            retryable = exc.response.status_code == 429 or 500 <= exc.response.status_code < 600
                            if failures >= 3 or not retryable:
                                raise self._error(f"OXR 查询 HTTP {exc.response.status_code}", "query_http_error") from exc
                            await self._pause(deadline, control)
                            continue
                        except httpx.TransportError as exc:
                            raise self._error(f"OXR 查询连接失败: {type(exc).__name__}", "query_transport_error") from exc
                        data = body.get("data")
                        if not isinstance(data, dict):
                            raise self._error("OXR 查询缺少 data 对象", "invalid_response")
                        status = str(data.get("status", "")).lower()
                        if status == "success":
                            try:
                                result = normalize_oxr_result(body)
                            except (ValueError, TypeError) as exc:
                                raise self._error("OXR 成功响应缺少有效文档结构", "invalid_response") from exc
                            result.metadata.update({"parser": self.service_name, "source_hash": source_hash})
                            return result
                        if status == "failed":
                            if control is not None:
                                checkpoint["oxr"]["terminal_status"] = "failed"
                                await control.save_checkpoint(checkpoint)
                            detail = str(data.get("error") or body.get("message") or "未返回失败原因")
                            raise self._error(f"OXR 解析失败: {detail}", "remote_failed")
                        if status not in {"queuing", "processing"}:
                            raise self._error(f"OXR 返回未知状态: {status[:80]}", "invalid_status")
                        await self._pause(deadline, control)
        except TimeoutError as exc:
            raise self._error("OXR 等待超过本地任务期限，远端任务未取消", "deadline_exceeded") from exc
        except httpx.HTTPStatusError as exc:
            raise self._error(f"OXR 提交 HTTP {exc.response.status_code}，未自动重传", "submit_http_error") from exc
        except httpx.TransportError as exc:
            raise self._error(f"OXR 提交连接失败，未自动重传: {type(exc).__name__}", "submit_transport_error") from exc
        except OSError as exc:
            raise self._error("OXR 源文件在上传时无法读取", "file_unreadable") from exc

    async def _request(self, client, endpoint, data, files, deadline, control) -> dict:
        """在每次网络 I/O 前后检查取消，并严格核对 HTTP 和业务代码。"""
        await self._check(control)
        remaining = deadline - time.monotonic()
        if control is not None:
            remaining = min(remaining, float(control.remaining_seconds()) - 30)
        if remaining <= 0:
            raise TimeoutError
        response = await client.post(self.server_url + endpoint, data=data, files=files, timeout=min(60, remaining))
        if endpoint != "/parse/async":
            await self._check(control)
        response.raise_for_status()
        try:
            body = response.json()
        except ValueError as exc:
            raise self._error("OXR 响应不是 JSON", "invalid_response") from exc
        if not isinstance(body, dict) or body.get("code") != 0:
            code = body.get("code") if isinstance(body, dict) else None
            detail = str(body.get("message", "未返回错误详情")) if isinstance(body, dict) else "响应不是对象"
            raise self._error(f"OXR 业务错误 code={code}: {detail}", "remote_error")
        return body

    async def _pause(self, deadline, control) -> None:
        """以三秒间隔轮询，期限不足时停止等待。"""
        await self._check(control)
        remaining = deadline - time.monotonic()
        if control is not None:
            remaining = min(remaining, float(control.remaining_seconds()) - 30)
        if remaining <= 3:
            raise TimeoutError
        await asyncio.sleep(3)
        await self._check(control)

    async def _checkpoint(self, control) -> dict:
        """读取任务所属的 checkpoint，不在解析器实例缓存任务状态。"""
        if control is None:
            return {}
        await self._check(control)
        try:
            checkpoint = await control.load_checkpoint()
        finally:
            await self._check(control)
        return dict(checkpoint or {})

    async def _check(self, control) -> None:
        """传播本地取消信号，不尝试不存在的远端取消接口。"""
        if control is not None:
            await control.check_cancelled()

    def _error(self, message, code) -> DocumentParserException:
        """为错误增加稳定服务标识，同时剔除当前配置密钥。"""
        if self.api_key and self.api_key != "dummy":
            message = message.replace(self.api_key, "[redacted]")
        return DocumentParserException(message, self.service_name, code)

    @staticmethod
    def _hash_source(path: Path) -> str:
        """分块读取源文件摘要，恢复时避免把不同文件绑定同一远端任务。"""
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
