"""同attempt产物、图片失败与来源边界的回归用例。"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from yuxi.knowledge.parser.result import ParsedDocument
from yuxi.services.knowledge_parse_service import save_parse_bundle
from yuxi.services.pdf_document_service import _read_service_image


class Control:
    async def check_cancelled(self):
        """允许本例的发布准备。"""


@pytest.mark.asyncio
async def test_bundle_has_one_version_and_separates_model_text(monkeypatch):
    """读取实际上传字节，不能仅凭上传次数证明纯正文。"""
    objects = {}

    async def upload(bucket, name, data):
        objects[name] = data
        return SimpleNamespace(url="https://minio.test/" + bucket + "/" + name)

    client = SimpleNamespace(KB_BUCKETS={"parsed": "parsed", "images": "images"}, ensure_bucket_exists=lambda _: None, aupload_file=upload)
    monkeypatch.setattr("yuxi.storage.minio.get_minio_client", lambda: client)
    result = ParsedDocument(markdown="Original paper ![](asset://fig)", assets={"fig": {"data": b"image", "mime": "image/png"}}, figures=[{"image_url": "asset://fig", "analysis": "模型解释"}], diagnostics=[{"reason": "解析提示"}])
    markdown, sidecar = await save_parse_bundle("kb1", "file1", "attempt1", result, Control())
    assert "/file1/attempt1/" in markdown and "/file1/attempt1/" in sidecar
    md = objects["kb1/parsed/file1/attempt1/document.md"].decode()
    evidence = json.loads(objects["kb1/parsed/file1/attempt1/assets.json"])
    assert md.startswith("Original paper ![](/api/knowledge/")
    assert "模型解释" not in md and "解析提示" not in md
    assert evidence["figures"][0]["analysis"] == "模型解释"
    assert (evidence["file_id"], evidence["kb_id"], evidence["attempt_id"]) == ("file1", "kb1", "attempt1")


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["https://other.test/x.png", "http://service.test/x.png", "https://user:secret@service.test/x.png"])
async def test_service_images_never_fetch_other_origin_or_embedded_credentials(monkeypatch, reference):
    """拒绝非法来源发生在创建网络客户端之前。"""
    monkeypatch.setattr("httpx.AsyncClient", lambda **kwargs: pytest.fail("不允许发出网络请求"))
    with pytest.raises(ValueError):
        await _read_service_image(reference, "https://service.test/v1")


@pytest.mark.asyncio
async def test_cancel_before_upload_never_writes_any_object(monkeypatch):
    class Cancelled:
        async def check_cancelled(self):
            raise asyncio.CancelledError

    async def forbidden(*args):
        pytest.fail("取消任务不得上传正文或图片")

    client = SimpleNamespace(KB_BUCKETS={"parsed": "parsed", "images": "images"}, ensure_bucket_exists=lambda _: None, aupload_file=forbidden)
    monkeypatch.setattr("yuxi.storage.minio.get_minio_client", lambda: client)
    with pytest.raises(asyncio.CancelledError):
        await save_parse_bundle("kb", "file", "attempt", ParsedDocument(markdown="paper"), Cancelled())
