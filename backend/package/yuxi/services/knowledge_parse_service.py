"""知识文件解析尝试的控制、不可变产物与对象归属。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from typing import Any

from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository


class FileParseControl:
    """把解析I/O连接到现有任务取消、截止时间和同事务checkpoint。"""

    def __init__(self, kb_id, file_id, attempt_id, task_context=None, runtime=None, task_id=None, owner=None):
        self.kb_id = kb_id
        self.file_id = file_id
        self.attempt_id = attempt_id
        self.context = task_context
        self.task_id = task_id or getattr(task_context, "task_id", None)
        self.owner = owner or getattr(task_context, "worker_id", None)
        self.runtime = dict(runtime or {})
        self.file_claimed = False
        self.deadline = getattr(task_context, "deadline", None) or time.monotonic() + 1800

    async def check_cancelled(self) -> None:
        """所有网络、模型和发布动作前核查当前任务控制。"""
        if self.context is not None:
            await self.context.raise_if_cancelled()
        elif self.task_id and self.owner:
            from yuxi.repositories.task_repository import TaskRepository

            live, cancelled = await TaskRepository().check_control(self.task_id, worker_id=self.owner)
            if not live or cancelled:
                raise asyncio.CancelledError("Task was cancelled or lease lost")
        if self.file_claimed:
            record = await KnowledgeFileRepository().get_by_file_id(self.file_id)
            if (record is None or record.kb_id != self.kb_id or record.status != "parsing"
                    or record.processing_task_id != self.task_id or record.processing_owner != self.owner):
                raise asyncio.CancelledError("File processing owner was lost")
        if self.remaining_seconds() <= 0:
            raise TimeoutError("文档解析超过任务剩余时间")

    def remaining_seconds(self) -> float:
        """返回本次执行剩余的单调时钟时间。"""
        return max(0.0, self.deadline - time.monotonic())

    async def load_checkpoint(self) -> dict:
        """读取仅属于本文件的恢复点。"""
        await self.check_cancelled()
        return dict(self.runtime)

    async def save_checkpoint(self, data: dict) -> None:
        """在现有任务lease事务内持久化远程ID。"""
        # 只记录已发生的远端副作用，取消不能丢掉已知任务编号；lease仍需有效。
        repo = KnowledgeFileRepository()
        if self.task_id and self.owner:
            async def operation(session, task):
                record = await repo.update_parse_in_session(
                    session, kb_id=self.kb_id, file_id=self.file_id,
                    task_id=self.task_id, owner=self.owner, data={"parse_runtime": data},
                )
                if record is None:
                    raise asyncio.CancelledError("File processing owner was lost")
            if self.context is not None:
                await self.context.run_owned_transaction(operation)
            else:
                from yuxi.repositories.task_repository import TaskRepository
                if not await TaskRepository().run_owned_transaction(
                    self.task_id, worker_id=self.owner, operation=operation,
                ):
                    raise asyncio.CancelledError("Task lease was lost")
        else:
            await self.write_fields({"parse_runtime": data})
        self.runtime = dict(data)

    async def write_fields(self, data: dict) -> None:
        """原子校验取消、任务lease和文件owner后写入。"""
        await self.check_cancelled()
        repo = KnowledgeFileRepository()
        if self.context is None:
            record = await repo.update_fields_if_status(
                kb_id=self.kb_id, file_id=self.file_id, allowed_statuses={"parsing"}, data=data,
                processing_task_id=self.task_id, processing_owner=self.owner,
            )
            if record is None:
                raise asyncio.CancelledError("File processing owner was lost")
            return

        async def operation(session, task):
            if task.cancel_requested:
                raise asyncio.CancelledError("Task was cancelled")
            record = await repo.update_parse_in_session(
                session, kb_id=self.kb_id, file_id=self.file_id,
                task_id=self.context.task_id, owner=self.context.worker_id, data=data,
            )
            if record is None:
                raise asyncio.CancelledError("File processing owner was lost")

        await self.context.run_owned_transaction(operation)


async def save_parse_bundle(kb_id: str, file_id: str, attempt_id: str, result, control) -> tuple[str, str]:
    """先写全同次解析对象，再把待发布指针交给数据库事务。"""
    from yuxi.knowledge.parser.pdf_structure import finalize_document
    from yuxi.knowledge.utils.kb_utils import build_kb_image_proxy_url
    from yuxi.storage.minio import get_minio_client

    _validate_scope(kb_id, file_id, attempt_id)
    client = get_minio_client()
    for name in ("images", "parsed"):
        await asyncio.to_thread(client.ensure_bucket_exists, client.KB_BUCKETS[name])
    urls = {}
    for asset_id, asset in result.assets.items():
        await control.check_cancelled()
        data = asset.get("data")
        if not isinstance(data, bytes) or not data:
            raise ValueError(f"原图资源不完整：{asset_id}")
        mime = asset.get("mime", "image/png")
        extension = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(mime)
        if extension is None:
            raise ValueError("不支持的解析图片格式")
        name = hashlib.sha256(str(asset_id).encode()).hexdigest()[:24]
        object_name = f"{kb_id}/kb-images/{file_id}/{attempt_id}/{name}.{extension}"
        await complete_storage_mutation(client.aupload_file(client.KB_BUCKETS["images"], object_name, data))
        urls[str(asset_id)] = build_kb_image_proxy_url(object_name)
    finalize_document(result, urls)
    result.metadata["attempt_id"] = attempt_id
    await control.check_cancelled()
    prefix = f"{kb_id}/parsed/{file_id}/{attempt_id}"
    markdown = await complete_storage_mutation(client.aupload_file(
        client.KB_BUCKETS["parsed"], f"{prefix}/document.md", result.markdown.encode("utf-8")
    ))
    sidecar = result.sidecar()
    sidecar.update(attempt_id=attempt_id, kb_id=kb_id, file_id=file_id)
    assets = await complete_storage_mutation(client.aupload_file(
        client.KB_BUCKETS["parsed"], f"{prefix}/assets.json",
        json.dumps(sidecar, ensure_ascii=False, allow_nan=False).encode("utf-8"),
    ))
    return markdown.url, assets.url


async def cleanup_unpublished_attempt(kb_id: str, file_id: str, attempt_id: str) -> None:
    """确认未发布后只清理本次尝试，无法确认时保留到文档删除。"""
    from yuxi.storage.minio import get_minio_client
    from yuxi.utils import logger

    _validate_scope(kb_id, file_id, attempt_id)
    try:
        record = await KnowledgeFileRepository().get_by_file_id(file_id)
        if record is not None and record.kb_id != kb_id:
            return
        if record is not None and f"/{file_id}/{attempt_id}/" in str(record.markdown_file or ""):
            return
        client = get_minio_client()
        await client.adelete_objects_by_prefix(
            client.KB_BUCKETS["parsed"], f"{kb_id}/parsed/{file_id}/{attempt_id}/"
        )
        await client.adelete_objects_by_prefix(
            client.KB_BUCKETS["images"], f"{kb_id}/kb-images/{file_id}/{attempt_id}/"
        )
    except Exception as exc:
        logger.warning("未发布解析对象保留待清理（%s）", type(exc).__name__)


async def delete_document_storage(kb_id: str, file_id: str, meta: dict[str, Any]) -> None:
    """删除该文档全部新旧attempt，任一存储错误保留元数据供重试。"""
    from yuxi.knowledge.utils.kb_utils import is_minio_url, parse_minio_url
    from yuxi.storage.minio import get_minio_client

    _validate_scope(kb_id, file_id)
    if meta.get("is_folder"):
        return
    client = get_minio_client()
    await client.adelete_objects_by_prefix(client.KB_BUCKETS["parsed"], f"{kb_id}/parsed/{file_id}/")
    await client.adelete_objects_by_prefix(client.KB_BUCKETS["images"], f"{kb_id}/kb-images/{file_id}/")
    await client.adelete_file(client.KB_BUCKETS["parsed"], f"{kb_id}/parsed/{file_id}.md")
    await client.adelete_file(client.KB_BUCKETS["parsed"], f"{kb_id}/preview/{file_id}.pdf")
    original = meta.get("minio_url") or meta.get("path")
    if original and is_minio_url(original):
        bucket, name = parse_minio_url(original)
        await client.adelete_file(bucket, name)


async def complete_storage_mutation(awaitable):
    """取消调用者时等待已启动的外部副作用，随后传播取消。"""
    pending = asyncio.ensure_future(awaitable)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(pending)
            break
        except asyncio.CancelledError:
            if pending.cancelled():
                raise
            cancelled = True
        except Exception:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    if cancelled:
        raise asyncio.CancelledError
    return result


def _validate_scope(*parts: str) -> None:
    """对象前缀仅接受服务生成的知识库、文件和attempt标识。"""
    if any(not re.fullmatch(r"[A-Za-z0-9_-]+", str(part)) for part in parts):
        raise ValueError("无效的解析产物归属标识")
