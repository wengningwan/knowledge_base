"""公开正文入口证明旧篇级额度外仍完整复核，失败不自证通过。"""

import pytest

from test.unit.services.pdf_review_fakes import acknowledge, configure_review_case
from yuxi.services.pdf_review_service import review_pdf_document


@pytest.mark.asyncio
async def test_no_change_correction_does_not_trigger_redundant_retry(monkeypatch, tmp_path):
    """Qwen式corrected+old==new应一次收敛，不能虚增修复或消费第二次请求。"""
    def reply(payload):
        """只改变状态标签，不制造任何内容补丁。"""
        answer = acknowledge(payload)
        for row, issue in zip(answer["results"], payload["issues"], strict=True):
            row.update(status="corrected", patches=[{"target_id": target["id"], "old": target["old"], "new": target["old"]} for target in issue["targets"]])
        return answer

    result, path, requests, _ = configure_review_case(monkeypatch, tmp_path, reply=reply)
    await review_pdf_document(result, path, {"document_review_model": "p:model"})
    assert len(requests) == 1
    assert result.blocks[0]["content"] == "The sample was 12 mg."
    assert result.metadata["review_status"] == "completed"
    assert result.metadata["pdf_repair"]["r2_patches"] == []
    assert result.metadata["pdf_repair"]["execution"]["known_tokens"]["total_tokens"] == 14
    assert [row["validation"] for row in result.reviews if row.get("targets")] == ["normalized_no_change"]


@pytest.mark.asyncio
async def test_retry_receives_specific_error_and_final_report_keeps_rejection(monkeypatch, tmp_path):
    """错误原文继续拒绝，结束后仍显示invalid_target_patch而不是missing_issue_id。"""
    def reply(payload):
        """人工注入错old，模拟真实日志拒绝类型。"""
        issue = payload["issues"][0]
        return {"batch_id": payload["batch_id"], "results": [{"issue_id": issue["issue_id"], "status": "corrected",
            "patches": [{"target_id": target["id"], "old": "wrong source", "new": "wrong replacement"} for target in issue["targets"]],
            "evidence_ids": [entry["id"] for entry in issue["evidence"]], "reason": "人工错误原文负例"}]}

    result, path, requests, redis = configure_review_case(monkeypatch, tmp_path, reply=reply)
    await review_pdf_document(result, path, {"document_review_model": "p:model"})
    assert len(requests) == 2
    assert requests[1]["retry_errors"] == [{"issue_id": requests[0]["issues"][0]["issue_id"], "validation": "invalid_target_patch"}]
    assert result.blocks[0]["content"] == "The sample was 12 mg."
    assert result.metadata["review_status"] == "incomplete"
    assert result.metadata["reviewed_regions"] == 0
    assert [row["validation"] for row in result.reviews if row.get("targets")] == ["invalid_target_patch"]
    assert redis.cache_writes == []


@pytest.mark.asyncio
async def test_successful_retry_clears_prior_failure_without_erasing_evidence(monkeypatch, tmp_path):
    """第二次明确回执通过后不能因先前坏JSON留下假未完成。"""
    attempts = 0

    def reply(payload):
        """第一次JSON非法，第二次使用有证据的显式无修改回执。"""
        nonlocal attempts
        attempts += 1
        return "invalid JSON" if attempts == 1 else acknowledge(payload)

    result, path, requests, _ = configure_review_case(monkeypatch, tmp_path, reply=reply)
    await review_pdf_document(result, path, {"document_review_model": "p:model"})
    assert requests[1]["retry_errors"][0]["validation"] == "invalid_batch_payload"
    assert result.metadata["review_status"] == "completed"
    assert result.metadata["reviewed_regions"] == 1
    assert result.metadata["pdf_repair"]["execution"]["known_tokens"]["total_tokens"] == 28


@pytest.mark.asyncio
async def test_spelling_difference_with_bare_subscript_still_reaches_model(monkeypatch, tmp_path):
    """添加数学定界符不能使同块错字问题变成过期而漏检。"""
    result, path, requests, _ = configure_review_case(
        monkeypatch, tmp_path, before="The electrosopun membrane adsorbs CO_2.",
        source="The electrospun membrane adsorbs CO₂.",
    )
    await review_pdf_document(result, path, {"document_review_model": "p:model"})
    assert requests
    mismatch = [row for row in result.reviews if row["kind"] in {"critical_mismatch", "text_mismatch"}]
    assert mismatch and all(row.get("validation") != "stale_snapshot" for row in mismatch)
    sent = {issue["issue_id"] for payload in requests for issue in payload["issues"]}
    assert all(row["id"] in sent and row["status"] == "checked" for row in mismatch)
    assert "electrosopun" in result.blocks[0]["content"]
    assert "$CO_2$" in result.blocks[0]["content"]
    assert result.metadata["review_status"] == "completed"


@pytest.mark.asyncio
async def test_public_review_completes_more_than_six_requests_and_three_visual_regions(monkeypatch, tmp_path):
    """八个独立原图问题都被实际请求和显式确认，原文逐字保留。"""
    result, path, requests, _ = configure_review_case(monkeypatch, tmp_path, count=8)
    await review_pdf_document(result, path, {"document_review_model": "p:model"})
    assert [block["content"] for block in result.blocks] == ["The sample was 12 mg."] * 8
    assert len(requests) == 8
    assert len({issue["issue_id"] for payload in requests for issue in payload["issues"]}) == 8
    assert all(row["status"] == "checked" for row in result.reviews if row.get("targets"))
    assert result.metadata["reviewed_regions"] == 8
    assert result.metadata["review_status"] == "completed"
    execution = result.metadata["pdf_repair"]["execution"]
    assert execution["calls"] == execution["vision_calls"] == 8
    assert execution["known_tokens"]["total_tokens"] == 112
    assert len(result.assets) == 8


@pytest.mark.asyncio
async def test_missing_receipt_remains_technical_failed_after_format_retry(monkeypatch, tmp_path):
    """空 results 不是已检查；只重试缺失项，不改正文。"""
    def reply(payload):
        """供应商省略全部已登记问题。"""
        return {"batch_id": payload["batch_id"], "results": []}

    result, path, requests, redis = configure_review_case(monkeypatch, tmp_path, reply=reply)
    await review_pdf_document(result, path, {"document_review_model": "p:model"})
    assert len(requests) == 2
    assert result.blocks[0]["content"] == "The sample was 12 mg."
    assert result.metadata["reviewed_regions"] == 0
    assert result.metadata["review_status"] == "incomplete"
    assert {row["status"] for row in result.reviews if row.get("targets")} == {"technical_failed"}
    assert redis.cache_writes == []


@pytest.mark.asyncio
async def test_invalid_json_is_charged_and_does_not_erase_other_valid_regions(monkeypatch, tmp_path):
    """第二页非法 JSON 仍记真实 token；第一页有效回执保持已检查。"""
    def reply(payload):
        """按人工页号注入坏响应，与批次遍历顺序无关。"""
        if payload["issues"][0]["targets"][0]["source_region"]["page"] == 2:
            return "PRIVATE invalid JSON"
        return acknowledge(payload)

    result, path, requests, _ = configure_review_case(monkeypatch, tmp_path, count=2, reply=reply)
    await review_pdf_document(result, path, {"document_review_model": "p:model"})
    statuses = {row["block_id"]: row["status"] for row in result.reviews if row.get("targets")}
    assert statuses == {"b1": "checked", "b2": "technical_failed"}
    assert result.metadata["review_status"] == "incomplete"
    assert len(requests) == 3
    assert result.metadata["pdf_repair"]["execution"]["known_tokens"]["total_tokens"] == 42
    assert [block["content"] for block in result.blocks] == ["The sample was 12 mg."] * 2


@pytest.mark.asyncio
async def test_registered_numeric_patch_uses_explicit_manual_replacement(monkeypatch, tmp_path):
    """人工 oracle 指定 12→13；响应只填 target 身份，不从候选生成科学真值。"""
    def reply(payload):
        """短例差异的唯一允许替换显式写为 2→3。"""
        issue = payload["issues"][0]
        assert [(target["old"], target.get("native_new")) for target in issue["targets"]] == [("2", "3")]
        return {"batch_id": payload["batch_id"], "results": [{
            "issue_id": issue["issue_id"], "status": "corrected", "patches": [{"target_id": issue["targets"][0]["id"], "old": "2", "new": "3"}],
            "evidence_ids": [entry["id"] for entry in issue["evidence"] if entry["source"] == "visual_region"], "reason": "人工图像短例明确为 13 mg",
        }]}

    result, path, _, _ = configure_review_case(monkeypatch, tmp_path, reply=reply)
    await review_pdf_document(result, path, {"document_review_model": "p:model"})
    assert result.markdown == "The sample was 13 mg."
    assert result.blocks[0]["content"] == "The sample was 13 mg."
    assert result.metadata["review_status"] == "completed"
    patches = result.metadata["pdf_repair"]["r2_patches"]
    assert len(patches) == 1 and patches[0]["before"] == "The sample was 12 mg."


@pytest.mark.asyncio
async def test_partial_bad_receipt_preserves_valid_issue_and_retries_only_bad_issue(monkeypatch, tmp_path):
    """同块数字差异及空数学组产生两个问题，一坏回执不抹掉另一有效回执。"""
    def reply(payload):
        """科学差异明确确认，数学问题持续返回非法状态。"""
        answer = acknowledge(payload)
        by_id = {issue["issue_id"]: issue for issue in payload["issues"]}
        for row in answer["results"]:
            if by_id[row["issue_id"]]["kind"] == "math_syntax":
                row["status"] = "bad-status"
        return answer

    result, path, requests, _ = configure_review_case(monkeypatch, tmp_path, reply=reply, before="The sample was 12 mg; x_{}.", source="The sample was 13 mg; x_{}.")
    await review_pdf_document(result, path, {"document_review_model": "p:model"})
    assert len(requests) == 2
    assert {issue["kind"] for issue in requests[0]["issues"]} == {"critical_mismatch", "math_syntax"}
    assert [issue["kind"] for issue in requests[1]["issues"]] == ["math_syntax"]
    statuses = {row["kind"]: row["status"] for row in result.reviews if row.get("targets")}
    assert statuses == {"critical_mismatch": "checked", "math_syntax": "technical_failed"}
    assert result.blocks[0]["content"] == "The sample was 12 mg; x_{}."
    assert result.metadata["review_status"] == "incomplete"
