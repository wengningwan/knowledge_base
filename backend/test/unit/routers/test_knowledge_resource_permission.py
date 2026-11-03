from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from server.routers import knowledge_router
from server.utils.auth_middleware import get_admin_user
from server.utils.knowledge_response import serialize_knowledge_base
from yuxi.knowledge.read_models import KnowledgeBaseSummary


def test_serialize_knowledge_base_redacts_credentials_from_compatibility_fields():
    database = KnowledgeBaseSummary(
        kb_id="kb-1",
        name="知识库",
        description=None,
        kb_type="dify",
        embedding_model_spec=None,
        llm_model_spec=None,
        query_params={},
        additional_params={"dify_token": "secret", "chunk_size": 100},
        share_config={"version": 2, "read_scope": None, "manage_scope": None},
        created_by=None,
        created_at=None,
    )

    response = serialize_knowledge_base(database, redact_secrets=True)

    assert response["additional_params"]["chunk_size"] == 100
    assert response["metadata"]["chunk_size"] == 100
    assert "dify_token" not in response["additional_params"]
    assert "dify_token" not in response["metadata"]


@pytest.mark.parametrize(("uid", "role", "can_read"), [("admin-1", "admin", True), ("other-user", "user", False)])
@pytest.mark.asyncio
async def test_non_manager_cannot_manage_global_read_knowledge_base(monkeypatch, uid, role, can_read):
    database = {
        "created_by": "owner",
        "share_config": {
            "version": 2,
            "read_scope": {"access_level": "global"},
            "manage_scope": None,
        },
    }

    async def fake_get_database_info(_kb_id):
        return database

    monkeypatch.setattr(knowledge_router.knowledge_base, "get_database_info", fake_get_database_info)
    user = SimpleNamespace(uid=uid, role=role, department_id=2)

    if can_read:
        assert await knowledge_router.require_knowledge_base_read("kb-1", user) is user

    with pytest.raises(HTTPException) as exc_info:
        await knowledge_router.require_knowledge_base_manage("kb-1", user)
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_query_parameter_routes_apply_knowledge_base_acl(monkeypatch):
    database = {
        "created_by": "owner",
        "share_config": {
            "version": 2,
            "read_scope": {"access_level": "user", "user_uids": ["admin-1"]},
            "manage_scope": None,
        },
    }

    async def fake_get_database_info(_kb_id):
        return database

    monkeypatch.setattr(knowledge_router.knowledge_base, "get_database_info", fake_get_database_info)
    readonly_admin = SimpleNamespace(uid="admin-1", role="admin", department_id=2)

    assert await knowledge_router.require_knowledge_base_read("kb-1", readonly_admin) is readonly_admin

    with pytest.raises(HTTPException) as exc_info:
        await knowledge_router.require_knowledge_base_read(
            "kb-1", SimpleNamespace(uid="admin-2", role="admin", department_id=2)
        )
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_review_confirmation_rejects_readonly_admin_at_real_route_dependency(monkeypatch):
    """读取权限可用的管理员仍不能调用校对写入路由。"""
    database = {
        "created_by": "owner",
        "kb_type": "milvus",
        "share_config": {"version": 2, "read_scope": {"access_level": "global"}, "manage_scope": None},
    }
    readonly_admin = SimpleNamespace(uid="readonly-admin", role="admin", department_id=2)
    published = {"markdown": "Original body", "reviews": [{"status": "unresolved"}]}

    async def get_database(_kb_id):
        """仅替换知识库读取，保留真实权限计算。"""
        return database

    async def get_document_support(_kb_id):
        """提供同一知识库的文档能力读取结果。"""
        return SimpleNamespace(kb_type="milvus", name="Fixture"), True

    async def confirm(*args, **kwargs):
        """错误放行会改变最终正文事实，负控不能只看调用次数。"""
        published["markdown"] = "Unauthorized body"
        published["reviews"] = [{"human_review": True}]
        return {"content_changed": True}

    monkeypatch.setattr(knowledge_router.knowledge_base, "get_database_info", get_database)
    monkeypatch.setattr(knowledge_router.knowledge_base, "get_database_document_support", get_document_support)
    monkeypatch.setattr(knowledge_router.knowledge_base, "confirm_file_review", confirm)
    assert await knowledge_router.require_knowledge_base_read("kb-1", readonly_admin) is readonly_admin
    app = FastAPI()
    app.include_router(knowledge_router.knowledge, prefix="/api")
    app.dependency_overrides[get_admin_user] = lambda: readonly_admin
    body = {"attempt_id": "attempt", "source_sha256": "a" * 64, "review_index": 0, "action": "keep"}
    endpoint = "/api/knowledge/databases/kb-1/documents/file-1/review-confirmations"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(endpoint, json=body)
        assert response.status_code == 403
        assert published == {"markdown": "Original body", "reviews": [{"status": "unresolved"}]}

        # 恢复 MANAGE 错接 READ 的缺陷，必须真的进入正文副作用。
        app.dependency_overrides[knowledge_router.require_knowledge_base_manage] = (
            knowledge_router.require_knowledge_base_read
        )
        weakened = await client.post(endpoint, json=body)
        assert weakened.status_code == 200, weakened.text
        assert published == {"markdown": "Unauthorized body", "reviews": [{"human_review": True}]}
