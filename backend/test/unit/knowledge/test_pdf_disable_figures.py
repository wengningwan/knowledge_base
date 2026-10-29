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
    """提供唯一原图载荷，便于确认模型只接收原图。"""
    return PdfFigure(
        page=page, index=index, name=f"p{page}_{index}", data=f"image-{page}-{index}".encode(), mime="image/png"
    )


_DISABLE_PARAMS = {"ocr_engine": "disable", "image_bucket": "kb-images", "image_prefix": "kb1/kb-images"}


def _patch_parse_inputs(monkeypatch, texts, figures):
    """替代输入和图片存储，不替代正文装配或模型请求。"""
    monkeypatch.setattr(parser_unified, "extract_pdf_page_texts", lambda _: texts)
    monkeypatch.setattr(pdf_figures, "extract_pdf_figures", lambda _, **kwargs: figures)
    monkeypatch.setattr(
        parser_unified, "_upload_image_to_minio", lambda data, filename, bucket, prefix: f"/proxy/{filename}"
    )


@pytest.mark.asyncio
async def test_no_model_preserves_native_text_and_original_images(monkeypatch):
    """没有模型也可提取正文和原图，说明缺失明确可见。"""
    import yuxi.models.chat as chat_module

    texts = ["First page C2H4", "Second page has its own text"]
    _patch_parse_inputs(monkeypatch, texts, [_figure(1, 1), _figure(2, 1)])

    def forbidden(*args):
        """未选择模型时不得创建模型请求。"""
        pytest.fail("不应创建模型")

    monkeypatch.setattr(chat_module, "select_model", forbidden)
    result = await parser_unified.parse_pdf_disable("fake.pdf", dict(_DISABLE_PARAMS))
    assert "\n\n".join(texts) in result
    assert "![figure_p1_1](/proxy/p1_1.png)" in result
    assert "![figure_p2_1](/proxy/p2_1.png)" in result
    assert result.index("Second page") < result.index("## 插图与模型说明")
    assert "图片说明未生成" in result
    assert "未选择插图解析模型" in result


@pytest.mark.asyncio
async def test_model_only_receives_extracted_figure_not_page_or_native_text(monkeypatch):
    """原文来自文字提取，只有原图进入模型，不要求返回 JSON。"""
    import base64

    import yuxi.models.chat as chat_module

    texts = ["Native text stays exactly as extracted.\nOriginal Figure 1. caption"]
    _patch_parse_inputs(monkeypatch, texts, [_figure(1, 1)])

    async def call(messages, stream=False):
        """检查真正的多模态请求，而不是模拟正文转录。"""
        assert len(messages) == 1
        content = messages[0].content
        assert "Native text" not in content[0]["text"]
        images = [block for block in content if block["type"] == "image_url"]
        assert len(images) == 1
        encoded = images[0]["image_url"]["url"].split(",", 1)[1]
        assert base64.b64decode(encoded) == b"image-1-1"
        return SimpleNamespace(content="This is a plot.\nThe measured value is 1.2.")

    monkeypatch.setattr(chat_module, "select_model", lambda _: SimpleNamespace(call=call))
    result = await parser_unified.parse_pdf_disable(
        "fake.pdf", {**_DISABLE_PARAMS, "image_analysis_model": "p:vision"}
    )
    assert texts[0] in result
    assert "> **插图解析（模型生成）**" in result
    assert "> This is a plot.\n> The measured value is 1.2." in result


@pytest.mark.asyncio
async def test_native_font_errors_are_flagged_without_model_rewriting_or_aborting(monkeypatch):
    """文字层缺陷需提示复核，不能让图片模型猜测修复正文。"""
    texts = ["Original C\ufffdN and equation\n≈\n≈\n≈\n≈\n≈"]
    _patch_parse_inputs(monkeypatch, texts, [])
    result = await parser_unified.parse_pdf_disable("fake.pdf", dict(_DISABLE_PARAMS))
    assert texts[0] in result
    assert "文字层存在异常符号" in result


@pytest.mark.asyncio
async def test_no_figures_does_not_require_or_invoke_a_model(monkeypatch):
    """无插图的文本 PDF 直接使用文字层，即使模型配置当前不可用。"""
    import yuxi.models.chat as chat_module

    _patch_parse_inputs(monkeypatch, ["Native paragraph"], [])

    def forbidden(*args):
        """没有图片时不使用模型。"""
        pytest.fail("不应选取模型")

    monkeypatch.setattr(chat_module, "select_model", forbidden)
    result = await parser_unified.parse_pdf_disable(
        "fake.pdf", {**_DISABLE_PARAMS, "image_analysis_model": "p:unavailable"}
    )
    assert result == "Native paragraph"


@pytest.mark.asyncio
async def test_figure_failure_is_retried_and_does_not_discard_text_or_other_figures(monkeypatch):
    """单图重试耗尽后保留正文、失败图原图和其他成功说明。"""
    import base64

    import yuxi.models.chat as chat_module

    _patch_parse_inputs(monkeypatch, ["Native text"], [_figure(1, 1), _figure(1, 2)])
    attempts = []

    async def call(messages, stream=False):
        """只使第一张图片调用失败。"""
        encoded = messages[0].content[1]["image_url"]["url"].split(",", 1)[1]
        image = base64.b64decode(encoded)
        attempts.append(image)
        if image == b"image-1-1":
            raise RuntimeError("provider failure")
        return SimpleNamespace(content="Second figure explanation")

    monkeypatch.setattr(chat_module, "select_model", lambda _: SimpleNamespace(call=call))
    result = await parser_unified.parse_pdf_disable(
        "fake.pdf", {**_DISABLE_PARAMS, "image_analysis_model": "p:vision"}
    )
    assert attempts.count(b"image-1-1") == 2
    assert attempts.count(b"image-1-2") == 1
    assert "Native text" in result
    assert "![figure_p1_1]" in result and "![figure_p1_2]" in result
    assert "插图解析失败" in result
    assert "Second figure explanation" in result


@pytest.mark.asyncio
async def test_transient_figure_failure_can_succeed_on_the_second_attempt(monkeypatch):
    """有限重试只针对图像说明，不重做正文。"""
    import yuxi.models.chat as chat_module

    _patch_parse_inputs(monkeypatch, ["Native text"], [_figure(1, 1)])
    attempts = []

    async def call(*args, **kwargs):
        """第一次失败后返回正常说明。"""
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("temporary failure")
        return SimpleNamespace(content="Recovered explanation")

    monkeypatch.setattr(chat_module, "select_model", lambda _: SimpleNamespace(call=call))
    result = await parser_unified.parse_pdf_disable(
        "fake.pdf", {**_DISABLE_PARAMS, "image_analysis_model": "p:vision"}
    )
    assert len(attempts) == 2
    assert "Recovered explanation" in result
    assert "插图解析失败" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["", "C\ufffdN", None])
async def test_unusable_figure_description_has_an_explicit_status(monkeypatch, content):
    """空、缺失或含乱码的模型回复不会无声消失。"""
    import yuxi.models.chat as chat_module

    _patch_parse_inputs(monkeypatch, ["Native text"], [_figure(1, 1)])

    async def call(*args, **kwargs):
        """模拟不可用的说明。"""
        return SimpleNamespace(content=content)

    monkeypatch.setattr(chat_module, "select_model", lambda _: SimpleNamespace(call=call))
    result = await parser_unified.parse_pdf_disable(
        "fake.pdf", {**_DISABLE_PARAMS, "image_analysis_model": "p:vision"}
    )
    assert "Native text" in result and "![figure_p1_1]" in result
    assert "插图解析失败" in result


@pytest.mark.asyncio
async def test_invalid_image_model_is_visible_without_discarding_the_document(monkeypatch):
    """模型配置错误保留原图并显示未完成状态。"""
    import yuxi.models.chat as chat_module

    _patch_parse_inputs(monkeypatch, ["Native text"], [_figure(1, 1)])

    def unavailable(*args):
        """模拟已选模型不存在。"""
        raise ValueError("model unavailable")

    monkeypatch.setattr(chat_module, "select_model", unavailable)
    result = await parser_unified.parse_pdf_disable(
        "fake.pdf", {**_DISABLE_PARAMS, "image_analysis_model": "p:missing"}
    )
    assert "Native text" in result and "![figure_p1_1]" in result
    assert "所选插图解析模型不可用" in result


@pytest.mark.asyncio
async def test_image_upload_failure_retains_native_text(monkeypatch):
    """存储故障在逐图状态中展示，不伪造图片或删除正文。"""
    _patch_parse_inputs(monkeypatch, ["Native text"], [_figure(1, 1)])

    def fail_upload(*args):
        """模拟图片对象存储失败。"""
        raise RuntimeError("storage failure")

    monkeypatch.setattr(parser_unified, "_upload_image_to_minio", fail_upload)
    result = await parser_unified.parse_pdf_disable("fake.pdf", dict(_DISABLE_PARAMS))
    assert "Native text" in result and "原图上传失败" in result
    assert "![figure_p1_1]" not in result


@pytest.mark.asyncio
async def test_image_only_pdf_cannot_publish_when_every_upload_fails(monkeypatch):
    """扫描件没有文字且原图均无法保留时，提示不能冒充可用产物。"""
    _patch_parse_inputs(monkeypatch, [""], [_figure(1, 1)])

    def fail_upload(*args):
        """模拟所有原图上传失败。"""
        raise RuntimeError("storage failure")

    monkeypatch.setattr(parser_unified, "_upload_image_to_minio", fail_upload)
    with pytest.raises(DocumentParserException, match="所有原图均上传失败"):
        await parser_unified.parse_pdf_disable("fake.pdf", dict(_DISABLE_PARAMS))


@pytest.mark.asyncio
async def test_image_only_pdf_retains_usable_originals_when_some_uploads_fail(monkeypatch):
    """至少保留一张原图时可输出图片产物，并明确标记其他失败。"""
    _patch_parse_inputs(monkeypatch, [""], [_figure(1, 1), _figure(1, 2)])

    def upload(data, filename, bucket, prefix):
        """只允许第二张原图上传。"""
        if filename == "p1_1.png":
            raise RuntimeError("storage failure")
        return f"/proxy/{filename}"

    monkeypatch.setattr(parser_unified, "_upload_image_to_minio", upload)
    result = await parser_unified.parse_pdf_disable("fake.pdf", dict(_DISABLE_PARAMS))
    assert "未提取到可用文字层" in result
    assert "原图上传失败" in result
    assert "![figure_p1_2](/proxy/p1_2.png)" in result


@pytest.mark.asyncio
async def test_model_response_blocks_only_publish_text(monkeypatch):
    """不把推理块和 Python 列表表示写入图片说明。"""
    import yuxi.models.chat as chat_module

    _patch_parse_inputs(monkeypatch, ["Native text"], [_figure(1, 1)])

    async def call(*args, **kwargs):
        """返回混合模型内容块。"""
        return SimpleNamespace(
            content=[{"type": "reasoning", "text": "private reasoning"}, {"type": "text", "text": "Plot explanation"}]
        )

    monkeypatch.setattr(chat_module, "select_model", lambda _: SimpleNamespace(call=call))
    result = await parser_unified.parse_pdf_disable(
        "fake.pdf", {**_DISABLE_PARAMS, "image_analysis_model": "p:vision"}
    )
    assert "Plot explanation" in result and "private reasoning" not in result


@pytest.mark.asyncio
async def test_empty_core_extraction_fails_with_a_relevant_error(monkeypatch):
    """真正没有可用原文或图片时提示选择 OCR，而非伪造成功文档。"""
    _patch_parse_inputs(monkeypatch, [""], [])
    with pytest.raises(DocumentParserException, match="没有可提取的文字或图片"):
        await parser_unified.parse_pdf_disable("fake.pdf", dict(_DISABLE_PARAMS))


def test_partial_figure_extraction_preserves_other_images_and_reports_problem(monkeypatch):
    """损坏图片不阻断可用图像，并提供明确诊断。"""
    _patch_pdf_reader(monkeypatch, [[_png_bytes(200, 200), b"broken", _png_bytes(220, 220)]])
    warnings = []
    figures = extract_pdf_figures("fake.pdf", warnings=warnings)
    assert len(figures) == 2
    assert len(warnings) == 1 and "无法解码" in warnings[0]


def test_lazy_image_lookup_failure_does_not_drop_the_rest_of_the_page(monkeypatch):
    """模拟 pypdf 索引时解码失败，同页前后好图均应保留。"""
    first = SimpleNamespace(data=_png_bytes(200, 200))
    last = SimpleNamespace(data=_png_bytes(220, 220))

    class LazyImages:
        """索引图片时才读取图像资源。"""

        def __len__(self):
            """返回资源数量而不解码。"""
            return 3

        def __getitem__(self, index):
            """第二张资源读取失败。"""
            if index == 1:
                raise OSError("cannot decode image object")
            return first if index == 0 else last

    monkeypatch.setattr(
        pdf_figures, "PdfReader", lambda _: SimpleNamespace(pages=[SimpleNamespace(images=LazyImages())])
    )
    warnings = []
    figures = extract_pdf_figures("fake.pdf", warnings=warnings)
    assert [figure.data for figure in figures] == [first.data, last.data]
    assert len(warnings) == 1 and "第 2 个图片资源读取失败" in warnings[0]


def test_figure_limit_can_return_available_images_with_a_visible_warning(monkeypatch):
    """到达上限时保留已提图像并说明尚有未处理内容。"""
    _patch_pdf_reader(monkeypatch, [[_png_bytes(200 + i, 200 + i) for i in range(3)]])
    warnings = []
    figures = extract_pdf_figures("fake.pdf", max_figures=2, warnings=warnings)
    assert len(figures) == 2
    assert warnings and "上限" in warnings[0]


@pytest.mark.asyncio
async def test_partial_extraction_warning_is_included_in_the_document(monkeypatch):
    """提图诊断进入最终产物，而非只记录在日志里。"""
    _patch_parse_inputs(monkeypatch, ["Native text"], [])

    def extract(file, *, warnings):
        """模拟页面图片资源损坏。"""
        warnings.append("Page image resource could not be decoded")
        return []

    monkeypatch.setattr(pdf_figures, "extract_pdf_figures", extract)
    result = await parser_unified.parse_pdf_disable("fake.pdf", dict(_DISABLE_PARAMS))
    assert "Native text" in result and "Page image resource could not be decoded" in result


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
    assert "image_analysis_model" not in result


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
