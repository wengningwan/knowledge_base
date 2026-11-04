"""补充证据必须进入实际 Search/评估输入且不改变主排序。"""

import copy
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from yuxi.knowledge import related_evidence as module
from yuxi.knowledge.base import KnowledgeBase
from yuxi.knowledge.eval.evaluator import build_answer_prompt
from yuxi.knowledge.implementations.milvus import MilvusKB
from yuxi.knowledge.read_models import KnowledgeBaseConfig


@pytest.mark.asyncio
async def test_supplement_budget_is_aggregate_atomic_and_all_primary_hits_are_deduplicated(monkeypatch):
    """补充不得重复后续主命中、跨主块预算或切开公式。"""
    main = [_hit("c1"), _hit("c2")]
    shared = _hit("c3", "$$whole equation$$")
    shared.pop("score")
    shared["evidence_role"] = "section_context"
    huge = _hit("c4", "x" * (module.RELATED_CHAR_LIMIT + 1))
    huge.pop("score")
    huge["evidence_role"] = "figure_reference"
    second_primary = {**main[1], "evidence_role": "section_context"}
    answers = [([second_primary, shared, huge], {"section": "complete"}), ([shared], {"section": "complete"})]
    monkeypatch.setattr(
        module,
        "KnowledgeChunkRepository",
        lambda: SimpleNamespace(get_paper_related_evidence=AsyncMock(side_effect=answers)),
    )
    before = copy.deepcopy(main)
    result = await module.attach_related_evidence("kb", main)
    assert [(item["metadata"], item["content"], item["score"]) for item in result] == [
        (item["metadata"], item["content"], item["score"]) for item in before
    ]
    assert result[0]["related_evidence"] == [shared]
    assert result[1]["related_evidence"] == []
    assert result[0]["related_primary_evidence"] == [{"chunk_id": "c2", "evidence_role": "section_context"}]
    assert result[0]["related_evidence_status"]["budget"] == "truncated"
    assert result[0]["related_evidence_status"]["omitted_chunks_at_least"] == 1
    assert "score" not in result[0]["related_evidence"][0]


def test_search_and_actual_answer_prompt_share_context_and_explicit_status():
    """只取前五个主命中的补充，缺失状态与角色进入真实回答 prompt。"""
    main = [_hit(f"c{i}", f"MAIN-{i}") for i in range(6)]
    for index in (0, 5):
        supplement = _hit(f"s{index}", f"SUPPLEMENT-{index}")
        supplement.pop("score")
        supplement["evidence_role"] = "figure_reference"
        main[index]["related_evidence"] = [supplement]
        main[index]["related_evidence_status"] = {
            "scope": "finest_section_and_explicit_figures",
            "figures": [{"figure_id": "S4", "status": "missing"}],
        }
    output = KnowledgeBase.build_search_output("kb", main)
    assert len(output["results"]) == 6 and len(output["context_results"]) == 2
    context = output["context_results"][0]
    assert context["metadata"]["evidence_role"] == "figure_reference"
    assert context["metadata"]["anchor_chunk_id"] == "c0"
    assert "score" not in context["metadata"]
    assert "_publication_id" not in str(output)
    prompt = build_answer_prompt("question", main)
    assert "MAIN-4" in prompt and "MAIN-5" not in prompt
    assert "SUPPLEMENT-0" in prompt and "SUPPLEMENT-5" not in prompt
    assert all(value in prompt for value in ("figure_reference", "S4", "missing", "source_sha256", "anchor_chunk_id"))


def test_search_nonpaper_exact_response_and_scope_only_marker():
    """旧策略输出不新增字段；空补充仍可表明无小节范围。"""
    assert KnowledgeBase.build_search_output("kb", [{"content": "legacy"}]) == {
        "kb_id": "kb",
        "results": [{"id": ":1", "kb_id": "kb", "file_id": "", "content": "legacy", "metadata": {}}],
    }
    main = _hit("c1")
    main["related_evidence_status"] = {"section": "not_applicable"}
    assert KnowledgeBase.build_search_output("kb", [main])["context_results"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("rerank", [False, True])
async def test_actual_query_attaches_only_after_final_rank_and_topk(monkeypatch, rerank):
    """有无重排均走相同出口，补取不得进入 Top K 或打分。"""
    hits = [
        SimpleNamespace(
            distance=score, entity={"content": content, "file_id": "file", "chunk_id": chunk_id, "chunk_index": index}
        )
        for index, (chunk_id, content, score) in enumerate((("c1", "first", 0.9), ("c2", "second", 0.8)))
    ]
    collection = SimpleNamespace(search=lambda **kwargs: [copy.deepcopy(hits)])
    kb = MilvusKB.__new__(MilvusKB)
    kb._load_query_collection = AsyncMock(return_value=collection)
    kb._build_file_name_expr = AsyncMock(return_value=None)

    async def hydrate(_kb_id, chunks):
        """把当前 PG 来源附到实际向量输出。"""
        for chunk in chunks:
            chunk["metadata"]["source_metadata"] = {"chunker_version": "paper_rules_v3", "source_sha256": "source"}
        return chunks

    kb._hydrate_chunk_sources = hydrate
    supplement = _hit("s1", "supplement")
    supplement.pop("score")
    supplement["evidence_role"] = "section_context"
    repository = SimpleNamespace(
        get_paper_related_evidence=AsyncMock(return_value=([supplement], {"section": "complete"}))
    )
    monkeypatch.setattr(module, "KnowledgeChunkRepository", lambda: repository)
    reranker = SimpleNamespace(acompute_score=AsyncMock(return_value=[0.1, 0.9]), aclose=AsyncMock())
    monkeypatch.setitem(sys.modules, "yuxi.models.rerank", SimpleNamespace(get_reranker=lambda _: reranker))
    config = KnowledgeBaseConfig(kb_id="kb", kb_type="milvus", embedding_model_spec="provider:embedding")
    kwargs = {"search_mode": "keyword", "final_top_k": 1, "use_reranker": rerank, "reranker_model": "test"}
    raw = await kb.aquery("query", "kb", config=config, include_related_evidence=False, **kwargs)
    enriched = await kb.aquery("query", "kb", config=config, **kwargs)
    assert len(raw) == len(enriched) == 1
    assert raw[0]["metadata"]["chunk_id"] == ("c2" if rerank else "c1")
    assert raw == [{key: value for key, value in enriched[0].items() if not key.startswith("related_")}]
    assert enriched[0]["related_evidence"][0]["content"] == "supplement"
    assert repository.get_paper_related_evidence.call_args.kwargs["anchor"] is enriched[0]


@pytest.mark.asyncio
async def test_evaluator_lifts_sixth_primary_when_related_to_first_five(monkeypatch):
    """主排序去重不能在评估前五块切片后吞掉第六块关联公式。"""
    primary = [_hit(f"c{index}", f"MAIN-{index}") for index in range(6)]
    related = {"content": "MAIN-5", "metadata": primary[5]["metadata"], "evidence_role": "section_context"}
    repository = SimpleNamespace(
        get_paper_related_evidence=AsyncMock(
            side_effect=[([related], {"section": "complete"}), *[([], {"section": "complete"}) for _ in range(5)]]
        )
    )
    monkeypatch.setattr(module, "KnowledgeChunkRepository", lambda: repository)
    enriched = await module.attach_related_evidence("kb", primary)
    output = KnowledgeBase.build_search_output("kb", enriched)
    assert len(output["results"]) == 6 and output["context_results"] == []
    assert enriched[0]["related_evidence"] == []
    prompt = build_answer_prompt("query", enriched)
    assert "MAIN-5" in prompt and '"role": "section_context"' in prompt


def test_promoted_primary_and_regular_context_share_original_chunk_order():
    """前五块外的主命中提升为补充后仍在原文的正确顺序。"""
    main = [_hit(f"c{index}") for index in (0, 1, 2, 4, 5, 6)]
    main[0]["related_primary_evidence"] = [{"chunk_id": "c6", "evidence_role": "section_context"}]
    main[0]["related_evidence"] = [{**_hit(f"c{index}"), "evidence_role": "section_context"} for index in (3, 9)]
    _primary, context = module.pack_retrieval_evidence("kb", main, max_docs=5)
    assert [chunk["id"] for chunk in context] == ["c3", "c6", "c9"]


def test_promoted_primary_obeys_answer_budget_without_mutating_hit_status():
    """第六主块提升为补充时仍受回答预算约束，缺失必须可见。"""
    primary = [_hit(f"c{index}", f"MAIN-{index}") for index in range(6)]
    primary[5]["content"] = "OVERSIZED-FORMULA" + "x" * module.RELATED_CHAR_LIMIT
    primary[0]["related_primary_evidence"] = [{"chunk_id": "c5", "evidence_role": "section_context"}]
    primary[0]["related_evidence_status"] = {"section": "complete", "budget": "complete"}
    before = copy.deepcopy(primary)
    main, context = module.pack_retrieval_evidence("kb", primary, max_docs=5)
    assert context == []
    assert main[0]["metadata"]["evidence_status"]["budget"] == "truncated"
    assert main[0]["metadata"]["evidence_status"]["omitted_chunks_at_least"] == 1
    assert primary == before
    prompt = build_answer_prompt("query", primary)
    assert "OVERSIZED-FORMULA" not in prompt and "truncated" in prompt


def test_promotions_and_ordinary_supplements_share_consumer_chunk_budget():
    """提升主块与普通补充共同受二十四块预算限制。"""
    primary = [_hit(f"c{index}") for index in range(10)]
    primary[0]["related_primary_evidence"] = [
        {"chunk_id": f"c{index}", "evidence_role": "section_context"} for index in range(5, 10)
    ]
    primary[0]["related_evidence"] = [
        {**_hit(f"s{index}"), "evidence_role": "section_context"} for index in range(module.RELATED_CHUNK_LIMIT)
    ]
    main, context = module.pack_retrieval_evidence("kb", primary, max_docs=5)
    assert len(context) == module.RELATED_CHUNK_LIMIT
    assert main[0]["metadata"]["evidence_status"] == {"budget": "truncated", "omitted_chunks_at_least": 5}


def _hit(chunk_id, content="body", *, file_id="file", version="source"):
    """构造有独立来源身份的主块。"""
    return {
        "content": content,
        "score": 0.8,
        "metadata": {
            "chunk_id": chunk_id,
            "file_id": file_id,
            "chunk_index": int(chunk_id[-1]) if chunk_id[-1].isdigit() else 0,
            "source_metadata": {
                "chunker_version": "paper_rules_v3",
                "source_sha256": version,
                "_publication_id": "secret",
            },
        },
    }
