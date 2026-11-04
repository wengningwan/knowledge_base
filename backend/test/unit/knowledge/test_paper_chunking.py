"""独立合成论文验证证据完整性和原文坐标。"""

import hashlib
import pytest
from pathlib import Path

from yuxi.knowledge.chunking.ragflow_like.dispatcher import chunk_markdown


def paper(text, target=30, parse_assets=None):
    """通过生产分发入口选择论文策略。"""
    return chunk_markdown(
        text,
        "paper-1",
        "合成论文.md",
        {
            "chunk_preset_id": "paper",
            "chunk_parser_config": {"chunk_token_num": target},
        },
        parse_assets=parse_assets,
    )


def test_equation_definitions_belong_to_previous_equation():
    """裸公式标记与前式定义不能进入下一式。"""
    text = (
        "# 合成论文\n\n## 模型\n\n$$\nx=a+b\n$$\n\n其中 a 为长度，b 为宽度。"
        "\n\n$$\ny=c+d\n$$\n\n其中 c 为温度，d 为时间。"
    )
    chunks = paper(text)
    first = next(chunk for chunk in chunks if "x=a+b" in chunk["content"])
    second = next(chunk for chunk in chunks if "y=c+d" in chunk["content"])
    assert "其中 a 为长度，b 为宽度。" in first["content"]
    assert "其中 a 为长度" not in second["content"]
    assert "其中 c 为温度，d 为时间。" in second["content"]
    assert all(chunk["content"].strip() != "$$" for chunk in chunks)
    assert "合成论文" in first["content"] and "模型" in first["content"]


def test_figure_and_markdown_html_tables_keep_captions_and_headers():
    """图表的标题和完整表头属于实际检索正文。"""
    text = (
        "# Study\n\n## Results\n\n![plot](plot.png)\n\nFigure 1. Sample A at 300 K."
        "\n\nTable 1. Measurements\n\n| Sample | Temperature | Yield |\n| --- | --- | --- |\n| A | 300 K | 80% |"
        "\n\nTable 2. Control\n\n<table>\n<tr><th>Sample</th><th>Yield</th></tr>"
        "\n<tr><td>B</td><td>20%</td></tr>\n</table>"
    )
    chunks = paper(text, target=1)
    figure = next(chunk for chunk in chunks if "![plot]" in chunk["content"])
    assert "Figure 1. Sample A at 300 K." in figure["content"]
    table = next(chunk for chunk in chunks if "| A |" in chunk["content"])
    assert "Table 1. Measurements" in table["content"]
    assert "| Sample | Temperature | Yield |" in table["content"]
    html = next(chunk for chunk in chunks if "<table>" in chunk["content"])
    assert "Table 2. Control" in html["content"] and "<th>Sample</th>" in html["content"]
    assert all("over_target_tokens" in chunk["source_metadata"]["warnings"] for chunk in (table, html))


def test_explicit_sample_conditions_only_link_when_unique():
    """不同样品不混合，跨章节出现多个条件来源时拒绝关联。"""
    text = (
        "# Study\n\n## Methods\n\nSample A was prepared at 300 K for 2 h."
        "\n\nSample B was prepared at 400 K for 4 h.\n\n## Results\n\nSample A yielded 80%.\n\nSample B yielded 20%."
    )
    chunks = paper(text)
    result = next(chunk for chunk in chunks if "Sample A yielded" in chunk["content"])
    assert "Sample A was prepared at 300 K for 2 h." in result["content"]
    assert "400 K" not in result["content"] and "Sample B" not in result["content"]
    assert result["source_metadata"]["associations"] == [{"identifier": "sample:a", "status": "linked"}]
    ambiguous = paper(text + "\n\n## Other methods\n\nSample A was prepared at 500 K for 1 h.")
    result = next(chunk for chunk in ambiguous if "Sample A yielded" in chunk["content"])
    assert "was prepared" not in result["content"]
    assert {entry["status"] for entry in result["source_metadata"]["associations"]} == {"ambiguous"}


def test_duplicate_unicode_text_has_distinct_explicit_source_spans():
    """重复原文与非 BMP 字符均保留准确 Unicode 坐标。"""
    text = "# 论文🧪\n\n## 第一章\n\n重复原文α。\n\n## 第二章\n\n重复原文α。"
    chunks = paper(text, target=1)
    assert len(chunks) == 2
    for chunk, line in zip(chunks, (5, 9)):
        metadata = chunk["source_metadata"]
        assert metadata["source_sha256"] == hashlib.sha256(text.encode()).hexdigest()
        body = next(span for span in metadata["source_spans"] if span["line_start"] == line)
        assert body["line_end"] == line
        assert text[body["start_char"] : body["end_char"]] == "重复原文α。"
    assert chunks[0]["source_metadata"]["section_path"] != chunks[1]["source_metadata"]["section_path"]


def test_long_formula_stays_whole_with_warning_and_separate_sections():
    """完整公式超目标仍保留，章节边界阻止误合并。"""
    formula = "x=" + "+a" * 1000
    text = f"# Study\n\n## One\n\n$$\n{formula}\n$$\n\nwhere a is the coefficient.\n\n## Two\n\nUnrelated evidence."
    chunks = paper(text, target=1)
    first = chunks[0]
    assert formula in first["content"] and "where a is the coefficient." in first["content"]
    assert "Unrelated evidence." not in first["content"]
    assert "over_target_tokens" in first["source_metadata"]["warnings"]


def test_layout_location_requires_unique_exact_same_parse_text():
    """重复文本和近似匹配都不能取得 PDF 页码。"""
    text = "# Study\n\nA unique paragraph.\n\nRepeated.\n\nRepeated."
    assets = {
        "blocks": [
            {"text": "A unique paragraph.", "page": 2, "block_id": "p2b1", "bbox": [1, 2, 3, 4]},
            {"text": "Repeated.", "page": 4, "block_id": "p4b1"},
            {"text": "A unique paragraph", "page": 9},
        ]
    }
    chunks = paper(text, target=1, parse_assets=assets)
    span = next(span for span in chunks[0]["source_metadata"]["source_spans"] if span["line_start"] == 3)
    assert span["page"] == 2 and span["block_id"] == "p2b1"
    for chunk in chunks[1:]:
        assert all("page" not in span for span in chunk["source_metadata"]["source_spans"])


def test_separator_retains_text_and_adds_exact_source_metadata():
    """旧策略正文保持不变，只补当前原文精确来源。"""
    text = "α🧪 first\n\nα🧪 first"
    chunks = chunk_markdown(
        text,
        "f",
        "test.md",
        {
            "chunk_preset_id": "separator",
            "chunk_parser_config": {"delimiter": "\\n\\n"},
        },
    )
    assert [chunk["content"] for chunk in chunks] == ["α🧪 first", "α🧪 first"]
    assert [chunk["source_metadata"]["source_spans"][0]["line_start"] for chunk in chunks] == [1, 3]


def test_in_equation_definitions_and_measured_result_from_independent_fixture():
    """固定同题文档证明前式定义及处理条件进入实际检索正文。"""
    text = (Path(__file__).resolve().parents[2] / "fixtures/paper_chunking/paper.md").read_text(encoding="utf-8")
    chunks = paper(text)
    first = next(chunk for chunk in chunks if r"\tag{1}" in chunk["content"])
    second = next(chunk for chunk in chunks if r"\tag{2}" in chunk["content"])
    assert "In eq 1, q is uptake" in first["content"]
    assert "In eq 1, q is uptake" not in second["content"]
    result = next(chunk for chunk in chunks if "Sample SX-1 reached" in chunk["content"])
    assert "300 °C for 2 h under nitrogen" in result["content"]
    assert "400 °C" not in result["content"]
    assert result["source_metadata"]["associations"] == [{"identifier": "sample:sx-1", "status": "linked"}]
    assert len(result["source_metadata"]["evidence_ids"]) == 2
    assert all("Synthetic reference reserved" not in chunk["content"] for chunk in chunks)


def test_reference_section_does_not_create_false_condition_ambiguity():
    """参考文献内容不能成为当前论文的实验条件。"""
    text = (
        "# Study\n\n## Methods\n\nSample A was prepared at 300 K.\n\n## Results\n\nSample A reached 80%."
        "\n\n## 3. References\n\nSample A was prepared at 500 K."
    )
    chunks = paper(text)
    result = next(chunk for chunk in chunks if "reached" in chunk["content"])
    assert "300 K" in result["content"] and "500 K" not in result["content"]
    assert all("500 K" not in chunk["content"] for chunk in chunks)


def test_markdown_table_without_outer_pipes_retains_full_header():
    """标准 Markdown 的可选外侧竖线不能决定表头是否丢失。"""
    text = "# Study\n\nTable 1. Yield\n\nSample | Yield\n--- | ---\nA | 80%\nB | 20%"
    table = paper(text, target=1)[0]
    assert table["source_metadata"]["content_type"] == "table"
    assert "Table 1. Yield" in table["content"] and "Sample | Yield\n--- | ---\nA | 80%\nB | 20%" in table["content"]


def test_h2_paper_title_survives_h1_chapters_and_plain_numbered_subsections():
    """论文题名不随 H1 章节替换，数字小节也构成证据边界。"""
    text = (
        "## Dual-site synthetic study\n\n# 1. INTRODUCTION\n\nBackground.\n\n# 2. METHODS"
        "\n\n2.1. Synthesis of sample A\n\nSample A was treated at 300 K."
        "\n\n2.2. Measurements\n\nUnrelated measurement discussion."
        "\n\n# 3. RESULTS\n\nSample A reached 80%."
    )
    chunks = paper(text, target=1000)
    assert all(chunk["content"].startswith("论文：Dual-site synthetic study\n") for chunk in chunks)
    method = next(chunk for chunk in chunks if "was treated" in chunk["content"] and "reached" not in chunk["content"])
    assert method["source_metadata"]["section_path"] == ["2. METHODS", "2.1. Synthesis of sample A"]
    assert "Unrelated measurement discussion." not in method["content"]
    result = next(chunk for chunk in chunks if "reached" in chunk["content"])
    assert "300 K" in result["content"] and "Unrelated measurement discussion." not in result["content"]
    assert all(chunk["source_metadata"]["context_source_spans"][0]["line_start"] == 1 for chunk in chunks)


def test_section_only_document_uses_filename_as_unconfirmed_title():
    """只有章节标题时不能把 Introduction 猜为论文题名。"""
    assert paper("# 1. INTRODUCTION\n\nBackground.")[0]["content"].startswith("论文：合成论文.md\n")


def test_run_in_subsections_keep_heading_and_body_ranges_separate():
    """同行正文和带末尾句点的小节标题不能混成同一节。"""
    text = (
        "# Study\n\n## 2. Experimental\n\n"
        "2.1. Synthesis of carbon. Sample A was heated at 300 K.\n\n"
        "2.2. Structure characterization. Raman measurements were performed.\n\n"
        "2.3. Adsorption tests.\nMeasurements were performed at 273 K."
    )
    chunks = paper(text, target=1000)
    assert len(chunks) == 3
    for chunk, label, body in zip(
        chunks,
        ("2.1. Synthesis of carbon", "2.2. Structure characterization", "2.3. Adsorption tests"),
        (
            "Sample A was heated at 300 K.",
            "Raman measurements were performed.",
            "Measurements were performed at 273 K.",
        ),
    ):
        assert chunk["source_metadata"]["section_path"] == ["2. Experimental", label]
        assert chunk["content"].split("\n\n", 1)[1] == body
        spans = chunk["source_metadata"]["source_spans"]
        assert any(text[span["start_char"] : span["end_char"]] == body for span in spans)
        context = chunk["source_metadata"]["context_source_spans"]
        assert any(text[span["start_char"] : span["end_char"]].rstrip(".") == label for span in context)
    assert all(chunk["source_metadata"]["chunker_version"] == "paper_rules_v3" for chunk in chunks)


def test_adjacent_lowercase_continuation_is_atomic_even_over_target():
    """跨页的半句话不能仅因小目标长度被拆成两个检索块。"""
    first = "Raman spectroscopy was employed to assess carbon defects, where the D"
    second = "peak corresponds to planar defects and the G peak is a lattice vibration."
    text = f"# Study\n\n## Results\n\n{first}\n\n{second}"
    chunks = paper(text, target=1)
    assert len(chunks) == 1
    assert first in chunks[0]["content"] and second in chunks[0]["content"]
    spans = chunks[0]["source_metadata"]["source_spans"]
    assert any(text[span["start_char"] : span["end_char"]] == first for span in spans)
    assert any(text[span["start_char"] : span["end_char"]] == second for span in spans)


@pytest.mark.parametrize("separator", [".\n\n", ".$^{31}$\n\n", "\n\n## Next section\n\n"])
def test_continuation_does_not_cross_completed_sentence_or_heading(separator):
    """句点、引用后句点和章节边界均保留独立证据。"""
    chunks = paper(f"# Study\n\n## Results\n\nFirst paragraph{separator}lowercase new evidence.", target=1)
    assert len(chunks) == 2


def test_continuation_does_not_merge_different_samples_or_uppercase_paragraphs():
    """明确样品差异和大写起始的新段落不按续句吞并。"""
    text = "# Study\n\nSample A was heated at 300 K\n\nsample B was heated at 400 K.\n\nNew paragraph."
    chunks = paper(text, target=1)
    assert len(chunks) == 3


def test_decimal_measurement_is_not_a_numbered_subsection():
    """以小数温度开头的科学正文不能被当作小节标题。"""
    body = "2.1 K was the temperature difference between the two samples."
    chunk = paper(f"# Study\n\n## Results\n\n{body}")[0]
    assert chunk["source_metadata"]["section_path"] == ["Results"]
    assert chunk["content"].split("\n\n", 1)[1] == body


def test_explicit_equation_number_mismatch_is_not_bound_to_adjacent_formula():
    """显式说明 eq1 的文字不能因为邻近就变成 eq2 的定义。"""
    text = (
        "# Study\n\n## Model\n\n$$\nx=a+b \\tag{1}\n$$"
        "\n\n$$\ny=c+d \\tag{2}\n$$\n\nIn eq 1, a is length and b is width."
    )
    chunks = paper(text, target=1)
    second = next(chunk for chunk in chunks if r"\tag{2}" in chunk["content"])
    assert "a is length" not in second["content"]
    definition = next(chunk for chunk in chunks if "a is length" in chunk["content"])
    assert "unresolved_formula_reference" in definition["source_metadata"]["warnings"]


def test_caption_between_two_unlabelled_figures_does_not_guess_ownership():
    """图注两侧都有未归属图片时显式保留歧义，不能接到错误图片。"""
    text = "# Study\n\n![Image A](a.png)\n\nFigure 2. Caption for image B.\n\n![Image B](b.png)"
    chunks = paper(text, target=1)
    first = next(chunk for chunk in chunks if "a.png" in chunk["content"])
    assert "Figure 2" not in first["content"]
    caption = next(chunk for chunk in chunks if "Figure 2" in chunk["content"])
    assert "ambiguous_caption" in caption["source_metadata"]["warnings"]


@pytest.mark.parametrize(
    ("method", "result"),
    [
        ("The experiment was conducted at 300 K.", "A separate experiment was reported to yield 80%."),
        ("The sample was treated at 300 K.", "Another sample was reported to yield 80%."),
        ("Experiment A was conducted at 300 K.", "A separate experiment A was reported to yield 80%."),
    ],
)
def test_unnamed_or_separate_experiments_are_not_linked_by_ordinary_words(method, result):
    """普通谓语和明确另一个实验不能形成相同实验身份。"""
    text = f"# Study\n\n## Methods\n\n{method}\n\n## Results\n\n{result}"
    chunks = paper(text, target=1)
    result_chunk = next(chunk for chunk in chunks if "yield 80%" in chunk["content"])
    assert "300 K" not in result_chunk["content"]
    assert not any(item["status"] == "linked" for item in result_chunk["source_metadata"]["associations"])
