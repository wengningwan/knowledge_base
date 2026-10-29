"""复核双候选与局部模型补丁的负向边界契约。"""

from yuxi.knowledge.parser.pdf_review import align_native_blocks, normalize_layout, validate_review_patch


def _block(content="The sample was heated to 120 °C for 2 h.", **changes):
    """创建有稳定 ID 与位置的正文块。"""
    return {"id": "p1_b4", "page": 1, "bbox": [0.1, 0.2, 0.9, 0.3], "type": "text", "content": content, **changes}


def test_normalize_layout_keeps_number_unit_and_chemical_differences():
    """排版归一只消除空白形式，不消除数字和单位差异。"""
    assert normalize_layout("10\n  mg") == "10 mg"
    assert normalize_layout("10 mg") != normalize_layout("1.0 mg")
    assert normalize_layout("10 mg") != normalize_layout("10 μg")
    assert normalize_layout("Fe2+") != normalize_layout("Fe3+")
    assert normalize_layout("CO") != normalize_layout("Co")


def test_high_risk_matching_native_still_requires_review_and_missing_is_weak():
    """公式即使两路一致也列入复核，无原生候选时标记弱参照。"""
    formula = _block(r"\ce{H2O}", type="formula")
    text = _block("other page", id="p2_b0", page=2)
    native = [{"page": 1, "bbox": [0.1, 0.2, 0.8, 0.25], "content": r"\ce{H2O}"}]
    problems = align_native_blocks([formula, text], native)
    assert formula["native_text"] == r"\ce{H2O}"
    assert problems[0]["kind"] == "high_risk"
    assert problems[0]["candidates"] == {"ocr": r"\ce{H2O}", "native": r"\ce{H2O}"}
    assert problems[1]["kind"] == "weak_reference"
    assert problems[1]["weak_reference"] is True


def test_full_before_mismatch_is_unresolved_without_applying_anything():
    """before 不能只给局部子串或依赖全局替换。"""
    block = _block()
    edits, unresolved = validate_review_patch([block], {"unresolved": [], "edits": [{"block_id": block["id"], "before": "120", "after": "125"}]})
    assert edits == []
    assert "完整且精确" in unresolved[0]["reason"]
    assert block["content"].endswith("2 h.")


def test_duplicate_patches_for_a_block_are_all_rejected():
    """不能以多个小补丁绕过单块局部编辑预算。"""
    block = _block()
    patch = {"block_id": block["id"], "before": block["content"], "after": block["content"].replace("120", "125")}
    edits, unresolved = validate_review_patch([block], {"unresolved": [], "edits": [patch, patch]})
    assert edits == []
    assert len(unresolved) == 2
    assert all("多个补丁" in item["reason"] for item in unresolved)


def test_local_numeric_correction_is_machine_corrected_and_critical():
    """数值修正可保留可审核补丁，但必须显著标记关键变化。"""
    block = _block(asset_id="region-1")
    after = block["content"].replace("120", "125")
    edits, unresolved = validate_review_patch([block], {"unresolved": [], "edits": [{"block_id": block["id"], "before": block["content"], "after": after, "confidence": 0.01, "critical": False}]})
    assert unresolved == []
    assert edits[0]["after"] == after
    assert edits[0]["critical"] is True
    assert edits[0]["status"] == "machine_corrected"
    assert edits[0]["asset_id"] == "region-1"
    assert block["content"] != after


def test_entire_paragraph_rewrite_and_global_replace_are_rejected():
    """模型高置信度或总结性改写不能突破局部边界。"""
    block = _block()
    edits, unresolved = validate_review_patch([block], {"unresolved": [], "edits": [{"block_id": block["id"], "before": block["content"], "after": "The experiment went well and confirmed the theory.", "confidence": 1.0}]})
    assert not edits
    assert unresolved
    edits, unresolved = validate_review_patch([block], {"unresolved": [], "edits": [{"block_id": block["id"], "before": block["content"], "after": block["content"], "replace_all": True}]})
    assert not edits
    assert "越界字段" in unresolved[0]["reason"]


def test_image_evidence_cannot_be_replaced_and_bad_items_do_not_abort_document():
    """拒绝替换图像引用，同时保留另一个合法块的独立修正。"""
    picture = _block("![](asset://original)", type="chemical_structure")
    text = _block(id="p1_b5")
    edits, unresolved = validate_review_patch([picture, text], {"unresolved": [], "edits": [{"block_id": picture["id"], "before": picture["content"], "after": "![](asset://invented)"}, {"block_id": text["id"], "before": text["content"], "after": text["content"].replace("120", "121")}, {"block_id": "unknown", "before": "", "after": "new"}]})
    assert len(edits) == 1
    assert edits[0]["block_id"] == text["id"]
    assert len(unresolved) == 2
    assert any("原图证据" in item["reason"] for item in unresolved)


def test_invalid_model_payload_remains_explicitly_unresolved():
    """错误模型 JSON 结构作为复核问题返回，不令整份文档异常。"""
    edits, unresolved = validate_review_patch([_block()], {"edits": "replace everything"})
    assert edits == []
    assert unresolved[0]["kind"] == "invalid_patch_payload"


def test_numeric_sign_decimal_and_exponent_edits_are_critical():
    """单字符科学意义变化不能被普通标点归类。"""
    for before, after in [("Potential was -0.5 V.", "Potential was +0.5 V."),
                          ("The mass was 1.0 mg.", "The mass was 10 mg."),
                          ("Rate was 1e-3 per s.", "Rate was 1e+3 per s.")]:
        block = _block(before)
        edits, pending = validate_review_patch([block], {"edits": [{"block_id": block["id"], "before": before, "after": after}], "unresolved": []})
        assert not pending
        assert edits[0]["critical"] is True


def test_bad_unresolved_shape_or_foreign_id_cannot_claim_checked():
    """模型遗漏协议字段或引用别的块时保留未决状态。"""
    for payload in [{"edits": []}, {"edits": [], "unresolved": "none"},
                    {"edits": [], "unresolved": [{"block_id": "other"}]}]:
        edits, pending = validate_review_patch([_block()], payload)
        assert not edits and pending



def test_uncovered_native_paragraph_and_page_remain_missing_regions():
    retained = _block("Retained paragraph.")
    native = [
        {"page": 1, "bbox": retained["bbox"], "content": retained["content"]},
        {"page": 1, "bbox": [.1, .5, .9, .6], "content": "Omitted paragraph on same page."},
        {"page": 2, "bbox": [.1, .2, .9, .4], "content": "Omitted complete second page."},
    ]
    missing = [item for item in align_native_blocks([retained], native) if item["kind"] == "missing_region"]
    assert [item["page"] for item in missing] == [1, 2]
    assert all(item["block_id"] is None for item in missing)
    assert retained["content"] == "Retained paragraph."
