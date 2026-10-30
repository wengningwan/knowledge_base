"""持久图片任务只发布同一正文版本的附属 JSON，正文可先下载和入库。"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import time
import uuid
from urllib.parse import unquote, urlsplit

from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository
from yuxi.repositories.task_repository import TaskRepository
from yuxi.services.knowledge_parse_service import complete_storage_mutation
from yuxi.utils.logging_config import logger

TASK_TYPE = "knowledge_figure_analysis"


def figure_task_intent(job_id, *, kb_id, file_id, attempt_id, markdown_file, assets_file, model_spec, filename):
    """提供与正文指针在同事务提交的待投递任务，不自行投递。"""
    return {"id": job_id, "data": {
        "name": f"插图解析：{filename}", "type": TASK_TYPE, "status": "pending", "progress": 0,
        "message": "正文已生成，等待独立图片解析", "handler_version": 1,
        "cancel_requested": 0, "attempt_count": 0,
        "payload": {"kb_id": kb_id, "file_id": file_id, "attempt_id": attempt_id,
                    "markdown_file": markdown_file, "assets_file": assets_file, "model_spec": model_spec, "filename": filename},
    }}


def figure_image_object(url: str, *, kb_id, file_id, attempt_id) -> str:
    """只从本文件本次解析的鉴权图片 URL 还原对象名，禁止外链与串文件。"""
    parts = urlsplit(str(url))
    prefix = f"/api/knowledge/databases/{kb_id}/images/"
    if parts.scheme or parts.netloc or parts.query or parts.fragment or not parts.path.startswith(prefix):
        raise ValueError("图片引用不属于本地鉴权代理")
    relative = unquote(parts.path[len(prefix):])
    if not relative.startswith(f"kb-images/{file_id}/{attempt_id}/") or "\\" in relative or any(item in {"", ".", ".."} for item in relative.split("/")):
        raise ValueError("图片对象不属于当前文档版本")
    return f"{kb_id}/{relative}"


class FigureTaskControl:
    """图片任务拥有自己的 lease，不依赖已经结束的 parsing 文件状态。"""

    def __init__(self, context):
        self.context = context
        self.task_id = context.task_id
        self.payload = context.payload
        self.kb_id = self.payload["kb_id"]
        self.file_id = self.payload["file_id"]
        self.filename = self.payload.get("filename", "")
        from yuxi.services.task_service import TASKER_DEFAULT_TIMEOUT_SECONDS

        self.deadline = context.deadline or time.monotonic() + TASKER_DEFAULT_TIMEOUT_SECONDS

    def remaining_seconds(self):
        """为附属文件上传和 CAS 预留阶段以外的时间。"""
        return max(0, self.deadline - time.monotonic())

    async def check_cancelled(self):
        """取消、删除、重解析发布或失去 lease 后禁止继续发布。"""
        await self.context.raise_if_cancelled()
        record = await KnowledgeFileRepository().get_by_file_id(self.file_id)
        if record is None or record.kb_id != self.kb_id or record.markdown_file != self.payload["markdown_file"]:
            raise asyncio.CancelledError("文档已删除或已发布另一正文版本")




async def run_knowledge_figure_analysis(context):
    """复用现有 durable worker；不写 markdown、blocks、reviews 或索引。"""
    from yuxi.knowledge.parser.result import ParsedDocument
    from yuxi.services.pdf_figure_analysis_service import analyze_document_figures
    from yuxi.storage.minio import get_minio_client

    payload = context.payload
    if any(not re.fullmatch(r"[A-Za-z0-9_-]+", str(payload.get(key, ""))) for key in ("kb_id", "file_id", "attempt_id")):
        raise ValueError("无效的图片任务文档归属")
    control = FigureTaskControl(context)
    await control.check_cancelled()
    client = get_minio_client()
    repo = KnowledgeFileRepository()
    record = await repo.get_by_file_id(control.file_id)
    pointer = record.parse_assets_file
    sidecar = await _read_sidecar(client, pointer, payload)
    stage = sidecar.get("metadata", {}).get("figure_analysis", {})
    if stage.get("task_id") != context.task_id:
        raise ValueError("附属快照未登记当前图片任务")
    result = ParsedDocument(figures=copy.deepcopy(sidecar.get("figures", [])), metadata=copy.deepcopy(sidecar.get("metadata", {})))
    result.metadata["figure_analysis"]["status"] = "running"
    await context.set_progress(5, "读取本次解析的原图")
    for figure in result.figures:
        await control.check_cancelled()
        if figure.get("analysis_status") == "completed" and figure.get("model") == payload["model_spec"]:
            continue
        try:
            name = figure_image_object(figure.get("image_url", ""), kb_id=control.kb_id, file_id=control.file_id, attempt_id=payload["attempt_id"])
            data = await client.adownload_file(client.KB_BUCKETS["images"], name)
            result.assets[figure["asset_id"]] = {"data": data, "mime": "image/png"}
        except Exception as exc:
            figure.update(analysis_status="failed", analysis="本次解析的原图不可读取")
            logger.warning(f"[pdf_figures] 原图读取失败 task_id={context.task_id} file_id={control.file_id} figure_id={figure.get('id')} error={type(exc).__name__}")
    await context.set_progress(15, "正文已就绪，独立解析插图")
    await analyze_document_figures(result, {"image_analysis_model": payload["model_spec"], "_background_figures": True}, control)
    await control.check_cancelled()
    await context.set_progress(90, "发布同版本图片说明")
    # 基于刚才校验的快照仅改图片和其状态；CAS 同时检查正文和旧附属指针。
    sidecar["figures"] = result.figures
    sidecar.setdefault("metadata", {})["figure_analysis"] = result.metadata["figure_analysis"]
    revision = uuid.uuid4().hex
    sidecar["metadata"]["figure_analysis"]["analysis_revision"] = revision
    name = f"{control.kb_id}/parsed/{control.file_id}/{payload['attempt_id']}/assets.figures.{context.task_id}.{revision}.json"
    published = False
    matched = False
    upload = None
    try:
        upload = await complete_storage_mutation(client.aupload_file(client.KB_BUCKETS["parsed"], name, json.dumps(sidecar, ensure_ascii=False, allow_nan=False).encode()))
        async with repo.lock_file_operation(control.kb_id, control.file_id, control):
            async def publish(session, task):
                """Task 行锁先于文件行锁，取消与 CAS 在同一事务裁决。"""
                nonlocal matched
                if task.cancel_requested:
                    raise asyncio.CancelledError("图片任务已取消")
                matched = await repo.publish_parse_assets_in_session(
                    session, kb_id=control.kb_id, file_id=control.file_id,
                    markdown_file=payload["markdown_file"], previous_assets_file=pointer, assets_file=upload.url,
                )
            await context.run_owned_transaction(publish)
            published = matched
        await context.set_result({"published": published, "attempt_id": payload["attempt_id"], "figure_status": result.metadata["figure_analysis"]["status"]})
        return {"published": published}
    finally:
        if not published:
            # commit 成功但 ACK/close 取消时，本地标志可能仍为 False；重读才能裁决删除。
            try:
                async with repo.lock_file_operation(control.kb_id, control.file_id):
                    current = await repo.get_by_file_id(control.file_id)
                    referenced = current is not None and (
                        current.kb_id != control.kb_id or (upload is not None and current.parse_assets_file == upload.url)
                    )
                    if not referenced:
                        await complete_storage_mutation(client.adelete_file(client.KB_BUCKETS["parsed"], name))
            except Exception as exc:
                logger.warning(f"[pdf_figures] 未确认的附属对象保留 task_id={context.task_id} error={type(exc).__name__}")


async def project_figure_task_status(data: dict, *, kb_id, file_id):
    """只读投影 durable 状态，使崩溃或取消的任务不在页签永久显示等待。"""
    stage = data.get("metadata", {}).get("figure_analysis", {})
    if stage.get("status") not in {"pending", "running"} or not stage.get("task_id"):
        return
    task = await TaskRepository().get_by_id(stage["task_id"])
    if task is None:
        stage["status"] = "failed"
        stage["reason"] = "图片任务记录不可用"
        return
    payload = task.payload or {}
    if task.type != TASK_TYPE or any(payload.get(key) != value for key, value in (("kb_id", kb_id), ("file_id", file_id), ("attempt_id", data.get("attempt_id")))):
        raise ValueError("图片任务与文档版本不匹配")
    status = {"pending": "pending", "running": "running", "failed": "failed", "cancelled": "cancelled", "success": "needs_review"}.get(task.status, "failed")
    stage["status"] = status
    if status in {"failed", "cancelled", "needs_review"}:
        for figure in data.get("figures", []):
            if figure.get("analysis_status") in {"pending", "running"}:
                figure.update(analysis_status="not_checked", analysis="图片任务未发布解释，原图已保留")


async def _read_sidecar(client, pointer, payload):
    """校验真实对象位置和附属快照归属，主机 URL 不触发 HTTP 请求。"""
    from yuxi.knowledge.utils.kb_utils import parse_minio_url

    bucket, name = parse_minio_url(pointer)
    prefix = f"{payload['kb_id']}/parsed/{payload['file_id']}/{payload['attempt_id']}/"
    if bucket != client.KB_BUCKETS["parsed"] or not name.startswith(prefix) or "/" in name[len(prefix):] or not name.endswith(".json"):
        raise ValueError("附属对象不属于当前解析版本")
    data = json.loads((await client.adownload_file(bucket, name)).decode("utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1 or any(data.get(key) != payload[key] for key in ("kb_id", "file_id", "attempt_id")):
        raise ValueError("附属快照归属无效")
    return data
