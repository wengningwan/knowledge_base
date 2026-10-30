"""OCR 禁用模式：PDF 插图提取与模型解析的单元测试。"""

from __future__ import annotations

import io
import os
from types import SimpleNamespace

import pytest
from PIL import Image

from test.unit.services.pdf_model_fakes import configure_pdf_execution

import yuxi.knowledge.parser.pdf_figures as pdf_figures
import yuxi.knowledge.parser.unified as parser_unified
from yuxi.knowledge.parser.pdf_figures import PdfFigure, extract_pdf_figures
from yuxi.knowledge.parser.base import DocumentParserException
from yuxi.services.ocr_service import resolve_ocr_task_params


def _png_bytes(width: int, height: int, mode: str = "RGB", noise: bool = True) -> bytes:
    image = Image.new(mode, (width, height))
    if noise:
        # 同时覆盖高熵图片和高度压缩的线图，压缩体积不作为内容重要性的依据。
        image.frombytes(os.urandom(width * height * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg_cmyk_bytes(width: int, height: int) -> bytes:
    image = Image.new("RGB", (width, height))
    image.frombytes(os.urandom(width * height * 3))
    buffer = io.BytesIO()
    image.convert("CMYK").save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


class _FakeReader:
    def __init__(self, pages):
        self.pages = [
            SimpleNamespace(images=[SimpleNamespace(name=f"img{i}", data=data) for i, data in enumerate(page, 1)])
            for page in pages
        ]


def _patch_pdf_reader(monkeypatch: pytest.MonkeyPatch, pages) -> None:
    monkeypatch.setattr(pdf_figures, "PdfReader", lambda path: _FakeReader(pages))


def test_extract_figures_preserves_small_and_compressed_images(monkeypatch: pytest.MonkeyPatch):
    small_edge = _png_bytes(64, 64)
    small_bytes = _png_bytes(200, 200, noise=False)
    normal = _png_bytes(200, 200)
    _patch_pdf_reader(monkeypatch, [[small_edge, small_bytes, normal]])

    figures = extract_pdf_figures("fake.pdf")

    assert len(figures) == 3
    assert figures[0].name == "p1_1"
    assert figures[0].mime == "image/png"
    assert figures[0].filename == "p1_1.png"


def test_extract_figures_preserves_repeated_images_on_each_page(monkeypatch: pytest.MonkeyPatch):
    shared = _png_bytes(200, 200)
    other = _png_bytes(210, 210)
    _patch_pdf_reader(monkeypatch, [[shared, shared], [shared, other]])

    figures = extract_pdf_figures("fake.pdf")

    assert [figure.name for figure in figures] == ["p1_1", "p1_2", "p2_1", "p2_2"]


def test_extract_figures_converts_cmyk_to_png(monkeypatch: pytest.MonkeyPatch):
    _patch_pdf_reader(monkeypatch, [[_jpeg_cmyk_bytes(200, 200)]])

    figures = extract_pdf_figures("fake.pdf")

    assert len(figures) == 1
    assert figures[0].mime == "image/png"
    reopened = Image.open(io.BytesIO(figures[0].data))
    assert reopened.mode == "RGB"


def test_extract_figures_respects_max_limit(monkeypatch: pytest.MonkeyPatch):
    _patch_pdf_reader(monkeypatch, [[_png_bytes(200 + i, 200 + i) for i in range(5)]])

    with pytest.raises(DocumentParserException, match="超过 2 张"):
        extract_pdf_figures("fake.pdf", max_figures=2)


_DISABLE_PARAMS = {"ocr_engine": "disable", "image_bucket": "kb-images", "image_prefix": "kb1/kb-images"}


@pytest.mark.asyncio
async def test_native_model_explanation_never_enters_paper_markdown(monkeypatch):
    """实际组合、渲染和模型消息路径保留图位，把解释与诊断留在 sidecar。"""
    import yuxi.knowledge.parser.pdf_native as native
    import yuxi.models.chat as chat
    from yuxi.knowledge.parser.pdf_structure import finalize_document
    from yuxi.services.pdf_document_service import build_native_document, enrich_document

    configure_pdf_execution(monkeypatch)
    source = _png_bytes(50, 30, noise=False)
    monkeypatch.setattr(native, "read_native_blocks", lambda _: [
        {"page": 1, "bbox": [.1, .1, .9, .2], "content": "Original text 12 mg."},
        {"page": 1, "bbox": [.1, .6, .9, .65], "content": "Figure 1. Original caption."},
        {"page": 1, "bbox": [.1, .7, .9, .8], "content": "Next paragraph."},
    ])
    monkeypatch.setattr(native, "read_native_image_regions", lambda _: [{"page": 1, "bbox": [.1, .3, .9, .59]}])
    monkeypatch.setattr(native, "render_pdf_region", lambda *args: source)
    received = []

    async def call(messages, stream=False):
        received.extend(messages)
        return SimpleNamespace(content="模型生成说明：a visible curve")

    monkeypatch.setattr(chat, "select_model", lambda *args, **kwargs: SimpleNamespace(call=call))
    result = await build_native_document("source.pdf")
    result.diagnostics.append({"reason": "解析提示仅在侧栏"})
    await enrich_document(result, "source.pdf", {"ocr_engine": "disable", "image_analysis_model": "p:vision"})
    finalize_document(result, {key: "/api/knowledge/test/image.png" for key in result.assets})
    assert result.markdown.index("Original text") < result.markdown.index("![](") < result.markdown.index("Figure 1.") < result.markdown.index("Next paragraph.")
    assert "模型生成" not in result.markdown and "解析提示" not in result.markdown
    assert result.figures[0]["analysis_status"] == "completed"
    assert "a visible curve" in result.sidecar()["figures"][0]["analysis"]
    assert "Original text 12 mg" not in str(received[0].content)
    assert "Figure 1. Original caption" in str(received[0].content)


@pytest.mark.asyncio
async def test_failed_figure_model_preserves_original_and_reports_separate_status(monkeypatch):
    import yuxi.models.chat as chat
    from yuxi.knowledge.parser.result import ParsedDocument
    from yuxi.services.pdf_figure_analysis_service import analyze_document_figures
    configure_pdf_execution(monkeypatch)
    calls = []

    async def call(*args, **kwargs):
        calls.append(True)
        raise ValueError("unavailable")

    monkeypatch.setattr(chat, "select_model", lambda *args, **kwargs: SimpleNamespace(call=call))
    result = ParsedDocument(markdown="Source ![](asset://a)", figures=[{"id": "f", "asset_id": "a"}], assets={"a": {"data": _png_bytes(10, 10)}})
    await analyze_document_figures(result, {"image_analysis_model": "p:vision"})
    assert len(calls) == 1
    assert result.markdown == "Source ![](asset://a)"
    assert result.figures[0]["analysis_status"] == "failed"


@pytest.mark.asyncio
async def test_empty_native_document_cannot_claim_success(monkeypatch):
    import yuxi.services.pdf_document_service as service
    from yuxi.knowledge.parser.result import ParsedDocument

    async def empty(*args):
        return ParsedDocument()

    monkeypatch.setattr(service, "build_native_document", empty)
    with pytest.raises(DocumentParserException, match="没有可提取"):
        await parser_unified.parse_pdf_disable_result("source.pdf", dict(_DISABLE_PARAMS))


def _patch_system_options(monkeypatch):
    """只有需要系统默认值的任务才查询配置。"""
    import yuxi.services.ocr_service as ocr_service

    async def forbidden(db=None):
        """显式选择和临时附件不读取默认模型。"""
        pytest.fail("不应查询系统配置")

    monkeypatch.setattr(ocr_service, "system_options", SimpleNamespace(get=forbidden))


@pytest.mark.asyncio
async def test_explicit_image_model_is_preserved(monkeypatch):
    """显式选择的图片模型进入解析参数。"""
    _patch_system_options(monkeypatch)
    result = await resolve_ocr_task_params({**_DISABLE_PARAMS, "image_analysis_model": "p:vision"})
    assert result["image_analysis_model"] == "p:vision"
    assert result["_ocr_processor_kwargs"] == {}


@pytest.mark.asyncio
async def test_explicitly_cleared_model_does_not_restore_the_system_default(monkeypatch):
    """页面清空图片模型即本次不调用模型，不能被默认值覆盖。"""
    _patch_system_options(monkeypatch)
    result = await resolve_ocr_task_params({**_DISABLE_PARAMS, "image_analysis_model": ""})
    assert result["image_analysis_model"] == ""


@pytest.mark.asyncio
async def test_knowledge_params_can_use_default_image_model_if_unspecified(monkeypatch):
    """未传模型字段的知识库 API 请求可读取系统默认。"""
    import yuxi.services.ocr_service as ocr_service

    async def get_options(db=None):
        """提供已配置的默认图像模型。"""
        return {"default_image_analysis_model": "p:default-vision"}

    monkeypatch.setattr(ocr_service, "system_options", SimpleNamespace(get=get_options))
    result = await resolve_ocr_task_params(dict(_DISABLE_PARAMS))
    assert result["image_analysis_model"] == "p:default-vision"


@pytest.mark.asyncio
async def test_ocr_enabled_keeps_existing_engine_and_ignores_picture_model(monkeypatch):
    """启用 OCR 时仍使用原引擎，图片模型参数不影响其行为。"""
    import yuxi.services.ocr_service as ocr_service

    async def build(db, engine_id):
        """提供引擎原有运行时配置。"""
        assert engine_id == "rapid_ocr"
        return {"existing_option": True}

    monkeypatch.setattr(ocr_service, "_build_processor_kwargs", build)
    result = await resolve_ocr_task_params(
        {"ocr_engine": "rapid_ocr", "image_analysis_model": "p:vision"}, db=object()
    )
    assert result["ocr_engine"] == "rapid_ocr"
    assert result["_ocr_processor_kwargs"] == {"existing_option": True}
    assert "image_analysis_model" not in result


@pytest.mark.asyncio
async def test_tmp_attachment_does_not_get_unknown_knowledge_images(monkeypatch):
    """临时附件无知识库图片归属时维持只读取文字层。"""
    _patch_system_options(monkeypatch)
    monkeypatch.setattr(parser_unified, "extract_pdf_page_texts", lambda _: ["Attachment body"])

    def forbidden(*args, **kwargs):
        """没有图片归属时不提取或上传知识库图片。"""
        pytest.fail("不应处理知识库图片")

    monkeypatch.setattr(pdf_figures, "extract_pdf_figures", forbidden)
    result = await resolve_ocr_task_params({"ocr_engine": "disable"})
    assert "image_analysis_model" not in result
    assert await parser_unified.parse_pdf_disable("fake.pdf", result) == "Attachment body"
