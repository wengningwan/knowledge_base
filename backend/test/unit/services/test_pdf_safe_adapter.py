"""真实模型适配器不得先于 PDF 服务日志泄漏异常正文。"""

from types import SimpleNamespace

import pytest

from yuxi.models import chat


@pytest.mark.asyncio
async def test_pdf_adapter_redacts_remote_exception_and_does_not_forward_logging_flag(monkeypatch):
    """保留实际 select/adapter 路径，只替换 SDK 请求及配置，检查最终日志和异常。"""
    logs, options = [], []

    class Model:
        """以敏感错误模拟供应商 HTTP 响应。"""

        async def ainvoke(self, messages):
            raise ValueError("PRIVATE_DOCUMENT_AND_TOKEN")

    def load(spec, **kwargs):
        """日志选项属于本地适配器，不能传给供应商 SDK。"""
        options.append(kwargs)
        return Model()

    monkeypatch.setattr(chat, "logger", SimpleNamespace(info=logs.append, error=logs.append))
    monkeypatch.setattr(chat, "_require_chat_model_info", lambda spec: SimpleNamespace(model_id="model", base_url="https://example.invalid/v1?key=PRIVATE", provider_id="provider", provider_type="openai"))
    monkeypatch.setattr(chat, "load_chat_model", load)
    adapter = chat.select_model("test:model", timeout=None, max_retries=0, safe_error_logging=True)
    with pytest.raises(RuntimeError) as caught:
        await adapter.call("PRIVATE_DOCUMENT")
    assert options == [{"timeout": None, "max_retries": 0}]
    assert "ValueError" in str(caught.value)
    assert "PRIVATE" not in str(caught.value) + "\n".join(logs)
    assert caught.value.__suppress_context__ is True
