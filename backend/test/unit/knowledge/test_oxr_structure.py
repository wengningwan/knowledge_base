"""结构化正文、图片与最终资产链接的纯函数契约。"""

from __future__ import annotations

import base64
import json

from yuxi.knowledge.parser.pdf_structure import finalize_document, normalize_oxr_result, render_document, restore_reading_flow
from yuxi.knowledge.parser.result import ParsedDocument


PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7WQAAAAASUVORK5CYII="


def _payload(blocks, markdown="unused duplicate upstream markdown"):
    """构造与正式 data.result.outputs.json 一致的结果包装。"""
    return {"code": 0, "data": {"status": "SUCCESS", "result": {"outputs": {"markdown": markdown, "json": {"pages": [{"page_num": 0, "page_size": {"w": 200, "h": 400}, "blocks": blocks}]}}}}}


def test_caption_and_four_numbered_chemical_formulas_survive_once():
    """公式本体、式号和化学语法不得被图注或服务原 Markdown 覆盖。"""
    formulas = [rf"\ce{{A_{index} + B -> C}}\tag{{{index}}}" for index in range(1, 5)]
    source = [{"idx": 0, "type": "Title", "bbox": [20, 10, 180, 30], "content": "1.2 Reaction methods"}, {"idx": 2, "type": "Picture", "bbox": [20, 40, 180, 90], "content": f"![](data:image/png;base64,{PNG})"}, {"idx": 4, "type": "Caption", "bbox": [20, 90, 180, 110], "content": "Fig. 1. Retained original caption"}]
    source.extend({"idx": 6 + i, "type": "Formula", "bbox": [20, 120 + 40 * i, 180, 150 + 40 * i], "format": "latex", "content": formula} for i, formula in enumerate(formulas))
    result = normalize_oxr_result(_payload(source))

    assert result.markdown.startswith("## 1.2 Reaction methods")
    assert result.markdown.count("Fig. 1. Retained original caption") == 1
    assert all(result.markdown.count(formula) == 1 for formula in formulas)
    assert result.figures[0]["caption"] == "Fig. 1. Retained original caption"
    assert result.blocks[1]["bbox"] == [0.1, 0.1, 0.9, 0.225]
    assert result.assets[result.blocks[1]["asset_id"]]["data"] == base64.b64decode(PNG)


def test_html_table_image_is_extracted_without_losing_table_structure():
    """HTML 表格中的图片入资产，数值和标签原样保留。"""
    html = f'<table><tr><td><img alt="structure" src="data:image/png;base64,{PNG}"></td><td>0.010 mg</td></tr></table>'
    result = normalize_oxr_result(_payload([{"idx": 8, "type": "Table", "bbox": [0, 0, 200, 100], "format": "html", "content": html}]))

    assert '<table><tr><td><img alt="structure" src="asset://p1_b8_img1">' in result.markdown
    assert "<td>0.010 mg</td></tr></table>" in result.markdown
    assert result.assets["p1_b8_img1"]["role"] == "table"
    assert "base64" not in json.dumps(result.sidecar())


def test_types_idx_holes_and_supplied_reading_order_are_preserved():
    """兼容两种类型大小写，idx 空洞与原阅读顺序均不能被排序消除。"""
    source = [
        {"idx": 9, "type": "Text", "bbox": [0, 20, 200, 40], "content": "first in reading order"},
        {"idx": 2, "type": "chemical_structure", "bbox": [0, 50, 200, 80], "content": "SMILES: C[C@H](O)Cl"},
        {"idx": 18, "type": "ChemicalEquation", "bbox": [0, 90, 200, 110], "format": "latex", "content": r"\ce{H2 + O2 -> H2O}"},
        {"idx": 21, "type": "PageHeader", "content": "hidden running header"},
        {"idx": 30, "type": "future_kind", "content": "future block text retained"},
        {"idx": 35, "type": "Footnote", "content": "footnote retained"},
    ]
    result = normalize_oxr_result(_payload(source))
    assert [block["idx"] for block in result.blocks] == [9, 2, 18, 21, 30, 35]
    assert [block["order"] for block in result.blocks] == list(range(6))
    assert result.blocks[1]["type"] == "chemical_structure"
    assert result.blocks[2]["type"] == "chemical_equation"
    assert "hidden running header" not in result.markdown
    assert "SMILES: C[C@H](O)Cl" in result.markdown
    assert "future block text retained" in result.markdown
    assert "footnote retained" in result.markdown
    assert any(item["kind"] == "unknown_type" for item in result.diagnostics)


def test_external_image_urls_are_recorded_without_fetching():
    """外部来源仅作为结构证据保留，不发生网络获取。"""
    result = normalize_oxr_result(_payload([{"idx": 1, "type": "picture", "bbox": [0, 0, 10, 10], "content": "", "img_url": "https://untrusted.test/image.png"}]))
    assert result.assets == {}
    assert result.blocks[0]["source_urls"] == ["https://untrusted.test/image.png"]
    assert "https://untrusted.test/image.png" in result.markdown


def test_finalize_replaces_all_evidence_links_and_preserves_legacy_markdown():
    """最终上传链接应同时进入正文、图片和复核记录。"""
    result = ParsedDocument(markdown="legacy body ![](asset://a)", figures=[{"image_url": "asset://a"}], reviews=[{"source_image": "asset://a", "nested": {"url": "asset://a"}}], diagnostics=[{"evidence": ["asset://a"]}], assets={"a": {"data": b"binary", "mime": "image/png"}})
    finalize_document(result, {"a": "https://objects.test/a.png"})
    assert result.markdown == "legacy body ![](https://objects.test/a.png)"
    assert result.figures[0]["image_url"] == "https://objects.test/a.png"
    assert result.reviews[0]["source_image"] == "https://objects.test/a.png"
    assert result.reviews[0]["nested"]["url"] == "https://objects.test/a.png"
    assert result.diagnostics[0]["evidence"] == ["https://objects.test/a.png"]
    assert result.assets["a"]["data"] == b"binary"


def test_asset_ids_do_not_replace_prefixes_and_factories_are_independent():
    """资产前缀相似时逐个替换，默认容器不能在文档间共享。"""
    first, second = ParsedDocument(), ParsedDocument()
    first.blocks.append({"id": "x"})
    assert second.blocks == []
    legacy = ParsedDocument(markdown="asset://a asset://a2")
    finalize_document(legacy, {"a": "https://objects.test/one"})
    assert legacy.markdown == "https://objects.test/one asset://a2"


def test_sidecar_is_json_safe_and_filters_nested_credentials():
    """结构文件不能包含 assets 二进制或嵌套凭据。"""
    result = ParsedDocument(assets={"a": {"data": b"not-json"}}, metadata={"page_count": 1, "api_key": "never-store", "nested": [[{"authorization": "also-never", "safe": 2}]]})
    encoded = json.dumps(result.sidecar(), allow_nan=False)
    assert "never" not in encoded
    assert "assets" not in result.sidecar()
    assert result.sidecar()["metadata"]["nested"] == [[{"safe": 2}]]


def test_cross_page_hyphenated_chemical_text_is_not_joined_or_dehyphenated():
    """跨页规则不能吞掉真实化学连字符或凭相邻页猜合段。"""
    blocks = [{"id": "a", "type": "text", "page": 1, "bbox": [0, 0.9, 1, 1], "content": "N-"}, {"id": "b", "type": "text", "page": 2, "bbox": [0, 0, 1, 0.1], "content": "methyl compound", "continues_previous": True}]
    assert render_document(blocks) == "N-\n\nmethyl compound"
    blocks[0]["content"] = "continued sentence"
    assert render_document(blocks) == "continued sentence methyl compound"



def test_floating_image_caption_follow_restored_cross_page_sentence():
    """正文续句跨过浮图后仍为一段，图片图注在同一阅读位置只出现一次。"""
    blocks = [
        {"id": "a", "type": "text", "page": 1, "bbox": [0.1, .85, .45, .95], "content": "near absence of"},
        {"id": "img", "type": "picture", "page": 2, "bbox": [.1, .02, .45, .10], "content": "![](asset://a)"},
        {"id": "cap", "type": "caption", "page": 2, "bbox": [.1, .11, .45, .14], "content": "Figure 1. Original caption"},
        {"id": "b", "type": "text", "page": 2, "bbox": [.1, .15, .45, .30], "content": "nonselective adsorption."},
    ]
    diagnostics = []
    md = render_document(restore_reading_flow(blocks, diagnostics))
    assert md.startswith("near absence of nonselective adsorption.\n\n![](asset://a)")
    assert md.count("Figure 1. Original caption") == 1
    assert diagnostics[0]["kind"] == "floating_figure_after_continuation"


def test_missing_geometry_does_not_join_unrelated_lowercase_paragraphs():
    blocks = [{"id": "a", "type": "text", "page": 1, "bbox": None, "content": "unclosed fragment"},
              {"id": "b", "type": "text", "page": 2, "bbox": None, "content": "different paragraph"}]
    assert render_document(blocks) == "unclosed fragment\n\ndifferent paragraph"



def test_right_column_end_continues_at_next_page_left_column():
    blocks = [
        {"id": "a", "type": "text", "page": 1, "bbox": [.55, .85, .9, .95], "content": "the original sentence"},
        {"id": "b", "type": "text", "page": 2, "bbox": [.1, .05, .45, .15], "content": "continues here.", "continues_previous": True},
    ]
    assert render_document(blocks) == "the original sentence continues here."
