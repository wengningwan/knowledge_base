"""真实 API、Durable Task worker、PG 与 Milvus 的论文入库回归。"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [pytest.mark.asyncio, pytest.mark.e2e]


async def test_paper_ingestion_persists_sources_and_returns_same_indexed_evidence(e2e_client, e2e_headers):
    """从上传入口经过独立 worker 后回读正文、来源及真实 BM25 召回。"""
    model_spec = os.getenv("TEST_PAPER_EMBEDDING_MODEL_SPEC")
    if not model_spec:
        pytest.skip("真实 embedding 未配置：设置 TEST_PAPER_EMBEDDING_MODEL_SPEC 后运行论文入库 E2E")
    paper = (Path(__file__).resolve().parents[1] / "fixtures/paper_chunking/paper.md").read_bytes()
    response = await e2e_client.post(
        "/api/knowledge/databases",
        headers=e2e_headers,
        json={
            "database_name": f"pytest_paper_{uuid.uuid4().hex[:8]}",
            "kb_type": "milvus",
            "embedding_model_spec": model_spec,
            "description": "synthetic paper ingestion regression",
            "additional_params": {"chunk_preset_id": "paper", "chunk_parser_config": {"chunk_token_num": 80}},
        },
    )
    assert response.status_code == 200, response.text
    kb_id = response.json()["kb_id"]
    base = f"/api/knowledge/databases/{kb_id}"
    engine = create_async_engine(os.environ["POSTGRES_URL"], pool_pre_ping=True)
    try:
        uploaded = await e2e_client.post(
            "/api/knowledge/files/upload",
            params={"kb_id": kb_id},
            headers=e2e_headers,
            files={"file": ("synthetic-paper.md", paper, "text/markdown")},
        )
        assert uploaded.status_code == 200, uploaded.text
        upload = uploaded.json()
        added = await e2e_client.post(
            f"{base}/documents/add",
            headers=e2e_headers,
            json={
                "items": [upload["file_path"]],
                "params": {
                    "content_type": "file",
                    "content_hashes": {upload["file_path"]: upload["content_hash"]},
                },
            },
        )
        assert added.status_code == 200 and added.json()["failed"] == 0, added.text
        file_id = added.json()["items"][0]["file_id"]
        task_ids = []
        for action in ("parse", "index"):
            submitted = await e2e_client.post(
                f"{base}/documents/{action}", headers=e2e_headers, json={"file_ids": [file_id], "params": {}}
            )
            assert submitted.status_code == 200 and submitted.json()["status"] == "queued", submitted.text
            task_id = submitted.json()["task_id"]
            task_ids.append(task_id)
            task = await _wait_for_terminal_task(e2e_client, e2e_headers, task_id)
            assert task["status"] == "success" and task["result"]["failed"] == 0, task

        info_response = await e2e_client.get(f"{base}/documents/{file_id}", headers=e2e_headers)
        assert info_response.status_code == 200, info_response.text
        info = info_response.json()
        assert info["meta"]["status"] == "indexed", info
        digest = hashlib.sha256(info["content"].encode("utf-8")).hexdigest()
        assert info["source_sha256"] == digest
        assert info["lines"] and all(line["source_metadata"]["source_sha256"] == digest for line in info["lines"])
        formula = next(line for line in info["lines"] if r"\tag{1}" in line["content"])
        assert "In eq 1, q is uptake" in formula["content"]
        result = next(line for line in info["lines"] if "Sample SX-1 reached" in line["content"])
        assert "300 °C for 2 h under nitrogen" in result["content"]

        async with engine.connect() as connection:
            rows = (
                (
                    await connection.execute(
                        text(
                            "SELECT chunk_id, content, source_metadata FROM knowledge_chunks "
                            "WHERE kb_id = :kb AND file_id = :file ORDER BY chunk_index"
                        ),
                        {"kb": kb_id, "file": file_id},
                    )
                )
                .mappings()
                .all()
            )
            tasks = (
                (
                    await connection.execute(
                        text(
                            "SELECT id, type, status, attempt_count, started_at, completed_at "
                            "FROM tasks WHERE id IN (:parse, :index)"
                        ),
                        {"parse": task_ids[0], "index": task_ids[1]},
                    )
                )
                .mappings()
                .all()
            )
        assert [(row["chunk_id"], row["content"], row["source_metadata"]) for row in rows] == [
            (line["id"], line["content"], line["source_metadata"]) for line in info["lines"]
        ]
        assert {task["type"] for task in tasks} == {"knowledge_parse", "knowledge_index"}
        assert all(task["attempt_count"] >= 1 and task["started_at"] and task["completed_at"] for task in tasks)

        # BM25 直接读取实际派生索引；入库和来源不能只由 PG 或任务成功消息证明。
        hits = []
        for _ in range(30):
            queried = await e2e_client.post(
                f"{base}/query",
                headers=e2e_headers,
                json={
                    "query": "affinity saturation uptake pressure",
                    "meta": {"search_mode": "keyword", "final_top_k": 30, "bm25_top_k": 30, "use_reranker": False},
                },
            )
            assert queried.status_code == 200 and queried.json()["status"] == "success", queried.text
            hits = queried.json()["result"]
            if any(hit["metadata"]["chunk_id"] == formula["id"] for hit in hits):
                break
            await asyncio.sleep(0.2)
        hit = next((hit for hit in hits if hit["metadata"]["chunk_id"] == formula["id"]), None)
        assert hit is not None, hits
        assert hit["content"] == formula["content"]
        assert hit["metadata"]["source_metadata"] == formula["source_metadata"]
    finally:
        await engine.dispose()
        deleted = await e2e_client.delete(base, headers=e2e_headers)
        assert deleted.status_code == 200, deleted.text


async def _wait_for_terminal_task(client, headers, task_id):
    """等待真实 worker 写入终态，失败时保留领域错误。"""
    for _ in range(300):
        response = await client.get(f"/api/tasks/{task_id}", headers=headers)
        assert response.status_code == 200, response.text
        task = response.json()["task"]
        if task["status"] in {"success", "failed", "cancelled"}:
            return task
        await asyncio.sleep(0.5)
    raise AssertionError(f"入库 worker 未在 150 秒内收敛：{task_id}")
