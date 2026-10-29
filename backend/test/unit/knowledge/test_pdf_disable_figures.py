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
from yuxi.knowledge.parser.base import DocumentParserException
from yuxi.knowledge.parser.pdf_markdown import PdfPageMarkdown
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


def _figure(page: int, index: int) -> PdfFigure:
    return PdfFigure(page=page, index=index, name=f"p{page}_{index}", data=b"imgdata", mime="image/png")


_DISABLE_PARAMS = {"ocr_engine": "disable", "image_bucket": "kb-images", "image_prefix": "kb1/kb-images"}


@pytest.fixture(autouse=True)
def _stub_page_transcription(monkeypatch: pytest.MonkeyPatch):
    """用显式段落及图片位置样本隔离实际模型和 PDF 渲染。"""
    async def transcribe(model, file, number, text, previous, figures):
        """提供插图在正文之前的装配 oracle。"""
        markers = "\n\n".join(f"<!-- figure:{figure.name} -->\n\nFigure {figure.index}. 原文图注" for figure in figures)
        return PdfPageMarkdown(markdown=f"{markers}\n\n{text}".strip())

    monkeypatch.setattr("yuxi.knowledge.parser.pdf_markdown.transcribe_pdf_page", transcribe)


@pytest.mark.asyncio
async def test_knowledge_pdf_without_model_fails_before_upload(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(parser_unified, "extract_pdf_page_texts", lambda file: ["第一页", "第二页"])
    monkeypatch.setattr(
        pdf_figures, "extract_pdf_figures", lambda file, max_figures=None: [_figure(1, 1), _figure(2, 1)]
    )
    monkeypatch.setattr(
        parser_unified,
        "_upload_image_to_minio",
        lambda data, filename, bucket, prefix: f"/proxy/{filename}",
    )

    with pytest.raises(ValueError, match="需要支持图像输入"):
        await parser_unified.parse_pdf_disable("fake.pdf", params=dict(_DISABLE_PARAMS))


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

    assert "> **插图解析（模型生成）**\n>\n> 这是一张数据图表" in result
    assert result.index("figure_p1_1") < result.index("Figure 1. 原文图注") < result.index("第一页")


@pytest.mark.asyncio
async def test_parse_pdf_disable_fails_when_figure_model_call_fails(monkeypatch: pytest.MonkeyPatch):
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
    with pytest.raises(DocumentParserException, match="模型解析失败"):
        await parser_unified.parse_pdf_disable("fake.pdf", params=params)


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
    # 临时附件无知识库图片归属，不能意外新增默认视觉模型调用。
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


@pytest.mark.asyncio
async def test_knowledge_params_resolve_default_vision_model(monkeypatch: pytest.MonkeyPatch):
    """知识库 API 调用也能解析已配置的默认视觉模型。"""
    import yuxi.services.ocr_service as ocr_service

    async def get_options(db=None):
        """提供已配置的系统默认模型。"""
        return {"default_image_analysis_model": "p:default-vision"}

    monkeypatch.setattr(ocr_service, "system_options", SimpleNamespace(get=get_options))
    resolved = await resolve_ocr_task_params(dict(_DISABLE_PARAMS))
    assert resolved["image_analysis_model"] == "p:default-vision"
    assert resolved["ocr_engine"] == "disable"


@pytest.mark.asyncio
async def test_tmp_attachment_remains_text_only_without_unknown_images(monkeypatch: pytest.MonkeyPatch):
    """临时附件无模型时继续返回文本且不创建图片副作用。"""
    monkeypatch.setattr(parser_unified, "extract_pdf_page_texts", lambda _: ["Attachment body"])

    def forbidden(*args, **kwargs):
        """临时附件不得提图或写入未知知识库归属。"""
        pytest.fail("不应提图或上传图片")

    monkeypatch.setattr(pdf_figures, "extract_pdf_figures", forbidden)
    monkeypatch.setattr(parser_unified, "_upload_image_to_minio", forbidden)
    assert await parser_unified.parse_pdf_disable("fake.pdf", {"ocr_engine": "disable"}) == "Attachment body"


@pytest.mark.asyncio
@pytest.mark.parametrize("description", ["", "C\ufffdN"])
async def test_empty_or_garbled_figure_analysis_is_not_silently_omitted(monkeypatch, description):
    """空或含乱码的图片解释不能静默省略。"""
    import yuxi.models.chat as chat_module

    monkeypatch.setattr(parser_unified, "extract_pdf_page_texts", lambda _: ["body"])
    monkeypatch.setattr(pdf_figures, "extract_pdf_figures", lambda _: [_figure(1, 1)])

    async def call(*args, **kwargs):
        """模拟不可用的图像解释结果。"""
        return SimpleNamespace(content=description)

    monkeypatch.setattr(chat_module, "select_model", lambda _: SimpleNamespace(call=call))
    with pytest.raises(DocumentParserException, match="为空或含乱码"):
        await parser_unified.parse_pdf_disable("fake.pdf", {**_DISABLE_PARAMS, "image_analysis_model": "p:vision"})


@pytest.mark.asyncio
async def test_upload_failure_prevents_incomplete_document(monkeypatch):
    """图片上传失败不会返回缺图的成功结果。"""
    import yuxi.models.chat as chat_module

    monkeypatch.setattr(parser_unified, "extract_pdf_page_texts", lambda _: ["body"])
    monkeypatch.setattr(pdf_figures, "extract_pdf_figures", lambda _: [_figure(1, 1)])

    async def call(*args, **kwargs):
        """返回可用的独立图像解释。"""
        return SimpleNamespace(content="图像解释")

    def fail_upload(*args, **kwargs):
        """模拟对象存储失败。"""
        raise RuntimeError("storage failed")

    monkeypatch.setattr(chat_module, "select_model", lambda _: SimpleNamespace(call=call))
    monkeypatch.setattr(parser_unified, "_upload_image_to_minio", fail_upload)
    with pytest.raises(DocumentParserException, match="上传失败"):
        await parser_unified.parse_pdf_disable("fake.pdf", {**_DISABLE_PARAMS, "image_analysis_model": "p:vision"})


@pytest.mark.asyncio
async def test_cross_page_continuation_is_joined_without_a_new_paragraph(monkeypatch):
    """跨页续句在视觉标记指示下合并。"""
    import yuxi.models.chat as chat_module

    monkeypatch.setattr(parser_unified, "extract_pdf_page_texts", lambda _: ["The adsorption", "capacity increased."])
    monkeypatch.setattr(pdf_figures, "extract_pdf_figures", lambda _: [])
    monkeypatch.setattr(chat_module, "select_model", lambda _: object())

    async def transcribe(model, file, number, text, previous, figures):
        """显式标注第二页续接上一页的句子。"""
        return PdfPageMarkdown(markdown=text) if number == 1 else PdfPageMarkdown("", text, "space")

    monkeypatch.setattr("yuxi.knowledge.parser.pdf_markdown.transcribe_pdf_page", transcribe)
    result = await parser_unified.parse_pdf_disable("fake.pdf", {**_DISABLE_PARAMS, "image_analysis_model": "p:vision"})
    assert result == "The adsorption capacity increased."


def test_corrupt_figure_fails_instead_of_disappearing(monkeypatch):
    """损坏图像不能从输出中静默消失。"""
    _patch_pdf_reader(monkeypatch, [[b"not an image"]])
    with pytest.raises(DocumentParserException, match="无法解码"):
        extract_pdf_figures("fake.pdf")


@pytest.mark.asyncio
async def test_excluded_publisher_cover_is_not_uploaded_or_analyzed(monkeypatch):
    """明确分类的期刊封面不作为科研插图解释或上传。"""
    import yuxi.models.chat as chat_module

    monkeypatch.setattr(parser_unified, "extract_pdf_page_texts", lambda _: ["body"])
    monkeypatch.setattr(pdf_figures, "extract_pdf_figures", lambda _: [_figure(1, 1)])
    monkeypatch.setattr(chat_module, "select_model", lambda _: object())

    async def transcribe(*args):
        """提供模型已明确分类的封面身份。"""
        return PdfPageMarkdown("# Body", excluded_figures=("p1_1",))

    def forbidden(*args):
        """装饰图片不产生上传副作用。"""
        pytest.fail("封面不应上传")

    monkeypatch.setattr("yuxi.knowledge.parser.pdf_markdown.transcribe_pdf_page", transcribe)
    monkeypatch.setattr(parser_unified, "_upload_image_to_minio", forbidden)
    result = await parser_unified.parse_pdf_disable("fake.pdf", {**_DISABLE_PARAMS, "image_analysis_model": "p:vision"})
    assert result == "# Body"
