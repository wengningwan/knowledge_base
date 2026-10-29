"""OCR 禁用模式：PDF 插图提取与模型解析的单元测试。"""

from __future__ import annotations

import io
import os
from types import SimpleNamespace

import pytest
from PIL import Image

import yuxi.knowledge.parser.pdf_figures as pdf_figures
import yuxi.knowledge.parser.unified as parser_unified
from yuxi.knowledge.parser.pdf_figures import PdfFigure, extract_pdf_figures
from yuxi.services.ocr_service import resolve_ocr_task_params


def _png_bytes(width: int, height: int, mode: str = "RGB", noise: bool = True) -> bytes:
    image = Image.new(mode, (width, height))
    if noise:
        # 真随机字节几乎不可压缩，确保超过 MIN_IMAGE_BYTES；规则图案会被 PNG 压到阈值以下
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


def test_extract_figures_filters_small_and_tiny_images(monkeypatch: pytest.MonkeyPatch):
    small_edge = _png_bytes(64, 64)  # 边长不足
    small_bytes = _png_bytes(200, 200, noise=False)  # 纯色 PNG 字节量不足
    normal = _png_bytes(200, 200)
    _patch_pdf_reader(monkeypatch, [[small_edge, small_bytes, normal]])

    figures = extract_pdf_figures("fake.pdf")

    assert len(figures) == 1
    assert figures[0].name == "p1_1"
    assert figures[0].mime == "image/png"
    assert figures[0].filename == "p1_1.png"


def test_extract_figures_deduplicates_by_content(monkeypatch: pytest.MonkeyPatch):
    shared = _png_bytes(200, 200)
    other = _png_bytes(210, 210)
    _patch_pdf_reader(monkeypatch, [[shared, shared], [shared, other]])

    figures = extract_pdf_figures("fake.pdf")

    assert [figure.name for figure in figures] == ["p1_1", "p2_1"]


def test_extract_figures_converts_cmyk_to_png(monkeypatch: pytest.MonkeyPatch):
    _patch_pdf_reader(monkeypatch, [[_jpeg_cmyk_bytes(200, 200)]])

    figures = extract_pdf_figures("fake.pdf")

    assert len(figures) == 1
    assert figures[0].mime == "image/png"
    reopened = Image.open(io.BytesIO(figures[0].data))
    assert reopened.mode == "RGB"


def test_extract_figures_respects_max_limit(monkeypatch: pytest.MonkeyPatch):
    _patch_pdf_reader(monkeypatch, [[_png_bytes(200 + i, 200 + i) for i in range(5)]])

    figures = extract_pdf_figures("fake.pdf", max_figures=2)

    assert len(figures) == 2


def _figure(page: int, index: int) -> PdfFigure:
    return PdfFigure(page=page, index=index, name=f"p{page}_{index}", data=b"imgdata", mime="image/png")


_DISABLE_PARAMS = {"ocr_engine": "disable", "image_bucket": "kb-images", "image_prefix": "kb1/kb-images"}


@pytest.mark.asyncio
async def test_parse_pdf_disable_without_model_embeds_images_only(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(parser_unified, "extract_pdf_page_texts", lambda file: ["第一页", "第二页"])
    monkeypatch.setattr(
        pdf_figures, "extract_pdf_figures", lambda file, max_figures=None: [_figure(1, 1), _figure(2, 1)]
    )
    monkeypatch.setattr(
        parser_unified,
        "_upload_image_to_minio",
        lambda data, filename, bucket, prefix: f"/proxy/{filename}",
    )

    result = await parser_unified.parse_pdf_disable("fake.pdf", params=dict(_DISABLE_PARAMS))

    assert "第一页" in result and "第二页" in result
    assert "![figure_p1_1](/proxy/p1_1.png)" in result
    assert "![figure_p2_1](/proxy/p2_1.png)" in result
    assert "插图解析" not in result
    assert result.index("第一页") < result.index("figure_p1_1") < result.index("第二页") < result.index("figure_p2_1")


@pytest.mark.asyncio
async def test_parse_pdf_disable_with_model_appends_analysis(monkeypatch: pytest.MonkeyPatch):
    import yuxi.models.chat as chat_module

    monkeypatch.setattr(parser_unified, "extract_pdf_page_texts", lambda file: ["第一页"])
    monkeypatch.setattr(pdf_figures, "extract_pdf_figures", lambda file, max_figures=None: [_figure(1, 1)])
    monkeypatch.setattr(
        parser_unified,
        "_upload_image_to_minio",
        lambda data, filename, bucket, prefix: f"/proxy/{filename}",
    )

    async def fake_call(message, stream=False):
        return SimpleNamespace(content="这是一张数据图表")

    monkeypatch.setattr(chat_module, "select_model", lambda spec: SimpleNamespace(call=fake_call))

    params = dict(_DISABLE_PARAMS)
    params["image_analysis_model"] = "p:vision"
    result = await parser_unified.parse_pdf_disable("fake.pdf", params=params)

    assert "**插图解析**：这是一张数据图表" in result


@pytest.mark.asyncio
async def test_parse_pdf_disable_degrades_when_model_call_fails(monkeypatch: pytest.MonkeyPatch):
    import yuxi.models.chat as chat_module

    monkeypatch.setattr(parser_unified, "extract_pdf_page_texts", lambda file: ["第一页"])
    monkeypatch.setattr(pdf_figures, "extract_pdf_figures", lambda file, max_figures=None: [_figure(1, 1)])
    monkeypatch.setattr(
        parser_unified,
        "_upload_image_to_minio",
        lambda data, filename, bucket, prefix: f"/proxy/{filename}",
    )

    async def failing_call(message, stream=False):
        raise RuntimeError("boom")

    monkeypatch.setattr(chat_module, "select_model", lambda spec: SimpleNamespace(call=failing_call))

    params = dict(_DISABLE_PARAMS)
    params["image_analysis_model"] = "p:vision"
    result = await parser_unified.parse_pdf_disable("fake.pdf", params=params)

    assert "![figure_p1_1]" in result
    assert "插图解析" not in result


@pytest.mark.asyncio
async def test_parse_pdf_disable_raises_for_invalid_model_spec(monkeypatch: pytest.MonkeyPatch):
    import yuxi.models.chat as chat_module

    monkeypatch.setattr(parser_unified, "extract_pdf_page_texts", lambda file: ["第一页"])
    monkeypatch.setattr(pdf_figures, "extract_pdf_figures", lambda file, max_figures=None: [_figure(1, 1)])

    def invalid_spec(spec):
        raise ValueError("未找到模型")

    monkeypatch.setattr(chat_module, "select_model", invalid_spec)

    params = dict(_DISABLE_PARAMS)
    params["image_analysis_model"] = "p:missing"
    with pytest.raises(ValueError):
        await parser_unified.parse_pdf_disable("fake.pdf", params=params)


def _patch_system_options(monkeypatch: pytest.MonkeyPatch):
    async def failing_get(db=None):
        raise AssertionError("显式指定 ocr_engine 时不应查询系统配置")

    import yuxi.services.ocr_service as ocr_service

    monkeypatch.setattr(ocr_service, "system_options", SimpleNamespace(get=failing_get))


@pytest.mark.asyncio
async def test_resolve_params_keeps_explicit_image_model_without_db(monkeypatch: pytest.MonkeyPatch):
    _patch_system_options(monkeypatch)

    resolved = await resolve_ocr_task_params({"ocr_engine": "disable", "image_analysis_model": "p:vision"})

    assert resolved["image_analysis_model"] == "p:vision"
    assert resolved["_ocr_processor_kwargs"] == {}


@pytest.mark.asyncio
async def test_resolve_params_ignores_default_image_model_fallback(monkeypatch: pytest.MonkeyPatch):
    # 系统默认值由前端预填进 params，后端显式指定 ocr_engine 时不回退查询配置
    _patch_system_options(monkeypatch)

    resolved = await resolve_ocr_task_params({"ocr_engine": "disable"})

    assert "image_analysis_model" not in resolved


@pytest.mark.asyncio
async def test_resolve_params_drops_image_model_for_ocr_engines(monkeypatch: pytest.MonkeyPatch):
    import yuxi.services.ocr_service as ocr_service

    async def fake_build(db, engine_id):
        return {}

    monkeypatch.setattr(ocr_service, "_build_processor_kwargs", fake_build)

    # 传入假 db，避免 _build_processor_kwargs 之外的 PG 会话创建
    resolved = await resolve_ocr_task_params(
        {"ocr_engine": "rapid_ocr", "image_analysis_model": "p:vision"},
        db=SimpleNamespace(),
    )

    assert "image_analysis_model" not in resolved
    assert resolved["ocr_engine"] == "rapid_ocr"
