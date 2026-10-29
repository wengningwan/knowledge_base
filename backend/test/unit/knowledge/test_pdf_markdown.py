"""页面视觉转录的结构校验与图片身份回归用例。"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from yuxi.knowledge.parser.base import DocumentParserException
from yuxi.knowledge.parser.pdf_figures import PdfFigure
from yuxi.knowledge.parser.pdf_markdown import (
    PdfPageMarkdown,
    assemble_pdf_pages,
    response_text,
    transcribe_pdf_page,
    validate_page_transcription,
)


def _figure(index=1):
    """提供同页不同身份的图片资料。"""
    return PdfFigure(page=1, index=index, name=f"p1_{index}", data=b"picture", mime="image/png")


def _payload(markdown, **overrides):
    """构造协议样本，期望内容由测试显式指定。"""
    return json.dumps(
        {
            "markdown": markdown,
            "continuation": "",
            "join_mode": "none",
            "unreadable": [],
            "unplaced_figures": [],
            "excluded_figures": [],
            **overrides,
        }
    )


def test_preserves_formula_bonds_and_caption_and_accepts_visual_order():
    """保留独立 oracle 的公式和图注，资源枚举顺序不覆盖视觉顺序。"""
    text = (
        "# Results\n\n$$W=W_0\\exp[-(A/(\\beta E_0))^n]$$\n\n"
        "$$A=RT\\ln(P_0/P)$$\n\n$$W_0=V_0\\rho_M$$\n\n"
        "$$\\Delta V=V_{0,A}-V_{0,B}$$\n\n$C\\equiv N$, $V_{CO_2}$\n\n"
        "<!-- figure:p1_2 -->\n\nFigure 1. Second resource appears first.\n\n<!-- figure:p1_1 -->"
    )
    result = validate_page_transcription(_payload(text), 1, "broken native text", [_figure(), _figure(2)])
    assert result.markdown.index("p1_2") < result.markdown.index("p1_1")
    assert "\\beta E_0" in result.markdown
    assert "\\rho_M" in result.markdown
    assert "\\Delta V" in result.markdown
    assert "C\\equiv N" in result.markdown
    assert "V_{CO_2}" in result.markdown
    assert "Figure 1. Second resource appears first." in result.markdown


@pytest.mark.parametrize(
    "markdown",
    ["text without figure", "<!-- figure:p1_1 --><!-- figure:p1_1 -->", "<!-- figure:p2_1 -->"],
)
def test_missing_duplicate_or_wrong_page_figure_fails(markdown):
    """缺图、重复图和串页不能发布成成功文档。"""
    with pytest.raises(DocumentParserException, match="占位符"):
        validate_page_transcription(_payload(markdown), 1, "body", [_figure()])


@pytest.mark.parametrize("markdown", ["C\ufffdN", "(cid:98)", "≈\n≈\n≈\n≈\n≈\n≈", "i\nk\nj\nj\nj\ny\nz", ""])
def test_garbled_or_empty_text_fails(markdown):
    """拒绝替换字符、字体碎片及缺失正文。"""
    with pytest.raises(DocumentParserException):
        validate_page_transcription(_payload(markdown), 1, "body", [])


@pytest.mark.parametrize("field", ["unreadable", "unplaced_figures"])
def test_unreadable_formulas_or_unmapped_vector_figures_fail(field):
    """模型报告未能完成的区域会阻止保存。"""
    with pytest.raises(DocumentParserException, match="不可读内容或无法定位"):
        validate_page_transcription(_payload("body", **{field: ["region"]}), 1, "body", [])


@pytest.mark.parametrize("text", ['{"markdown":', '{"markdown":"partial"}', '[]'])
def test_malformed_or_incomplete_model_payload_fails(text):
    """截断 JSON 和缺失字段不能冒充完整页面。"""
    with pytest.raises(DocumentParserException):
        validate_page_transcription(text, 1, "body", [])


def test_blank_page_and_continuation_are_explicit():
    """允许空白页并严格区分首页与后续页的续段标记。"""
    assert validate_page_transcription(_payload(""), 2, "", []).markdown == ""
    result = validate_page_transcription(_payload("", continuation="continued", join_mode="space"), 2, "continued", [])
    assert result.continuation == "continued"
    with pytest.raises(DocumentParserException, match="首页"):
        validate_page_transcription(_payload("", continuation="continued", join_mode="space"), 1, "continued", [])


def test_content_blocks_are_not_dumped_as_python_repr():
    """只提取文本块，过滤推理内容及缺失内容。"""
    response = SimpleNamespace(content=[{"type": "reasoning", "text": "private"}, {"type": "text", "text": "body"}])
    assert response_text(response) == "body"
    assert response_text(SimpleNamespace(content=None)) == ""


@pytest.mark.asyncio
async def test_page_request_contains_original_image_ids_and_returns_markdown(monkeypatch):
    """页面请求显式携带原图与 ID，避免模型猜测图片身份。"""
    monkeypatch.setattr("yuxi.knowledge.parser.pdf_markdown.render_pdf_page", lambda *_: b"page")

    async def call(messages, stream=False):
        """检查给模型的独立原图与身份，返回显式期待的视觉位置。"""
        content = messages[1].content
        assert "p1_1" in content[2]["text"]
        assert "p1_2" in content[4]["text"]
        assert sum(block["type"] == "image_url" for block in content) == 3
        return SimpleNamespace(content=_payload("<!-- figure:p1_2 -->\n\n<!-- figure:p1_1 -->"))

    result = await transcribe_pdf_page(SimpleNamespace(call=call), "fake.pdf", 1, "native", "", [_figure(), _figure(2)])
    assert result.markdown.startswith("<!-- figure:p1_2 -->")


@pytest.mark.asyncio
async def test_page_model_failure_is_visible(monkeypatch):
    """供应商异常作为解析错误传播。"""
    monkeypatch.setattr("yuxi.knowledge.parser.pdf_markdown.render_pdf_page", lambda *_: b"page")

    async def call(*args, **kwargs):
        """模拟供应商失败。"""
        raise RuntimeError("provider failed")

    with pytest.raises(DocumentParserException, match="视觉转录失败"):
        await transcribe_pdf_page(SimpleNamespace(call=call), "fake.pdf", 1, "native", "", [])


@pytest.mark.parametrize(
    ("tail", "continuation", "mode", "expected"),
    [
        ("The adsorption", "capacity increased.", "space", "The adsorption capacity increased."),
        ("The adsorp-", "tion increased.", "dehyphenate", "The adsorption increased."),
        ("CPN-", "1 retained ethylene.", "none", "CPN-1 retained ethylene."),
        ("吸附", "容量增加。", "none", "吸附容量增加。"),
    ],
)
def test_continuation_precedes_floating_figure_and_preserves_word_boundaries(tail, continuation, mode, expected):
    """浮动图片不打断续文，排版断词和化学连字符分别处理。"""
    pages = [
        PdfPageMarkdown(tail),
        PdfPageMarkdown("<!-- figure:p2_1 -->\n\nFigure 2. Caption", continuation, mode),
    ]
    result = assemble_pdf_pages(pages, {"p2_1": "![figure_p2_1](/proxy/p2_1.png)"})
    assert result == f"{expected}\n\n![figure_p2_1](/proxy/p2_1.png)\n\nFigure 2. Caption"


def test_invalid_dehyphenation_or_continuation_without_previous_text_fails():
    """协议不能删除没有明确存在的末尾断词符。"""
    with pytest.raises(DocumentParserException, match="断词标记"):
        assemble_pdf_pages([PdfPageMarkdown("whole word"), PdfPageMarkdown("", "next", "dehyphenate")], {})
    with pytest.raises(DocumentParserException, match="缺少上一页"):
        assemble_pdf_pages([PdfPageMarkdown("", "next", "space")], {})


def test_cover_exclusion_has_explicit_unique_ownership():
    """封面可明确排除，正文图必须保留且不能同时被排除。"""
    payload = _payload(
        "<!-- figure:p1_1 -->",
        excluded_figures=[{"id": "p1_2", "reason": "publisher_cover"}],
    )
    result = validate_page_transcription(payload, 1, "body", [_figure(), _figure(2)])
    assert result.excluded_figures == ("p1_2",)
    with pytest.raises(DocumentParserException, match="占位符"):
        validate_page_transcription(payload, 1, "body", [_figure()])
    with pytest.raises(DocumentParserException, match="占位符"):
        validate_page_transcription(
            _payload("<!-- figure:p1_1 -->", excluded_figures=[{"id": "p1_1", "reason": "repeated_logo"}]),
            1, "body", [_figure()],
        )
    cover_only = validate_page_transcription(
        _payload("", excluded_figures=[{"id": "p1_1", "reason": "publisher_cover"}]),
        1, "Publisher navigation", [_figure()],
    )
    assert cover_only.markdown == ""
    assert cover_only.excluded_figures == ("p1_1",)


def test_small_scientific_figure_cannot_be_excluded_by_size():
    """不允许重新引入按大小过滤真实科研图的隐式规则。"""
    with pytest.raises(DocumentParserException, match="占位符"):
        validate_page_transcription(
            _payload("body", excluded_figures=[{"id": "p1_1", "reason": "small"}]), 1, "body", [_figure()]
        )


@pytest.mark.parametrize("operation", ["preflight", "render"])
def test_preflight_and_render_share_the_native_pdfium_lock(monkeypatch, operation):
    """持有共同锁时，两条原生入口都必须等待，移除任一锁会让负控失败。"""
    import yuxi.knowledge.parser.pdf_markdown as page_module
    import yuxi.knowledge.utils.pdf_utils as pdf_utils

    native_entered = threading.Event()
    requested = threading.Event()

    class FakeImage:
        """模拟已渲染图像，不启动真正的 PDFium。"""
        def save(self, output, format):
            """写出固定的像素载荷。"""
            output.write(b"png")

        def close(self):
            """模拟释放图像。"""

    class FakePage:
        """提供预检和渲染需要的页面接口。"""
        def get_size(self):
            """返回固定页面尺寸。"""
            return 600, 800

        def render(self, scale):
            """复用对象提供位图接口。"""
            return self

        def to_pil(self):
            """返回独立图像接口。"""
            return FakeImage()

        def close(self):
            """模拟释放原生页面或位图。"""

    class FakeDocument:
        """记录进入原生库的时点。"""
        def __init__(self, path):
            """标记原生调用已开始。"""
            native_entered.set()

        def __enter__(self):
            """返回上下文中的文档。"""
            return self

        def __exit__(self, *args):
            """模拟关闭上下文。"""

        def __len__(self):
            """返回一页。"""
            return 1

        def __getitem__(self, index):
            """返回待检页面。"""
            return FakePage()

        def close(self):
            """模拟释放文档。"""

    assert page_module.PDFIUM_LOCK is pdf_utils.PDFIUM_LOCK
    monkeypatch.setattr(pdf_utils.pdfium, "PdfDocument", FakeDocument)

    def invoke():
        """在另一个线程申请真实的公共入口。"""
        requested.set()
        if operation == "preflight":
            return pdf_utils.validate_pdf_page_tree_loadable("fake.pdf")
        return page_module.render_pdf_page("fake.pdf", 1)

    with ThreadPoolExecutor(max_workers=1) as executor:
        with pdf_utils.PDFIUM_LOCK:
            future = executor.submit(invoke)
            assert requested.wait(timeout=2)
            assert not native_entered.wait(timeout=0.1)
        future.result(timeout=2)
    assert native_entered.is_set()
