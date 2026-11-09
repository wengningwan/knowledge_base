"""在原文入库后自动抽取实验草稿，并提供权限受控的读取与重试。"""

from __future__ import annotations

import asyncio

from yuxi.knowledge.chunking.source import source_sha256
from yuxi.permissions import ResourcePermission
from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository
from yuxi.repositories.knowledge_structure_repository import (
    KnowledgeStructureRepository,
    StructureConflictError,
)
from yuxi.repositories.task_repository import TaskRepository
from yuxi.services.task_service import TaskContext, tasker
from yuxi.storage.postgres.manager import pg_manager
from yuxi.utils.logging_config import logger

TASK_TYPE = "knowledge_structure"


async def finish_index_with_structure(
    *, kb_id, file_id, operator_id, markdown_file, source_hash, data,
    processing_task_id=None, processing_owner=None,
):
    """同事务发布原文 indexed 与结构化任务意图，事务提交后才投递。"""
    repo = KnowledgeStructureRepository()
    published_file = None
    structure_task = None

    async def finish(session, task):
        """复用 owning Task 行锁，随后取得文件行锁。"""
        nonlocal published_file, structure_task
        if task is not None and task.cancel_requested:
            raise asyncio.CancelledError("原文入库已取消")
        published_file, kb = await repo.finish_index(
            session, kb_id=kb_id, file_id=file_id, uid=operator_id, markdown_file=markdown_file,
            data=data, task_id=processing_task_id, owner=processing_owner,
        )
        structure_task = await _create_structure_intent(
            session, file=published_file, model_spec=kb.llm_model_spec,
            source_hash=source_hash, operator_id=operator_id,
        )

    if processing_task_id is not None and processing_owner is not None:
        await TaskContext(processing_task_id, processing_owner).run_owned_transaction(finish)
    elif processing_task_id is None and processing_owner is None:
        async with pg_manager.get_async_session_context() as session:
            await finish(session, None)
    else:
        raise ValueError("入库任务缺少完整执行身份")
    if structure_task is not None:
        await _publish_structure_task(structure_task)
    return published_file


async def read_experiment_cards(kb_id, file_id, *, uid):
    """读取当前文档草稿及任务状态，旧正文版本不作为当前卡片返回。"""
    repo = KnowledgeStructureRepository()
    async with KnowledgeFileRepository().lock_file_operation(kb_id, file_id):
        async with pg_manager.get_async_session_context() as session:
            file, _kb = await repo.require_file(
                session, kb_id=kb_id, file_id=file_id, uid=uid, permission=ResourcePermission.READ,
            )
            revision = await repo.latest(session, kb_id=kb_id, file_id=file_id)
            if revision is None:
                return {
                    "status": "not_generated", "cards": [], "evidence": [],
                    "message": "Paper 分块入库成功后自动生成实验草稿；已有文档可重新入库。",
                }
            result = {
                "status": revision.status, "revision_id": revision.revision_id,
                "task_id": revision.task_id, "source_sha256": revision.source_sha256,
                "model_spec": revision.model_spec, "coverage": revision.coverage,
                "message": revision.error_message or "", "cards": [], "evidence": [],
                "review_status": "machine_only",
            }
            if file.markdown_file != revision.markdown_file:
                result.update(status="stale", message="正文已变化，请重新入库生成当前版本实验草稿。")
                return result
            markdown = await _read_structure_markdown(revision.markdown_file, kb_id=kb_id, file_id=file_id)
            if source_sha256(markdown) != revision.source_sha256:
                result.update(status="stale", message="正文内容与抽取版本不一致，请重新入库。")
                return result
            if revision.status == "draft":
                result["cards"], result["evidence"] = await repo.read_result(session, revision)
                result["message"] = "机器生成的实验草稿，尚未经人工核对，不参与正式问答。"
            elif revision.status == "pending":
                task = await TaskRepository().get_by_id(revision.task_id)
                if task is None:
                    result.update(status="failed", message="实验任务记录不可用，请重试。")
                else:
                    result.update(status=task.status, progress=task.progress, message=task.message or "等待生成实验草稿")
            return result


async def retry_experiment_cards(kb_id, file_id, *, uid, revision_id, expected_source_sha256):
    """只对当前失败或取消版本重试，拒绝并发页面覆盖新任务。"""
    repo = KnowledgeStructureRepository()
    async with KnowledgeFileRepository().lock_file_operation(kb_id, file_id):
        async with pg_manager.get_async_session_context() as session:
            file, kb = await repo.require_file(
                session, kb_id=kb_id, file_id=file_id, uid=uid, permission=ResourcePermission.MANAGE, lock=True,
            )
            previous = await repo.latest(session, kb_id=kb_id, file_id=file_id)
            if (
                previous is None or previous.revision_id != revision_id
                or previous.source_sha256 != expected_source_sha256
                or previous.markdown_file != file.markdown_file
                or previous.status not in {"failed", "cancelled"}
            ):
                raise StructureConflictError("实验草稿或正文已变化，请刷新后重试")
            if file.status != "indexed":
                raise StructureConflictError("请等待原文入库成功后重试")
            markdown = await _read_structure_markdown(file.markdown_file, kb_id=kb_id, file_id=file_id)
            if source_sha256(markdown) != expected_source_sha256:
                raise StructureConflictError("正文内容已变化，请重新入库")
            task = await _create_structure_intent(
                session, file=file, model_spec=kb.llm_model_spec, source_hash=expected_source_sha256,
                operator_id=uid, force=True,
            )
        await _publish_structure_task(task)
    return {"status": "pending", "task_id": task.id}


async def run_knowledge_structure(context):
    """从冻结正文分批抽取，只有有效 lease 可以保存完整草稿。"""
    from yuxi.knowledge.structure import extract_experiment_cards

    repo = KnowledgeStructureRepository()
    control = StructureTaskControl(context)
    await control.check_cancelled()
    async with pg_manager.get_async_session_context() as session:
        revision, _file = await repo.require_input(
            session, task_id=context.task_id, uid=context.payload["operator_id"],
        )
    if not revision.model_spec:
        raise ValueError("请在知识库详情 → 配置 → 基础信息 → 知识库聊天模型中选择本地模型并保存，再重试实验卡片任务")
    markdown = await _read_structure_markdown(
        revision.markdown_file, kb_id=revision.kb_id, file_id=revision.file_id,
    )
    if source_sha256(markdown) != revision.source_sha256:
        raise ValueError("冻结正文指纹不匹配，停止实验抽取")
    parse_assets = await _read_structure_assets(revision)
    if (parse_assets or {}).get("metadata", {}).get("review_status") in {"needs_review", "incomplete"}:
        raise ValueError("正文仍有待校对内容，请先完成校对后重新入库")
    result = await extract_experiment_cards(
        markdown, model_spec=revision.model_spec, parse_assets=parse_assets, control=control,
    )
    await control.check_cancelled()
    # 使用现有文件操作锁与 Task lease 事务，排除正文发布与迟到写入。
    async with KnowledgeFileRepository().lock_file_operation(revision.kb_id, revision.file_id, control):
        async def save(session, task):
            """取消标志与证据写入在同一 owning transaction 裁决。"""
            if task.cancel_requested:
                raise asyncio.CancelledError("实验抽取已取消")
            await repo.save_result(session, task_id=context.task_id, uid=context.payload["operator_id"], result=result)

        await context.run_owned_transaction(save)
    return {"revision_id": revision.revision_id, "card_count": len(result["cards"])}


async def finish_knowledge_structure(session, task, result):
    """复用 Task success 事务发布机器草稿。"""
    await KnowledgeStructureRepository().finish_success(session, task, result)


async def fail_knowledge_structure(session, task, error):
    """复用 Task failure 事务记录取消、失联或抽取失败。"""
    await KnowledgeStructureRepository().finish_failure(session, task, error)


class StructureTaskControl:
    """让领域抽取在模型调用之间核对取消、权限和正文版本。"""

    def __init__(self, context):
        """保留 durable Task 的唯一执行上下文。"""
        self.context = context

    async def check_cancelled(self):
        """撤权、正文变化或 Task 失权后停止抽取。"""
        await self.context.raise_if_cancelled()
        async with pg_manager.get_async_session_context() as session:
            await KnowledgeStructureRepository().require_input(
                session, task_id=self.context.task_id, uid=self.context.payload["operator_id"],
            )

    async def set_progress(self, progress, message):
        """将阶段进度交给现有 Task 状态拥有者。"""
        await self.context.set_progress(progress, message)


async def _create_structure_intent(session, *, file, model_spec, source_hash, operator_id, force=False):
    """在文件行锁内复用同版本结果，或原子创建新 Task 和草稿。"""
    repo = KnowledgeStructureRepository()
    previous = await repo.latest(session, kb_id=file.kb_id, file_id=file.file_id)
    if (
        not force and previous is not None and previous.markdown_file == file.markdown_file
        and previous.source_sha256 == source_hash and previous.model_spec == model_spec
        and previous.status in {"pending", "draft"}
    ):
        return None
    task = await tasker.create_in_session(
        session, name=f"实验卡片：{file.filename}", task_type=TASK_TYPE,
        payload={"kb_id": file.kb_id, "file_id": file.file_id, "operator_id": operator_id},
    )
    await repo.create_revision(
        session, task_id=task.id, file=file, model_spec=model_spec,
        source_sha256=source_hash, operator_id=operator_id,
    )
    return task


async def _publish_structure_task(task):
    """投递失败保留 pending 意图，由已有 publisher 补发。"""
    try:
        await tasker.publish(task)
    except Exception as exc:
        logger.warning("实验任务投递待补发 task_id=%s error=%s", task.id, type(exc).__name__)


async def _read_structure_markdown(pointer, *, kb_id, file_id):
    """仅读取当前文档解析目录的 MinIO 正文，不访问任意外部 URL。"""
    from yuxi.knowledge.utils.kb_utils import parse_minio_url
    from yuxi.storage.minio import get_minio_client

    client = get_minio_client()
    bucket, name = parse_minio_url(pointer)
    prefix = f"{kb_id}/parsed/{file_id}/"
    suffix = name.removeprefix(prefix)
    if (
        bucket != client.KB_BUCKETS["parsed"] or not name.startswith(prefix)
        or len(suffix.split("/")) != 2 or any(part in {"", ".", ".."} for part in suffix.split("/"))
        or "\\" in name or not name.endswith(".md")
    ):
        raise ValueError("正文对象不属于当前文档解析目录，请重新解析")
    return (await client.adownload_file(bucket, name)).decode("utf-8")


async def _read_structure_assets(revision):
    """附属数据必须属于冻结正文的同次解析版本。"""
    import json

    from yuxi.knowledge.utils.kb_utils import parse_minio_url
    from yuxi.storage.minio import get_minio_client

    if not revision.parse_assets_file:
        return None
    client = get_minio_client()
    bucket, name = parse_minio_url(revision.parse_assets_file)
    markdown_bucket, markdown_name = parse_minio_url(revision.markdown_file)
    if (
        bucket != markdown_bucket or bucket != client.KB_BUCKETS["parsed"]
        or name.rsplit("/", 1)[0] != markdown_name.rsplit("/", 1)[0]
        or not name.endswith(".json") or "\\" in name or ".." in name.split("/")
    ):
        raise ValueError("实验输入的正文与附属数据版本不一致")
    result = json.loads((await client.adownload_file(bucket, name)).decode("utf-8"))
    if not isinstance(result, dict) or result.get("schema_version") != 1 or any(
        result.get(key) != value for key, value in (
            ("kb_id", revision.kb_id), ("file_id", revision.file_id),
            ("attempt_id", markdown_name.rsplit("/", 2)[-2]),
        )
    ):
        raise ValueError("实验输入附属数据归属不匹配")
    return result
