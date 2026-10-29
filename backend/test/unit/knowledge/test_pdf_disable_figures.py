"""OCR 禁用模式：PDF 布局提取（插图定位、公式检测）与模型解析的单元测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import yuxi.knowledge.parser.pdf_figures as pdf_figures
import yuxi.knowledge.parser.unified as parser_unified
from yuxi.knowledge.parser.pdf_figures import LayoutBlock, keep_figure_by_bounds, looks_like_formula
from yuxi.services.ocr_service import resolve_ocr_task_params


# ---------------------------------------------------------------------------
# 公式行启发式
# ---------------------------------------------------------------------------


def test_formula_detector_accepts_garbled_math_text():
    # 数学字体缺 Unicode 映射的典型输出：私有区字符 + 零散算符
    garbled = "\ue000\uf02d\uf061\ue012\uf0aa\u222b\u2264\u2265\u2211\u220f = \ue001\ue002"
    assert looks_like_formula(garbled) is True


def test_formula_detector_rejects_plain_text():
    assert looks_like_formula("在 25 °C 下测量了溶液的浓度变化。") is False
    assert looks_like_formula("The conversion reached 95.2% at 350 C for 2 h.") is False
    assert looks_like_formula("X ≥ 5 且 Y < 10 时的产率约为 80%。") is False


# ---------------------------------------------------------------------------
# 图片显示矩形过滤
# ---------------------------------------------------------------------------


def test_keep_figure_filters_small_and_fullpage_bounds():
    page_size = (600.0, 800.0)
    assert keep_figure_by_bounds((100, 100, 170, 160), page_size) is False  # 图标（70x60pt）
    assert keep_figure_by_bounds((0, 0, 590, 790), page_size) is False  # 整页背景
    assert keep_figure_by_bounds((100, 100, 400, 400), page_size) is True


# ---------------------------------------------------------------------------
# 布局合并：图片插入正确位置
# ---------------------------------------------------------------------------


def test_formula_variable_fonts_excludes_ligature_font():
    ligature_boxes = [("LigFont", c, (1.0, 1.0, 2.0, 2.0)) for c in "flfiff"]
    variable_boxes = [("MathVar", c, (1.0, 1.0, 2.0, 2.0)) for c in "qKCn"]
    body_boxes = [("BodyFont", c, (1.0, 1.0, 2.0, 2.0)) for c in "regularbodytext" * 10]

    fonts = pdf_figures._formula_variable_fonts(ligature_boxes + variable_boxes + body_boxes)

    assert fonts == {"MathVar"}


def _char_item(font: str, char: str, x: float, y: float) -> tuple[str, str, tuple[float, float, float, float]]:
    return (font, char, (x, y, x + 4.0, y + 6.0))


def test_formula_row_region_requires_variable_density():
    row = [(10.0, 100.0, 14.0, 106.0), (20.0, 100.0, 24.0, 106.0)]  # 两个公式变量字符
    formula_fonts = {"MathVar"}
    # 公式行：变量占比 ≥ 0.25，多字体混排
    formula_items = (
        [_char_item("MathVar", "q", 10, 100), _char_item("MathVar", "K", 20, 100)]
        + [_char_item("Num", "1", 14, 100), _char_item("Op", "=", 17, 100), _char_item("Op", "+", 24, 100)]
    )
    region = pdf_figures._formula_row_region(row, formula_items, formula_fonts)
    assert region is not None and (region[2] - region[0]) > 10

    # 正文行：行内只有零星变量引用，密度不足，保留原文本
    text_items = (
        [_char_item("MathVar", "T", 10, 100), _char_item("MathVar", "P", 20, 100)]
        + [_char_item(f"Body{i}", c, 30 + i, 100) for i, c in enumerate("adsorbed at 298 K in the bed")]
    )
    assert pdf_figures._formula_row_region(row, text_items, formula_fonts) is None


def _text_block(page: int, text: str, left=50, right=550, top=700, bottom=680) -> LayoutBlock:
    return LayoutBlock(kind="text", page=page, left=left, bottom=bottom, right=right, top=top, text=text)


def _figure_block(page: int, seq: int, top: float, bottom: float, left=100, right=400) -> LayoutBlock:
    return LayoutBlock(
        kind="figure", page=page, left=left, bottom=bottom, right=right, top=top, image=b"img", seq=seq
    )


def test_merge_blocks_inserts_figure_between_texts_by_position():
    upper_text = _text_block(1, "图前文字", top=700, bottom=680)
    lower_text = _text_block(1, "图后文字", top=400, bottom=380)
    figure = _figure_block(1, 1, top=660, bottom=430)

    merged = pdf_figures._merge_blocks([upper_text, lower_text], [figure])

    assert [block.kind for block in merged] == ["text", "figure", "text"]
    assert merged[2].text == "图后文字"


def test_merge_blocks_keeps_text_stream_order_for_two_columns():
    # 双栏流序：左栏上下两块、右栏上下两块；图在右栏中部
    left_top = _text_block(1, "左栏上", left=50, right=290, top=700, bottom=660)
    left_bottom = _text_block(1, "左栏下", left=50, right=290, top=400, bottom=360)
    right_top = _text_block(1, "右栏上", left=310, right=550, top=700, bottom=660)
    right_bottom = _text_block(1, "右栏下", left=310, right=550, top=400, bottom=360)
    figure = _figure_block(1, 1, top=640, bottom=420, left=320, right=540)

    merged = pdf_figures._merge_blocks([left_top, left_bottom, right_top, right_bottom], [figure])

    assert [block.text or block.kind for block in merged] == ["左栏上", "左栏下", "右栏上", "figure", "右栏下"]


def test_assign_media_sequence_numbers_within_page():
    blocks = [
        _text_block(1, "文字"),
        _figure_block(1, 0, 600, 500),
        LayoutBlock(kind="formula", page=1, image=b"f", seq=0),
    ]
    pdf_figures._assign_media_sequence(blocks)

    assert [block.seq for block in blocks] == [0, 1, 2]
    assert blocks[2].name == "formula_p1_2"


# ---------------------------------------------------------------------------
# parse_pdf_disable 组装
# ---------------------------------------------------------------------------

_DISABLE_PARAMS = {"ocr_engine": "disable", "image_bucket": "kb-images", "image_prefix": "kb1/kb-images"}


def _layout_pages() -> list[list[LayoutBlock]]:
    return [
        [
            _text_block(1, "第一段正文", top=700, bottom=660),
            _figure_block(1, 1, top=650, bottom=430),
            _text_block(1, "第二段正文", top=420, bottom=380),
            LayoutBlock(kind="formula", page=1, top=300, bottom=260, text="μ=ρgh", image=b"formula", seq=2),
        ]
    ]


def _patch_layout(monkeypatch: pytest.MonkeyPatch, pages=None) -> None:
    monkeypatch.setattr(pdf_figures, "extract_pdf_layout", lambda file, max_figures=None: pages or _layout_pages())


def _patch_upload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        parser_unified,
        "_upload_image_to_minio",
        lambda data, filename, bucket, prefix: f"/proxy/{filename}",
    )


@pytest.mark.asyncio
async def test_parse_pdf_disable_without_model_embeds_media_in_place(monkeypatch: pytest.MonkeyPatch):
    _patch_layout(monkeypatch)
    _patch_upload(monkeypatch)

    result = await parser_unified.parse_pdf_disable("fake.pdf", params=dict(_DISABLE_PARAMS))

    assert "第一段正文" in result and "第二段正文" in result
    assert "![figure_p1_1](/proxy/figure_p1_1.png)" in result
    assert "![formula_p1_2](/proxy/formula_p1_2.png)" in result
    assert "插图解析" not in result
    # 顺序：正文 → 插图 → 正文 → 公式
    assert (
        result.index("第一段正文")
        < result.index("figure_p1_1")
        < result.index("第二段正文")
        < result.index("formula_p1_2")
    )


@pytest.mark.asyncio
async def test_parse_pdf_disable_with_model_appends_analysis(monkeypatch: pytest.MonkeyPatch):
    import yuxi.models.chat as chat_module

    _patch_layout(monkeypatch)
    _patch_upload(monkeypatch)

    async def fake_call(message, stream=False):
        prompt = message[0].content[0]["text"]
        if "公式" in prompt:
            return SimpleNamespace(content="$$\\mu = \\rho g h$$")
        return SimpleNamespace(content="这是一张数据图表")

    monkeypatch.setattr(chat_module, "select_model", lambda spec: SimpleNamespace(call=fake_call))

    params = dict(_DISABLE_PARAMS)
    params["image_analysis_model"] = "p:vision"
    result = await parser_unified.parse_pdf_disable("fake.pdf", params=params)

    assert "**插图解析**：这是一张数据图表" in result
    assert "$$\\mu = \\rho g h$$" in result
    # LaTeX 转写在公式图片之前
    assert result.index("$$\\mu") < result.index("![formula_p1_2]")


@pytest.mark.asyncio
async def test_parse_pdf_disable_degrades_when_model_call_fails(monkeypatch: pytest.MonkeyPatch):
    import yuxi.models.chat as chat_module

    _patch_layout(monkeypatch)
    _patch_upload(monkeypatch)

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

    _patch_layout(monkeypatch)

    def invalid_spec(spec):
        raise ValueError("未找到模型")

    monkeypatch.setattr(chat_module, "select_model", invalid_spec)

    params = dict(_DISABLE_PARAMS)
    params["image_analysis_model"] = "p:missing"
    with pytest.raises(ValueError):
        await parser_unified.parse_pdf_disable("fake.pdf", params=params)


# ---------------------------------------------------------------------------
# ocr_service 参数解析
# ---------------------------------------------------------------------------


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
