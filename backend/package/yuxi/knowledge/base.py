import asyncio
import mimetypes
import os
import re
from abc import ABC, abstractmethod
from typing import Any

from yuxi.knowledge.chunking.ragflow_like.presets import ensure_chunk_defaults_in_additional_params
from yuxi.knowledge.read_models import KnowledgeBaseConfig
from yuxi.knowledge.schemas import (
    FindOutputSchema,
    FindWindowSchema,
    SearchOutputSchema,
    SearchResultSchema,
)
from yuxi.knowledge.utils import resolve_processing_params, sanitize_processing_params
from yuxi.utils import logger
from yuxi.utils.datetime_utils import utc_isoformat


class FileStatus:
    UPLOADED = "uploaded"
    PARSING = "parsing"
    PARSED = "parsed"
    ERROR_PARSING = "error_parsing"
    INDEXING = "indexing"
    INDEXED = "indexed"
    ERROR_INDEXING = "error_indexing"


INDEXED_STATS_STATUSES = {FileStatus.INDEXED, "done"}


def _should_repair_file_stats(file_meta: dict) -> bool:
    status = file_meta.get("status")
    return status is None or status in INDEXED_STATS_STATUSES


class KnowledgeBaseException(Exception):
    """知识库统一异常基类"""

    pass


class KBNotFoundError(KnowledgeBaseException):
    """知识库不存在错误"""

    pass


class KBNameConflictError(KnowledgeBaseException):
    """知识库名称冲突错误。"""

    pass


class KnowledgeBase(ABC):
    """知识库抽象基类，定义统一接口"""

    kb_type = ""
    name = ""
    description = ""
    requires_embedding_model = True
    supports_documents = True
    apply_chunk_defaults = True

    def __init__(self, work_dir: str):
        """
        初始化知识库

        Args:
            work_dir: 工作目录
        """
        self.work_dir = work_dir
        os.makedirs(work_dir, exist_ok=True)

    def _file_record_to_meta(self, record: Any) -> dict:
        return {
            "file_id": record.file_id,
            "kb_id": record.kb_id,
            "parent_id": record.parent_id,
            "filename": record.filename,
            "file_type": record.file_type,
            "path": record.path,
            "markdown_file": record.markdown_file,
            "parse_assets_file": getattr(record, "parse_assets_file", None),
            "has_parse_assets": bool(getattr(record, "parse_assets_file", None)),
            "status": record.status,
            "content_hash": record.content_hash,
            "size": record.file_size,
            "chunk_count": int(getattr(record, "chunk_count", 0) or 0),
            "token_count": int(getattr(record, "token_count", 0) or 0),
            "content_type": record.content_type,
            "processing_params": sanitize_processing_params(record.processing_params),
            "is_folder": record.is_folder,
            "error": record.error_message,
            "created_by": record.created_by,
            "updated_by": record.updated_by,
            "created_at": utc_isoformat(record.created_at) if record.created_at else None,
            "updated_at": utc_isoformat(record.updated_at) if record.updated_at else None,
            "original_filename": record.original_filename,
            "minio_url": record.minio_url,
        }

    @staticmethod
    def _file_meta_to_record_data(meta: dict) -> dict[str, Any]:
        return {
            "kb_id": meta.get("kb_id"),
            "parent_id": meta.get("parent_id"),
            "filename": meta.get("filename") or "",
            "original_filename": meta.get("original_filename"),
            "file_type": meta.get("file_type"),
            "path": meta.get("path"),
            "minio_url": meta.get("minio_url"),
            "markdown_file": meta.get("markdown_file"),
            "parse_assets_file": meta.get("parse_assets_file"),
            "status": meta.get("status"),
            "content_hash": meta.get("content_hash"),
            "file_size": meta.get("size"),
            "chunk_count": int(meta.get("chunk_count") or 0),
            "token_count": int(meta.get("token_count") or 0),
            "content_type": meta.get("content_type"),
            "processing_params": sanitize_processing_params(meta.get("processing_params")),
            "is_folder": meta.get("is_folder", False),
            "error_message": meta.get("error"),
            "created_by": str(meta.get("created_by")) if meta.get("created_by") else None,
            "updated_by": str(meta.get("updated_by")) if meta.get("updated_by") else None,
        }

    async def _load_file_meta(self, kb_id: str, file_id: str, *, refresh: bool = False) -> dict:
        del refresh

        from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository

        record = await KnowledgeFileRepository().get_by_file_id(file_id)
        if record is None or record.kb_id != kb_id:
            raise ValueError(f"File {file_id} not found")

        return self._file_record_to_meta(record)

    @classmethod
    def get_create_params_config(cls) -> dict[str, Any]:
        """获取创建知识库时的类型特定参数配置。"""
        return {"options": []}

    @classmethod
    def validate_additional_params(cls, additional_params: dict | None) -> dict:
        """校验并规范化类型特定配置。"""
        return dict(additional_params or {})

    @classmethod
    def normalize_additional_params(cls, additional_params: dict | None) -> dict:
        """规范化 additional_params，仅文档型知识库补充分块默认值。"""
        params = cls.validate_additional_params(additional_params)
        if cls.apply_chunk_defaults:
            return ensure_chunk_defaults_in_additional_params(params)
        return params

    @abstractmethod
    async def _create_kb_instance(self, kb_id: str, embedding_model_spec: str | None) -> Any:
        """
        创建底层知识库实例

        Args:
            kb_id: 数据库ID
            embedding_model_spec: 嵌入模型配置

        Returns:
            底层知识库实例
        """
        pass

    @abstractmethod
    async def _initialize_kb_instance(self, instance: Any) -> None:
        """
        初始化底层知识库实例

        Args:
            instance: 底层知识库实例
        """
        pass

    async def add_file_record(
        self,
        kb_id: str,
        item: str,
        params: dict | None = None,
        operator_id: str | None = None,
        *,
        additional_params: dict[str, Any],
    ) -> dict:
        """
        Add a file record to metadata (Status: UPLOADED)

        Args:
            kb_id: Database ID
            item: File path or URL
            params: Parameters
            operator_id: Operator ID who created the file

        Returns:
            File metadata record
        """
        from yuxi.knowledge.utils.kb_utils import prepare_item_metadata

        params = params or {}
        content_type = params.get("content_type", "file")

        # Prepare metadata
        metadata = await prepare_item_metadata(item, content_type, kb_id, params=params)
        file_id = metadata["file_id"]
        metadata["processing_params"] = resolve_processing_params(
            kb_additional_params=additional_params,
            file_processing_params=metadata.get("processing_params"),
        )

        # Fallback: fetch file size from MinIO if not provided
        if metadata.get("size") is None and content_type == "file":
            try:
                from yuxi.knowledge.utils.kb_utils import is_minio_url, parse_minio_url
                from yuxi.storage.minio import get_minio_client

                file_path = metadata.get("path") or item
                if is_minio_url(file_path):
                    bucket_name, obj_name = parse_minio_url(file_path)
                    minio_client = get_minio_client()
                    file_size = await minio_client.astat_file(bucket_name, obj_name)
                    if file_size is not None:
                        metadata["size"] = file_size
            except Exception as exc:
                logger.warning(f"Failed to stat file size from MinIO for {item}: {exc}")

        # Initial status
        metadata["status"] = FileStatus.UPLOADED
        metadata["created_at"] = utc_isoformat()
        if operator_id:
            metadata["created_by"] = operator_id

        from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository

        async with KnowledgeFileRepository().lock_file_operation(kb_id, file_id):
            await self._persist_file_meta(file_id, metadata)

        return metadata

    async def parse_file(
        self, kb_id: str, file_id: str, operator_id: str | None = None, *,
        additional_params: dict[str, Any], processing_task_id: str | None = None,
        processing_owner: str | None = None, task_context=None, reparse: bool = False,
    ) -> dict:
        """生成同次解析的正文和附属产物，持有owner的任务才能发布。"""
        import uuid

        from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository
        from yuxi.services.knowledge_parse_service import (
            FileParseControl, cleanup_unpublished_attempt, save_parse_bundle,
        )
        from yuxi.services.ocr_service import parse_document_result
        from yuxi.storage.minio import get_minio_client

        attempt_id = uuid.uuid4().hex
        control = FileParseControl(
            kb_id, file_id, attempt_id, task_context,
            task_id=processing_task_id, owner=processing_owner,
        )
        repo = KnowledgeFileRepository()
        allowed = {FileStatus.UPLOADED, FileStatus.ERROR_PARSING, "failed"}
        if reparse:
            allowed.update({FileStatus.PARSED, FileStatus.INDEXED, FileStatus.ERROR_INDEXING, "done"})
        async with repo.lock_file_operation(kb_id, file_id, control):
            previous = await self._load_file_meta(kb_id, file_id)
            if previous.get("is_folder"):
                raise ValueError("文件夹不支持解析")
            if reparse and not (previous.get("path") or previous.get("minio_url")):
                raise ValueError("缺少原文件，无法重新解析")
            claim = {
                "status": FileStatus.PARSING, "error_message": None,
                "processing_task_id": processing_task_id, "processing_owner": processing_owner,
            }
            if operator_id:
                claim["updated_by"] = operator_id
            record = await repo.update_fields_if_status(
                kb_id=kb_id, file_id=file_id, allowed_statuses=allowed, data=claim,
            )
            if record is None:
                raise ValueError("文件正在处理或当前状态不允许解析")
            file_meta = self._file_record_to_meta(record)
            control.filename = str(file_meta.get("filename") or file_meta.get("original_filename") or file_id)
            control.file_claimed = True
            control.runtime = dict(getattr(record, "parse_runtime", None) or {})

        try:
            file_path = file_meta.get("minio_url") or file_meta.get("path")
            if not file_path:
                raise ValueError("文件缺少可读取的原文路径")
            params = resolve_processing_params(
                kb_additional_params=additional_params,
                file_processing_params=file_meta.get("processing_params"),
            )
            params["image_bucket"] = get_minio_client().KB_BUCKETS["images"]
            params["image_prefix"] = f"{kb_id}/kb-images/{file_id}/{attempt_id}"
            params["_defer_figure_analysis"] = task_context is not None and os.path.splitext(str(file_path).split("?", 1)[0])[1].lower() == ".pdf"
            result = await parse_document_result(source=file_path, params=params, control=control)
            if not result.markdown.strip() and not result.blocks:
                raise ValueError("没有生成可用的论文内容")
            figure_job_id = None
            figure_stage = result.metadata.get("figure_analysis", {})
            if task_context is not None and figure_stage.get("status") == "pending":
                figure_job_id = uuid.uuid4().hex
                figure_stage["task_id"] = figure_job_id
            markdown_url, assets_url = await save_parse_bundle(kb_id, file_id, attempt_id, result, control)
            figure_task = None
            if figure_job_id:
                from yuxi.services.knowledge_figure_analysis_service import figure_task_intent

                figure_task = figure_task_intent(
                    figure_job_id, kb_id=kb_id, file_id=file_id, attempt_id=attempt_id,
                    markdown_file=markdown_url, assets_file=assets_url,
                    model_spec=figure_stage["model"], filename=control.filename,
                )
            async with repo.lock_file_operation(kb_id, file_id, control):
                await control.check_cancelled()
                # 原文发布前清除与旧版关联的索引，失败时保留旧正文指针。
                if reparse or previous.get("markdown_file") or previous.get("chunk_count"):
                    cleanup = getattr(self, "delete_file_chunks_only", None)
                    if cleanup is None:
                        raise ValueError("当前知识库不支持重新解析派生数据清理")
                    await cleanup(kb_id, file_id)
                data = {
                    "status": FileStatus.PARSED, "markdown_file": markdown_url,
                    "parse_assets_file": assets_url, "parse_runtime": None,
                    "error_message": None, "processing_task_id": None, "processing_owner": None,
                }
                if operator_id:
                    data["updated_by"] = operator_id
                await control.write_fields(data, figure_task=figure_task)
            if figure_task:
                from yuxi.services.task_queue_service import publish_task

                try:
                    await publish_task(figure_task["id"])
                except Exception as exc:
                    # intent 已与正文指针提交；定期 publisher 会补投，不撤回已发布正文。
                    logger.warning(f"[pdf_figures] 投递待重试 task_id={figure_task['id']} error={type(exc).__name__}")
            published_meta = await self._load_file_meta(kb_id, file_id)
            published_meta["review_status"] = result.metadata.get("review_status", "not_requested")
            return published_meta
        except (Exception, asyncio.CancelledError) as exc:
            await cleanup_unpublished_attempt(kb_id, file_id, attempt_id)
            error = "解析任务已取消" if isinstance(exc, asyncio.CancelledError) else str(exc)
            await repo.update_fields_if_status(
                kb_id=kb_id, file_id=file_id, allowed_statuses={FileStatus.PARSING},
                processing_task_id=processing_task_id, processing_owner=processing_owner,
                data={
                    "status": FileStatus.ERROR_PARSING, "error_message": error,
                    "processing_task_id": None, "processing_owner": None,
                },
            )
            raise

    async def update_file_params(
        self,
        kb_id: str,
        file_id: str,
        params: dict,
        operator_id: str | None = None,
        *,
        additional_params: dict[str, Any],
    ) -> None:
        """Update file processing params"""
        # Skip if no params to update
        if not params:
            return

        file_meta = await self._load_file_meta(kb_id, file_id)
        current_params = file_meta.get("processing_params", {}) or {}
        logger.debug(f"[update_file_params] file_id={file_id}, current_params={current_params}, new_params={params}")

        current_params = resolve_processing_params(
            kb_additional_params=additional_params,
            file_processing_params=current_params,
            request_params=params,
        )

        file_meta["processing_params"] = current_params
        file_meta["updated_at"] = utc_isoformat()
        if operator_id:
            file_meta["updated_by"] = operator_id

        logger.debug(f"[update_file_params] file_id={file_id}, updated_params={current_params}")

        from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository

        update_data = {"processing_params": sanitize_processing_params(current_params)}
        if operator_id:
            update_data["updated_by"] = operator_id
        record = await KnowledgeFileRepository().update_fields(file_id=file_id, kb_id=kb_id, data=update_data)
        if record is None:
            raise ValueError(f"File {file_id} not found")

    async def get_file_parse_assets(self, kb_id: str, file_id: str) -> dict:
        """读取当前文件同次解析的图片解释、校对与诊断。"""
        import json

        meta = await self._load_file_meta(kb_id, file_id)
        pointer = meta.get("parse_assets_file")
        if not pointer:
            return {
                "figures": [], "reviews": [], "diagnostics": [],
                "attempt_id": None, "legacy_without_assets": True,
            }
        data = json.loads((await self._read_minio_bytes(pointer)).decode("utf-8"))
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise ValueError("解析附属数据格式无效，请重新解析")
        if data.get("file_id") != file_id or data.get("kb_id") != kb_id:
            raise ValueError("解析附属数据不属于当前文件")
        from yuxi.services.knowledge_figure_analysis_service import project_figure_task_status

        await project_figure_task_status(data, kb_id=kb_id, file_id=file_id)
        return {**data, "legacy_without_assets": False}

    async def _save_markdown_to_minio(self, kb_id: str, file_id: str, content: str) -> str:
        """Save markdown content to MinIO and return HTTP URL"""
        from yuxi.storage.minio import get_minio_client

        minio_client = get_minio_client()
        bucket_name = minio_client.KB_BUCKETS["parsed"]
        await asyncio.to_thread(minio_client.ensure_bucket_exists, bucket_name)

        object_name = f"{kb_id}/parsed/{file_id}.md"
        data = content.encode("utf-8")

        # Return standard HTTP URL from UploadResult
        upload_result = await minio_client.aupload_file(
            bucket_name=bucket_name,
            object_name=object_name,
            data=data,
        )

        return upload_result.url

    async def _read_minio_bytes(self, file_path: str) -> bytes:
        from yuxi.knowledge.utils.kb_utils import is_minio_url, parse_minio_url
        from yuxi.storage.minio import get_minio_client

        if not file_path or not is_minio_url(file_path):
            raise ValueError(f"Invalid MinIO path format: {file_path}")

        bucket_name, object_name = parse_minio_url(file_path)
        minio_client = get_minio_client()
        return await minio_client.adownload_file(bucket_name, object_name)

    async def _read_markdown_from_minio(self, file_path: str) -> str:
        """Read markdown content from MinIO"""
        content_bytes = await self._read_minio_bytes(file_path)
        return content_bytes.decode("utf-8")

    async def _get_file_meta(self, kb_id: str, file_id: str) -> dict:
        return await self._load_file_meta(kb_id, file_id)

    @staticmethod
    def _original_file_path(file_meta: dict) -> str | None:
        return file_meta.get("minio_url") or file_meta.get("path")

    def _knowledge_file_entry(self, kb_id: str, file_id: str, file_meta: dict) -> dict:
        is_dir = bool(file_meta.get("is_folder"))
        original_path = self._original_file_path(file_meta)
        path = f"/{file_id}"
        if is_dir:
            path = f"{path}/"
        return {
            "source": "knowledge",
            "kb_id": kb_id,
            "file_id": file_id,
            "parent_id": file_meta.get("parent_id"),
            "path": path,
            "virtual_path": f"/knowledge/{kb_id}/{file_id}",
            "name": file_meta.get("filename") or file_meta.get("original_filename") or file_id,
            "is_dir": is_dir,
            "size": 0 if is_dir else file_meta.get("size") or 0,
            "modified_at": file_meta.get("updated_at") or file_meta.get("created_at") or "",
            "readonly": True,
            "status": file_meta.get("status", "done"),
            "has_original_file": bool(original_path),
            "has_parsed_markdown": bool(file_meta.get("markdown_file")),
            "has_parse_assets": bool(file_meta.get("parse_assets_file")),
        }

    def _sort_file_entries(self, entries: list[dict]) -> list[dict]:
        return sorted(
            entries,
            key=lambda item: (not bool(item.get("is_dir")), str(item.get("name") or "").lower()),
        )

    async def _list_knowledge_children(
        self,
        kb_id: str,
        parent_id: str | None,
        *,
        recursive: bool,
        files_only: bool,
    ) -> list[dict]:
        from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository

        children = []
        for record in await KnowledgeFileRepository().list_children(kb_id=kb_id, parent_id=parent_id):
            meta = self._file_record_to_meta(record)
            children.append((record.file_id, meta))

        entries = []
        for file_id, meta in children:
            if not files_only or not meta.get("is_folder"):
                entries.append(self._knowledge_file_entry(kb_id, file_id, meta))
            if recursive and meta.get("is_folder"):
                entries.extend(
                    await self._list_knowledge_children(
                        kb_id,
                        file_id,
                        recursive=True,
                        files_only=files_only,
                    )
                )
        return self._sort_file_entries(entries)

    async def list_file_tree(
        self,
        kb_id: str,
        parent_id: str | None = None,
        recursive: bool = False,
        files_only: bool = False,
    ) -> dict:
        if parent_id:
            parent_meta = await self._get_file_meta(kb_id, parent_id)
            if not parent_meta.get("is_folder"):
                raise ValueError("Parent is not a folder")
        return {
            "entries": await self._list_knowledge_children(
                kb_id,
                parent_id,
                recursive=recursive,
                files_only=files_only,
            ),
            "readonly": True,
        }

    async def get_file_download(self, kb_id: str, file_id: str, variant: str = "original") -> dict:
        file_meta = await self._get_file_meta(kb_id, file_id)
        if file_meta.get("is_folder"):
            raise ValueError("Cannot download a folder")
        if variant not in {"original", "parsed"}:
            raise ValueError("Unsupported download variant")

        filename = file_meta.get("filename") or file_meta.get("original_filename") or file_id
        if variant == "parsed":
            markdown_file = file_meta.get("markdown_file")
            if not markdown_file:
                raise ValueError("文件尚未生成解析结果")
            return {
                "filename": f"{filename}.parsed.md",
                "content": await self._read_minio_bytes(markdown_file),
                "media_type": "text/markdown; charset=utf-8",
            }

        original_path = self._original_file_path(file_meta)
        if not original_path:
            raise ValueError("文件没有可下载的原始内容")
        media_type = file_meta.get("content_type") or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return {
            "filename": filename,
            "content": await self._read_minio_bytes(original_path),
            "media_type": media_type,
        }

    def _build_open_file_window(self, content: str, *, offset: int = 0, limit: int = 800) -> dict[str, Any]:
        lines = content.splitlines()
        total_lines = len(lines)
        start = min(max(int(offset), 0), total_lines)
        window_size = min(max(int(limit), 1), 2000)
        selected = lines[start : start + window_size]
        end = start + len(selected)

        return {
            "start_line": start + 1 if selected else 0,
            "end_line": end,
            "total_lines": total_lines,
            "offset": start,
            "window_size": window_size,
            "has_more_before": start > 0,
            "has_more_after": end < total_lines,
            "next_offset": end if end < total_lines else None,
            "content": "\n".join(f"{start + idx + 1:6d}\t{line}" for idx, line in enumerate(selected)),
        }

    @staticmethod
    def build_search_output(kb_id: str, retrieval_results: Any) -> dict[str, Any] | Any:
        if not isinstance(retrieval_results, list):
            return retrieval_results

        results = []
        for index, chunk in enumerate(retrieval_results):
            if not isinstance(chunk, dict):
                continue

            metadata = chunk.get("metadata") if isinstance(chunk.get("metadata"), dict) else {}
            metadata = {
                key: value
                for key, value in metadata.items()
                if key not in {"filepath", "parsed_path", "path", "markdown_file"}
            }
            file_id = metadata.get("file_id") or chunk.get("file_id") or chunk.get("full_doc_id") or ""
            chunk_id = metadata.get("chunk_id") or chunk.get("chunk_id") or chunk.get("id")
            chunk_index = metadata.get("chunk_index")
            if chunk_index is None:
                chunk_index = chunk.get("chunk_index")
            if chunk_index is not None:
                metadata.setdefault("chunk_index", chunk_index)
            if chunk.get("score") is not None:
                metadata.setdefault("score", chunk.get("score"))
            if chunk.get("distance") is not None:
                metadata.setdefault("distance", chunk.get("distance"))

            results.append(
                SearchResultSchema(
                    id=str(chunk_id or f"{file_id}:{index + 1}"),
                    kb_id=str(kb_id),
                    file_id=str(file_id or ""),
                    content=str(chunk.get("content") or ""),
                    metadata=metadata,
                )
            )

        return SearchOutputSchema(kb_id=str(kb_id), results=results).model_dump()

    @staticmethod
    def _build_find_file_windows(
        content: str,
        *,
        patterns: list[str],
        use_regex: bool = False,
        case_sensitive: bool = False,
        max_windows: int = 5,
        window_size: int = 80,
    ) -> dict[str, Any]:
        patterns = [pattern for pattern in patterns if pattern]
        if not patterns:
            raise ValueError("请提供至少一个 pattern")

        lines = content.splitlines()
        flags = 0 if case_sensitive else re.IGNORECASE
        if use_regex:
            matchers = [re.compile(pattern, flags) for pattern in patterns]

            def line_matches(line: str) -> bool:
                return any(matcher.search(line) for matcher in matchers)

        else:
            normalized_patterns = patterns if case_sensitive else [pattern.lower() for pattern in patterns]

            def line_matches(line: str) -> bool:
                haystack = line if case_sensitive else line.lower()
                return any(pattern in haystack for pattern in normalized_patterns)

        matched_indexes = [index for index, line in enumerate(lines) if line_matches(line)]
        windows: list[FindWindowSchema] = []
        covered_until = -1
        normalized_window_size = min(max(int(window_size), 1), 200)
        half_window = normalized_window_size // 2

        for matched_index in matched_indexes:
            if matched_index < covered_until:
                continue
            start = max(matched_index - half_window, 0)
            end = min(start + normalized_window_size, len(lines))
            start = max(end - normalized_window_size, 0)
            matched_lines = [index + 1 for index in matched_indexes if start <= index < end]
            selected = lines[start:end]
            windows.append(
                FindWindowSchema(
                    start_line=start + 1 if selected else 0,
                    end_line=end,
                    matched_lines=matched_lines,
                    content="\n".join(f"{start + idx + 1:6d}\t{line}" for idx, line in enumerate(selected)),
                )
            )
            covered_until = end
            if len(windows) >= max_windows:
                break

        return FindOutputSchema(
            kb_id="",
            file_id="",
            semantic=False,
            match_mode="regex" if use_regex else "keyword",
            total_matches=len(matched_indexes),
            windows=windows,
        ).model_dump(exclude={"kb_id", "file_id"})

    async def open_file_content(self, kb_id: str, file_id: str, offset: int = 0, limit: int = 800) -> dict:
        """按行窗口打开文件解析后的 Markdown 内容"""
        try:
            file_meta = await self._load_file_meta(kb_id, file_id)
        except ValueError as exc:
            raise Exception(f"文件不存在: {file_id}") from exc
        if file_meta.get("is_folder"):
            raise Exception(f"文件 {file_id} 是文件夹")

        markdown_file = file_meta.get("markdown_file")
        if not markdown_file:
            raise Exception(f"文件 {file_id} 没有解析后的 Markdown 内容")

        content = await self._read_markdown_from_minio(markdown_file)
        return self._build_open_file_window(content, offset=offset, limit=limit)

    async def find_file_content(
        self,
        kb_id: str,
        file_id: str,
        patterns: list[str],
        *,
        use_regex: bool = False,
        case_sensitive: bool = False,
        max_windows: int = 5,
        window_size: int = 80,
    ) -> dict:
        try:
            file_meta = await self._load_file_meta(kb_id, file_id)
        except ValueError as exc:
            raise Exception(f"文件不存在: {file_id}") from exc
        if file_meta.get("is_folder"):
            raise Exception(f"文件 {file_id} 是文件夹")

        markdown_file = file_meta.get("markdown_file")
        if not markdown_file:
            raise Exception(f"文件 {file_id} 没有解析后的 Markdown 内容")

        content = await self._read_markdown_from_minio(markdown_file)
        return self._build_find_file_windows(
            content,
            patterns=patterns,
            use_regex=use_regex,
            case_sensitive=case_sensitive,
            max_windows=max_windows,
            window_size=window_size,
        )

    @abstractmethod
    async def index_file(
        self,
        kb_id: str,
        file_id: str,
        operator_id: str | None = None,
        *,
        embedding_model_spec: str | None,
        additional_params: dict[str, Any],
        processing_task_id: str | None = None,
        processing_owner: str | None = None,
    ) -> dict:
        """
        Index parsed file (Status: INDEXING -> INDEXED/ERROR_INDEXING)

        Args:
            kb_id: Database ID
            file_id: File ID
            operator_id: ID of the user performing the operation

        Returns:
            Updated file metadata
        """
        pass

    async def cleanup_database_resources(self, kb_id: str) -> dict:
        """
        清理知识库关联的文件与存储资源。

        Args:
            kb_id: 数据库ID

        Returns:
            操作结果
        """
        from yuxi.knowledge.utils.kb_utils import is_minio_url, parse_minio_url
        from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository
        from yuxi.storage.minio import get_minio_client

        minio_client = get_minio_client()
        file_repo = KnowledgeFileRepository()

        # 1. 删除文件元数据中记录的 MinIO 文件
        after_file_id = None
        while True:
            records = await file_repo.list_by_kb_id_after(kb_id, after_file_id=after_file_id, limit=500)
            if not records:
                break
            after_file_id = records[-1].file_id
            for record in records:
                file_id = record.file_id
                file_path = record.minio_url or record.path
                if file_path and is_minio_url(file_path):
                    try:
                        bucket_name, object_name = parse_minio_url(file_path)
                        await minio_client.adelete_file(bucket_name, object_name)
                    except Exception as e:
                        logger.warning(f"Failed to delete MinIO file {file_path}: {e}")

                # 删除解析后的 markdown 文件
                parsed_object = f"{kb_id}/parsed/{file_id}.md"
                await minio_client.adelete_file(minio_client.KB_BUCKETS["parsed"], parsed_object)

        # 2. 并行删除所有知识库 bucket 中该 kb_id 下的文件
        prefix = f"{kb_id}/"
        cleanup_buckets = {
            minio_client.KB_BUCKETS["parsed"],
            minio_client.KB_BUCKETS["documents"],
            minio_client.KB_BUCKETS["images"],
        }
        cleanup_tasks = [minio_client.adelete_objects_by_prefix(bucket_name, prefix) for bucket_name in cleanup_buckets]
        await asyncio.gather(*cleanup_tasks)

        # 3. 删除知识库的文件记录；知识库主记录由 Manager 统一删除。
        await file_repo.delete_by_kb_id(kb_id)

        # 删除工作目录
        working_dir = os.path.join(self.work_dir, kb_id)
        if os.path.exists(working_dir):
            import shutil

            try:
                shutil.rmtree(working_dir)
            except Exception as e:
                logger.error(f"Error deleting working directory {working_dir}: {e}")

        return {"message": "删除成功"}

    async def detect_data_inconsistencies(
        self,
        known_kb_ids: set[str],
        managed_kb_ids: set[str],
    ) -> dict[str, list[dict]]:
        """检测当前知识库类型管理的外部资源不一致。"""
        return {"missing_collections": [], "missing_files": []}

    async def create_folder(
        self,
        kb_id: str,
        folder_name: str,
        parent_id: str | None = None,
        operator_id: str | None = None,
    ) -> dict:
        """创建文件夹并记录操作者。"""
        import uuid

        if parent_id:
            parent_meta = await self._load_file_meta(kb_id, parent_id)
            if not parent_meta.get("is_folder"):
                raise ValueError("Parent is not a folder")

        folder_id = f"folder-{uuid.uuid4()}"

        folder_meta = {
            "file_id": folder_id,
            "filename": folder_name,
            "is_folder": True,
            "parent_id": parent_id,
            "kb_id": kb_id,
            "created_at": utc_isoformat(),
            "status": "done",
            "path": folder_name,
            "file_type": "folder",
            "created_by": operator_id,
        }
        await self._persist_file_meta(folder_id, folder_meta)
        return folder_meta

    async def rename_folder(self, kb_id: str, folder_id: str, folder_name: str) -> dict:
        """重命名真实文件夹，不改写其子记录。"""
        normalized_name = folder_name.strip()
        if not normalized_name:
            raise ValueError("Folder name cannot be empty")
        if "/" in normalized_name or "\\" in normalized_name:
            raise ValueError("Folder name cannot contain path separators")

        meta = await self._load_file_meta(kb_id, folder_id)
        if not meta.get("is_folder"):
            raise ValueError("Document is not a folder")

        from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository

        record = await KnowledgeFileRepository().update_fields(
            file_id=folder_id,
            kb_id=kb_id,
            data={"filename": normalized_name, "path": normalized_name},
        )
        if record is None:
            raise ValueError(f"File {folder_id} not found")
        return self._file_record_to_meta(record)

    @abstractmethod
    async def aquery(
        self,
        query_text: str,
        kb_id: str,
        *,
        config: KnowledgeBaseConfig,
        **kwargs,
    ) -> list[dict]:
        """
        异步查询知识库

        Args:
            query_text: 查询文本
            kb_id: 数据库ID
            **kwargs: 查询参数

        Returns:
            一个包含字典的列表，每个字典代表一个检索到的文档块。
        """
        pass

    @abstractmethod
    def get_query_params_config(self, kb_id: str, **kwargs) -> dict:
        """
        获取知识库类型的查询参数配置

        Args:
            kb_id: 数据库ID
            **kwargs: 额外参数

        Returns:
            dict: {
                "type": "kb_type",
                "options": [
                    {
                        "key": "param_name",
                        "label": "参数名称",
                        "type": "select|number|boolean",
                        "default": default_value,
                        "options": [...],  # 对于 select 类型
                        "description": "参数描述",
                        "min": 1,  # 对于 number 类型
                        "max": 100,
                        "step": 0.1
                    },
                    ...
                ]
            }
        """
        pass

    async def export_data(self, kb_id: str, format: str = "zip", **kwargs) -> str:
        pass

    def get_default_query_params(self, kb_id: str) -> dict[str, Any]:
        """从 get_query_params_config 中提取所有参数的默认值，返回 {"options": {...}}"""
        config = self.get_query_params_config(kb_id)
        defaults = {}
        for opt in config.get("options", []):
            if "default" in opt:
                defaults[opt["key"]] = opt["default"]
        return {"options": defaults}

    async def repair_missing_file_stats(self, kb_id: str) -> dict:
        from yuxi.knowledge.chunking.ragflow_like.nlp import count_tokens
        from yuxi.repositories.knowledge_chunk_repository import KnowledgeChunkRepository
        from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository

        chunk_repo = KnowledgeChunkRepository()
        file_repo = KnowledgeFileRepository()
        after_file_id = None
        scanned_files = 0
        scanned_indexed_files = 0
        skipped_file_count = 0
        scanned_token_files = 0
        updated_files = 0
        updated_chunk_files = 0
        updated_token_files = 0
        updated_size_files = 0

        while True:
            records = await file_repo.list_by_kb_id_after(
                kb_id,
                after_file_id=after_file_id,
                limit=500,
                files_only=True,
            )
            if not records:
                break
            after_file_id = records[-1].file_id

            indexed_records = [record for record in records if _should_repair_file_stats({"status": record.status})]
            indexed_file_ids = [record.file_id for record in indexed_records]
            indexed_file_id_set = set(indexed_file_ids)
            chunk_counts = await chunk_repo.count_by_file_ids(indexed_file_ids)
            token_file_ids = [record.file_id for record in indexed_records if int(record.token_count or 0) <= 0]
            token_counts = {file_id: 0 for file_id in token_file_ids}
            for chunk in await chunk_repo.list_by_file_ids(token_file_ids):
                token_counts[chunk.file_id] = token_counts.get(chunk.file_id, 0) + count_tokens(chunk.content or "")
            size_updates = await self._fill_missing_file_sizes_for_records(records)

            scanned_files += len(records)
            scanned_indexed_files += len(indexed_file_ids)
            skipped_file_count += len(records) - len(indexed_file_ids)
            scanned_token_files += len(token_file_ids)

            for record in records:
                file_id = record.file_id
                update_data: dict[str, Any] = {}
                if file_id not in indexed_file_id_set:
                    if int(record.chunk_count or 0) != 0:
                        update_data["chunk_count"] = 0
                        updated_chunk_files += 1
                    if int(record.token_count or 0) != 0:
                        update_data["token_count"] = 0
                        updated_token_files += 1
                else:
                    next_chunk_count = int(chunk_counts.get(file_id, 0))
                    if int(record.chunk_count or 0) != next_chunk_count:
                        update_data["chunk_count"] = next_chunk_count
                        updated_chunk_files += 1

                    if file_id in token_counts:
                        next_token_count = int(token_counts[file_id])
                        if record.token_count is None or int(record.token_count or 0) != next_token_count:
                            update_data["token_count"] = next_token_count
                            updated_token_files += 1

                if file_id in size_updates:
                    update_data["file_size"] = size_updates[file_id]
                    updated_size_files += 1

                if update_data:
                    updated_files += 1
                    await file_repo.update_fields(file_id=file_id, kb_id=kb_id, data=update_data)

        return {
            "status": "success",
            "scanned_files": scanned_files,
            "scanned_indexed_files": scanned_indexed_files,
            "skipped_unindexed_files": skipped_file_count,
            "scanned_token_files": scanned_token_files,
            "updated_files": updated_files,
            "updated_chunk_files": updated_chunk_files,
            "updated_token_files": updated_token_files,
            "updated_size_files": updated_size_files,
        }

    async def delete_folder(self, kb_id: str, folder_id: str) -> None:
        """
        Recursively delete a folder and its content.

        Args:
            kb_id: Database ID
            folder_id: Folder ID to delete
        """
        from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository

        children = await KnowledgeFileRepository().list_children(kb_id=kb_id, parent_id=folder_id)
        for child in children:
            child_id = child.file_id
            if child.is_folder:
                await self.delete_folder(kb_id, child_id)
            else:
                await self.delete_file(kb_id, child_id)

        # Delete the folder itself
        # We call delete_file which should handle the actual removal.
        # Implementations should ensure they handle folder deletion gracefully (e.g. skip vector deletion)
        await self.delete_file(kb_id, folder_id)

    async def move_file(self, kb_id: str, file_id: str, new_parent_id: str | None) -> dict:
        """
        Move a file or folder to a new parent folder.

        Args:
            kb_id: Database ID
            file_id: File/Folder ID to move
            new_parent_id: New parent folder ID (None for root)

        Returns:
            dict: Updated metadata
        """
        from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository

        async with KnowledgeFileRepository().lock_file_tree(kb_id):
            meta = await self._load_file_meta(kb_id, file_id)

            if meta.get("is_folder") and new_parent_id:
                if new_parent_id == file_id:
                    raise ValueError("Cannot move a folder into itself")

                current = new_parent_id
                while current:
                    parent_meta = await self._load_file_meta(kb_id, current)
                    if current == new_parent_id and not parent_meta.get("is_folder"):
                        raise ValueError("Parent is not a folder")
                    if current == file_id:
                        raise ValueError("Cannot move a folder into its own subfolder")
                    current = parent_meta.get("parent_id")
            elif new_parent_id:
                parent_meta = await self._load_file_meta(kb_id, new_parent_id)
                if not parent_meta.get("is_folder"):
                    raise ValueError("Parent is not a folder")

            record = await KnowledgeFileRepository().update_fields(
                file_id=file_id,
                kb_id=kb_id,
                data={"parent_id": new_parent_id},
            )
            if record is None:
                raise ValueError(f"File {file_id} not found")
            return self._file_record_to_meta(record)

    @abstractmethod
    async def delete_file(self, kb_id: str, file_id: str) -> None:
        """
        删除文件

        Args:
            kb_id: 数据库ID
            file_id: 文件ID
        """
        pass

    @abstractmethod
    async def get_file_basic_info(self, kb_id: str, file_id: str) -> dict:
        """
        获取文件基本信息（仅元数据）

        Args:
            kb_id: 数据库ID
            file_id: 文件ID

        Returns:
            dict: 包含文件基本信息的字典
        """
        pass

    @abstractmethod
    async def get_file_content(self, kb_id: str, file_id: str) -> dict:
        """
        获取文件内容信息（chunks和lines）

        Args:
            kb_id: 数据库ID
            file_id: 文件ID

        Returns:
            dict: 包含文件内容信息的字典
        """
        pass

    @abstractmethod
    async def get_file_info(self, kb_id: str, file_id: str) -> dict:
        """
        获取文件完整信息（基本信息+内容信息）

        Args:
            kb_id: 数据库ID
            file_id: 文件ID

        Returns:
            dict: 包含文件信息和chunks的字典
        """
        pass

    async def _fill_missing_file_sizes_for_records(self, records: list[Any]) -> dict[str, int]:
        """为显式修复任务中的缺失 size 文件从 MinIO 补全大小信息。"""
        from yuxi.knowledge.utils.kb_utils import is_minio_url, parse_minio_url
        from yuxi.storage.minio import get_minio_client

        candidates: list[tuple[str, str]] = []
        for record in records:
            if record.is_folder or record.file_size is not None:
                continue
            file_path = record.minio_url or record.path
            if not file_path or not is_minio_url(file_path):
                continue
            candidates.append((record.file_id, file_path))

        if not candidates:
            return {}

        minio_client = get_minio_client()
        semaphore = asyncio.Semaphore(20)

        async def _stat_file(file_id: str, file_path: str) -> tuple[str, int | None]:
            bucket_name, obj_name = parse_minio_url(file_path)
            try:
                async with semaphore:
                    return file_id, await minio_client.astat_file(bucket_name, obj_name)
            except Exception as exc:
                logger.warning(f"Failed to fill size for {file_id}: {exc}")
                return file_id, None

        updates: dict[str, int] = {}
        for offset in range(0, len(candidates), 100):
            batch = candidates[offset : offset + 100]
            for file_id, file_size in await asyncio.gather(
                *(_stat_file(file_id, file_path) for file_id, file_path in batch)
            ):
                if file_size is not None:
                    updates[file_id] = file_size

        if updates:
            logger.info(f"Filled {len(updates)}/{len(candidates)} missing file sizes from MinIO for {self.kb_type}")
        return updates

    async def _persist_file_meta(self, file_id: str, meta: dict) -> None:
        """Persist one file metadata record without storing it on the KB instance."""
        from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository

        data = self._file_meta_to_record_data(meta)
        if not data.get("kb_id"):
            return
        await KnowledgeFileRepository().upsert(file_id=file_id, data=data)
