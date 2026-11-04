"""固定原文范围的对照指标不依赖任何切分方案的块编号。"""

from __future__ import annotations

import copy
import hashlib
from collections import Counter
from pathlib import Path

import pytest

from yuxi.knowledge.eval.chunking_comparison import (
    compare_variants,
    load_cases,
    score_context,
    validate_comparison_sources,
)


def _case() -> dict:
    """构造独立原文范围和必要条件。"""
    return {
        "id": "conditions",
        "query": "What conditions produced the reported uptake?",
        "gold_evidence": [
            {"source_sha256": "a" * 64, "start_char": 10, "end_char": 30, "line_start": 2, "line_end": 2}
        ],
        "required_conditions": [["300 °C", "300°C"], ["1 bar"]],
        "answer_points": ["State treatment temperature and measurement pressure."],
    }


def _chunk(start: int, end: int, text: str, fingerprint: str = "a" * 64) -> dict:
    """返回带明确来源的测试检索片段。"""
    return {
        "content": text,
        "metadata": {
            "chunk_id": f"unrelated-id-{start}",
            "source_metadata": {
                "source_sha256": fingerprint,
                "source_spans": [{"start_char": start, "end_char": end, "line_start": 2, "line_end": 2}],
            },
        },
    }


def test_evidence_coverage_uses_source_ranges_and_deduplicates_overlap() -> None:
    """重叠命中不虚增覆盖；相同位置但不同原文不计命中。"""
    case = _case()
    chunks = [_chunk(10, 22, "300°C"), _chunk(18, 30, "1 bar"), _chunk(10, 30, "other", "b" * 64)]
    assert score_context(chunks, case)["evidence_coverage"] == 1
    assert score_context(chunks[:1], case)["evidence_details"][0]["range_coverage"] == 0.6
    assert score_context(chunks[-1:], case)["evidence_coverage"] == 0


def test_short_fragment_on_same_line_does_not_cover_the_whole_evidence() -> None:
    """同一行只命中少量字符不能宣称完整证据命中。"""
    assert score_context([_chunk(10, 12, "q")], _case())["evidence_coverage"] == 0


def test_condition_text_check_does_not_confuse_11_bar_with_1_bar() -> None:
    """词面条件检查容忍空白，不混淆不同数值。"""
    metrics = score_context([_chunk(10, 30, "300°C at 11 bar")], _case())
    assert metrics["condition_text_coverage"] == 0.5
    assert metrics["condition_details"][1]["present"] is False


@pytest.mark.parametrize(
    ("text", "term"),
    [("0.1 bar", "1 bar"), ("1.1 bar", "1 bar"), ("-300 K", "300 K"), ("−300 K", "300 K"), ("1.1", "1")],
)
def test_condition_check_does_not_match_decimal_or_signed_number_fragments(text, term) -> None:
    """小数尾部和负数绝对值不能冒充所需数值条件。"""
    case = _case()
    case["required_conditions"] = [[term]]
    assert score_context([_chunk(10, 30, text)], case)["condition_text_coverage"] == 0


@pytest.mark.parametrize(
    ("corpora", "embeddings"),
    [
        ([Counter({"a" * 64: 1}), Counter({"b" * 64: 1})], ["embed", "embed"]),
        ([Counter({"a" * 64: 1}), Counter({"a" * 64: 2})], ["embed", "embed"]),
        ([Counter({"a" * 64: 1})] * 2, ["embed-a", "embed-b"]),
    ],
)
def test_different_corpus_duplicates_or_embedding_cannot_be_compared(corpora, embeddings) -> None:
    """防止文档重复量、原文版本或模型差异冒充切分改进。"""
    with pytest.raises(ValueError, match="同一批 Markdown"):
        validate_comparison_sources([_case()], corpora, embeddings)


def test_stale_gold_fingerprint_is_rejected() -> None:
    """旧版本原文题目不能套用新解析正文。"""
    with pytest.raises(ValueError, match="原文指纹"):
        validate_comparison_sources([_case()], [Counter({"b" * 64: 1})] * 2, ["embed"] * 2)


def test_fixed_synthetic_oracle_matches_committed_source() -> None:
    """只核对已提交金标准，不在测试中重新生成它。"""
    root = Path(__file__).resolve().parents[2] / "fixtures" / "paper_chunking"
    text = (root / "paper.md").read_text(encoding="utf-8")
    cases = load_cases(root / "questions.jsonl")
    fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert [case["id"] for case in cases] == ["formula", "sample_conditions", "figure_caption", "table_header"]
    for case in cases:
        for item in case["gold_evidence"]:
            assert item["source_sha256"] == fingerprint
            assert 0 <= item["start_char"] < item["end_char"] <= len(text)
            assert item["line_start"] == text.count("\n", 0, item["start_char"]) + 1
            assert item["line_end"] == text.count("\n", 0, item["end_char"] - 1) + 1


@pytest.mark.asyncio
async def test_comparison_scores_the_actual_first_five_answer_chunks() -> None:
    """第六块召回正确也不能冒充实际回答上下文已含证据。"""
    cases = [_case()]
    original = copy.deepcopy(cases)
    calls = []
    prompts = []

    async def query(text, *, kb_id, **config):
        """记录每组实际使用同一问题与配置。"""
        calls.append((text, kb_id, config))
        return [_chunk(0, 5, "unrelated")] * 5 + [_chunk(10, 30, "300 °C at 1 bar")]

    def prompt(text, chunks, *, max_docs):
        """模拟真实入口的前五块选择，而不模拟质量判定。"""
        return text + "\n" + "\n".join(item["content"] for item in chunks[:max_docs])

    async def answer(text):
        """仅记录真实传入回答函数的文字。"""
        prompts.append(text)
        return "Insufficient evidence."

    report = await compare_variants(
        cases,
        {"separator": "kb_a", "paper": "kb_b"},
        query=query,
        answer=answer,
        build_prompt=prompt,
        retrieval_config={"final_top_k": 10, "include_related_evidence": True},
    )
    assert cases == original
    expected = {"final_top_k": 10, "include_related_evidence": False}
    assert calls == [(cases[0]["query"], "kb_a", expected), (cases[0]["query"], "kb_b", expected)]
    assert report["retrieval_config"] == expected
    row = report["variants"]["paper"]["items"][0]
    assert row["retrieval_metrics"]["evidence_coverage"] == 1
    assert row["answer_context_metrics"]["evidence_coverage"] == 0
    assert "300" not in prompts[0]
    assert row["answer_prompt"] == prompts[1]
    assert row["human_review"]["answer_supported"] is None
    assert report["answer_quality"] == "pending_human_review"


@pytest.mark.asyncio
async def test_missing_source_is_rejected_and_omitted_model_is_not_run() -> None:
    """旧索引无来源必须先重建；未配置回答模型不产生质量分。"""

    async def missing(*args, **kwargs):
        """返回缺失来源的旧索引片段。"""
        return [{"content": "300°C"}]

    with pytest.raises(ValueError, match="缺少可靠来源"):
        await compare_variants(
            [_case()],
            {"a": "one", "b": "two"},
            query=missing,
            build_prompt=lambda *args, **kwargs: "prompt",
            retrieval_config={},
        )

    async def present(*args, **kwargs):
        """返回能检查来源的片段。"""
        return [_chunk(10, 30, "300°C at 1 bar")]

    report = await compare_variants(
        [_case()],
        {"a": "one", "b": "two"},
        query=present,
        build_prompt=lambda *args, **kwargs: "prompt",
        retrieval_config={},
    )
    assert report["answer_quality"] == "not_run"
    assert report["variants"]["a"]["items"][0]["answer_status"] == "not_run"
