"""复用真实 HTTP/admin fixture 证明并发配置校验与持久回读。"""

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_document_concurrency_numeric_update_persists_and_invalid_updates_preserve_value(test_client, admin_headers):
    """正整数可保存，非法输入返回 400 且独立 GET 仍读到原值。"""
    before = await test_client.get("/api/system/config", headers=admin_headers)
    assert before.status_code == 200, before.text
    previous = before.json()["document_model_concurrency"]
    try:
        response = await test_client.post("/api/system/config/update", headers=admin_headers, json={"document_model_concurrency": "3"})
        assert response.status_code == 200, response.text
        read = await test_client.get("/api/system/config", headers=admin_headers)
        assert read.status_code == 200, read.text
        assert read.json()["document_model_concurrency"] == 3
        assert read.json()["_config_items"]["document_model_concurrency"]["type"] == "positive_int"
        for invalid in (0, -1, True, 1.5, "not-a-number"):
            rejected = await test_client.post("/api/system/config/update", headers=admin_headers, json={"document_model_concurrency": invalid})
            assert rejected.status_code == 400, rejected.text
            read = await test_client.get("/api/system/config", headers=admin_headers)
            assert read.status_code == 200, read.text
            assert read.json()["document_model_concurrency"] == 3
    finally:
        restored = await test_client.post("/api/system/config/update", headers=admin_headers, json={"document_model_concurrency": previous})
        assert restored.status_code == 200, restored.text


async def test_standard_user_cannot_change_document_concurrency(test_client, admin_headers, standard_user):
    """普通用户不能改变共享额度，拒绝后管理员回读值保持。"""
    before = await test_client.get("/api/system/config", headers=admin_headers)
    assert before.status_code == 200, before.text
    response = await test_client.post("/api/system/config/update", headers=standard_user["headers"], json={"document_model_concurrency": 7})
    assert response.status_code == 403, response.text
    after = await test_client.get("/api/system/config", headers=admin_headers)
    assert after.status_code == 200, after.text
    assert after.json()["document_model_concurrency"] == before.json()["document_model_concurrency"]
