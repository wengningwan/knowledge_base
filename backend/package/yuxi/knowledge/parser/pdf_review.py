"""对齐原生候选并限制模型只能提交可定位的局部复核补丁。"""

from __future__ import annotations

import difflib
import math
import re
import unicodedata
from collections import Counter


def align_native_blocks(blocks: list[dict], native_blocks: list[dict]) -> list[dict]:
    """为每块附加重叠原生文字，为差异和高风险内容建立双候选问题。"""
    by_page = {}
    for native in native_blocks:
        by_page.setdefault(native["page"], []).append(native)
    problems = []
    high_risk = {"formula", "chemical_equation", "chemical_structure", "table"}
    for block in blocks:
        matches = [
            item
            for item in by_page.get(block["page"], [])
            if _overlap_fraction(item.get("bbox"), block.get("bbox")) >= 0.35
        ]
        native_text = "\n".join(str(item.get("content", "")) for item in matches).strip()
        block["native_text"] = native_text
        content = str(block.get("content", ""))
        if block["type"] in {"page_header", "page_footer"}:
            continue
        if block["type"] in high_risk or not native_text or normalize_layout(content) != normalize_layout(native_text):
            kind = (
                "high_risk"
                if block["type"] in high_risk
                else ("weak_reference" if not native_text else "text_mismatch")
            )
            problems.append(
                {
                    "id": block["id"],
                    "block_id": block["id"],
                    "page": block["page"],
                    "bbox": block.get("bbox"),
                    "kind": kind,
                    "block_type": block["type"],
                    "candidates": {"ocr": content, "native": native_text},
                    "weak_reference": not bool(native_text) or "\ufffd" in native_text or bool(re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", native_text)),
                    "asset_id": block.get("asset_id"),
                    "source_image": f"asset://{block['asset_id']}" if block.get("asset_id") else "",
                }
            )
    # 反向覆盖审计：遗漏的原生正文不能因为剩余块一致而被判为全部完成。
    for index, native in enumerate(native_blocks):
        box = native.get("bbox")
        content = str(native.get("content") or "").strip()
        if not box or box[1] < 0.08 or box[3] > 0.94 or len(content) < 4:
            continue
        covered = any(block["page"] == native["page"] and _overlap_fraction(box, block.get("bbox")) >= 0.35 for block in blocks)
        if not covered:
            problems.append({
                "id": f"missing_p{native['page']}_{index}", "block_id": None,
                "page": native["page"], "bbox": box, "kind": "missing_region", "block_type": "missing_region",
                "candidates": {"ocr": "", "native": content}, "weak_reference": "\ufffd" in content,
                "reason": "原生正文区域未被主解析块覆盖，保留候选待核对，未猜写正文",
            })
    return problems


def validate_review_patch(blocks: list[dict], payload) -> tuple[list[dict], list[dict]]:
    """逐项验证模型补丁，拒绝重复、跨块替换、改写和原文不匹配。"""
    by_id = {block["id"]: block for block in blocks}
    if (not isinstance(payload, dict) or set(payload) != {"edits", "unresolved"}
            or not isinstance(payload.get("edits"), list) or not isinstance(payload.get("unresolved"), list)):
        return [], [{"kind": "invalid_patch_payload", "reason": "复核输出必须包含 edits 和 unresolved 数组"}]
    if len(payload["unresolved"]) > 1000 or any(
        not isinstance(item, dict) or not isinstance(item.get("block_id"), str) or item["block_id"] not in by_id
        for item in payload["unresolved"]
    ):
        return [], [{"kind": "invalid_patch_payload", "reason": "unresolved 必须引用本次检查的块"}]
    raw_edits = payload["edits"]
    if len(raw_edits) > min(1000, len(blocks) * 2 + 10):
        return [], [{"kind": "too_many_patches", "reason": "补丁数量超过文档块数限制"}]
    counts = Counter(
        item.get("block_id")
        for item in raw_edits
        if isinstance(item, dict) and isinstance(item.get("block_id"), str)
    )
    valid, unresolved = [], []
    for edit in raw_edits:
        reason = ""
        block_id = edit.get("block_id") if isinstance(edit, dict) else None
        block = by_id.get(block_id) if isinstance(block_id, str) else None
        if not isinstance(edit, dict):
            reason = "补丁必须是对象"
        elif block is None:
            reason = "block_id 不存在"
        elif counts[block_id] != 1:
            reason = "同一块出现多个补丁，全部留待复核"
        elif set(edit) - {"block_id", "before", "after", "reason", "kind", "confidence", "critical"}:
            reason = "补丁含越界字段或全局替换指令"
        elif not isinstance(edit.get("before"), str) or edit["before"] != block.get("content", ""):
            reason = "before 必须完整且精确匹配原块"
        elif not isinstance(edit.get("after"), str) or not edit["after"].strip():
            reason = "after 必须是非空字符串"
        elif len(edit["after"]) > max(len(edit["before"]) * 2, len(edit["before"]) + 4096):
            reason = "补丁文本增长超出局部修改范围"
        elif any(unicodedata.category(char) == "Cc" and char not in "\n\r\t" for char in edit["after"]):
            reason = "after 含非法控制字符"
        elif not _preserves_image_evidence(edit["before"], edit["after"]):
            reason = "补丁不能移除或更换原图证据"
        elif not _local_change(block, edit["before"], edit["after"]):
            reason = "补丁超出合理局部修正或可比对候选选择范围"
        if reason:
            unresolved.append(
                {
                    "block_id": block_id,
                    "page": block.get("page") if block else None,
                    "bbox": block.get("bbox") if block else None,
                    "kind": "rejected_patch",
                    "reason": reason,
                }
            )
            continue
        before, after = edit["before"], edit["after"]
        if before == after:
            continue
        critical = _critical_change(block, before, after)
        valid.append(
            {
                "block_id": block_id,
                "page": block["page"],
                "bbox": block.get("bbox"),
                "kind": block["type"],
                "reason": str(edit.get("reason") or "双候选局部差异复核")[:1000],
                "before": before,
                "after": after,
                "status": "machine_corrected",
                "critical": critical,
                "asset_id": block.get("asset_id"),
            }
        )
    extra_unresolved = payload.get("unresolved", [])
    if isinstance(extra_unresolved, list):
        for item in extra_unresolved[:1000]:
            if isinstance(item, dict) and isinstance(item.get("block_id"), str) and item["block_id"] in by_id:
                block = by_id[item["block_id"]]
                unresolved.append(
                    {
                        "block_id": block["id"],
                        "page": block["page"],
                        "bbox": block.get("bbox"),
                        "kind": "model_unresolved",
                        "reason": str(item.get("reason") or "模型未能确认")[:1000],
                    }
                )
    return valid, unresolved


def normalize_layout(text: str) -> str:
    """仅统一 Unicode 组合、换行与空白，不改数字、单位或化学符号。"""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text).replace("\r\n", "\n")).strip()


def _overlap_fraction(native_bbox, block_bbox) -> float:
    """按原生行被覆盖的面积匹配块，避免用宽大块的 IoU 丢失行。"""
    if not native_bbox or not block_bbox:
        return 0.0
    nx0, ny0, nx1, ny1 = native_bbox
    bx0, by0, bx1, by1 = block_bbox
    area = (nx1 - nx0) * (ny1 - ny0)
    overlap = max(0, min(nx1, bx1) - max(nx0, bx0)) * max(0, min(ny1, by1) - max(ny0, by0))
    return overlap / area if area > 0 else 0.0


def _preserves_image_evidence(before: str, after: str) -> bool:
    """要求全部原图引用逐字保留，拒绝新增未受审阅的图像引用。"""
    pattern = r"asset://[A-Za-z0-9_.-]+|!\[[^\]]*\]\([^)]*\)|<img\b[^>]*>"
    return Counter(re.findall(pattern, before, re.I)) == Counter(re.findall(pattern, after, re.I))


def _local_change(block: dict, before: str, after: str) -> bool:
    """按实际编辑跨度设上限，即使模型选择候选也不接受整段替写。"""
    if before == after:
        return True
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    changes = [(i2 - i1, j2 - j1) for op, i1, i2, j1, j2 in matcher.get_opcodes() if op != "equal"]
    changed = sum(max(left, right) for left, right in changes)
    total = max(len(before), len(after), 1)
    if len(changes) > 12 or abs(len(after) - len(before)) > max(12, int(len(before) * 0.2)):
        return False
    candidate = block.get("native_text", "")
    candidate_selected = bool(candidate) and after == candidate
    limit = max(2, min(64, math.ceil(total * (0.18 if candidate_selected else 0.12))))
    return changed <= limit and matcher.ratio() >= (0.70 if total > 8 else 0.50)


def _critical_change(block: dict, before: str, after: str) -> bool:
    """把化学、公式、数字和单位等意义变化标成必须显著呈现的关键修正。"""
    if block["type"] in {"formula", "chemical_equation", "chemical_structure", "table"}:
        return True
    # 比较完整数值 token，覆盖仅改变正负号、小数点或指数符号的修订。
    numeric = r"[+−-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+−-]?\d+)?"
    if re.findall(numeric, before) != re.findall(numeric, after):
        return True
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    changed = " ".join(
        before[i1:i2] + after[j1:j2]
        for op, i1, i2, j1, j2 in matcher.get_opcodes()
        if op != "equal"
    )
    return bool(re.search(r"[0-9A-Za-zµμ°%℃℉α-ωΑ-Ω\u4e00-\u9fff]|\\(?:ce|tag|frac|mathrm)\b", changed))
