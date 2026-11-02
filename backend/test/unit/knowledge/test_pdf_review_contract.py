"""人工最小摘录与恶意回执负例；仅静态检查，运行验证由验证机完成。"""

from copy import deepcopy

import pytest

from yuxi.knowledge.parser.pdf_review_contract import (
    make_review_request,
    prepare_review_issues,
    validate_issue_results,
)


@pytest.mark.parametrize("units", [("mM", "nM"), ("cm", "dm"), ("kPa", "hPa")])
def test_prefixed_units_require_independent_visual_evidence(units):
    """浓度/长度/压力尺度变化不能借原生候选绕过视觉门槛。"""
    before = f"The value was 1 {units[0]}."
    after = f"The value was 1 {units[1]}."
    block = _block(before)
    issue = prepare_review_issues([block], [_issue(block, after)])[0]
    assert issue["review_route"] == "vision"
    edits, outcomes = validate_issue_results([block], _payload(_result(issue)), [issue], batch_id="batch_1", visual=False)
    assert edits == [] and outcomes[0]["status"] == "technical_failed"


def test_spelling_patch_preserves_registered_hard_line_break():
    """原生排版空白不一致时仅修错字，Markdown强制换行仍在。"""
    before = "The electrosopun membrane.  \nThe next sentence."
    block = _block(before)
    issue = prepare_review_issues([block], [_issue(block, "The electrospun membrane.\nThe next sentence.")])[0]
    edits, outcomes = validate_issue_results([block], _payload(_result(issue)), [issue], batch_id="batch_1", visual=False)
    assert edits and edits[0]["after"] == "The electrospun membrane.  \nThe next sentence."
    assert outcomes[0]["status"] == "machine_corrected"


def test_long_block_targets_partition_without_losing_original_characters():
    """弱证据长正文拆成稳定片段，原文跨度连续且不重复。"""
    text = "A sufficiently long original paragraph. " * 250
    block = _block(text)
    issue = _issue(block, "")
    issue["evidence"] = []
    children = prepare_review_issues([block], [issue])
    assert len(children) > 1
    targets = [target for child in children for target in child["targets"]]
    assert "".join(target["old"] for target in targets) == text
    assert all(len(target["old"]) <= 2000 for target in targets)
    assert len({target["id"] for target in targets}) == len(targets)
    assert all(child["parent_issue_id"] == issue["id"] for child in children)


def test_long_raw_latex_formula_is_never_split_inside_arguments():
    """公式块没有美元定界时仍是数学原子，不能从frac参数中间切开。"""
    content = r"\frac{" + "a+b" * 1000 + "}{c}"
    block = _block(content, type="formula")
    issues = prepare_review_issues([block], [_issue(block, "")])
    assert len(issues) == 1 and issues[0]["targets"] == []
    assert issues[0]["status"] == "unresolved"
    assert issues[0]["validation"] == "indivisible_protected_target"


def test_fragment_wire_evidence_does_not_repeat_all_other_target_ids():
    """程序内部完整绑定仍在，请求只发送当前片段自己的target标识。"""
    block = _block("Original text. " * 1000)
    issue = _issue(block, "")
    issue["evidence"] = []
    children = prepare_review_issues([block], [issue])
    own = children[0]
    own["evidence"].append({"id": "v", "source": "visual_region", "quality": "high", "page": 1,
                            "bbox": block["bbox"], "block_id": block["id"],
                            "block_hash": own["targets"][0]["block_hash"],
                            "target_ids": [target["id"] for child in children for target in child["targets"]]})
    request = make_review_request("part", [own], [block])
    assert request["issues"][0]["evidence"][0]["target_ids"] == [own["targets"][0]["id"]]
    assert len(own["evidence"][0]["target_ids"]) == len(children)


def _block(content, **changes):
    """创建有区域身份的人工块。"""
    return {"id": "b1", "page": 1, "bbox": [.1,.2,.9,.3], "type": "text", "content": content, **changes}


def _issue(block, native, *, id="issue1", chars=True):
    """构造明确标记的人工原生证据，字符框只用于协议负例。"""
    box = block["bbox"]
    advance = (box[2] - box[0]) / max(1, len(native))
    entry = {"id": "n_" + id, "page": block["page"], "bbox": list(box), "content": native, "quality": "high", "source": "native"}
    if chars:
        entry["chars"] = [{"text": char, "bbox": [box[0] + index * advance, box[1], box[0] + (index + 1) * advance, box[3]]} for index, char in enumerate(native)]
    return {"id": id, "block_id": block["id"], "before": block["content"], "status": "unresolved", "kind": "text_mismatch", "classification": "content", "review_route": "text", "evidence": [entry], "evidence_ids": [entry["id"]], "candidates": {"ocr": block["content"], "native": native}}


def _result(issue, *, status="corrected", news=None, visual=False):
    """生成每目标显式返回的人工回执。"""
    return {
        "issue_id": issue["id"], "status": status, "reason": "人工短例的区域原文证据",
        "evidence_ids": [entry["id"] for entry in issue["evidence"] if entry["source"] == ("visual_region" if visual else "native")],
        "patches": [{"target_id": target["id"], "old": target["old"], "new": news[index] if news is not None else target.get("native_new", target["old"])} for index, target in enumerate(issue["targets"])] if status == "corrected" else [],
    }


def _payload(*results):
    """固定 batch_id 的人工协议对象。"""
    return {"batch_id": "batch_1", "results": list(results)}


def _vision(issue, *, bbox=None):
    """模拟程序渲染登记，绝不把模型自报位置当作定位。"""
    target = issue["targets"][0]
    region = target["source_region"]
    issue["evidence"].append({"id": "visual_" + issue["id"], "source": "visual_region", "quality": "high", "page": region["page"], "bbox": list(region["bbox"] if bbox is None else bbox), "target_ids": [target["id"] for target in issue["targets"]], "block_id": issue["block_id"], "block_hash": target["block_hash"]})


def test_exact_minimal_name_patch_rebuilds_original_block_without_mutating():
    """Stoeckeli的单字删除按目标offset重建，其余原文逐字保留。"""
    block = _block("Dubinin, M.; Stoeckeli, H.")
    issue = prepare_review_issues([block], [_issue(block, "Dubinin, M.; Stoeckli, H.")])[0]
    snapshot = deepcopy(block)
    assert [(target["old"], target["native_new"]) for target in issue["targets"]] == [("e", "")]
    edits, outcomes = validate_issue_results([block], _payload(_result(issue)), [issue], batch_id="batch_1", visual=False)
    assert len(edits) == 1 and edits[0]["after"] == "Dubinin, M.; Stoeckli, H."
    assert block == snapshot
    assert outcomes[0]["status"] == "machine_corrected"


@pytest.mark.parametrize(("before", "after"), [
    ("This: Energy Fuels 2025, 39", "Cite This: Energy Fuels 2025, 39"),
    ("The electrosopun membrane was prepared.", "The electrospun membrane was prepared."),
    ("C2H6 suggesting a promising potential", "C2H6, suggesting a promising potential"),
])
def test_manual_plain_local_oracles_accept_only_registered_native_spans(before, after):
    """Cite、electrospun与逗号是原文短例，expected人工指定。"""
    block = _block(before)
    issue = prepare_review_issues([block], [_issue(block, after)])[0]
    edits, outcomes = validate_issue_results([block], _payload(_result(issue)), [issue], batch_id="batch_1", visual=False)
    assert edits[0]["after"] == after and outcomes[0]["status"] == "machine_corrected"
    wrong = _result(issue)
    wrong["patches"][0]["new"] = "invented"
    edits, outcomes = validate_issue_results([block], _payload(wrong), [issue], batch_id="batch_1", visual=False)
    assert edits == [] and outcomes[0]["validation"] == "replacement_not_registered_native_span"


@pytest.mark.parametrize(("before", "after"), [
    ("The sample is SA.", "The sample is 5A."),
    ("The mode is Eₘg.", "The mode is E₂g."),
    ("See reference 1.", "See reference 11."),
    ("At 1595 cm⁻¹ the bond is C≡N.", "At 1595 cm⁻¹ the bond is C=N."),
    ("The value is -1.0 mg.", "The value is 1.0 mg."),
    ("The heat is 22 K/mol.", "The heat is 22 KJ/mol."),
    ("The amount is 1 µg.", "The amount is 1 mg."),
])
def test_scientific_changes_require_independent_visual_evidence(before, after):
    """数字、正负号、上下标与化学键不被精确native候选绕过。"""
    block = _block(before)
    issue = prepare_review_issues([block], [_issue(block, after)])[0]
    assert issue["review_route"] == "vision"
    edits, outcomes = validate_issue_results([block], _payload(_result(issue)), [issue], batch_id="batch_1", visual=False)
    assert edits == [] and outcomes[0]["status"] == "technical_failed"
    _vision(issue, bbox=block["bbox"])
    edits, outcomes = validate_issue_results([block], _payload(_result(issue, visual=True)), [issue], batch_id="batch_1", visual=True)
    assert edits[0]["after"] == after and outcomes[0]["status"] == "machine_corrected"


def test_character_bound_small_crop_is_valid_without_whole_block_coverage():
    """小crop只需覆盖登记字符区域，不能沿用旧全块85%门槛。"""
    block = _block("At 1595 cm⁻¹ the bond is C≡N; the remote 2243 cm⁻¹ assignment is C≡N.")
    after = "At 1595 cm⁻¹ the bond is C=N; the remote 2243 cm⁻¹ assignment is C≡N."
    issue = prepare_review_issues([block], [_issue(block, after)])[0]
    assert issue["targets"][0]["source_region"]["scope"] == "characters"
    assert issue["targets"][0]["source_region"]["bbox"][2] - issue["targets"][0]["source_region"]["bbox"][0] < .1
    _vision(issue)
    edits, outcomes = validate_issue_results([block], _payload(_result(issue, visual=True)), [issue], batch_id="batch_1", visual=True)
    assert edits[0]["after"] == after
    assert outcomes[0]["status"] == "machine_corrected"
    # 同页右侧2243的另一个三键crop，即使伪报target_id也不覆盖1595目标。
    issue["evidence"][-1]["bbox"] = [.75,.2,.9,.3]
    edits, outcomes = validate_issue_results([block], _payload(_result(issue, visual=True)), [issue], batch_id="batch_1", visual=True)
    assert edits == [] and outcomes[0]["validation"] == "visual_region_does_not_cover_target"


def test_no_character_geometry_requires_whole_block_image_and_context():
    """只有块框时不假造局部字符坐标，也不接受不覆盖整块的截图。"""
    block = _block("The sample is SA.")
    issue = prepare_review_issues([block], [_issue(block, "The sample is 5A.", chars=False)])[0]
    target = issue["targets"][0]
    assert target["source_region"] == {"page": 1, "bbox": block["bbox"], "scope": "block"}
    assert target["context_before"] and target["context_after"]
    _vision(issue, bbox=[.6,.2,.8,.3])
    edits, outcomes = validate_issue_results([block], _payload(_result(issue, visual=True)), [issue], batch_id="batch_1", visual=True)
    assert edits == [] and outcomes[0]["validation"] == "visual_region_does_not_cover_target"
    issue["evidence"][-1]["bbox"] = block["bbox"]
    edits, _ = validate_issue_results([block], _payload(_result(issue, visual=True)), [issue], batch_id="batch_1", visual=True)
    assert edits[0]["after"] == "The sample is 5A."


@pytest.mark.parametrize("failure", ["missing", "duplicate", "invalid_status"])
def test_partial_bad_result_does_not_erase_other_independent_correction(failure):
    """独立问题保留有效结果，缺失或重复ID绝不计作已检查。"""
    blocks = [_block("The electrosopun membrane was prepared."), _block("Dubinin, M.; Stoeckeli, H.", id="b2")]
    issues = prepare_review_issues(blocks, [_issue(blocks[0], "The electrospun membrane was prepared."), _issue(blocks[1], "Dubinin, M.; Stoeckli, H.", id="issue2")])
    first, second = _result(issues[0]), _result(issues[1])
    results = [first] if failure == "missing" else [first, second, deepcopy(second)] if failure == "duplicate" else [first, {**second, "status": ["corrected"]}]
    edits, outcomes = validate_issue_results(blocks, _payload(*results), issues, batch_id="batch_1", visual=False)
    assert len(edits) == 1 and edits[0]["block_id"] == "b1"
    assert [(outcome["issue_id"], outcome["status"]) for outcome in outcomes] == [("issue1", "machine_corrected"), ("issue2", "technical_failed")]


def test_unknown_extra_issue_is_rejected_and_batch_protocol_error_is_visible():
    """未知ID不得修改正文，独立合法结果保留且总账不能宣称完整。"""
    block = _block("Dubinin, M.; Stoeckeli, H.")
    issue = prepare_review_issues([block], [_issue(block, "Dubinin, M.; Stoeckli, H.")])[0]
    result = _result(issue)
    unknown = {**deepcopy(result), "issue_id": "unknown"}
    edits, outcomes = validate_issue_results([block], _payload(result, unknown), [issue], batch_id="batch_1", visual=False)
    assert len(edits) == 1
    assert outcomes[0]["protocol_errors"] == ["unknown_issue_id"]


@pytest.mark.parametrize("failure", ["hash", "offset", "unknown_evidence", "old", "extra_patch_field"])
def test_target_binding_and_snapshot_guards_reject_precise_failure(failure):
    """块hash、offset、原文与证据引用是程序拥有的不可越界定位。"""
    block = _block("Dubinin, M.; Stoeckeli, H.")
    issue = prepare_review_issues([block], [_issue(block, "Dubinin, M.; Stoeckli, H.")])[0]
    result = _result(issue)
    if failure == "hash":
        block["content"] += " changed"
    elif failure == "offset":
        issue["targets"][0]["start"] = 0
    elif failure == "unknown_evidence":
        result["evidence_ids"] = ["other"]
    elif failure == "old":
        result["patches"][0]["old"] = "other"
    else:
        result["patches"][0]["start"] = 0
    edits, outcomes = validate_issue_results([block], _payload(result), [issue], batch_id="batch_1", visual=False)
    assert edits == [] and outcomes[0]["status"] == "technical_failed"


def test_text_route_cannot_delete_not_from_incomplete_native_middle():
    """首尾native包围框不证明缺失中段，文字路线不得删掉否定句。"""
    before = "The first sentence does not describe the final observation."
    block = _block(before)
    raw = _issue(block, before.replace(" not", ""))
    raw["evidence"] = [
        {"id": "top", "source": "native", "quality": "high", "page": 1, "bbox": [.1,.2,.9,.23], "content": "The first sentence does"},
        {"id": "bottom", "source": "native", "quality": "high", "page": 1, "bbox": [.1,.27,.9,.3], "content": "describe the final observation."},
    ]
    issue = prepare_review_issues([block], [raw])[0]
    assert issue["review_route"] == "vision"
    edits, outcomes = validate_issue_results([block], _payload(_result(issue, news=[before.replace(" not", "")])), [issue], batch_id="batch_1", visual=False)
    assert edits == [] and outcomes[0]["status"] == "technical_failed"
    assert block["content"] == before


def test_protected_spans_and_structural_issues_do_not_receive_edit_targets():
    """代码和结构问题留待核对，不能用unchanged回执伪称结构已检查。"""
    block = _block("`CO_2` and text")
    issue = prepare_review_issues([block], [_issue(block, "`CO_3` and text")])[0]
    assert issue["targets"] == [] and issue["validation"] == "protected_target"
    for kind in ("missing_region", "reading_flow", "title_hierarchy", "table_math"):
        raw = _issue(_block("The original sentence."), "The original sentence.")
        raw["kind"] = kind
        prepared = prepare_review_issues([_block("The original sentence.")], [raw])[0]
        assert prepared["targets"] == [] and prepared["status"] == "unresolved" and prepared["review_route"] == "none"


def test_all_targets_require_explicit_response_and_unresolved_needs_no_patch():
    """同一问题两处差异不能只回一处；unresolved明确留存且不修正文。"""
    block = _block("The electrosopun membrane had a commmon surface.")
    issue = prepare_review_issues([block], [_issue(block, "The electrospun membrane had a common surface.")])[0]
    assert len(issue["targets"]) == 2
    result = _result(issue)
    result["patches"].pop()
    edits, outcomes = validate_issue_results([block], _payload(result), [issue], batch_id="batch_1", visual=False)
    assert edits == [] and outcomes[0]["validation"] == "missing_duplicate_or_unknown_target"
    edits, outcomes = validate_issue_results([block], _payload(_result(issue, status="unresolved")), [issue], batch_id="batch_1", visual=False)
    assert edits == [] and outcomes[0]["status"] == "unresolved"


def test_request_contains_minimal_targets_and_no_full_original_or_char_geometry():
    """长块只发送目标上下文，全部目标仍完整登记。"""
    before = "An introductory sentence. " * 40 + "electrosopun membrane" + " A final sentence." * 40
    block = _block(before)
    issue = prepare_review_issues([block], [_issue(block, before.replace("electrosopun", "electrospun"))])[0]
    request = make_review_request("batch_1", [issue], [block])
    rendered = str(request)
    assert before not in rendered
    assert "chars" not in rendered
    assert request["issues"][0]["targets"][0]["id"] == issue["targets"][0]["id"]


def test_scanned_native_unavailable_block_can_be_explicitly_unchanged_by_visual():
    """native缺失只是证据不足，扫描原文可在整块视觉回执下保持。"""
    block = _block("这是扫描区域的正常OCR正文。")
    raw = {"id": "scan", "block_id": "b1", "before": block["content"], "kind": "native_region_unavailable", "status": "not_checked", "review_route": "none", "evidence": []}
    issue = prepare_review_issues([block], [raw])[0]
    assert issue["review_route"] == "vision"
    _vision(issue)
    edits, outcomes = validate_issue_results([block], _payload(_result(issue, status="unchanged", visual=True)), [issue], batch_id="batch_1", visual=True)
    assert edits == [] and outcomes[0]["status"] == "checked"


def test_overlapping_registered_issues_cannot_apply_conflicting_corrections():
    """两个问题对同一原字提出互斥修正时，全部冲突项失败而不修改。"""
    block = _block("The sample is SA.")
    issues = prepare_review_issues([block], [_issue(block, "The sample is 5A."), _issue(block, "The sample is 6A.", id="issue2")])
    for issue in issues:
        _vision(issue, bbox=block["bbox"])
    edits, outcomes = validate_issue_results([block], _payload(*(_result(issue, visual=True) for issue in issues)), issues, batch_id="batch_1", visual=True)
    assert edits == []
    assert [outcome["validation"] for outcome in outcomes] == ["overlapping_targets", "overlapping_targets"]
    assert block["content"] == "The sample is SA."


@pytest.mark.parametrize("failure", ["wrong_page", "wrong_hash", "missing_target_id"])
def test_visual_evidence_binding_cannot_be_supplied_by_model(failure):
    """即使bbox相同，错误页码/版本/目标绑定也不能证明修改。"""
    block = _block("The sample is SA.")
    issue = prepare_review_issues([block], [_issue(block, "The sample is 5A.")])[0]
    _vision(issue, bbox=block["bbox"])
    if failure == "wrong_page":
        issue["evidence"][-1]["page"] = 2
    elif failure == "wrong_hash":
        issue["evidence"][-1]["block_hash"] = "other"
    else:
        issue["evidence"][-1]["target_ids"] = []
    edits, outcomes = validate_issue_results([block], _payload(_result(issue, visual=True)), [issue], batch_id="batch_1", visual=True)
    assert edits == [] and outcomes[0]["validation"] == "visual_region_does_not_cover_target"


def test_running_issue_is_valid_but_finished_issue_cannot_be_reapplied():
    """调度状态running可回执，已终态的问题不可重复修改。"""
    block = _block("Dubinin, M.; Stoeckeli, H.")
    issue = prepare_review_issues([block], [_issue(block, "Dubinin, M.; Stoeckli, H.")])[0]
    issue["status"] = "running"
    edits, _ = validate_issue_results([block], _payload(_result(issue)), [issue], batch_id="batch_1", visual=False)
    assert edits[0]["after"] == "Dubinin, M.; Stoeckli, H."
    issue["status"] = "checked"
    edits, outcomes = validate_issue_results([block], _payload(_result(issue)), [issue], batch_id="batch_1", visual=False)
    assert edits == [] and outcomes[0]["validation"] == "unregistered_target"


def test_text_unchanged_against_conflicting_native_stays_unresolved():
    """文字模型不能把与native冲突的OCR直接称为已核对。"""
    block = _block("Dubinin, M.; Stoeckeli, H.")
    issue = prepare_review_issues([block], [_issue(block, "Dubinin, M.; Stoeckli, H.")])[0]
    edits, outcomes = validate_issue_results([block], _payload(_result(issue, status="unchanged")), [issue], batch_id="batch_1", visual=False)
    assert edits == [] and outcomes[0]["status"] == "unresolved"
    assert outcomes[0]["validation"] == "native_disagreement_requires_vision"


def test_visual_reconstruction_cannot_introduce_an_unregistered_asset():
    """最小target也不能增加图像/链接保护区，重建后仍做完整块保护验证。"""
    block = _block("The sample is SA.")
    issue = prepare_review_issues([block], [_issue(block, "The sample is 5A.")])[0]
    _vision(issue, bbox=block["bbox"])
    edits, outcomes = validate_issue_results([block], _payload(_result(issue, news=["![](asset://fake)"], visual=True)), [issue], batch_id="batch_1", visual=True)
    assert edits == [] and outcomes[0]["validation"] == "protected_content_changed"


@pytest.mark.parametrize("status,empty", [("corrected", False), ("corrected", True), ("unchanged", False)])
def test_valid_no_change_receipt_is_checked_without_creating_patch(status, empty):
    """不同模型的无修改表达只归一状态，原文和所有证据守卫保持。"""
    block = _block("The sample is SA.")
    issue = prepare_review_issues([block], [_issue(block, "The sample is 5A.")])[0]
    _vision(issue, bbox=block["bbox"])
    result = _result(issue, news=[target["old"] for target in issue["targets"]], visual=True)
    result["status"] = status
    if empty:
        result["patches"] = []
    edits, outcomes = validate_issue_results([block], _payload(result), [issue], batch_id="batch_1", visual=True)
    assert edits == [] and block["content"] == "The sample is SA."
    assert outcomes[0]["status"] == "checked"
    assert outcomes[0]["validation"] == "normalized_no_change"


def test_text_no_change_receipt_cannot_resolve_native_disagreement():
    """状态兼容不能让文字模型用原值自证与原生候选的冲突已解决。"""
    block = _block("Dubinin, M.; Stoeckeli, H.")
    issue = prepare_review_issues([block], [_issue(block, "Dubinin, M.; Stoeckli, H.")])[0]
    result = _result(issue, news=[target["old"] for target in issue["targets"]])
    edits, outcomes = validate_issue_results([block], _payload(result), [issue], batch_id="batch_1", visual=False)
    assert edits == [] and outcomes[0]["status"] == "unresolved"
    assert outcomes[0]["validation"] == "native_disagreement_requires_vision"


@pytest.mark.parametrize("fault", ["old", "id", "page", "hash"])
def test_no_change_receipt_still_rejects_invalid_target_or_evidence(fault):
    """无修改不是绕过身份、原文和图像区域绑定的通行证。"""
    block = _block("The sample is SA.")
    issue = prepare_review_issues([block], [_issue(block, "The sample is 5A.")])[0]
    _vision(issue, bbox=block["bbox"])
    result = _result(issue, news=[target["old"] for target in issue["targets"]], visual=True)
    if fault == "old":
        result["patches"][0].update(old="invented", new="invented")
    elif fault == "id":
        result["patches"][0]["target_id"] = "unknown"
    elif fault == "page":
        issue["evidence"][-1]["page"] = 2
    else:
        issue["evidence"][-1]["block_hash"] = "obsolete"
    edits, outcomes = validate_issue_results([block], _payload(result), [issue], batch_id="batch_1", visual=True)
    assert edits == [] and outcomes[0]["status"] == "technical_failed"


def test_request_gives_valid_unchanged_response_shape():
    """示例复用登记身份和证据，不引导模型制造修正。"""
    block = _block("The sample is SA.")
    issue = prepare_review_issues([block], [_issue(block, "The sample is 5A.")])[0]
    _vision(issue, bbox=block["bbox"])
    request = make_review_request("batch_1", [issue], [block])
    edits, outcomes = validate_issue_results([block], request["response_example"], [issue], batch_id="batch_1", visual=True)
    assert request["schema_version"] == 3 and edits == []
    assert outcomes[0]["status"] == "checked"
