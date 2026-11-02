"""人工原图短摘录的保真正反例；运行留给验证机。"""

import hashlib

import pytest

from yuxi.knowledge.parser.pdf_inline_atoms import expand_target_spans, validate_inline_change
from yuxi.knowledge.parser.pdf_review_contract import prepare_review_issues, validate_issue_results


@pytest.mark.parametrize(("old", "new", "reason"), [
    ("sp² and sp³ hybridized", "sp2and sp3hybridized", "script_scope_loss_requires_native_geometry"),
    (r"min$^{-1}$ of", "min−1}of", "introduced_math_damage"),
    (r"$C_{2}H_{4}/C_{2}H_{6}=50/50$", "C2H4/C2H6 =50/50", "script_scope_loss_requires_native_geometry"),
    (r"$N_{2}$, $CO_{2}$, $O_{2}$, $H_{2}$", "N2, CO2, O2, H2", "script_scope_loss_requires_native_geometry"),
    ("69–85..", "69–85.", "punctuation_change_requires_native_evidence"),
    ("Miao, and Xiao", "Miao,*and Xiao", "introduced_word_join"),
    (r"$E_{2g}$", r"$E_{2}$g", "script_scope_loss_requires_native_geometry"),
    (r"$C_{2}H$", r"$CH_{2}$", "script_scope_loss_requires_native_geometry"),
    ("common membrane", "commonmembrane", "introduced_word_join"),
    ("The electrosopun membrane..", "The electrospun membrane.", "punctuation_change_requires_native_evidence"),
    (r"The mode is $x_\alpha$.", r"The mode is $x\alpha$.", "script_scope_loss_requires_native_geometry"),
    (r"The mode is $x_{\mathrm{ab}}$.", r"The mode is $x_{\mathrm{a}}b$.", "script_scope_loss_requires_native_geometry"),
    ("The gases CO₂ and N2 and H2 were measured by the same method.",
     "The gases CO2 and N₂ and H₂ were measured by the same method.", "script_scope_loss_requires_native_geometry"),
    ("The gases CO₂ and CO2 were measured by the same method.",
     "The gases CO2 and CO₂ were measured by the same method.", "script_scope_loss_requires_native_geometry"),
])
def test_new_damage_is_rejected_after_complete_rebuild(old, new, reason):
    """正确脚本、数学边界、分隔及原文标点不能被校对破坏。"""
    assert validate_inline_change(old, new) == reason
    block, issue, payload = _visual_case(old, new)
    edits, outcomes = validate_issue_results([block], payload, [issue], batch_id="b", visual=True)
    assert edits == [] and block["content"] == old
    assert outcomes[0]["validation"] == reason
    assert outcomes[0]["model_proposal"]["patches"][0]["new"] == new
    assert outcomes[0]["applied"] is False


@pytest.mark.parametrize(("old", "new"), [
    ("The zeolite is SA.", "The zeolite is 5A."),
    ("The electrosopun membrane.", "The electrospun membrane."),
    ("This: Energy Fuels 2025", "Cite This: Energy Fuels 2025"),
    ("The mode is Eₘg.", r"The mode is $E_{2g}$."),
    ("See reference 1.", "See reference 11."),
    ("Miao, and Xiao", r"Miao,$^{*}$ and Xiao"),
    ("A membrane in [Figure 1] has pores [1] and [2].",
     "A membrane in Figure 1 has pores 1 and 2."),
    (r"The ultramicropore structure of carbonaceous adsorbents.$^{[1]}$ "
     r"The D-A equation evaluates heterogeneous surfaces.$^{[20-22]}$ "
     r"The D-A equation is formally written as$^{[2,3]}$",
     r"The ultramicropore structure of carbonaceous adsorbents.$^{11}$ "
     r"The D-A equation evaluates heterogeneous surfaces.$^{20-22}$ "
     r"The D-A equation is formally written as$^{2,3}$"),
])
def test_real_local_corrections_remain_possible(old, new):
    """视觉纠错可以补字、改读数、还原整段引文及完整脚本，不能全部冻结。"""
    block, issue, payload = _visual_case(old, new)
    edits, outcomes = validate_issue_results([block], payload, [issue], batch_id="b", visual=True)
    assert len(edits) == 1 and edits[0]["after"] == new
    assert outcomes[0]["status"] == "machine_corrected"


@pytest.mark.parametrize(("text", "needle", "expected"), [
    (r"At min$^{-1}$ of flow.", "^{-", r"min$^{-1}$"),
    (r"The $C_{2}H_{4}$ value.", "2", r"$C_{2}H_{4}$"),
    ("A sp² hybrid.", "²", "sp²"),
    ("The electrosopun membrane.", "sop", "electrosopun"),
    ("The bond C≡N is printed.", "≡", "C≡N"),
    (r"The E$_{2}$g mode.", "2", r"E$_{2}$g"),
])
def test_target_expansion_never_sends_formula_fragments(text, needle, expected):
    """整词和附着数学脚本保持一个目标，偏移仍来自原字符串。"""
    start = text.index(needle)
    spans = expand_target_spans(text, [(start, start + len(needle))])
    assert len(spans) == 1
    assert text[spans[0][0]:spans[0][1]] == expected


def test_overlapping_atom_changes_merge_without_duplicate_offsets():
    """同一数学原子多处差异必须合并，不允许重叠补丁重复落地。"""
    text = r"min$^{-1}$ of"
    assert expand_target_spans(text, [(4, 6), (7, 9)]) == [(0, 10)]


@pytest.mark.parametrize(("old", "new"), [
    ("The mode sp² remains.", r"The mode sp$^{2}$ remains."),
    ("The gas CO₂ remains.", r"The gas $CO_{2}$ remains."),
])
def test_equivalent_typography_retains_original_bytes(old, new):
    """等价仅用于保留原串，不能让模型反复改写包装。"""
    block, issue, payload = _visual_case(old, new)
    edits, outcomes = validate_issue_results([block], payload, [issue], batch_id="b", visual=True)
    assert edits == [] and block["content"] == old
    assert outcomes[0]["status"] == "checked"
    assert outcomes[0]["validation"] == "normalized_no_change"


def test_identical_overlapping_issue_results_apply_once_and_keep_both_receipts():
    """同块两条疑点认可相同完整目标时合并应用，不丢任何问题回执。"""
    from copy import deepcopy

    block, first, payload = _visual_case("The zeolite is SA.", "The zeolite is 5A.")
    second = deepcopy(first)
    second["id"] = "second"
    second["targets"][0]["id"] = "second_target"
    second["evidence"][0]["target_ids"] = ["second_target"]
    answer = deepcopy(payload["results"][0])
    answer["issue_id"] = "second"
    answer["patches"][0]["target_id"] = "second_target"
    payload["results"].append(answer)
    edits, outcomes = validate_issue_results([block], payload, [first, second], batch_id="b", visual=True)
    assert len(edits) == 1 and edits[0]["after"] == "The zeolite is 5A."
    assert edits[0]["issue_ids"] == ["i", "second"]
    assert [row["status"] for row in outcomes] == ["machine_corrected", "machine_corrected"]


@pytest.mark.parametrize("original", [
    "At 1595 cm⁻¹ the printed bond is C≡N.",
    r"The printed expression is $P_{0}P^{1-}$.",
    "The reference ends 69–85..",
    "CO2/CH4 is printed on the baseline.",
    "The comparison is repeated. The comparison is repeated.",
    "A literal {set} is printed.",
])
def test_original_oddities_are_not_automatically_corrected(original):
    """科学可疑内容或重复排印仍是忠实转录的依据。"""
    block, issue, payload = _visual_case(original, original)
    payload["results"][0].update(status="unchanged", patches=[])
    edits, outcomes = validate_issue_results([block], payload, [issue], batch_id="b", visual=True)
    assert edits == [] and block["content"] == original
    assert outcomes[0]["status"] == "checked"


def _visual_case(old, new):
    """扫描场景无原生候选，人工构造真实区域绑定与显式模型建议。"""
    block = {"id": "p1_b0", "page": 1, "type": "text", "bbox": [.1, .2, .8, .3], "content": old}
    raw = {"id": "i", "block_id": block["id"], "before": old, "status": "unresolved",
           "kind": "text_mismatch", "review_route": "vision", "evidence": []}
    issue = prepare_review_issues([block], [raw])[0]
    target = issue["targets"][0]
    assert target["old"] == old and target["block_hash"] == hashlib.sha256(old.encode()).hexdigest()
    issue["evidence"] = [{"id": "v", "source": "visual_region", "quality": "high", "page": 1,
                          "bbox": block["bbox"], "block_id": block["id"], "block_hash": target["block_hash"],
                          "target_ids": [target["id"]]}]
    payload = {"batch_id": "b", "results": [{"issue_id": "i", "status": "corrected",
               "patches": [{"target_id": target["id"], "old": old, "new": new}],
               "evidence_ids": ["v"], "reason": "人工原图短例的显式转录"}]}
    return block, issue, payload
