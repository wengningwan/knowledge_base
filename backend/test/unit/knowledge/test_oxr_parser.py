"""OXR 表单、状态与任务恢复契约；全部 HTTP 使用内存传输。"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from email.parser import BytesParser
from email.policy import default
from pathlib import Path

import httpx
import pytest

import yuxi.knowledge.parser.oxr as oxr
from yuxi.knowledge.parser.base import DocumentParserException


@dataclass
class Control:
    """只保存当前测试任务的取消与恢复状态。"""

    checkpoint: dict = field(default_factory=dict)
    cancelled: bool = False
    checks: int = 0

    async def check_cancelled(self):
        """模拟持久控制对象的取消边界。"""
        self.checks += 1
        if self.cancelled:
            raise asyncio.CancelledError

    def remaining_seconds(self):
        """给本地测试一个足够且确定的任务预算。"""
        return 90

    async def load_checkpoint(self):
        """读取当前任务 checkpoint。"""
        return self.checkpoint

    async def save_checkpoint(self, data):
        """保存已提交的远端任务标识。"""
        self.checkpoint = data


def _install_transport(monkeypatch, handler):
    """把真实客户端限定到 MockTransport，禁止测试访问网络。"""
    client = httpx.AsyncClient

    def build_client(**kwargs):
        """复用真实编码器，替换唯一网络传输边界。"""
        return client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(oxr.httpx, "AsyncClient", build_client)


def _source(tmp_path: Path) -> Path:
    """写入仅供上传协议测试使用的临时源文件。"""
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"%PDF contract fixture")
    return path


def _success(status="SUCCESS") -> dict:
    """构造有正文块的线上或开源状态结果。"""
    return {"code": 0, "data": {"status": status, "result": {"outputs": {"json": {"pages": [{"page_num": 0, "page_size": {"w": 100, "h": 100}, "blocks": [{"idx": 3, "type": "Text", "bbox": [5, 5, 95, 15], "content": "accepted 12 mg"}]}]}}}}}


def test_async_protocol_repeats_formats_and_accepts_uppercase_success(tmp_path, monkeypatch):
    """真实 multipart 编码须保留两个格式字段和已确认的查询协议。"""
    requests = []

    def handler(request):
        """按协议响应提交和查询，不运行服务。"""
        requests.append(request)
        if request.url.path.endswith("/async"):
            return httpx.Response(200, json={"code": 0, "data": {"task_id": "task-1", "status": "QUEUING"}})
        assert request.method == "POST"
        assert request.url.path.endswith("/parse/async_check")
        assert request.content == b"task_id=task-1&api_key=dummy"
        return httpx.Response(200, json=_success())

    _install_transport(monkeypatch, handler)
    result = asyncio.run(oxr.OXRParser("https://oxr.test/api/v1").process_file_result(str(_source(tmp_path))))

    assert result.markdown == "accepted 12 mg"
    message = BytesParser(policy=default).parsebytes(b"Content-Type: " + requests[0].headers["content-type"].encode() + b"\r\n\r\n" + requests[0].content)
    fields = [(part.get_param("name", header="content-disposition"), part.get_payload(decode=True)) for part in message.iter_parts()]
    assert [value for key, value in fields if key == "output_formats"] == [b"markdown", b"json"]
    assert ("feature_config", b'{"keep_header_footer": false}') in fields
    assert ("element_formats", b'{"image": "base64", "formula": "latex", "table": "html", "cs": "image"}') in fields
    assert not any(key in {"feature", "extra"} for key, _ in fields)


def test_http_200_business_failure_is_not_success(tmp_path, monkeypatch):
    """HTTP 成功不能覆盖 code 非零的提交失败。"""
    calls = []

    def handler(request):
        """返回官方参数错误代码。"""
        calls.append(request)
        return httpx.Response(200, json={"code": 1133, "message": "invalid options", "data": None})

    _install_transport(monkeypatch, handler)
    with pytest.raises(DocumentParserException, match="code=1133"):
        asyncio.run(oxr.OXRParser("https://oxr.test/v1").process_file_result(str(_source(tmp_path))))
    assert len(calls) == 1


def test_checkpoint_resumes_poll_without_resubmitting(tmp_path, monkeypatch):
    """源摘要和服务指纹匹配时只查询原任务，并保留其它阶段数据。"""
    path = _source(tmp_path)
    server = "https://oxr.test/v1"
    control = Control(checkpoint={"other_stage": "kept", "oxr": {"remote_task_id": "existing-task", "source_hash": hashlib.sha256(path.read_bytes()).hexdigest(), "server_url_fingerprint": hashlib.sha256(server.encode()).hexdigest()}})

    def handler(request):
        """任何意外重传都会在路径断言处失败。"""
        assert request.url.path == "/v1/parse/async_check"
        assert b"existing-task" in request.content
        return httpx.Response(200, json=_success("success"))

    _install_transport(monkeypatch, handler)
    result = asyncio.run(oxr.OXRParser(server).process_file_result(str(path), control=control))
    assert result.blocks[0]["idx"] == 3
    assert control.checkpoint["other_stage"] == "kept"


def test_cancel_after_submit_persists_task_then_propagates(tmp_path, monkeypatch):
    """持久任务提交后收到取消时保留远端 ID，且不查询或假装取消远端。"""
    control = Control()
    original_save = control.save_checkpoint

    async def save_then_cancel(data):
        """在上传 checkpoint 落地之后请求取消。"""
        await original_save(data)
        control.cancelled = True

    control.save_checkpoint = save_then_cancel
    calls = []

    def handler(request):
        """模拟唯一的一次成功提交。"""
        calls.append(request)
        return httpx.Response(200, json={"code": 0, "data": {"task_id": "cancelled-local", "status": "queuing"}})

    _install_transport(monkeypatch, handler)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(oxr.OXRParser("https://oxr.test/v1").process_file_result(str(_source(tmp_path)), control=control))
    assert control.checkpoint["oxr"]["remote_task_id"] == "cancelled-local"
    assert len(calls) == 1


def test_submit_http_failure_does_not_retry(tmp_path, monkeypatch):
    """避免上传不确定失败后的盲重试创建多个远端任务。"""
    calls = []

    def handler(request):
        """提交返回暂时错误，但适配器仍不得自动重传。"""
        calls.append(request)
        return httpx.Response(503, json={"code": 1})

    _install_transport(monkeypatch, handler)
    with pytest.raises(DocumentParserException, match="未自动重传"):
        asyncio.run(oxr.OXRParser("https://oxr.test/v1").process_file_result(str(_source(tmp_path))))
    assert len(calls) == 1


def test_poll_http_retries_are_bounded(tmp_path, monkeypatch):
    """查询暂时错误有三次上限，三秒等待由测试替换。"""
    calls = []

    async def no_delay(self, deadline, control):
        """移除测试等待，不改变查询重试上限。"""

    def handler(request):
        """提交成功，查询持续返回限流。"""
        calls.append(request)
        if request.url.path.endswith("/async"):
            return httpx.Response(200, json={"code": 0, "data": {"task_id": "rate-limited"}})
        return httpx.Response(429)

    _install_transport(monkeypatch, handler)
    monkeypatch.setattr(oxr.OXRParser, "_pause", no_delay)
    with pytest.raises(DocumentParserException, match="HTTP 429"):
        asyncio.run(oxr.OXRParser("https://oxr.test/v1").process_file_result(str(_source(tmp_path))))
    assert len(calls) == 4


def test_uppercase_failed_surfaces_redacted_remote_error(tmp_path, monkeypatch):
    """大写 FAILED 不能误判成功，错误中回显的配置密钥必须删除。"""
    def handler(request):
        """回传带密钥回显的错误文本以验证脱敏。"""
        if request.url.path.endswith("/async"):
            return httpx.Response(200, json={"code": 0, "data": {"task_id": "failed-task"}})
        return httpx.Response(200, json={"code": 0, "data": {"status": "FAILED", "error": "rejected secret-test-key"}})

    _install_transport(monkeypatch, handler)
    with pytest.raises(DocumentParserException) as error:
        asyncio.run(oxr.OXRParser("https://oxr.test/v1", "secret-test-key").process_file_result(str(_source(tmp_path))))
    assert "secret-test-key" not in str(error.value)
    assert "OXR 解析失败" in str(error.value)


def test_health_only_reports_configuration(monkeypatch):
    """健康检查不创建客户端或提交探针。"""
    def forbidden_client(**kwargs):
        """一旦健康检查尝试访问网络就失败。"""
        raise AssertionError("health must not perform I/O")

    monkeypatch.setattr(oxr.httpx, "AsyncClient", forbidden_client)
    assert oxr.OXRParser().check_health()["status"] == "configured"



def test_cancel_flag_during_successful_submit_retains_known_remote_id(tmp_path, monkeypatch):
    """响应已知但取消尚未检查时也须保存恢复编号。"""
    control = Control()
    calls = []

    def handler(request):
        calls.append(request)
        control.cancelled = True
        return httpx.Response(200, json={"code": 0, "data": {"task_id": "known-before-cancel"}})

    _install_transport(monkeypatch, handler)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(oxr.OXRParser("https://oxr.test/v1").process_file_result(str(_source(tmp_path)), control=control))
    assert control.checkpoint["oxr"]["remote_task_id"] == "known-before-cancel"
    assert len(calls) == 1



@pytest.mark.asyncio
async def test_worker_task_cancel_waits_for_known_submission_checkpoint(tmp_path, monkeypatch):
    """直接取消执行协程，模拟worker heartbeat在PG提交等待中取消。"""
    control = Control()
    started, release = asyncio.Event(), asyncio.Event()
    original_save = control.save_checkpoint

    async def blocked_save(data):
        started.set()
        await release.wait()
        await original_save(data)

    control.save_checkpoint = blocked_save

    def handler(request):
        assert request.url.path.endswith("/async")
        return httpx.Response(200, json={"code": 0, "data": {"task_id": "committed-remote"}})

    _install_transport(monkeypatch, handler)
    task = asyncio.create_task(oxr.OXRParser("https://oxr.test/v1").process_file_result(str(_source(tmp_path)), control=control))
    try:
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert control.checkpoint["oxr"]["remote_task_id"] == "committed-remote"
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
