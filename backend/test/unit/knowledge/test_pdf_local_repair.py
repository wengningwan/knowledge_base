"""人工局部修复 oracle；只编写，留待指定验证机执行。"""

from copy import deepcopy

import pytest

from yuxi.knowledge.parser.pdf_repair import run_rule_repair
from yuxi.knowledge.parser.result import ParsedDocument


def test_plain_word_in_math_paragraph_preserves_original_scientific_ink():
    """普通拼写可局部修复，化学键、双点和完整脚本逐字保留。"""
    before = r"The electrosopun membrane retained C≡N.. and $x_{12}^{34}$."
    native = _native(r"The electrospun membrane retained C≡N.. and $x_{12}^{34}$.")
    result = ParsedDocument(blocks=[_block(before)])

    rows = run_rule_repair(result, [native], 1)

    assert result.blocks[0]["content"] == r"The electrospun membrane retained C≡N.. and $x_{12}^{34}$."
    repaired = next(row for row in rows if row["kind"] == "local_word_repair")
    assert repaired["before"] == before
    assert repaired["candidates"]["ocr"] == before
    assert repaired["after"] == result.blocks[0]["content"]
    assert repaired["status"] == "machine_corrected"
    assert not any(row["status"] == "unresolved" for row in rows)


def test_remaining_scientific_difference_uses_repaired_snapshot():
    """局部拼写之后仍以完整原生候选复核余下科学差异。"""
    before = r"The electrosopun membrane retained C≡N.. at $x_{12}$ and 399.8 eV."
    source = r"The electrospun membrane retained C=N. at $x_{2}$ and 398.3 eV."
    expected = r"The electrospun membrane retained C≡N.. at $x_{12}$ and 399.8 eV."
    result = ParsedDocument(blocks=[_block(before)])

    rows = run_rule_repair(result, [_native(source)], 1)

    assert result.blocks[0]["content"] == expected
    remaining = next(row for row in rows if row["kind"] == "critical_mismatch")
    assert remaining["status"] == "unresolved"
    assert remaining["review_route"] == "vision"
    assert remaining["before"] == expected
    assert remaining["candidates"] == {"ocr": expected, "native": source}


def test_script_geometry_elsewhere_does_not_disqualify_plain_word_geometry():
    """别处脚本真实变小不降低普通词及邻词的独立几何证明。"""
    before = "The electrosopun membrane retained CO₂."
    native = _native("The electrospun membrane retained CO₂.")
    native["chars"][-2].update(font_size=5., origin_normal=14.)
    result = ParsedDocument(blocks=[_block(before)])

    rows = run_rule_repair(result, [native], 1)

    assert result.blocks[0]["content"] == "The electrospun membrane retained CO₂."
    assert any(row["kind"] == "local_word_repair" and row["status"] == "machine_corrected" for row in rows)


@pytest.mark.parametrize(("field", "value"), [("font_size", None), ("font_size", 5.), ("origin_normal", 15.), ("bbox", None)])
def test_local_word_requires_its_own_native_character_geometry(field, value):
    """候选词缺失或异常字符几何时保留 OCR 供视觉裁决。"""
    before = r"The electrosopun membrane retained $x_2$."
    native = _native(r"The electrospun membrane retained $x_2$.")
    native["chars"][5][field] = value
    result = ParsedDocument(blocks=[_block(before)])

    rows = run_rule_repair(result, [native], 1)

    assert result.blocks[0]["content"] == before
    assert not any(row["kind"] == "local_word_repair" for row in rows)
    assert any(row["status"] == "unresolved" for row in rows)


@pytest.mark.parametrize(("before", "source"), [
    (r"The `electrosopun` membrane retained $x_2$.", r"The `electrospun` membrane retained $x_2$."),
    (r"The $electrosopun$ membrane retained $x_2$.", r"The $electrospun$ membrane retained $x_2$."),
    (r"The 399.8 membrane retained $x_2$.", r"The 398.3 membrane retained $x_2$."),
    (r"The mM membrane retained $x_2$.", r"The nM membrane retained $x_2$."),
    (r"The electrosopun changed retained $x_2$.", r"The electrospun membrane retained $x_2$."),
    (r"The electrosopun-membrane retained $x_2$.", r"The electrospun membrane retained $x_2$."),
])
def test_protected_scientific_or_unaligned_words_remain_unresolved(before, source):
    """保护区、科学词或邻词及词界不对齐不能冒充局部普通词证据。"""
    result = ParsedDocument(blocks=[_block(before)])

    rows = run_rule_repair(result, [_native(source)], 1)

    assert result.blocks[0]["content"] == before
    assert not any(row["kind"] == "local_word_repair" for row in rows)
    assert any(row["status"] == "unresolved" for row in rows)


def test_local_word_preserves_markdown_break_and_requires_unique_region_owner():
    """原生空白不覆盖有效换行，共享同一原生区域也不允许局部修复。"""
    before = "The electrosopun membrane.  \nThe value is $x_2$."
    source = "The electrospun membrane.\nThe value is $x_2$."
    result = ParsedDocument(blocks=[_block(before)])
    run_rule_repair(result, [_native(source)], 1)
    assert result.blocks[0]["content"] == "The electrospun membrane.  \nThe value is $x_2$."

    ambiguous = ParsedDocument(blocks=[_block(before), _block(before, id="b2")])
    rows = run_rule_repair(ambiguous, [_native(source)], 1)
    assert [block["content"] for block in ambiguous.blocks] == [before, before]
    assert not any(row["kind"] == "local_word_repair" for row in rows)


def test_local_word_requires_full_region_and_exact_character_mapping():
    """短原生区域或与候选不一致的字符流不能证明正文局部修改。"""
    before = r"The electrosopun membrane retained $x_2$."
    source = r"The electrospun membrane retained $x_2$."
    partial = _native(source, bbox=[.1, .2, .9, .24])
    inconsistent = _native(source)
    inconsistent["chars"][5]["text"] = "z"
    for native in (partial, inconsistent):
        result = ParsedDocument(blocks=[_block(before)])
        rows = run_rule_repair(result, [native], 1)
        assert result.blocks[0]["content"] == before
        assert not any(row["kind"] == "local_word_repair" for row in rows)


def test_repeated_neighborhood_does_not_prove_unique_word_alignment():
    """重复的局部词组不能仅凭序列匹配选择其中一次原生出现。"""
    before = r"The electrosopun membrane and The electrosopun membrane retained $x_2$."
    source = r"The electrospun membrane and The electrosopun membrane retained $x_2$."
    result = ParsedDocument(blocks=[_block(before)])

    rows = run_rule_repair(result, [_native(source)], 1)

    assert result.blocks[0]["content"] == before
    assert not any(row["kind"] == "local_word_repair" for row in rows)


def test_short_native_body_line_with_math_splits_date_without_guessing():
    """短正文首行和数学不会抹去独立原生日期/正文行边界。"""
    before = "Received: June 1, 2025 The sample retained $x_{12}$."
    result = ParsedDocument(blocks=[_block(before, bbox=[.1, .2, .9, .5])])
    rows = run_rule_repair(result, [
        _native("Received: June 1, 2025", bbox=[.1, .2, .9, .25]),
        _native("The sample", id="n2", bbox=[.1, .3, .9, .35]),
        _native(r"retained $x_{12}$.", id="n3", bbox=[.1, .4, .9, .5]),
    ], 1)
    assert [(block["type"], block["content"]) for block in result.blocks] == [
        ("footnote", "Received: June 1, 2025"),
        ("text", r" The sample retained $x_{12}$."),
    ]
    assert any(row["kind"] == "date_body_split" for row in rows)

    uncertain = ParsedDocument(blocks=[_block(before, bbox=[.1, .2, .9, .5])])
    run_rule_repair(uncertain, [_native(before, bbox=[.1, .2, .9, .5])], 1)
    assert uncertain.blocks == [_block(before, bbox=[.1, .2, .9, .5], repair_role="article_metadata")]


def test_structure_owner_title_offset_survives_font_proof():
    """结构 Owner 已确证主标题时，原生字号不能把编号层级再压回。"""
    title = _native("3.2 Results")
    for char in title["chars"]:
        char.update(font_size=14., origin_normal=14.)
    result = ParsedDocument(blocks=[_block("3.2 Results", type="title", level=3, title_hierarchy_document_title_id="main")])
    before = deepcopy(result.blocks)

    rows = run_rule_repair(result, [title, _native("The body text.", id="n2", bbox=[.1, .4, .9, .5])], 1)

    assert result.blocks == [{**before[0], "repair_role": "body"}]
    assert not any(row["kind"] == "title_hierarchy" for row in rows)


def _block(content, **changes):
    """创建可定位人工正文块。"""
    return {"id": "b1", "page": 1, "bbox": [.1, .2, .9, .3], "type": "text", "content": content, **changes}


def _native(content, **changes):
    """构造独立等字号平基线字形证据。"""
    box = changes.get("bbox", [.1, .2, .9, .3])
    advance = (box[2] - box[0]) / max(1, len(content))
    return {
        "id": "n1", "page": 1, "bbox": box, "content": content,
        "chars": [{"text": char, "bbox": [box[0] + index * advance, box[1], box[0] + (index + 1) * advance, box[3]],
                   "baseline": (10., 10., 0.), "font_size": 10., "origin_normal": 10.} for index, char in enumerate(content)],
        **changes,
    }
