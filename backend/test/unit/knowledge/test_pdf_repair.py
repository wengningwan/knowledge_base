"""人工微型 oracle：两份审计报告的 7/8 处问题及配对负例。

只保存最短人工摘录，不包含完整论文、自动生成 expected 或模型输出。
这些测试尚未执行，须在用户指定验证机上运行。
"""

from copy import deepcopy

import pytest

from yuxi.knowledge.parser.pdf_repair import run_rule_repair
from yuxi.knowledge.parser.pdf_repair_rules import (
    apply_block_edits, clean_prose_whitespace, proven_representation_equivalent,
)
from yuxi.knowledge.parser.pdf_structure import render_document
from yuxi.knowledge.parser.result import ParsedDocument


@pytest.mark.parametrize("units", [("mM", "nM"), ("cm", "dm"), ("kPa", "hPa")])
def test_r1_cannot_change_prefixed_unit_scale(units):
    """原生候选即使字形清晰也不能自动改变科学单位尺度。"""
    before = f"The value was 1 {units[0]}."
    result = ParsedDocument(blocks=[_block(before)])
    rows = run_rule_repair(result, [_native(f"The value was 1 {units[1]}.")], 1)
    assert result.blocks[0]["content"] == before
    assert any(row.get("review_route") == "vision" for row in rows)


def test_r1_spelling_candidate_cannot_flatten_markdown_line_breaks():
    """拼写变化与空白保护组合时不能把整块压成一行。"""
    before = "The electrosopun membrane.  \nThe next sentence."
    result = ParsedDocument(blocks=[_block(before)])
    run_rule_repair(result, [_native("The electrospun membrane.\nThe next sentence.")], 1)
    assert "  \n" in result.blocks[0]["content"]


def _block(content, **changes):
    """创建可定位的人工正文块。"""
    return {"id": "b1", "page": 1, "bbox": [0.1, 0.2, 0.9, 0.3], "type": "text", "content": content, **changes}


def _native(content, **changes):
    """构造平基线人工字符证据，不伪称真实 PDF 提取结果。"""
    box = changes.get("bbox", [0.1, 0.2, 0.9, 0.3])
    advance = (box[2] - box[0]) / max(1, len(content))
    return {
        "id": "n1", "page": 1, "bbox": box, "content": content,
        "chars": [{"text": char, "bbox": [box[0] + index * advance, box[1], box[0] + (index + 1) * advance, box[3]], "baseline": (10.0, 10.0, 0.0), "font_size": 10.0, "origin_normal": 10.0} for index, char in enumerate(content)],
        **changes,
    }


@pytest.mark.parametrize(("before", "source", "expected"), [
    # LIT-00066 D001/3；D004 以独立重复区域用例覆盖，科学 token 差异另列未决。
    ("compared to the common adsorbents.", "compared to the common absorbents.", "compared to the common absorbents."),
    ("stronger van der Waals attractive interaction", "stronger van der waals attractive interaction", "stronger van der waals attractive interaction"),
    # LIT-00165 D001/5/7/8；数字、型号和字形位置差异保守未决。
    ("This: Energy Fuels 2025, 39", "Cite This: Energy Fuels 2025, 39", "Cite This: Energy Fuels 2025, 39"),
    ("C2H6 suggesting a promising potential", "C2H6, suggesting a promising potential", "C2H6, suggesting a promising potential"),
    ("Dubinin, M.; Stoeckeli, H.", "Dubinin, M.; Stoeckli, H.", "Dubinin, M.; Stoeckli, H."),
    ("Graham, M. J.; Greiner, A; Reneker, D. H.;", "Graham, M. J.; Greiner, A.; Reneker, D. H.;", "Graham, M. J.; Greiner, A.; Reneker, D. H.;"),
])
def test_manual_plain_text_oracles(before, source, expected):
    """确证的局部原生候选修复后仍由 blocks 渲染。"""
    result = ParsedDocument(blocks=[_block(before)])
    rows = run_rule_repair(result, [_native(source)], 1)
    assert result.blocks[0]["content"] == expected
    assert render_document(result.blocks) == expected
    assert any(row["status"] == "machine_corrected" for row in rows)
    assert result.reviews == []
    assert result.metadata["pdf_repair"]["model_requests"] == 0


@pytest.mark.parametrize(("before", "source"), [
    ("The peak at 399.8 eV is assigned to pyridinic nitrogen", "The peak at 398.3 eV is assigned to pyridinic nitrogen"),
    ("The heat was 20-22.2 K/mol.", "The heat was 20-22.2 KJ/mol."),
    ("AICChE J. 11, 121–127.", "AIChE J. 11, 121–127."),
    ("AICHE J. 2016.", "AIChE J. 2016."),
    ("zeolite SA (1.4),", "zeolite 5A (1.4),"),
    ("the regenerability of adsorbents.$^{7}$", "the regenerability of adsorbents.⁶"),
    ("carbonaceous adsorbents.$^{[1]}$", "carbonaceous adsorbents.¹¹"),
    ("represents the Eₘg symmetric vibration mode", "represents the E₂g symmetric vibration mode"),
])
def test_manual_scientific_oracles_remain_vision_unresolved(before, source):
    """数值、单位、引用、型号和上下标差异不伪称 R1 已修复。"""
    result = ParsedDocument(blocks=[_block(before)])
    rows = run_rule_repair(result, [_native(source)], 1)
    assert result.blocks[0]["content"] == before
    assert any(row["status"] == "unresolved" and row["review_route"] == "vision" for row in rows)


def test_extra_ocr_copy_requires_same_region_and_single_native_occurrence():
    """LIT-00066 D004 类新增重复只在同位置出现次数可证明时删除。"""
    text = "Table 3 summarizes the adsorption capacities."
    result = ParsedDocument(blocks=[_block(text), _block(text, id="b2")])
    rows = run_rule_repair(result, [_native(text)], 1)
    assert [block["content"] for block in result.blocks] == [text, ""]
    assert any(row["kind"] == "ocr_duplicate" and row["status"] == "machine_corrected" for row in rows)
    true_repeat = ParsedDocument(blocks=[_block(text), _block(text, id="b2", bbox=[0.1, 0.4, 0.9, 0.5])])
    run_rule_repair(true_repeat, [_native(text), _native(text, id="n2", bbox=[0.1, 0.4, 0.9, 0.5])], 1)
    assert [block["content"] for block in true_repeat.blocks] == [text, text]


@pytest.mark.parametrize("text", ["ambinent temperature", "2022, 2022", "AICHE J. 2016.", "zeolite SA (1.4),", "C-PDA-2 has a metal-organic structure."])
def test_source_spelling_repetition_and_chemical_hyphens_are_preserved(text):
    """原文自身拼写、年份、型号和合法连字符不可按常识修改。"""
    result = ParsedDocument(blocks=[_block(text)])
    rows = run_rule_repair(result, [_native(text)], 1)
    assert result.blocks[0]["content"] == text
    assert not any(row["status"] == "machine_corrected" for row in rows)


def test_wrong_region_and_damaged_native_cannot_override_correct_ocr():
    """同页另一处 398.3 或损坏字体不支持修改正确的 399.8。"""
    text = "The peak was 399.8 eV."
    for native in [_native("The peak was 398.3 eV.", bbox=[0.1, 0.5, 0.9, 0.6]), _native("The peak was 398.3 \ue001V.")]:
        result = ParsedDocument(blocks=[_block(text)])
        rows = run_rule_repair(result, [native], 1)
        assert result.blocks[0]["content"] == text
        assert not any(row["status"] == "machine_corrected" for row in rows)


def test_incomplete_native_line_cannot_delete_tail_of_large_ocr_block():
    """单行仅被 OCR 大框包含，不能证明完整段落或删除数值尾句。"""
    before = "The common adsorbents were studied. The amount was 12 mg."
    result = ParsedDocument(blocks=[_block(before, bbox=[0.1, 0.2, 0.9, 0.7])])
    rows = run_rule_repair(result, [_native("The common absorbents were studied.", bbox=[0.1, 0.2, 0.9, 0.3])], 1)
    assert result.blocks[0]["content"] == before
    assert any(row["status"] == "unresolved" for row in rows)
    spelling_tail = "The common adsorbents were studied. More text."
    result = ParsedDocument(blocks=[_block(spelling_tail, bbox=[0.1, 0.2, 0.9, 0.7])])
    run_rule_repair(result, [_native("The common absorbents were studied.", bbox=[0.1, 0.2, 0.9, 0.3])], 1)
    assert result.blocks[0]["content"] == spelling_tail


def test_incomplete_character_geometry_cannot_support_whole_line_repair():
    """字符包围框仅覆盖行的一角时证据质量必须降级。"""
    before = "compared to the common adsorbents."
    native = _native("compared to the common absorbents.")
    for char in native["chars"]:
        char["bbox"] = [0.1, 0.2, 0.11, 0.3]
    result = ParsedDocument(blocks=[_block(before)])
    rows = run_rule_repair(result, [native], 1)
    assert result.blocks[0]["content"] == before
    assert rows[0]["source_quality"] == "weak"


@pytest.mark.parametrize("contact", ["* E-mail: person@example.test", "* person@example.test", "Correspondence: person@example.test"])
def test_repeated_margin_contact_variants_remain_article_metadata(contact):
    """重复页边的邮箱、E-mail 与 Correspondence 不作为出版噪声排除。"""
    result = ParsedDocument()
    rows = run_rule_repair(result, [_native(contact, bbox=[0.1, 0.95, 0.9, 0.99]), _native(contact, id="n2", page=2, bbox=[0.1, 0.95, 0.9, 0.99])], 2)
    assert result.metadata["pdf_repair"]["excluded_publication_elements"] == 0
    assert len([row for row in rows if row["kind"] == "missing_region"]) == 2


def test_unreliable_baseline_and_pua_require_vision():
    """非平基线提示上下标风险，PUA 不采用猜字替换。"""
    native = _native("The sample was 5A.")
    native["chars"][4]["baseline"] = (15.0, 5.0, 0.0)
    native["chars"][4].update(font_size=5.0, origin_normal=15.0)
    result = ParsedDocument(blocks=[_block("The sample was SA.")])
    rows = run_rule_repair(result, [native], 1)
    assert result.blocks[0]["content"] == "The sample was SA."
    assert rows[0]["review_route"] == "vision"
    damaged = ParsedDocument(blocks=[_block("Q \ue001 \ufffd")])
    rows = run_rule_repair(damaged, [], 1)
    assert damaged.blocks[0]["content"] == "Q \ue001 \ufffd"
    assert rows[0]["kind"] == "damaged_glyph"


def test_chinese_and_short_digit_coverage_and_publication_classification():
    """中文、短数字、唯一 DOI/SI/通讯均不被噪声过滤吞掉。"""
    natives = [
        _native("中文缺句"), _native("6", id="n2", bbox=[0.1, 0.4, 0.2, 0.5]),
        _native("Downloaded via EXAMPLE", id="n3", bbox=[0.1, 0.95, 0.9, 0.99]),
        _native("https://doi.org/10.1234/example", id="n4", bbox=[0.1, 0.95, 0.9, 0.99]),
        _native("Supporting Information is available", id="n5", bbox=[0.1, 0.95, 0.9, 0.99]),
        _native("Corresponding author Email: a@example.test", id="n6", bbox=[0.1, 0.95, 0.9, 0.99]),
    ]
    result = ParsedDocument()
    rows = run_rule_repair(result, natives, 1)
    missing = [row["candidates"]["native"] for row in rows if row["kind"] == "missing_region"]
    assert missing == ["中文缺句", "6", "https://doi.org/10.1234/example", "Supporting Information is available", "Corresponding author Email: a@example.test"]
    assert result.blocks == []
    assert result.metadata["pdf_repair"]["excluded_publication_elements"] == 1


def test_repeated_margin_and_image_text_are_separate_from_body_omission():
    """重复页眉和图内文字不触发正文漏段修复。"""
    result = ParsedDocument(blocks=[_block("![](asset://figure)", type="picture")])
    natives = [_native("axis label"), _native("Journal running header", id="h1", bbox=[0.1, 0.01, 0.9, 0.05]), _native("Journal running header", id="h2", page=2, bbox=[0.1, 0.01, 0.9, 0.05])]
    rows = run_rule_repair(result, natives, 2)
    assert not any(row["kind"] == "missing_region" for row in rows)
    assert result.blocks[0]["content"] == "![](asset://figure)"
    assert result.metadata["pdf_repair"]["excluded_publication_elements"] == 2


def test_footer_copyright_page_sequence_and_nearby_doi_are_excluded():
    """页脚版权、递进页码与邻近独立 DOI 留存证据，不报漏段。"""
    natives = [
        _native("© 2025 American Chemical Society", bbox=[.08, .95, .4, .97]),
        _native("15855", id="page1", bbox=[.48, .95, .53, .97]),
        _native("15856", id="page2", page=2, bbox=[.48, .95, .53, .97]),
        _native("https://doi.org/10.1021/example", id="doi", bbox=[.1, .92, .75, .94]),
    ]
    original = deepcopy(natives)
    result = ParsedDocument()

    rows = run_rule_repair(result, natives, 2)

    assert len(rows) == 4
    assert all(row["kind"] == "publication_element" and row["classification"] == "excluded" for row in rows)
    assert all(row["status"] == "checked" and row["review_route"] == "none" for row in rows)
    assert [row["candidates"]["native"] for row in rows] == [entry["content"] for entry in natives]
    assert all(row["evidence"] for row in rows)
    assert result.blocks == [] and natives == original
    assert result.metadata["pdf_repair"]["excluded_publication_elements"] == 4


@pytest.mark.parametrize("content", [
    "© 2025 The Authors. Published by Example publisher.",
    "Copyright 2025 Example publisher",
    "(c) 2025 Example publisher",
])
def test_single_margin_copyright_variants_are_excluded(content):
    """明确版权声明含 Authors 时仍按出版用途分类。"""
    rows = run_rule_repair(ParsedDocument(), [_native(content, bbox=[.1, .95, .9, .97])], 1)

    assert len(rows) == 1
    assert rows[0]["classification"] == "excluded"
    assert rows[0]["candidates"]["native"] == content


@pytest.mark.parametrize("kind", ["page_header", "page_footer"])
@pytest.mark.parametrize("content", ["15855", "Unique journal heading", "https://doi.org/10.1021/example"])
def test_explicit_header_footer_proves_native_publication_region(kind, content):
    """解析块类型支持原生区域归属，单页页眉页脚不再产生差异。"""
    block = _block(content, type=kind, bbox=[.1, .86, .9, .89])
    result = ParsedDocument(blocks=[block])

    rows = run_rule_repair(result, [_native(content, bbox=block["bbox"])], 1)

    assert len(rows) == 1
    assert rows[0]["classification"] == "excluded"
    assert rows[0]["candidates"]["native"] == content
    assert block["content"] == content
    assert render_document(result.blocks) == ""


def test_repeated_footer_doi_is_excluded_but_body_reference_is_preserved():
    """同位重复页脚 DOI 可排除，正文相同 DOI 仍是遗漏候选。"""
    doi = "https://doi.org/10.1021/example"
    natives = [
        _native(doi, bbox=[.1, .95, .8, .97]),
        _native(doi, id="n2", page=2, bbox=[.1, .95, .8, .97]),
        _native(doi, id="n3", bbox=[.1, .3, .8, .32]),
    ]

    rows = run_rule_repair(ParsedDocument(), natives, 2)

    assert [row["classification"] for row in rows] == ["excluded", "excluded", "content"]
    assert rows[2]["kind"] == "missing_region"


@pytest.mark.parametrize("content", [
    "(12) Smith. Title. https://doi.org/10.1021/example",
    "Data are available at https://doi.org/10.1021/example",
    "Author Contributions",
    "Author: Jane Doe",
    "Supporting Information is available",
])
def test_repeated_margin_references_and_author_metadata_remain_missing(content):
    """完整引用、DOI 说明和作者元数据不能因重复而排除。"""
    natives = [
        _native(content, bbox=[.1, .95, .8, .97]),
        _native(content, id="n2", page=2, bbox=[.1, .95, .8, .97]),
    ]

    rows = run_rule_repair(ParsedDocument(), natives, 2)

    assert len(rows) == 2
    assert all(row["kind"] == "missing_region" and row["classification"] == "content" for row in rows)


def test_explicit_footnotes_do_not_supply_running_footer_evidence():
    """明确脚注虽逐页同位重复，仍保留内容核对与原文。"""
    text = "Footnote with experimental details."
    blocks = [_block(text, id=f"b{page}", page=page, type="footnote", bbox=[.1, .95, .8, .97]) for page in (1, 2)]
    natives = [_native(text, id=f"n{page}", page=page, bbox=[.1, .95, .8, .97]) for page in (1, 2)]
    result = ParsedDocument(blocks=blocks)

    rows = run_rule_repair(result, natives, 2)

    assert not any(row["classification"] == "excluded" for row in rows)
    assert all(block["content"] == text for block in result.blocks)
    assert render_document(result.blocks).count(text) == 2


def test_body_repetition_does_not_prove_running_header():
    """正文中的相同文字不能给单次页边文字提供重复页眉证据。"""
    natives = [
        _native("Results and discussion", bbox=[.1, .02, .8, .04]),
        _native("Results and discussion", id="n2", page=2),
    ]

    rows = run_rule_repair(ParsedDocument(), natives, 2)

    assert len(rows) == 2
    assert all(row["kind"] == "missing_region" for row in rows)


@pytest.mark.parametrize("content,box", [
    ("15855", [.1, .3, .2, .32]),
    ("© 2025 American Chemical Society", [.1, .3, .8, .32]),
    ("https://doi.org/10.1021/example", [.1, .3, .8, .32]),
    ("15855", [.48, .95, .53, .97]),
    ("https://doi.org/10.1021/example", [.1, .95, .8, .97]),
    ("Measured yield was 15855 at the final step.", [.1, .95, .8, .97]),
    ("(12) Author. Title. https://doi.org/10.1021/example", [.1, .95, .8, .97]),
    ("Footnote with experimental details.", [.1, .95, .8, .97]),
])
def test_publication_like_text_without_layout_proof_remains_missing(content, box):
    """数字、版权和 DOI 内容本身不构成出版区域证明。"""
    rows = run_rule_repair(ParsedDocument(), [_native(content, bbox=box)], 1)

    assert len(rows) == 1
    assert rows[0]["kind"] == "missing_region"
    assert rows[0]["candidates"]["native"] == content


@pytest.mark.parametrize("content", ["15855", "https://doi.org/10.1021/example", "© 2025 Example publisher"])
def test_figure_and_table_margin_text_is_not_classified_as_publication(content):
    """图表覆盖的页边文字按图表保留，不套用出版噪声规则。"""
    blocks = [_block("table data", type="table", bbox=[.1, .90, .9, .99])]
    natives = [_native(content, bbox=[.1, .95, .8, .97])]
    result = ParsedDocument(blocks=blocks)

    rows = run_rule_repair(result, natives, 1)

    assert rows == []
    assert result.metadata["pdf_repair"]["excluded_publication_elements"] == 0
    assert blocks[0]["content"] == "table data"


@pytest.mark.parametrize("value,box", [
    ("15860", [.48, .95, .53, .97]),
    ("15856", [.48, .91, .53, .93]),
])
def test_nonsequential_or_unaligned_margin_numbers_remain_missing(value, box):
    """页码需要同时满足相邻页递进与位置对应。"""
    natives = [
        _native("15855", bbox=[.48, .95, .53, .97]),
        _native(value, id="n2", page=2, bbox=box),
    ]

    rows = run_rule_repair(ParsedDocument(), natives, 2)

    assert len(rows) == 2
    assert all(row["kind"] == "missing_region" for row in rows)


def test_bare_math_delimiters_preserve_tokens_and_protected_content():
    """图注定界只补完整数学 token；代码、HTML、图片和 URL 保留。"""
    content = "CO_2, C_2H_4/C_2H_6 and min^{-1}; `CO_2` https://x.test/C_2H_4 ![](asset://a) <table><th>CO_2</th></table>"
    result = ParsedDocument(blocks=[_block(content, type="caption")])
    run_rule_repair(result, [], 1)
    assert result.blocks[0]["content"] == "$CO_2$, $C_2H_4/C_2H_6$ and $min^{-1}$; `CO_2` https://x.test/C_2H_4 ![](asset://a) <table><th>CO_2</th></table>"
    protected = ParsedDocument(blocks=[_block("```\nCO_2", type="code_block"), _block("file_name and C-PDA-2", id="b2")])
    run_rule_repair(protected, [], 1)
    assert protected.blocks[0]["content"] == "```\nCO_2"
    assert protected.blocks[1]["content"] == "file_name and C-PDA-2"


def test_empty_math_and_figure_interruption_do_not_claim_full_repair():
    """空公式组、标题和浮图结构在证据不足时明确未决。"""
    result = ParsedDocument(blocks=[
        _block("ratio of 4:1,"), _block("![](asset://fig)", id="fig", page=2, type="picture"),
        _block("Figure 3. CO₂", id="cap", page=2, type="caption"),
        _block("the percentage of sites", id="b2", page=2),
        _block(r"$x_{}$", id="math", page=2, type="formula"),
        _block("3.2 Results", id="title", page=2, type="title", level=1),
    ])
    rows = run_rule_repair(result, [], 2)
    assert {"reading_flow", "math_syntax", "title_hierarchy", "native_evidence_unavailable"}.issubset({row["kind"] for row in rows})
    assert result.blocks[0]["content"] == "ratio of 4:1,"
    assert result.blocks[3]["content"] == "the percentage of sites"
    assert result.blocks[-1]["level"] == 1
    assert all(row["status"] in {"unresolved", "not_checked"} for row in rows)


def test_group_failure_rolls_back_all_edits_and_forbids_protected_changes():
    """同组任一 before 失败不会留下部分修改，代码不可被补丁替换。"""
    blocks = [_block("first"), _block("second", id="b2")]
    assert not apply_block_edits(blocks, [{"block_id": "b1", "before": "first", "after": "changed"}, {"block_id": "b2", "before": "stale", "after": "changed"}])
    assert [block["content"] for block in blocks] == ["first", "second"]
    block = _block("`CO_2` and text")
    assert not apply_block_edits([block], [{"block_id": "b1", "before": block["content"], "after": "`CO_3` and text"}])


def test_rule_replay_is_deterministic_from_same_snapshot():
    """固定人工快照两次重放具有相同正文和稳定问题 ID。"""
    first = ParsedDocument(blocks=[_block("The peak was 399.8 eV.")])
    second = deepcopy(first)
    first_rows = run_rule_repair(first, [_native("The peak was 398.3 eV.")], 1)
    second_rows = run_rule_repair(second, [_native("The peak was 398.3 eV.")], 1)
    assert first.blocks == second.blocks
    assert first_rows == second_rows


def test_real_small_punctuation_ink_is_not_treated_as_small_font():
    """句号墨迹小但字号/文字原点一致，普通行仍支持局部拼写修复。"""
    source = "Dubinin, M.; Stoeckli, H."
    native = _native(source)
    punctuation = native["chars"][-1]
    punctuation["bbox"][1] = .28
    punctuation["baseline"] = (8., 1., 0.)
    result = ParsedDocument(blocks=[_block("Dubinin, M.; Stoeckeli, H.")])
    rows = run_rule_repair(result, [native], 1)
    assert result.blocks[0]["content"] == source
    assert any(row["status"] == "machine_corrected" for row in rows)


@pytest.mark.parametrize("field,value", [("font_size", 5.), ("origin_normal", 15.), ("font_size", None)])
def test_native_font_or_actual_baseline_shift_prevents_automatic_replacement(field, value):
    """真实上下标或缺少字号不能以墨迹框一致代替强证据。"""
    native = _native("Dubinin, M.; Stoeckli, H.")
    native["chars"][3][field] = value
    before = "Dubinin, M.; Stoeckeli, H."
    result = ParsedDocument(blocks=[_block(before)])
    rows = run_rule_repair(result, [native], 1)
    assert result.blocks[0]["content"] == before
    assert any(row["status"] == "unresolved" for row in rows)


def test_mixed_page_scanned_region_is_explicitly_not_checked():
    """同页另一块有原生文字不能证明扫描正文区域已检查。"""
    scanned = _block("这是扫描区域的正常OCR正文。", id="scan", bbox=[.1,.5,.9,.6])
    result = ParsedDocument(blocks=[_block("Native paragraph."), scanned])
    rows = run_rule_repair(result, [_native("Native paragraph.")], 1)
    assert any(row["block_id"] == "scan" and row["status"] == "not_checked" and row["review_route"] == "none" for row in rows)


@pytest.mark.parametrize(("left", "right"), [
    (r"$C_{2}H_{4}$", "C₂H₄"),
    (r"$\mathrm{CO}_{2}$", "CO₂"),
    (r"$cm^{-1}$", "cm⁻¹"),
    ("cafe\u0301", "café"),
    ("ﬁber", "fiber"),
])
def test_only_proven_presentation_equivalence_skips_model(left, right):
    """人工表示正例有同一科学 token 与脚本位置。"""
    assert proven_representation_equivalent(left, right)
    result = ParsedDocument(blocks=[_block(left)])
    rows = run_rule_repair(result, [_native(right)], 1)
    assert result.blocks[0]["content"] == left
    assert not any(row["kind"] in {"text_mismatch", "critical_mismatch"} and row["status"] == "unresolved" for row in rows)
    assert all(row["status"] in {"proven_equivalent", "machine_corrected"} for row in rows)


@pytest.mark.parametrize(("left", "right"), [
    (r"$C_{2}H_{4}$", "C₂H₆"), ("E₂g", "Eₘg"), ("5A", "SA"),
    ("C=N", "C≡N"), ("C-N", "C=N"), ("−1", "1"),
    (r"$C_2H_4$", "C2H4"), (r"$x^{2}$", "x₂"),
    (r"$\unknown{x}$", "x"), ("{a,b}", "a,b"),
])
def test_scientific_differences_and_unknown_commands_are_not_equivalence(left, right):
    """负例不能因消除标点、上下标或未知命令而免去核对。"""
    assert not proven_representation_equivalent(left, right)


def test_whitespace_s01_to_s06_preserves_structures_and_math():
    """逐字 expected 人工指定，保留双空格断行、缩进、数学和全角空格。"""
    before = "Text  with\tspace.   \n \t \n\n\n汉 字 ， 下 文　保 留\nA\u00a0B\u2007C\u202fD $x  +  y$ and `a  b`\n- list  untouched\n    code  untouched\n| A  | B |\n<eq>x  y</eq>\n"
    expected = "Text with space.  \n\n汉字，下文　保留\nA B C D $x  +  y$ and `a  b`\n- list  untouched\n    code  untouched\n| A  | B |\n<eq>x  y</eq>\n"
    assert clean_prose_whitespace(before) == expected
    fenced = "```\n \t \n\n\na  b\n```\n"
    assert clean_prose_whitespace(fenced) == fenced


def test_unique_native_date_boundary_splits_without_losing_characters():
    """日期混入正文只能由独立原生行边界拆开，所有原字符守恒。"""
    date = "Received: June 1, 2025"
    body = "The membrane was prepared by electrospinning."
    original = date + " " + body
    result = ParsedDocument(blocks=[_block(original, bbox=[.1, .2, .9, .4])])
    rows = run_rule_repair(result, [
        _native(date, bbox=[.1, .2, .9, .25]),
        _native(body, id="n2", bbox=[.1, .3, .9, .4]),
    ], 1)
    assert [block["id"] for block in result.blocks] == ["b1", "b1_body"]
    assert "".join(block["content"] for block in result.blocks) == original
    assert [block["type"] for block in result.blocks] == ["footnote", "text"]
    audit = next(row for row in rows if row["kind"] == "date_body_split")
    assert audit["original_block_ids"] == ["b1"]
    assert audit["final_block_ids"] == ["b1", "b1_body"]
    uncertain = ParsedDocument(blocks=[_block(original)])
    run_rule_repair(uncertain, [], 1)
    assert len(uncertain.blocks) == 1 and uncertain.blocks[0]["content"] == original


def test_same_native_line_proves_fragment_join_but_page_hint_does_not():
    """单一原生行身份能合碎块；跨页句法启发式不能证明段落。"""
    left, right = "The membrane", "was prepared."
    result = ParsedDocument(blocks=[_block(left, bbox=[.1,.2,.45,.3]), _block(right, id="b2", bbox=[.45,.2,.9,.3])])
    rows = run_rule_repair(result, [_native(left + " " + right)], 1)
    assert len(result.blocks) == 1
    assert result.blocks[0]["content"] == "The membrane was prepared."
    assert next(row for row in rows if row["kind"] == "same_line_join")["original_block_ids"] == ["b1", "b2"]
    uncertain = ParsedDocument(blocks=[_block(left), _block(right, id="b2", page=2, continues_previous=True)])
    run_rule_repair(uncertain, [], 2)
    assert len(uncertain.blocks) == 2


def test_title_font_and_exact_native_number_support_hierarchy():
    """标题身份由独立字号支持时才按原文编号修层级。"""
    title = _native("3.2 Results")
    for char in title["chars"]:
        char.update(font_size=14., origin_normal=14.)
    result = ParsedDocument(blocks=[_block("3.2 Results", type="title", level=1)])
    body = _native("The sample was prepared.", id="n2", bbox=[.1,.4,.9,.5])
    rows = run_rule_repair(result, [title, body], 1)
    assert result.blocks[0]["level"] == 2
    assert any(row["kind"] == "title_hierarchy" and row["status"] == "machine_corrected" for row in rows)
