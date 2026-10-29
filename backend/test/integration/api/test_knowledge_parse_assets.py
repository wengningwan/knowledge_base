"""通过真实 HTTP、PostgreSQL 和 MinIO 验证论文与附属产物的读取边界。"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from yuxi.storage.minio import get_minio_client

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest_asyncio.fixture
async def parsed_document_records(knowledge_database):
    """直写已发布的小型产物和历史记录，避免上传论文或调用解析服务。"""
    kb_id = knowledge_database["kb_id"]
    suffix = uuid.uuid4().hex
    file_id = f"file_{suffix}"
    legacy_id = f"legacy_{suffix}"
    attempt_id = uuid.uuid4().hex
    prefix = f"{kb_id}/parsed/{file_id}/{attempt_id}"
    markdown = "# Original paper\n\nThe measured value is 5 A.\n"
    indexed_text = "Stale indexed summary. Model explanation must not become the original paper."
    assets = {
        "schema_version": 1,
        "kb_id": kb_id,
        "file_id": file_id,
        "attempt_id": attempt_id,
        "figures": [{"page": 1, "caption": "Original caption", "analysis": "MODEL_ANALYSIS_ONLY"}],
        "reviews": [{"block_id": "b1", "before": "5A", "after": "5 A", "critical": True}],
        "diagnostics": [{"kind": "needs_review", "reason": "REVIEW_DIAGNOSTIC_ONLY"}],
    }
    client = get_minio_client()
    bucket = client.KB_BUCKETS["parsed"]
    object_names = [f"{prefix}/document.md", f"{prefix}/assets.json"]
    engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    try:
        await asyncio.to_thread(client.ensure_bucket_exists, bucket)
        markdown_object = await client.aupload_file(bucket, object_names[0], markdown.encode("utf-8"))
        assets_object = await client.aupload_file(bucket, object_names[1], json.dumps(assets).encode("utf-8"))
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO knowledge_files "
                    "(file_id, kb_id, filename, file_type, status, is_folder, markdown_file, "
                    "parse_assets_file, chunk_count, token_count) VALUES "
                    "(:file, :kb, 'paper.pdf', 'pdf', 'indexed', FALSE, :markdown, :assets, 1, 5), "
                    "(:legacy, :kb, 'legacy.pdf', 'pdf', 'parsed', FALSE, NULL, NULL, 0, 0)"
                ),
                {
                    "file": file_id, "legacy": legacy_id, "kb": kb_id,
                    "markdown": markdown_object.url, "assets": assets_object.url,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO knowledge_chunks "
                    "(chunk_id, file_id, kb_id, chunk_index, content, graph_structure_indexed, "
                    "graph_indexed, graph_extraction_details) VALUES "
                    "(:chunk, :file, :kb, 0, :content, FALSE, FALSE, CAST(:details AS jsonb))"
                ),
                {
                    "chunk": f"chunk_{suffix}", "file": file_id, "kb": kb_id,
                    "content": indexed_text, "details": '{"status":"pending","attempt_count":0}',
                },
            )
        yield {
            "kb_id": kb_id, "file_id": file_id, "legacy_id": legacy_id,
            "attempt_id": attempt_id, "markdown": markdown, "assets": assets,
            "indexed_text": indexed_text, "engine": engine, "minio": client,
            "bucket": bucket, "object_names": object_names,
        }
    finally:
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM knowledge_files WHERE kb_id = :kb AND file_id IN (:file, :legacy)"),
                    {"kb": kb_id, "file": file_id, "legacy": legacy_id},
                )
        finally:
            try:
                for name in object_names:
                    await client.adelete_file(bucket, name)
            finally:
                await engine.dispose()


async def test_parse_assets_returns_published_sidecar(test_client, admin_headers, parsed_document_records):
    """返回的图、校对与诊断来自当前数据库指针指向的同次产物。"""
    doc = parsed_document_records
    response = await test_client.get(
        f"/api/knowledge/databases/{doc['kb_id']}/documents/{doc['file_id']}/parse-assets",
        headers=admin_headers,
    )

    assert response.status_code == 200, response.text
    assert response.json() == {**doc["assets"], "legacy_without_assets": False}


async def test_parse_assets_legacy_record_requests_reparse(test_client, admin_headers, parsed_document_records):
    """无 sidecar 的旧文件明确返回历史状态，不从旧正文猜测模型说明。"""
    doc = parsed_document_records
    response = await test_client.get(
        f"/api/knowledge/databases/{doc['kb_id']}/documents/{doc['legacy_id']}/parse-assets",
        headers=admin_headers,
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "figures": [], "reviews": [], "diagnostics": [],
        "attempt_id": None, "legacy_without_assets": True,
    }


@pytest.mark.parametrize("suffix", ["/content", ""])
async def test_document_content_prefers_published_raw_markdown(
    test_client, admin_headers, parsed_document_records, suffix
):
    """即使已有旧 chunks，正文仍逐字来自已发布 MD，附属模型文本不混入。"""
    doc = parsed_document_records
    response = await test_client.get(
        f"/api/knowledge/databases/{doc['kb_id']}/documents/{doc['file_id']}{suffix}",
        headers=admin_headers,
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["content"] == doc["markdown"]
    assert payload["attempt_id"] == doc["attempt_id"]
    assert payload["content_error"] is None
    assert [line["content"] for line in payload["lines"]] == [doc["indexed_text"]]
    assert "MODEL_ANALYSIS_ONLY" not in payload["content"]
    assert "REVIEW_DIAGNOSTIC_ONLY" not in payload["content"]


async def test_missing_raw_markdown_reports_error_without_exporting_chunks(
    test_client, admin_headers, parsed_document_records
):
    """正文对象丢失时保留读取错误，禁止拿分块或说明冒充论文原文。"""
    doc = parsed_document_records
    await doc["minio"].adelete_file(doc["bucket"], doc["object_names"][0])
    response = await test_client.get(
        f"/api/knowledge/databases/{doc['kb_id']}/documents/{doc['file_id']}/content",
        headers=admin_headers,
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["content"] == ""
    assert payload["content_error"] == "论文正文读取失败，请重试"
    assert [line["content"] for line in payload["lines"]] == [doc["indexed_text"]]


@pytest.mark.parametrize("missing", ["knowledge-base", "document"])
async def test_parse_assets_missing_resource_returns_404(
    test_client, admin_headers, parsed_document_records, missing
):
    """知识库或文档不存在时，真实路由都返回 404。"""
    doc = parsed_document_records
    kb_id = f"missing_{uuid.uuid4().hex}" if missing == "knowledge-base" else doc["kb_id"]
    file_id = doc["file_id"] if missing == "knowledge-base" else f"missing_{uuid.uuid4().hex}"
    response = await test_client.get(
        f"/api/knowledge/databases/{kb_id}/documents/{file_id}/parse-assets", headers=admin_headers
    )

    assert response.status_code == 404, response.text
    assert isinstance(response.json()["detail"], str)
    assert "MODEL_ANALYSIS_ONLY" not in response.text


async def test_parse_assets_rejects_file_from_another_knowledge_base(
    test_client, admin_headers, knowledge_database, parsed_document_records
):
    """可访问另一个知识库也不能跨库读取指定文档的解析产物。"""
    doc = parsed_document_records
    create = await test_client.post(
        "/api/knowledge/databases",
        headers=admin_headers,
        json={
            "database_name": f"pytest_parse_scope_{uuid.uuid4().hex}",
            "description": "Parse asset document ownership test",
            "kb_type": "milvus",
            "embedding_model_spec": knowledge_database["embedding_model_spec"],
            "additional_params": {},
        },
    )
    assert create.status_code == 200, create.text
    other_kb = create.json()["kb_id"]
    try:
        response = await test_client.get(
            f"/api/knowledge/databases/{other_kb}/documents/{doc['file_id']}/parse-assets",
            headers=admin_headers,
        )
        assert response.status_code == 404, response.text
        assert "not found" in response.json()["detail"]
        assert "MODEL_ANALYSIS_ONLY" not in response.text
    finally:
        deleted = await test_client.delete(f"/api/knowledge/databases/{other_kb}", headers=admin_headers)
        assert deleted.status_code == 200, deleted.text


@pytest.mark.parametrize("foreign_field", ["kb_id", "file_id"])
async def test_parse_assets_rejects_sidecar_with_foreign_ownership(
    test_client, admin_headers, parsed_document_records, foreign_field
):
    """即使数据库指针可读，附属 JSON 的知识库及文档归属仍必须匹配。"""
    doc = parsed_document_records
    foreign_assets = {**doc["assets"], foreign_field: f"other_{uuid.uuid4().hex}"}
    await doc["minio"].aupload_file(
        doc["bucket"], doc["object_names"][1], json.dumps(foreign_assets).encode("utf-8")
    )
    response = await test_client.get(
        f"/api/knowledge/databases/{doc['kb_id']}/documents/{doc['file_id']}/parse-assets",
        headers=admin_headers,
    )

    assert response.status_code == 404, response.text
    assert "不属于当前文件" in response.json()["detail"]
    assert "MODEL_ANALYSIS_ONLY" not in response.text


@pytest.mark.parametrize("suffix", ["/parse-assets", "/content"])
async def test_document_products_reject_standard_user(
    test_client, standard_user, parsed_document_records, suffix
):
    """真实认证依赖拒绝无管理端权限的用户，正文和 sidecar 都不可泄露。"""
    doc = parsed_document_records
    response = await test_client.get(
        f"/api/knowledge/databases/{doc['kb_id']}/documents/{doc['file_id']}{suffix}",
        headers=standard_user["headers"],
    )

    assert response.status_code == 403, response.text
    assert isinstance(response.json()["detail"], str)
    assert "MODEL_ANALYSIS_ONLY" not in response.text
    assert doc["markdown"] not in response.text
