"""在原文入库后抽取实验草稿，管理读取、审核及重试。"""

from __future__ import annotations

import asyncio
import uuid

from yuxi.knowledge.adsorption_contract import STRUCTURE_PROMPT_VERSION, STRUCTURE_VERSION, output_schema
from yuxi.knowledge.chunking.source import source_sha256
from yuxi.knowledge.experiment_corrections import experiment_correction_tasks
from yuxi.knowledge.experiment_review import (
    ExperimentReviewRequest,
    experiment_review_records,
    validate_experiment_review,
)
from yuxi.permissions import ResourcePermission
from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository
from yuxi.repositories.knowledge_structure_repository import (
    KnowledgeStructureRepository,
    StructureConflictError,
    StructureNotFoundError,
)
from yuxi.repositories.task_repository import TaskRepository
from yuxi.services.task_service import TaskContext, tasker
from yuxi.storage.postgres.manager import pg_manager
from yuxi.utils.datetime_utils import utc_now
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
                    "message": (
                        "Paper 分块入库成功后自动生成实验草稿；已有文档可重新入库。"
                        if _is_pdf_file(file) else "规范实验 JSON 需要可核对页码的 PDF 原件。"
                    ),
                }
            coverage = dict(revision.coverage or {})
            result = {
                "status": revision.status, "revision_id": revision.revision_id,
                "task_id": revision.task_id, "source_sha256": revision.source_sha256,
                "model_spec": revision.model_spec,
                "coverage": {
                    key: value for key, value in coverage.items()
                    if key not in {"manual_review", "review_candidates", "canonical", "correction_tasks"}
                },
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
                result["canonical_available"] = revision.schema_version == STRUCTURE_VERSION and "canonical" in coverage
                result["review"] = {
                    "version": coverage.get("manual_review", {}).get("version", 0),
                    "records": list(experiment_review_records(result["cards"], coverage).values()),
                }
                result["review"]["tasks"] = experiment_correction_tasks(
                    coverage, result["evidence"], {item["record_id"]: item for item in result["review"]["records"]},
                )
                if coverage.get("partial_result"):
                    count = sum(not item["resolution"] for item in result["review"]["tasks"])
                    result["message"] = (
                        f"已保留可用数据，还有 {count} 项待人工校对。"
                        if count else "已保留可用数据。提取异常可在人工校对页的技术说明中查看。"
                    )
                elif not result["cards"]:
                    result["message"] = (
                        "已生成可下载的结构化 JSON，当前结果未归入样品卡。"
                        if result["canonical_available"] else "本次没有组装出带明确出处的实验记录。"
                    )
                elif (revision.coverage or {}).get("source_coverage") == "partial":
                    result["message"] = "已保留完成组装的实验记录，部分内容仍缺少完整的取值或归属证据。"
                else:
                    result["message"] = "实验记录已自动组装，可逐项查看原文出处。"
            elif revision.status == "pending":
                task = await TaskRepository().get_by_id(revision.task_id)
                if task is None:
                    result.update(status="failed", message="实验任务记录不可用，请重试。")
                else:
                    result.update(status=task.status, progress=task.progress, message=task.message or "等待生成实验草稿")
            return result


async def read_experiment_json(kb_id, file_id, *, uid, revision_id):
    """只下载与当前冻结正文一致、已完成发布的规范 JSON。"""
    repo = KnowledgeStructureRepository()
    async with KnowledgeFileRepository().lock_file_operation(kb_id, file_id):
        async with pg_manager.get_async_session_context() as session:
            file, _kb = await repo.require_file(
                session, kb_id=kb_id, file_id=file_id, uid=uid, permission=ResourcePermission.READ,
            )
            revision = await repo.latest(session, kb_id=kb_id, file_id=file_id)
            if revision is None or revision.status != "draft" or revision.schema_version != STRUCTURE_VERSION:
                raise StructureNotFoundError("当前文档还没有可下载的规范 JSON")
            if revision.revision_id != revision_id or file.markdown_file != revision.markdown_file:
                raise StructureConflictError("实验草稿或正文已更新，请刷新后下载")
            markdown = await _read_structure_markdown(revision.markdown_file, kb_id=kb_id, file_id=file_id)
            if source_sha256(markdown) != revision.source_sha256:
                raise StructureConflictError("正文内容已变化，请重新入库")
            canonical = (revision.coverage or {}).get("canonical")
            if not isinstance(canonical, dict):
                raise StructureNotFoundError("当前文档还没有可下载的规范 JSON")
            return {field: canonical[field] for field in output_schema()["required"]}


async def save_experiment_review(kb_id, file_id, *, uid, change: ExperimentReviewRequest):
    """在文档锁与当前正文版本内保存修改或补录。"""
    repo = KnowledgeStructureRepository()
    async with KnowledgeFileRepository().lock_file_operation(kb_id, file_id):
        async with pg_manager.get_async_session_context() as session:
            file, _kb = await repo.require_file(
                session, kb_id=kb_id, file_id=file_id, uid=uid, permission=ResourcePermission.MANAGE, lock=True,
            )
            revision = await repo.latest(session, kb_id=kb_id, file_id=file_id)
            if (
                revision is None or revision.status != "draft" or revision.revision_id != change.revision_id
                or revision.source_sha256 != change.source_sha256 or file.markdown_file != revision.markdown_file
                or file.status not in {"indexed", "done"}
            ):
                raise StructureConflictError("实验草稿或正文已更新，请重新打开页面")
            coverage = dict(revision.coverage or {})
            review = coverage.get("manual_review", {})
            if change.version != review.get("version", 0):
                raise StructureConflictError("记录已被修改，请重新打开页面")
            markdown = await _read_structure_markdown(revision.markdown_file, kb_id=kb_id, file_id=file_id)
            if source_sha256(markdown) != change.source_sha256:
                raise StructureConflictError("正文内容已变化，请重新入库")
            cards, evidence = await repo.read_result(session, revision)
            records = experiment_review_records(cards, coverage)
            task = None
            if change.task_id is not None:
                task = next((item for item in experiment_correction_tasks(coverage, evidence, records)
                             if item["task_id"] == change.task_id), None)
                if task is None:
                    raise ValueError("该校对任务不属于当前草稿")
                if change.action not in {"save", "resolve"}:
                    raise ValueError("请选择保存校对结果或处理说明")
                if change.record_id is not None and change.record_id != task.get("record_id"):
                    raise ValueError("校对任务与目标记录不一致")
            record_id = task.get("record_id") if task else change.record_id
            fragment_id = task.get("fragment_id") if task else change.fragment_id
            if task is None and fragment_id is not None and record_id is not None:
                raise ValueError("该原文片段不能用于当前记录")
            if record_id is not None and record_id not in records:
                raise ValueError("该实验记录不属于当前草稿")
            if fragment_id is not None and record_id is None:
                if not any(
                    fact["fact_id"] == fragment_id for fact in coverage.get("unassigned_facts", [])
                ):
                    raise ValueError("该原文片段不能用于当前记录")
                if any(record.get("fragment_id") == fragment_id for record in records.values()):
                    raise StructureConflictError("该原文片段已整理，请重新打开页面")
            if task is None and (change.resolution is not None or change.action == "resolve"):
                raise ValueError("请指定当前校对任务")
            record = None
            resolution_evidence = None
            if change.action == "resolve":
                if change.resolution is None:
                    raise ValueError("请选择处理说明")
                if change.resolution == "unreported" and task["kind"] != "missing":
                    raise ValueError("仅缺失资料可以标记为原文未提供")
                if change.resolution == "checked":
                    resolution_evidence = (
                        change.fields.evidence_ids if change.fields else task["fields"]["evidence_ids"]
                    )
                    known_evidence = {item["evidence_id"] for item in evidence}
                    if task["kind"] != "source" or not resolution_evidence:
                        raise ValueError("请先定位疑点的原文出处")
                    if any(ref not in known_evidence for ref in resolution_evidence):
                        raise ValueError("所选出处不属于当前正文草稿")
                if record_id is not None and change.resolution != "checked":
                    record = {**records[record_id], "status": "excluded"}
            elif change.action == "exclude":
                if record_id is None:
                    raise ValueError("请指定要排除的记录")
                record = {**records[record_id], "status": "excluded"}
            else:
                if change.fields is None:
                    raise ValueError("请填写实验字段")
                editing = change.action == "save" and (record_id is not None or task is not None)
                validate_experiment_review(
                    change.fields, evidence, confirm=change.action == "confirm", edit=editing,
                )
                if task is not None and not change.fields.evidence_ids:
                    raise ValueError("请为校对结果选择原文出处")
                if task is not None and task["kind"] == "source" and record_id is None:
                    raise ValueError("原文疑点请记录核对结论；已有数据可在样品结果中直接修改")
                record_id = record_id or f"manual-{uuid.uuid4().hex}"
                record = {
                    **change.fields.model_dump(), "record_id": record_id,
                    "status": "edited" if editing else "confirmed" if change.action == "confirm" else "pending",
                }
                fragment_id = fragment_id or records.get(record_id, {}).get("fragment_id")
                if fragment_id is not None:
                    record["fragment_id"] = fragment_id
                if records.get(record_id, {}).get("canonical_id"):
                    record["canonical_id"] = records[record_id]["canonical_id"]
            version = change.version + 1
            items = dict(review.get("items", {}))
            if record is not None:
                items[record["record_id"]] = {"record": record, "operator_id": uid, "updated_at": utc_now().isoformat()}
                records[record["record_id"]] = record
            resolutions = dict(review.get("resolutions", {}))
            if task is not None:
                resolutions[task["task_id"]] = {
                    "outcome": "saved" if change.action == "save" else change.resolution,
                    "record_id": record["record_id"] if record else task.get("record_id"),
                    "evidence_ids": resolution_evidence,
                    "operator_id": uid, "updated_at": utc_now().isoformat(),
                }
            review = {"version": version, "items": items, "resolutions": resolutions}
            await repo.save_review(session, revision, review)
            tasks = experiment_correction_tasks({**coverage, "manual_review": review}, evidence, records)
    return {"version": version, "record": record, "tasks": tasks}


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
            if not _is_pdf_file(file):
                raise ValueError("规范实验 JSON 需要可核对页码的 PDF 原件")
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
    """从冻结正文分批抽取，只有有效 lease 可以保存草稿。"""
    from yuxi.knowledge.runtime import knowledge_base
    from yuxi.knowledge.structure import extract_adsorption_record

    repo = KnowledgeStructureRepository()
    control = StructureTaskControl(context)
    await control.check_cancelled()
    async with pg_manager.get_async_session_context() as session:
        revision, _file = await repo.require_input(
            session, task_id=context.task_id, uid=context.payload["operator_id"],
        )
    if not revision.model_spec:
        raise ValueError("请在知识库详情 → 配置 → 基础信息 → 知识库聊天模型中选择本地模型并保存，再重试实验卡片任务")
    await control.set_progress(5, "读取冻结正文和 PDF 证据")
    markdown = await _read_structure_markdown(
        revision.markdown_file, kb_id=revision.kb_id, file_id=revision.file_id,
    )
    if source_sha256(markdown) != revision.source_sha256:
        raise ValueError("冻结正文指纹不匹配，停止实验抽取")
    parse_assets = await _read_structure_assets(revision)
    original = await knowledge_base.get_file_download(revision.kb_id, revision.file_id)
    if b"%PDF-" not in original["content"][:1024]:
        raise ValueError("规范吸附分离 JSON 需要 PDF 原件及可核对的期刊页码")
    await control.check_cancelled()
    result = await extract_adsorption_record(
        markdown, pdf_content=original["content"], file_id=revision.file_id,
        model_spec=revision.model_spec, parse_assets=parse_assets, control=control,
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
        self.task_id = context.task_id
        self.file_id = context.payload["file_id"]

    async def check_cancelled(self):
        """撤权、正文变化或 Task 失权后停止抽取。"""
        await self.context.raise_if_cancelled()
        async with pg_manager.get_async_session_context() as session:
            await KnowledgeStructureRepository().require_input(
                session, task_id=self.context.task_id, uid=self.context.payload["operator_id"],
            )

    async def set_progress(self, progress, message):
        """同步 Task 和 Worker 日志中的真实阶段进度。"""
        await self.context.set_progress(progress, message)
        logger.info("[knowledge_structure] task_id={} progress={}% {}", self.task_id, progress, message)


async def _create_structure_intent(session, *, file, model_spec, source_hash, operator_id, force=False):
    """在文件行锁内复用同版本结果，或原子创建新 Task 和草稿。"""
    if not _is_pdf_file(file):
        return None
    repo = KnowledgeStructureRepository()
    previous = await repo.latest(session, kb_id=file.kb_id, file_id=file.file_id)
    if (
        not force and previous is not None and previous.markdown_file == file.markdown_file
        and previous.source_sha256 == source_hash and previous.model_spec == model_spec
    ):
        if previous.status == "pending":
            return None
        if (
            previous.status == "draft" and previous.schema_version == STRUCTURE_VERSION
            and previous.prompt_version == STRUCTURE_PROMPT_VERSION
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


def _is_pdf_file(file) -> bool:
    """用知识文件登记的类型判断是否可安排 PDF 结构化任务。"""
    return file.file_type == "pdf" or (not file.file_type and file.filename.lower().endswith(".pdf"))


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
