"""局部复核的真实裁图、拒绝建议与最终状态对账。"""

import pytest

from test.unit.services.pdf_review_fakes import configure_review_case
from yuxi.services.pdf_review_service import review_pdf_document


@pytest.mark.asyncio
async def test_missing_model_does_not_hide_manual_review(monkeypatch, tmp_path):
    """没有选择模型时仍有疑点的文档必须阻止自动入库。"""
    result, path, requests, _ = configure_review_case(monkeypatch, tmp_path)
    await review_pdf_document(result, path, {})
    assert result.metadata["review_status"] == "needs_review"
    assert any(row["status"] in {"not_checked", "unresolved"} for row in result.reviews)
    assert requests == []


@pytest.mark.asyncio
async def test_actual_crop_and_rejected_proposal_remain_in_sidecar(monkeypatch, tmp_path):
    """拒绝原文降级时保留完整建议与实际裁图，而不是只保存reason。"""
    def reply(payload):
        """人工注入下标降级，禁止视为等价表达。"""
        issue = payload["issues"][0]
        return {"batch_id": payload["batch_id"], "results": [{
            "issue_id": issue["issue_id"], "status": "corrected",
            "patches": [{"target_id": target["id"], "old": target["old"], "new": "The gas is CO2."}
                        for target in issue["targets"]],
            "evidence_ids": [entry["id"] for entry in issue["evidence"]],
            "reason": "声称语义相同的人工错误回执",
        }]}

    result, path, requests, _ = configure_review_case(
        monkeypatch, tmp_path, before=r"The gas is $CO_{2}$.", source="The gas is CO2.", reply=reply, native_geometry=False,
    )
    rendered = []

    def crop(path, page, bbox):
        """记录真实渲染参数，测试不启动PDFium。"""
        rendered.append(list(bbox))
        return b"explicit crop boundary fixture"

    monkeypatch.setattr("yuxi.knowledge.parser.pdf_native.render_pdf_region", crop)
    await review_pdf_document(result, path, {"document_review_model": "p:model"})
    assert result.markdown == r"The gas is $CO_{2}$."
    row = next(row for row in result.reviews if row.get("targets"))
    assert row["validation"] == "script_scope_loss_requires_native_geometry"
    assert row["status"] == "technical_failed" and row["applied"] is False
    assert row["model_proposal"]["patches"][0]["new"] == "The gas is CO2."
    assert len(row["review_attempts"]) == 2 and len(requests) == 2
    visual = next(entry for entry in row["evidence"] if entry["source"] == "visual_region")
    assert visual["bbox"] == rendered[0]
    assert result.assets[visual["id"]]["bbox"] == rendered[0]
    assert visual["bbox"] != result.blocks[0]["bbox"]
    assert result.sidecar()["reviews"] == result.reviews
    assert result.metadata["pdf_repair"]["review_contract_version"] == 4
    assert result.metadata["review_status"] == "incomplete"


@pytest.mark.asyncio
async def test_partial_correction_applies_proven_edit_and_retains_other_conflict(monkeypatch, tmp_path):
    """同问题的A已纠错不等于B已确认，部分成果保留且整篇待核。"""
    def reply(payload):
        """人工修正12为13，另一个22/23差异只返回原值。"""
        issue = payload["issues"][0]
        assert [target["old"] for target in issue["targets"]] == ["12", "22"]
        return {"batch_id": payload["batch_id"], "results": [{
            "issue_id": issue["issue_id"], "status": "corrected",
            "patches": [{"target_id": target["id"], "old": target["old"], "new": "13" if target["old"] == "12" else "22"}
                        for target in issue["targets"]],
            "evidence_ids": [entry["id"] for entry in issue["evidence"]],
            "reason": "人工短例仅第一个目标明确纠错",
        }]}

    result, path, requests, redis = configure_review_case(
        monkeypatch, tmp_path, before="The sample was 12 mg and 22 mol.",
        source="The sample was 13 mg and 23 mol.", reply=reply,
    )
    await review_pdf_document(result, path, {"document_review_model": "p:model"})

    assert result.markdown == "The sample was 13 mg and 22 mol."
    row = next(row for row in result.reviews if row.get("targets"))
    assert row["status"] == "machine_corrected" and row["applied"] is True
    assert row["patch_accepted"] is True
    assert row["remaining_conflicts"] == [row["targets"][1]["id"]]
    assert row["review_attempts"][0]["applied"] is False
    assert result.metadata["review_status"] == "needs_review"
    assert result.metadata["reviewed_regions"] == 0
    assert result.metadata["pdf_repair"]["counts"] == {"unresolved": 1}
    assert len(requests) == 1 and redis.cache_writes == []
