"""将人工核对结果发布为同一解析版本的正文与回执。"""

from __future__ import annotations

import hashlib
import json
import re
import uuid

from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository
from yuxi.services.knowledge_parse_service import complete_storage_mutation
from yuxi.utils import logger
from yuxi.utils.datetime_utils import utc_isoformat, utc_now


class ReviewConflictError(ValueError):
    """当前文档已变化，客户端必须重新加载。"""


class ReviewNotFoundError(ValueError):
    """指定文档不属于当前知识库。"""


async def confirm_document_review(
    executor,
    kb_id: str,
    file_id: str,
    *,
    operator_id: str,
    attempt_id: str,
    review_revision: str | None,
    source_sha256: str,
    review_index: int | None,
    review_indexes: list[int] | None,
    action: str,
    content: str,
    anchor_block_id: str | None,
) -> dict:
    """校验快照、写全不可变对象，再以 CAS 发布正文和人工确认。"""
    from yuxi.knowledge.parser.pdf_structure import render_document
    from yuxi.knowledge.utils.kb_utils import parse_minio_url
    from yuxi.storage.minio import get_minio_client

    if any(not re.fullmatch(r"[A-Za-z0-9_-]+", value) for value in (kb_id, file_id, attempt_id)):
        raise ValueError("无效的文档版本标识")
    repo = KnowledgeFileRepository()
    client = get_minio_client()
    bucket = client.KB_BUCKETS["parsed"]
    prefix = f"{kb_id}/parsed/{file_id}/{attempt_id}/"
    async with repo.lock_file_operation(kb_id, file_id):
        record = await repo.get_by_file_id(file_id)
        if record is None or record.kb_id != kb_id:
            raise ReviewNotFoundError("文档不存在或不属于当前知识库")
        allowed_statuses = {"parsed", "indexed", "done", "error_indexing", "error_parsing"}
        if record.status not in allowed_statuses:
            raise ReviewConflictError("文件正在处理或尚未解析，请稍后重新打开校对页面")
        previous_markdown, previous_assets = record.markdown_file, record.parse_assets_file
        for pointer in (previous_markdown, previous_assets):
            if not pointer:
                raise ValueError("缺少独立解析产物，请重新解析后校对")
            object_bucket, name = parse_minio_url(pointer)
            if object_bucket != bucket or not name.startswith(prefix) or "/" in name[len(prefix) :]:
                raise ReviewConflictError("文档解析版本已变化，请重新打开校对页面")
        _, markdown_name = parse_minio_url(previous_markdown)
        _, assets_name = parse_minio_url(previous_assets)
        markdown = (await client.adownload_file(bucket, markdown_name)).decode("utf-8")
        sidecar = json.loads(await client.adownload_file(bucket, assets_name))
        if (
            not isinstance(sidecar, dict)
            or sidecar.get("schema_version") != 1
            or any(
                sidecar.get(key) != value
                for key, value in (("kb_id", kb_id), ("file_id", file_id), ("attempt_id", attempt_id))
            )
        ):
            raise ValueError("解析产物与当前文档不一致，请重新解析")
        metadata = sidecar.setdefault("metadata", {})
        if (
            metadata.get("human_review_revision") != review_revision
            or hashlib.sha256(markdown.encode()).hexdigest() != source_sha256
        ):
            raise ReviewConflictError("正文或校对结果已变化，请重新打开页面后再确认")
        reviews, blocks = sidecar.get("reviews", []), sidecar.get("blocks", [])
        updated_markdown = markdown
        if review_indexes is not None:
            # 批量确认只处理显式集合：复核会改写原因，同一原问题的分片可能分属不同簇，禁止按身份扩展冒认。
            if action != "keep":
                raise ValueError("批量确认仅支持保留正文")
            if any(not isinstance(index, int) or not 0 <= index < len(reviews) for index in review_indexes):
                raise ValueError("存在超出范围的校对记录")
            confirmed = [reviews[index] for index in review_indexes if not reviews[index].get("human_review")]
            if not confirmed:
                return {
                    "content_changed": False,
                    "index_invalidated": False,
                    "confirmed_count": 0,
                    "review_revision": metadata.get("human_review_revision"),
                }
            for row in confirmed:
                row["human_review"] = {
                    "confirmed_by": operator_id,
                    "confirmed_at": utc_isoformat(utc_now()),
                    "block_id": row.get("block_id"),
                }
        else:
            if not isinstance(review_index, int) or not 0 <= review_index < len(reviews):
                raise ValueError("未找到待确认的校对问题")
            row = reviews[review_index]
            target_id = row.get("human_review", {}).get("block_id") or row.get("block_id")
            block = next(
                (
                    item
                    for item in blocks
                    if target_id is not None
                    and item.get("id") == target_id
                    and item.get("type") not in {"page_header", "page_footer"}
                ),
                None,
            )
            if action not in {"keep", "replace", "insert_after"}:
                raise ValueError("无效的校对操作")
            if action != "keep":
                if not blocks or render_document(blocks) != markdown:
                    raise ValueError("正文与段落结构不一致，请重新解析后修改")
                if action == "replace":
                    if block is None:
                        raise ValueError("无法定位原段落，请选择明确的补入位置")
                    block["content"] = content
                else:
                    if block is not None or not content.strip():
                        raise ValueError("补入操作需要一个缺失段落问题和非空正文")
                    anchor = next((item for item in blocks if item.get("id") == anchor_block_id), None)
                    if anchor is None and anchor_block_id != "__start__":
                        raise ValueError("请选择补入正文的位置")
                    index = blocks.index(anchor) + 1 if anchor else 0
                    target_id = f"human-{uuid.uuid4().hex}"
                    blocks.insert(
                        index,
                        {
                            "id": target_id,
                            "type": "text",
                            "content": content,
                            "page": row.get("page"),
                            "bbox": None,
                        },
                    )
            updated_markdown = render_document(blocks) if action != "keep" else markdown
            # 人工确认只覆盖当前问题的分片片段；同段其它问题未经核对，不得冒认人工结论。
            issue_identity = row.get("parent_issue_id") or row.get("id")
            confirmed = [
                item
                for item in reviews
                if block is not None
                and (item.get("human_review", {}).get("block_id") or item.get("block_id")) == block["id"]
                and (item.get("parent_issue_id") or item.get("id")) == issue_identity
            ]
            if not confirmed:
                confirmed = [row]
            receipt = {"confirmed_by": operator_id, "confirmed_at": utc_isoformat(utc_now()), "block_id": target_id}
            for item in confirmed:
                item["human_review"] = receipt.copy()
        changed = updated_markdown != markdown
        remaining = [item for item in reviews if not item.get("human_review")]
        technical = any(
            item.get("status") in {"technical_failed", "pending", "running"} or item.get("protocol_errors")
            for item in remaining
        )
        unresolved = any(
            item.get("classification") != "excluded"
            and (
                item.get("status") not in {"checked", "machine_corrected", "proven_equivalent"}
                or item.get("remaining_conflicts")
            )
            for item in remaining
        )
        metadata["review_status"] = "incomplete" if technical else "needs_review" if unresolved else "completed"
        revision = uuid.uuid4().hex
        metadata["human_review_revision"] = revision
        if "pdf_repair" in metadata:
            metadata["pdf_repair"]["final_markdown_hash"] = hashlib.sha256(updated_markdown.encode()).hexdigest()
        names = [f"{prefix}assets.review.{revision}.json"]
        if changed:
            names.append(f"{prefix}document.review.{revision}.md")
        published = False
        new_markdown, new_assets = previous_markdown, None
        invalidated = False
        try:
            if changed:
                uploaded = await complete_storage_mutation(
                    client.aupload_file(bucket, names[1], updated_markdown.encode())
                )
                new_markdown = uploaded.url
            uploaded = await complete_storage_mutation(
                client.aupload_file(
                    bucket,
                    names[0],
                    json.dumps(sidecar, ensure_ascii=False, allow_nan=False).encode(),
                )
            )
            new_assets = uploaded.url
            if changed:
                cleanup = getattr(executor, "delete_file_chunks_only", None)
                if cleanup is None:
                    raise ValueError("当前知识库不支持校对正文后清理旧索引")
                await cleanup(kb_id, file_id)
                invalidated = record.status in {"indexed", "done", "error_indexing"} or bool(record.chunk_count)
            published = await repo.publish_review(
                kb_id=kb_id,
                file_id=file_id,
                previous_markdown=previous_markdown,
                previous_assets=previous_assets,
                markdown_file=new_markdown,
                assets_file=new_assets,
                allowed_statuses=allowed_statuses,
                content_changed=changed,
                operator_id=operator_id,
            )
            if not published:
                raise ReviewConflictError("文档状态已变化，本次未保存，请重新打开页面")
            return {
                "content_changed": changed,
                "index_invalidated": invalidated,
                "confirmed_count": len(confirmed),
                "review_revision": revision,
            }
        finally:
            if not published:
                # commit 的确认也可能中断；只删除明确不再被数据库引用的对象。
                try:
                    current = await repo.get_by_file_id(file_id)
                    if current is None or current.kb_id == kb_id:
                        referenced = {current.markdown_file, current.parse_assets_file} if current else set()
                        for name in names:
                            if not any(
                                pointer and parse_minio_url(pointer) == (bucket, name) for pointer in referenced
                            ):
                                await complete_storage_mutation(client.adelete_file(bucket, name))
                except Exception as exc:
                    logger.warning("未确认的人工校对对象保留待清理（%s）", type(exc).__name__)
