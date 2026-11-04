"""独立合成原文验证正文范围、小节身份和明确图号关联。"""

import pytest

from yuxi.knowledge.chunking.paper import chunk_paper


def test_body_spans_exclude_title_but_keep_real_duplicate_and_runin_heading():
    """标题同文正文必须保留，同行标题不能扩大正文范围。"""
    text = "# Study\n\n## Repeated\n\nRepeated\n\n1.1. Method. Body evidence."
    chunks = _split(text)
    first, second = [chunk["source_metadata"] for chunk in chunks]
    assert first["chunker_version"] == "paper_rules_v3"
    assert [text[s["start_char"] : s["end_char"]] for s in first["source_spans"]] == ["Repeated"]
    assert [text[s["start_char"] : s["end_char"]] for s in first["context_source_spans"]] == ["# Study", "## Repeated"]
    assert [text[s["start_char"] : s["end_char"]] for s in second["source_spans"]] == ["Body evidence."]
    assert (
        "1.1. Method."
        in text[second["context_source_spans"][-1]["start_char"] : second["context_source_spans"][-1]["end_char"]]
    )


def test_section_identity_uses_file_source_and_heading_position():
    """同名小节、同名文件与无标题全文不得互串。"""
    text = "# Study\n\n## Same\n\nFirst.\n\n## Same\n\nSecond."
    one, two = _split(text), _split(text, "other")
    assert one[0]["source_metadata"]["section_id"] != one[1]["source_metadata"]["section_id"]
    assert one[0]["source_metadata"]["section_id"] != two[0]["source_metadata"]["section_id"]
    assert _split("Whole untitled paper.")[0]["source_metadata"]["section_id"] is None


def test_explicit_figures_panels_lists_ranges_and_missing_are_exact():
    """S4 与 4 独立，面板归母图，缺失保持可观察。"""
    text = (
        "# Study\n\n## Results\n\nWe compare Figures 1e–g, 2 and 3–4, Fig. S4 and 图9."
        "\n\n## Figures\n\n![a](a.png)\n\nFigure 1. First."
        "\n\nOther paragraph.\n\n![b](b.png)\n\nFigure 2. Second."
        "\n\nOther paragraph.\n\n![c](c.png)\n\nFigure 3. Third."
        "\n\nOther paragraph.\n\n![d](d.png)\n\nFigure 4. Fourth."
        "\n\nOther paragraph.\n\n![s](s.png)\n\nFigure S4. Supplemental."
    )
    chunks = _split(text)
    refs = {ref["figure_id"]: ref for ref in chunks[0]["source_metadata"]["figure_references"]}
    assert set(refs) == {"1", "2", "3", "4", "S4", "9"}
    assert refs["9"]["status"] == "missing"
    for number in ("1", "2", "3", "4", "S4"):
        assert refs[number]["status"] == "linked"
        target = next(chunk for chunk in chunks if chunk["chunk_id"] == refs[number]["target_chunk_id"])
        assert target["source_metadata"]["figure_ids"] == [number]
        assert "![" in target["content"]


def test_ambiguous_caption_and_duplicate_numbers_are_not_guessed():
    """夹在两图间的图注及重复图号都不能作为唯一目标。"""
    text = (
        "# Study\n\n## Results\n\nSee Fig. 1 and Fig. 2."
        "\n\n![a](a.png)\n\nFigure 1. Between.\n\n![b](b.png)"
        "\n\nFigure 2. Two.\n\n![c](c.png)\n\nFigure 2. Duplicate."
    )
    refs = {ref["figure_id"]: ref for ref in _split(text)[0]["source_metadata"]["figure_references"]}
    assert refs["1"]["status"] != "linked"
    assert refs["2"]["status"] != "linked"


def test_panel_and_mixed_supplement_refs_do_not_accept_partial_words():
    """显式面板与补充图号保留，单词尾部不能产生假引用。"""
    text = "# Study\n\n## Result\n\nFigure 2d and Figures 1 and S4. Fig. 1foo is invalid; Fig. 1.2 is exact."
    refs = _split(text)[0]["source_metadata"]["figure_references"]
    assert [ref["figure_id"] for ref in refs] == ["2", "1", "S4", "1.2"]


def test_formula_definition_body_can_explicitly_reference_figure():
    """公式附属的正文解释同样拥有显式图号证据。"""
    text = "# Study\n\n## Model\n\n$$x=a$$\n\nwhere a is the constant in Figure 2d."
    assert _split(text)[0]["source_metadata"]["figure_references"] == [{"figure_id": "2", "status": "missing"}]


def test_unique_bound_figures_with_duplicate_number_are_ambiguous():
    """两张各自明确绑定的图片重用同一图号时不得任选一张。"""
    text = (
        "# Study\n\n## Body\n\nSee Figure 1."
        "\n\n![a](a.png)\n\nFigure 1. One.\n\nSpacer paragraph."
        "\n\n![b](b.png)\n\nFigure 1. Other."
    )
    chunks = _split(text)
    assert sum(chunk["source_metadata"]["figure_ids"] == ["1"] for chunk in chunks) == 2
    assert chunks[0]["source_metadata"]["figure_references"] == [{"figure_id": "1", "status": "ambiguous"}]


def test_reference_list_at_paragraph_start_is_body_not_caption():
    """正文开头的单数图号列表不能被吸成图注。"""
    text = "# Study\n\n## Body\n\nFigure 1 and S4 show the result.\n\n图2、3显示结果。"
    assert [[ref["figure_id"] for ref in chunk["source_metadata"]["figure_references"]] for chunk in _split(text)] == [
        ["1", "S4"],
        ["2", "3"],
    ]


@pytest.mark.parametrize("body", ["Figure 1 shows the result.", "图1显示结果。"])
def test_body_starting_with_figure_reference_links_across_section(body):
    """图号前缀的真实正文仍须关联跨节图片，不能按前缀误删正文。"""
    text = f"# Study\n\n## Result\n\n{body}\n\n## Images\n\n![a](a.png)\n\nFigure 1. Exact caption."
    chunks = _split(text)
    assert chunks[0]["source_metadata"]["figure_references"] == [
        {"figure_id": "1", "status": "linked", "target_chunk_id": chunks[1]["chunk_id"]}
    ]


@pytest.mark.parametrize("conjunction", ["and", "or"])
@pytest.mark.parametrize("last_number", ["3", "4"])
def test_oxford_comma_list_links_last_figure_or_reports_it_missing(conjunction, last_number):
    """牛津逗号后的末项须关联跨节唯一图，缺图时明确报告缺失。"""
    text = (
        f"# Study\n\n## Results\n\nWe compare Figures 1, 2, {conjunction} {last_number}."
        "\n\n## Images\n\n![plot](plot.png)\n\nFigure 3. Exact cross-section caption."
    )
    chunks = _split(text)
    references = chunks[0]["source_metadata"]["figure_references"]
    assert [reference["figure_id"] for reference in references] == ["1", "2", last_number]
    if last_number == "3":
        assert references[-1] == {"figure_id": "3", "status": "linked", "target_chunk_id": chunks[1]["chunk_id"]}
    else:
        assert references[-1] == {"figure_id": "4", "status": "missing"}


def test_unsupported_list_word_does_not_guess_a_following_number():
    """不支持的列表连接词不能把后续数字猜成明确图号。"""
    text = "# Study\n\n## Results\n\nWe compare Figures 1, 2, approximately 3."
    assert [reference["figure_id"] for reference in _split(text)[0]["source_metadata"]["figure_references"]] == [
        "1",
        "2",
    ]


def _split(text, file_id="file"):
    """使用极小目标保留独立证据块。"""
    return chunk_paper(text, file_id, "same-title.md", {"chunk_token_num": 1})
