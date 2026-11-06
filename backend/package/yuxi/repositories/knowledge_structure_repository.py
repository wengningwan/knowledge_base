"""实验草稿的归属、版本与持久化边界。"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from yuxi.permissions import ResourcePermission, require_knowledge_base_permission
from yuxi.storage.postgres.models_business import User
from yuxi.storage.postgres.models_knowledge import (
    KnowledgeBase,
    KnowledgeExperimentCard,
    KnowledgeFile,
    KnowledgeStructureEvidence,
    KnowledgeStructureRevision,
)
from yuxi.utils.datetime_utils import utc_now


class StructureNotFoundError(ValueError):
    """目标文档或实验草稿不存在。"""


class StructureConflictError(ValueError):
    """正文或任务版本变化，需要重新读取。"""


class KnowledgeStructureRepository:
    """只在调用者拥有的事务内读写实验记录。"""

    async def require_file(self, session, *, kb_id, file_id, uid, permission, lock=False):
        """同时校验当前身份、知识库权限和文件归属。"""
        user = await session.scalar(select(User).where(User.uid == uid, User.is_deleted == 0))
        kb = await session.scalar(select(KnowledgeBase).where(KnowledgeBase.kb_id == kb_id))
        if user is None or kb is None:
            raise StructureNotFoundError("用户或知识库不存在")
        require_knowledge_base_permission(user, kb, permission)
        statement = select(KnowledgeFile).where(
            KnowledgeFile.kb_id == kb_id, KnowledgeFile.file_id == file_id, KnowledgeFile.is_folder.is_(False),
        )
        if lock:
            statement = statement.with_for_update()
        record = await session.scalar(statement)
        if record is None:
            raise StructureNotFoundError("文档不存在或不属于当前知识库")
        return record, kb

    async def finish_index(self, session, *, kb_id, file_id, uid, markdown_file, data, task_id, owner):
        """在入库任务的 lease 事务中发布原文索引状态。"""
        record, kb = await self.require_file(
            session, kb_id=kb_id, file_id=file_id, uid=uid, permission=ResourcePermission.MANAGE, lock=True,
        )
        if (
            record.status != "indexing" or record.markdown_file != markdown_file
            or record.processing_task_id != task_id or record.processing_owner != owner
        ):
            raise asyncio.CancelledError("原文入库执行权或正文版本已变化")
        for name in ("status", "error_message", "processing_task_id", "processing_owner", "chunk_count", "token_count"):
            if name in data:
                setattr(record, name, data[name])
        record.updated_by = uid
        record.updated_at = utc_now()
        await session.flush()
        return record, kb

    async def latest(self, session, *, kb_id, file_id):
        """按文档归属读取最新尝试，不跨文件猜测结果。"""
        return await session.scalar(select(KnowledgeStructureRevision).where(
            KnowledgeStructureRevision.kb_id == kb_id, KnowledgeStructureRevision.file_id == file_id,
        ).order_by(KnowledgeStructureRevision.created_at.desc(), KnowledgeStructureRevision.revision_id.desc()).limit(1))

    async def create_revision(self, session, *, task_id, file, model_spec, source_sha256, operator_id):
        """与 Task 意图同事务保存冻结输入。"""
        record = KnowledgeStructureRevision(
            revision_id=task_id, task_id=task_id, kb_id=file.kb_id, file_id=file.file_id,
            markdown_file=file.markdown_file, parse_assets_file=file.parse_assets_file,
            source_sha256=source_sha256, model_spec=model_spec, operator_id=operator_id,
            status="pending", schema_version=1, prompt_version="chemical-draft-v1",
        )
        session.add(record)
        await session.flush()
        return record

    async def require_input(self, session, *, task_id, uid, lock=False):
        """核对任务身份和当前正文；取消、重解析或撤权不能写回。"""
        revision = await session.get(KnowledgeStructureRevision, task_id)
        if revision is None:
            raise StructureNotFoundError("实验草稿已删除")
        if revision.operator_id != uid:
            raise StructureConflictError("实验任务身份不匹配")
        record, _kb = await self.require_file(
            session, kb_id=revision.kb_id, file_id=revision.file_id, uid=uid,
            permission=ResourcePermission.MANAGE, lock=lock,
        )
        if record.markdown_file != revision.markdown_file:
            raise StructureConflictError("正文已变化，本次实验抽取已失效")
        latest = await self.latest(session, kb_id=revision.kb_id, file_id=revision.file_id)
        if latest.revision_id != revision.revision_id:
            raise StructureConflictError("已有更新的实验抽取版本")
        return revision, record

    async def save_result(self, session, *, task_id, uid, result):
        """只在有效 Task lease 内保存完整草稿，等待任务终态提交。"""
        revision, _file = await self.require_input(session, task_id=task_id, uid=uid, lock=True)
        if revision.status != "pending":
            raise StructureConflictError("实验草稿已结束")
        evidence_ids = {item["evidence_id"] for item in result["evidence"]}
        if len(evidence_ids) != len(result["evidence"]):
            raise ValueError("实验原文证据标识重复")
        for card in result["cards"]:
            self._check_evidence_refs(card, evidence_ids)
        session.add_all(KnowledgeStructureEvidence(
            revision_id=task_id, evidence_id=item["evidence_id"], source=item,
        ) for item in result["evidence"])
        session.add_all(KnowledgeExperimentCard(
            revision_id=task_id, card_index=index, content=card,
        ) for index, card in enumerate(result["cards"]))
        revision.coverage = result["coverage"]
        revision.schema_version = result["schema_version"]
        revision.prompt_version = result["prompt_version"]
        revision.updated_at = utc_now()
        await session.flush()

    async def finish_success(self, session, task, result):
        """与 Task success 同事务把已保存候选标记为机器草稿。"""
        revision, _file = await self.require_input(
            session, task_id=task.id, uid=task.payload["operator_id"], lock=True,
        )
        if result.get("revision_id") != revision.revision_id or revision.coverage is None:
            raise ValueError("实验任务没有保存完整抽取结果")
        revision.status = "draft"
        revision.updated_at = utc_now()

    async def finish_failure(self, session, task, error):
        """随 Task 终态收敛失败或取消，保留真实失败原因。"""
        revision = await session.get(KnowledgeStructureRevision, task.id)
        if revision is not None and revision.status == "pending":
            revision.status = "cancelled" if task.cancel_requested else "failed"
            revision.error_message = error
            revision.updated_at = utc_now()

    async def read_result(self, session, revision):
        """只返回指定版本的卡片和证据。"""
        cards = (await session.scalars(select(KnowledgeExperimentCard).where(
            KnowledgeExperimentCard.revision_id == revision.revision_id,
        ).order_by(KnowledgeExperimentCard.card_index))).all()
        evidence = (await session.scalars(select(KnowledgeStructureEvidence).where(
            KnowledgeStructureEvidence.revision_id == revision.revision_id,
        ))).all()
        return [card.content for card in cards], [item.source for item in evidence]

    def _check_evidence_refs(self, value, evidence_ids):
        """拒绝 JSON 中跨版本或不存在的字段引用。"""
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"evidence_ids", "assignment_evidence_ids"}:
                    if not isinstance(item, list) or not item or any(ref not in evidence_ids for ref in item):
                        raise ValueError("实验字段缺少同版本原文证据")
                else:
                    self._check_evidence_refs(item, evidence_ids)
        elif isinstance(value, list):
            for item in value:
                self._check_evidence_refs(item, evidence_ids)
