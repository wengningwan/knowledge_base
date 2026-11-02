"""以独立版面样例约束结构修复，原文字形与块身份始终守恒。"""

from copy import deepcopy

import pytest

from yuxi.knowledge.parser.pdf_structure import normalize_oxr_result, render_document, restore_reading_flow


def test_numbered_headings_follow_a_font_confirmed_document_title():
    """主标题、章节和小节具有严格父子层级，正文编号不自动变标题。"""
    payload = {"pages": [{"page_num": 0, "page_size": [100, 100], "blocks": [
        {"idx": 0, "type": "title", "bbox": [10, 5, 90, 12], "font_size": 24, "content": "Transport through porous solids"},
        {"idx": 1, "type": "text", "bbox": [10, 20, 45, 40], "font_size": 10, "content": "Abstract remains unchanged."},
        {"idx": 2, "type": "title", "bbox": [10, 45, 45, 50], "font_size": 14, "content": "1. METHODS"},
        {"idx": 3, "type": "title", "bbox": [10, 55, 45, 60], "font_size": 12, "content": "1.1. Preparation"},
        {"idx": 4, "type": "text", "bbox": [10, 65, 45, 75], "font_size": 10, "content": "1.2 units were measured."},
    ]}]}

    result = normalize_oxr_result(payload)

    assert result.markdown == "# Transport through porous solids\n\nAbstract remains unchanged.\n\n## 1. METHODS\n\n### 1.1. Preparation\n\n1.2 units were measured."
    assert [block["id"] for block in result.blocks] == ["p1_b0", "p1_b1", "p1_b2", "p1_b3", "p1_b4"]
    assert any(row["kind"] == "title_hierarchy" and row["block_id"] == "p1_b3" for row in result.diagnostics)


def test_missing_font_evidence_does_not_invent_a_document_title():
    """无字号证据时不把第一个无编号标题猜成论文主标题。"""
    blocks = [
        {"id": "lead", "type": "title", "level": 2, "page": 1, "bbox": [.1, .1, .9, .2], "content": "Background"},
        {"id": "chapter", "type": "title", "level": 1, "page": 1, "bbox": [.1, .3, .45, .4], "content": "1. Methods"},
    ]
    before = deepcopy(blocks)

    restored = restore_reading_flow(blocks, [])

    assert restored == before


@pytest.mark.parametrize("metadata", [
    "Received: March 2, 2024", "Revised: April 3, 2024", "Accepted: April 4, 2024",
    "Published: April 5, 2024", "Published as part of a special issue.", "Cite This: Journal 2024, 7, 10–12",
    "Received 2 March 2024", "Published online 5 April 2024", "Accepted April 4, 2024",
    "Revised 2024-04-03",
])
def test_publication_metadata_cannot_join_a_body_fragment(metadata):
    """出版标签即使位于页边且供应商声明续接，也不能并入正文。"""
    blocks = [
        {"id": "a", "type": "text", "page": 1, "bbox": [.1, .85, .45, .95], "content": metadata},
        {"id": "b", "type": "text", "page": 2, "bbox": [.1, .05, .45, .2], "content": "abundant source material remains available.", "continues_previous": True},
    ]

    assert render_document(blocks) == metadata + "\n\nabundant source material remains available."


def test_explicit_author_role_cannot_be_used_as_body_continuation():
    """有作者身份的块即使没有标签，也保留独立边界。"""
    payload = {"pages": [
        {"page_num": 0, "page_size": [100, 100], "blocks": [{"type": "text", "role": "authors", "bbox": [10, 85, 45, 95], "content": "A. Author and B. Writer"}]},
        {"page_num": 1, "page_size": [100, 100], "blocks": [{"type": "text", "bbox": [10, 5, 45, 20], "content": "an independent paragraph", "continues_previous": True}]},
    ]}

    result = normalize_oxr_result(payload)

    assert result.markdown == "A. Author and B. Writer\n\nan independent paragraph"


def test_column_transition_requires_matched_column_widths():
    """跨栏块须具有兼容栏宽，不能将侧栏或页下注拼入正文。"""
    blocks = [
        {"id": "a", "type": "text", "page": 1, "bbox": [.1, .8, .45, .95], "content": "the measured capacity of"},
        {"id": "b", "type": "text", "page": 1, "bbox": [.55, .1, .9, .25], "content": "the sample was unchanged."},
    ]
    assert render_document(blocks) == "the measured capacity of the sample was unchanged."
    blocks[1]["bbox"] = [.55, .1, .68, .25]
    assert render_document(blocks) == "the measured capacity of\n\nthe sample was unchanged."


@pytest.mark.parametrize("change", [
    {"paragraph_start": True}, {"bbox": [.15, .05, .45, .2]}, {"bbox": None}, {"page": 3},
    {"font_size": 8},
])
def test_paragraph_or_geometry_boundary_overrules_upstream_continuation(change):
    """明确段首、缩进、未知位置和跳页均否决供应商续接提示。"""
    blocks = [
        {"id": "a", "type": "text", "page": 1, "bbox": [.1, .85, .45, .95], "font_size": 12, "content": "incomplete sentence"},
        {"id": "b", "type": "text", "page": 2, "bbox": [.1, .05, .45, .2], "content": "lowercase paragraph", "continues_previous": True, **change},
    ]

    assert render_document(blocks) == "incomplete sentence\n\nlowercase paragraph"


@pytest.mark.parametrize("beginning", ["Recently published measurements of", "Received signals indicate the presence of"])
def test_ordinary_prose_about_publication_can_still_continue(beginning):
    """正文提到出版一词不构成元数据，避免按关键词误挡续句。"""
    blocks = [
        {"id": "a", "type": "text", "page": 1, "bbox": [.1, .85, .45, .95], "content": beginning},
        {"id": "b", "type": "text", "page": 2, "bbox": [.1, .05, .45, .2], "content": "the sample remain available."},
    ]
    diagnostics = []

    restored = restore_reading_flow(blocks, diagnostics)

    assert render_document(restored) == beginning + " the sample remain available."
    assert diagnostics[0]["kind"] == "paragraph_continuation"
    assert diagnostics[0]["original_block_ids"] == ["a", "b"]


@pytest.mark.parametrize("ending", ["N-", "C‐", "Zn‑"])
def test_original_chemical_hyphens_survive_an_explicit_page_continuation(ending):
    """未知断词身份时保留原连字符和块边界，不按常识去连字符。"""
    blocks = [
        {"id": "a", "type": "text", "page": 1, "bbox": [.1, .85, .45, .95], "content": ending},
        {"id": "b", "type": "text", "page": 2, "bbox": [.1, .05, .45, .2], "content": "linked C−N, 69–85..", "continues_previous": True},
    ]

    assert render_document(restore_reading_flow(blocks, [])) == ending + "\n\nlinked C−N, 69–85.."


def test_known_complete_sentence_cannot_join_after_a_citation():
    """末句标点后的上标引文不能掩盖真实段落边界。"""
    blocks = [
        {"id": "a", "type": "text", "page": 1, "bbox": [.1, .85, .45, .95], "content": r"Complete sentence.$^{7}$"},
        {"id": "b", "type": "text", "page": 2, "bbox": [.1, .05, .45, .2], "content": "new paragraph", "continues_previous": True},
    ]

    assert render_document(blocks) == "Complete sentence.$^{7}$\n\nnew paragraph"


def test_restored_float_group_preserves_identity_assets_and_audits_movement():
    """续句先于浮图，原图与图注成组，移动记录包含全部原始块身份。"""
    blocks = [
        {"id": "a", "order": 0, "type": "text", "page": 1, "bbox": [.1, .85, .45, .95], "content": "The transport rate of"},
        {"id": "image", "order": 1, "type": "picture", "page": 2, "bbox": [.1, .01, .45, .08], "content": "![](asset://image_img1)", "asset_id": "image_img1"},
        {"id": "caption", "order": 2, "type": "caption", "page": 2, "bbox": [.1, .09, .45, .13], "content": "Figure 2. Raw C−N and 69–85.."},
        {"id": "b", "order": 3, "type": "text", "page": 2, "bbox": [.1, .15, .45, .3], "content": "the sample increased."},
    ]
    original = deepcopy(blocks)
    diagnostics = []

    restored = restore_reading_flow(blocks, diagnostics)

    assert [block["id"] for block in restored] == ["a", "b", "image", "caption"]
    assert blocks == original
    assert {block["id"]: block for block in restored} == {block["id"]: block for block in original}
    assert render_document(restored) == "The transport rate of the sample increased.\n\n![](asset://image_img1)\n\nFigure 2. Raw C−N and 69–85.."
    row = diagnostics[0]
    assert row["original_block_ids"] == ["a", "image", "caption", "b"]
    assert row["final_block_ids"] == ["a", "b", "image", "caption"]


def test_orphan_caption_is_not_evidence_for_moving_text():
    """孤立图注缺少成组原图时保留服务次序，不猜测图位。"""
    blocks = [
        {"id": "a", "type": "text", "page": 1, "bbox": [.1, .85, .45, .95], "content": "a fragment of"},
        {"id": "c", "type": "caption", "page": 2, "bbox": [.1, .05, .45, .1], "content": "Figure 1. Caption"},
        {"id": "b", "type": "text", "page": 2, "bbox": [.1, .15, .45, .3], "content": "the original sentence."},
    ]
    diagnostics = []

    assert restore_reading_flow(blocks, diagnostics) == blocks
    assert diagnostics == []


def test_reference_entries_are_separated_only_in_a_confirmed_reference_section():
    """文献区连续编号与作者年份共同确认边界，不改正文块或原标点。"""
    content = "(15) Alpha, A.; Beta, B. Pores. Journal 2023, 8, 69–85..\n(16) Gamma, C. Transport. Journal 2024, 9, 12–16.\n(17) Delta, D. Carbon. Journal 2024, 10, 20–24."
    blocks = [
        {"id": "r", "type": "title", "page": 9, "level": 2, "content": "■ REFERENCES"},
        {"id": "refs", "type": "text", "page": 9, "bbox": [.1, .1, .45, .9], "content": content},
    ]
    original = deepcopy(blocks)
    diagnostics = []

    restored = restore_reading_flow(blocks, diagnostics)

    assert render_document(restored) == "## ■ REFERENCES\n\n(15) Alpha, A.; Beta, B. Pores. Journal 2023, 8, 69–85..\n\n(16) Gamma, C. Transport. Journal 2024, 9, 12–16.\n\n(17) Delta, D. Carbon. Journal 2024, 10, 20–24."
    assert [block["content"] for block in restored] == [block["content"] for block in original]
    assert [block["id"] for block in restored] == ["r", "refs"]
    assert diagnostics[0]["kind"] == "reference_boundaries"
    assert diagnostics[0]["boundaries"] == [content.index("(16)"), content.index("(17)")]


def test_incomplete_last_reference_preserves_the_preceding_confirmed_boundaries():
    """末项跨页未完时仍分开此前确证条目，末项文字与块身份保持。"""
    content = "(15) Alpha, A. Pores. Journal 2023, 8, 69–85..\n(16) Beta, B. Transport. Journal 2024, 9, 12–16.\n(17) Gamma, C. A long title that"
    blocks = [
        {"id": "heading", "type": "title", "page": 9, "level": 2, "content": "REFERENCES"},
        {"id": "entries", "type": "text", "page": 9, "bbox": [.1, .1, .45, .95], "content": content},
        {"id": "continuation", "type": "text", "page": 10, "bbox": [.1, .05, .45, .2], "content": "continues on the next page. Journal 2025, 10, 20–24."},
    ]
    original = deepcopy(blocks)
    diagnostics = []

    restored = restore_reading_flow(blocks, diagnostics)

    assert render_document(restored) == "## REFERENCES\n\n(15) Alpha, A. Pores. Journal 2023, 8, 69–85..\n\n(16) Beta, B. Transport. Journal 2024, 9, 12–16.\n\n(17) Gamma, C. A long title that\n\ncontinues on the next page. Journal 2025, 10, 20–24."
    assert restored == original
    assert diagnostics[0]["boundaries"] == [content.index("(16)"), content.index("(17)")]


def test_uncertain_later_number_does_not_erase_an_earlier_reference_boundary():
    """编号跳跃只否决该处边界，不否决此前已有完整证据的相邻条目。"""
    content = "(15) Alpha, A. Journal 2023.\n(16) Beta, B. Journal 2024.\n(18) Gamma, C. A long title that"
    blocks = [
        {"id": "heading", "type": "title", "page": 9, "level": 2, "content": "REFERENCES"},
        {"id": "entries", "type": "text", "page": 9, "content": content},
    ]
    diagnostics = []

    restored = restore_reading_flow(blocks, diagnostics)

    assert render_document(restored) == "## REFERENCES\n\n(15) Alpha, A. Journal 2023.\n\n(16) Beta, B. Journal 2024.\n(18) Gamma, C. A long title that"
    assert restored == blocks
    assert diagnostics[0]["boundaries"] == [content.index("(16)")]


@pytest.mark.parametrize("heading, content", [
    ("Results", "(15) Alpha, A. Journal 2023.\n(16) Beta, B. Journal 2024."),
    ("References", "For equation (15) Alpha was measured in 2023.\n(16) Beta was measured in 2024."),
    ("References", "(15) Alpha, A. Journal 2023.\n(17) Beta, B. Journal 2024."),
    ("References", "(15) First measurement in 2023.\n(16) Second measurement in 2024."),
])
def test_numbered_body_equations_and_uncertain_references_are_not_split(heading, content):
    """缺少文献区、连续编号或作者样式任一证据时保持原段落。"""
    blocks = [
        {"id": "h", "type": "title", "page": 1, "level": 2, "content": heading},
        {"id": "b", "type": "text", "page": 1, "content": content},
    ]

    assert render_document(restore_reading_flow(blocks, [])) == f"## {heading}\n\n{content}"


def test_reference_context_stops_at_the_next_heading():
    """后续章节的编号列表不继承参考文献边界规则。"""
    content = "(15) Alpha, A. Journal 2023.\n(16) Beta, B. Journal 2024."
    blocks = [
        {"id": "refs", "type": "title", "page": 1, "level": 2, "content": "References"},
        {"id": "appendix", "type": "title", "page": 2, "level": 2, "content": "Appendix"},
        {"id": "body", "type": "text", "page": 2, "content": content},
    ]

    assert render_document(blocks) == "## References\n\n## Appendix\n\n" + content
