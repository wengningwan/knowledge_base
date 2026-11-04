"""压缩后的论文工具正文仍显示补充证据及其省略状态。"""

from __future__ import annotations

import ast
import json
from pathlib import Path


def test_paper_compaction_preserves_figure_formula_roles_and_source_version():
    """正文、图注、公式与缺失提示需要一同进入压缩后的实际文字。"""
    status = {"section": "complete", "figures": [{"figure_id": "S4", "status": "missing"}], "budget": "complete"}
    payload = {
        "kb_id": "kb",
        "results": [_result("anchor", "See Figure 1 and equation (2).", evidence_status=status)],
        "context_results": [
            _result(
                "figure",
                "Figure 1. Uptake at 298 K.",
                evidence_role="figure_reference",
                anchor_chunk_id="anchor",
                source_metadata={"source_sha256": "a" * 64, "_publication_id": "internal-secret"},
            ),
            _result("equation", "$$x=P/P_0$$", evidence_role="section_context", anchor_chunk_id="anchor"),
        ],
    }
    compact = _preview(payload, 2400)
    assert compact["results"][0]["id"] == "anchor"
    assert compact["results"][0]["metadata"]["evidence_status"] == status
    assert [item["id"] for item in compact["context_results"]] == ["figure", "equation"]
    figure, equation = compact["context_results"]
    assert figure["metadata"]["evidence_role"] == "figure_reference"
    assert equation["metadata"]["anchor_chunk_id"] == "anchor"
    assert figure["metadata"]["source_sha256"] == "a" * 64
    assert "298 K" in figure["content_preview"]
    assert "x=P/P_0" in equation["content_preview"]
    assert "internal-secret" not in json.dumps(compact)


def test_paper_compaction_reports_omitted_context_and_shortened_bodies():
    """小预算不能无声丢掉补取内容并继续表示证据齐全。"""
    payload = {
        "results": [_result("a", "Main evidence " * 200, evidence_status={"budget": "truncated"})],
        "context_results": [
            _result(f"ctx-{i}", "Formula definition " * 200, evidence_role="section_context", anchor_chunk_id="a")
            for i in range(12)
        ],
    }
    compact = _preview(payload, 1100)
    assert compact["context_result_count"] == 12
    assert compact["omitted_context_results"] > 0
    assert any(item["content_truncated"] for item in [*compact["results"], *compact["context_results"]])
    tiny = _preview(payload, 80)
    assert tiny["omitted_context_results"] == 12
    assert tiny["omitted_results"] == 1


def test_paper_status_survives_when_related_picture_is_missing():
    """没有补充块也必须保留缺图和无法核实来源的状态。"""
    status = {"scope": "unavailable", "reason": "anchor_not_current"}
    compact = _preview({"results": [_result("a", "Body", evidence_status=status)], "context_results": []}, 1000)
    assert compact["results"][0]["metadata"]["evidence_status"] == status
    assert compact["context_results"] == []


def test_paper_status_keeps_middle_missing_reference_when_budget_allows():
    """状态不能先按固定字符数裁掉中间的缺图提示。"""
    status = {
        "section": "complete",
        "budget": "complete",
        "figures": [{"figure_id": str(i), "status": "missing" if i == 5 else "linked"} for i in range(12)],
    }
    compact = _preview({"results": [_result("a", "Body", evidence_status=status)], "context_results": []}, 4000)
    assert compact["results"][0]["metadata"]["evidence_status"] == status


def _preview(payload, budget):
    """直接执行生产纯函数，避免加载依赖 POSIX 文件系统的 Agent 启动模块。"""
    path = Path(__file__).resolve().parents[3] / "package/yuxi/agents/middlewares/summary.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {
        "_structured_search_preview",
        "_structured_knowledge_context_preview",
        "_parse_structured_tool_result",
        "_search_header",
        "_search_result_record",
        "_encode_search_preview",
        "_bounded_search_scalar",
        "_clip_search_content",
        "_STRUCTURED_SEARCH_TOOL_NAMES",
        "_SEARCH_CONTENT_KEYS",
    }
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets):
            nodes.append(node)
    namespace = {"json": json}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(path), "exec"), namespace)
    encoded = namespace["_structured_search_preview"]("query_kb", json.dumps(payload), budget)
    assert encoded is not None
    assert len(encoded) <= budget
    return json.loads(encoded)


def _result(chunk_id, body, **metadata):
    """创建明确身份的工具结果，不通过实现生成预期答案。"""
    return {"id": chunk_id, "kb_id": "kb", "file_id": "paper", "content": body, "metadata": metadata}
