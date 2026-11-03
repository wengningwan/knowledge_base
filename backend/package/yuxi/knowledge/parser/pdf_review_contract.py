"""登记不可越界的复核目标，并将逐问题回执重建为完整块补丁。"""

from __future__ import annotations

import difflib
import hashlib
import math
from collections import Counter
from copy import deepcopy

from yuxi.knowledge.parser.pdf_inline_atoms import expand_target_spans, parse_inline_atoms, validate_inline_change

from yuxi.knowledge.parser.pdf_repair_rules import (
    covers_block_region,
    critical_signature,
    damaged_text,
    is_local_candidate,
    overlap_fraction,
    preserves_protected,
    protected_spans,
    proven_representation_equivalent,
)
from yuxi.knowledge.parser.pdf_review import validate_review_patch
from yuxi.knowledge.parser.pdf_review_messages import readable_review_reason


def prepare_review_issues(blocks: list[dict], issues: list[dict]) -> list[dict]:
    """保留问题账，为唯一块快照登记全部差异；证据弱时转整块视觉。"""
    by_id = {block["id"]: block for block in blocks}
    prepared = deepcopy(issues)
    for issue in prepared:
        issue["targets"] = []
        if issue.get("status") not in {"unresolved", "not_checked", "pending", "running"} or issue.get("classification") == "excluded":
            continue
        if issue.get("kind") in {"reading_flow", "title_hierarchy", "missing_region", "table_math"}:
            issue.update(status="unresolved", review_route="none", validation="structure_requires_unique_proof")
            continue
        block = by_id.get(issue.get("block_id"))
        if not block or not _valid_bbox(block.get("bbox")) or block.get("type") not in {"text", "caption", "title", "footnote", "formula", "chemical_equation"}:
            issue.update(status="unresolved", review_route="none", validation="no_reliable_target", reason="缺少唯一正文块、可定位区域或可修改的文字目标，保留人工复核")
            continue
        before = str(block.get("content") or "")
        if not before.strip() or issue.get("before", before) != before:
            issue.update(status="unresolved", review_route="none", validation="stale_snapshot", reason="问题快照过期或没有可核对原文")
            continue
        issue["before"] = before
        issue["block_type"] = block["type"]
        native = [entry for entry in issue.get("evidence", []) if entry.get("source") == "native"]
        candidate = "\n".join(str(entry.get("content") or "") for entry in native)
        strong = bool(native) and covers_block_region(block["bbox"], native) and all(entry.get("quality") == "high" and entry.get("page") == block["page"] and overlap_fraction(entry.get("bbox"), block["bbox"]) >= .98 and sum(other["page"] == entry["page"] and overlap_fraction(entry.get("bbox"), other.get("bbox")) >= .85 for other in blocks) == 1 for entry in native)
        changes = _complete_changes(before, candidate) if candidate else []
        # 未可靠对齐的整块保留原文；不从同页其他原生区域推断目标位置。
        if not changes or not strong or len(changes) > 12 or not is_local_candidate(before, candidate):
            changes = [(0, len(before), 0, len(candidate))]
            strong = False
        if critical_signature(before) != critical_signature(candidate) or block["type"] in {"formula", "chemical_equation"}:
            issue["review_route"] = "vision"
        if not strong:
            issue["review_route"] = "vision"
        elif issue.get("review_route") not in {"text", "vision"}:
            issue["review_route"] = "text"
        digest = hashlib.sha256(before.encode()).hexdigest()
        for start, end, native_start, native_end in changes:
            context_before, context_after = before[max(0, start - 64):start], before[end:end + 64]
            target_id = "target_" + hashlib.sha256(f"{issue['id']}|{digest}|{start}|{end}".encode()).hexdigest()[:24]
            target = {
                "id": target_id, "block_id": block["id"], "block_hash": digest,
                "start": start, "end": end, "old": before[start:end],
                "context_before": context_before, "context_after": context_after,
                "source_region": {"page": block["page"], "bbox": list(block["bbox"]), "scope": "block"},
                "native_evidence_ids": [],
            }
            if strong:
                target["native_new"] = candidate[native_start:native_end]
                target["native_evidence_ids"] = [entry["id"] for entry in native]
                char_region = _native_span_region(native, native_start, native_end)
                if char_region:
                    target["source_region"] = {"page": block["page"], "bbox": char_region, "scope": "characters"}
            if any(start < right and end > left or start == end and left < start < right for left, right in protected_spans(before)):
                issue.update(status="unresolved", review_route="none", validation="protected_target", reason="目标与代码、HTML、链接或图片保护区重叠")
                issue["targets"] = []
                break
            issue["targets"].append(target)
        issue["focus_conflicts"] = [
            {"target_id": target["id"], "old": target["old"], "candidate": target["native_new"]}
            for target in issue["targets"]
            if "native_new" in target and target["old"] != target["native_new"]
            and not proven_representation_equivalent(target["old"], target["native_new"])
        ]
    prepared = _split_large_targets(prepared)
    for issue in prepared:
        issue["reason"] = readable_review_reason(issue.get("reason", ""))
    return prepared


def make_review_request(batch_id: str, issues: list[dict], blocks: list[dict]) -> dict:
    """仅发送当前问题的目标、相邻原文与登记证据，不要求模型复写整段。"""
    by_id = {block["id"]: block for block in blocks}
    requests, image_order = [], []
    for issue in issues:
        evidence = []
        for entry in issue.get("evidence", []):
            if entry.get("source") not in {"native", "visual_region"}:
                continue
            public = {key: entry[key] for key in ("id", "page", "bbox", "source", "quality", "target_ids", "block_id", "block_hash") if key in entry}
            if entry.get("source") == "native":
                if issue.get("review_route") == "vision":
                    continue
                required = {value for target in issue.get("targets", []) for value in target.get("native_evidence_ids", [])}
                if entry["id"] not in required:
                    continue
                public = {key: public[key] for key in ("id", "page", "source", "quality") if key in public}
            else:
                own_ids = {target["id"] for target in issue.get("targets", [])}
                public["target_ids"] = [value for value in public.get("target_ids", []) if value in own_ids]
                if entry["id"] not in image_order:
                    image_order.append(entry["id"])
            evidence.append(public)
        block = by_id.get(issue.get("block_id"), {})
        wire_targets = deepcopy(issue.get("targets", []))
        if issue.get("review_route") == "vision":
            for target in wire_targets:
                target.pop("native_evidence_ids", None)
        requests.append({
            "issue_id": issue["id"], "kind": issue.get("kind"), "targets": wire_targets,
            "block_type": block.get("type"), "evidence": evidence,
            "focus_conflicts": issue.get("focus_conflicts", []),
        })
    return {
        "schema_version": 4, "batch_id": batch_id, "image_order": image_order, "issues": requests,
        "instructions": "每个 issue_id 恰好返回一次，status 只能 corrected/unchanged/unresolved。只逐字读取target所对应原图，focus_conflicts给出需要裁决的具体差异，两种文字都是候选。只检查目标及上下文，不复写无误整段。原图与现有文字一致或只有等价排印差异时返回unchanged、patches=[]。response_example不是预先结论。原图证明确有错误时必须corrected，看不清返回unresolved。corrected的patches覆盖该问题全部target_id，不变目标old==new；仅含target_id/old/new，old逐字复制target.old，new仅含目标替换。unchanged/unresolved的patches为空。引用给出的evidence_ids，reason简短。保留完整公式、上下标及词间空格，作者上标星号使用数学上标，不用裸星号触发强调。保留原文标点、双句点、连字符、可疑化学键和重复句，禁止规范化或按科学常识纠正论文。输出仅batch_id/results，每项仅issue_id/status/patches/evidence_ids/reason。",
        "response_example": {
            "batch_id": batch_id,
            "results": [{"issue_id": issue["issue_id"], "status": "unchanged", "patches": [],
                         "evidence_ids": [entry["id"] for entry in issue["evidence"]],
                         "reason": "逐目标检查原图后，无需修改。"} for issue in requests],
        },
    }


def validate_issue_results(blocks: list[dict], payload, issues: list[dict], *, batch_id: str, visual: bool) -> tuple[list[dict], list[dict]]:
    """逐问题校验回执、目标及局部证据，独立有效结果不受其他失败项连带丢弃。"""
    by_id = {block["id"]: block for block in blocks}
    if not isinstance(payload, dict) or set(payload) != {"batch_id", "results"} or payload.get("batch_id") != batch_id or not isinstance(payload.get("results"), list):
        return [], [_outcome(issue, "technical_failed", "invalid_batch_payload") for issue in issues]
    registered = {issue["id"] for issue in issues}
    results = payload["results"]
    counts = Counter(item.get("issue_id") for item in results if isinstance(item, dict) and isinstance(item.get("issue_id"), str))
    protocol_errors = []
    if any(not isinstance(item, dict) or not isinstance(item.get("issue_id"), str) for item in results):
        protocol_errors.append("invalid_unbound_result")
    if any(issue_id not in registered for issue_id in counts):
        protocol_errors.append("unknown_issue_id")
    outcomes, accepted = [], []
    for issue in issues:
        if counts[issue["id"]] != 1:
            outcomes.append(_outcome(issue, "technical_failed", "duplicate_issue_id" if counts[issue["id"]] else "missing_issue_id"))
            continue
        item = next(item for item in results if isinstance(item, dict) and item.get("issue_id") == issue["id"])
        if set(item) != {"issue_id", "status", "patches", "evidence_ids", "reason"} or not isinstance(item.get("status"), str) or item.get("status") not in {"corrected", "unchanged", "unresolved"} or not isinstance(item.get("patches"), list) or not isinstance(item.get("evidence_ids"), list) or any(not isinstance(value, str) for value in item.get("evidence_ids", [])) or not isinstance(item.get("reason"), str) or not item["reason"].strip():
            outcomes.append(_outcome(issue, "technical_failed", "invalid_issue_result"))
            continue
        if item["status"] == "unresolved":
            outcomes.append(_outcome(issue, "unresolved" if not item["patches"] else "technical_failed", "model_unresolved" if not item["patches"] else "conflicting_status", item["reason"]))
            continue
        block = by_id.get(issue.get("block_id"))
        targets = issue.get("targets", [])
        error = _snapshot_error(issue, block, targets)
        evidence = [entry for entry in issue.get("evidence", []) if entry.get("id") in item["evidence_ids"]]
        if not error and (len(item["evidence_ids"]) != len(set(item["evidence_ids"])) or not evidence or len(evidence) != len(item["evidence_ids"])):
            error = "unknown_or_missing_evidence"
        if not error:
            error = _evidence_error(block, targets, evidence, visual=visual)
        if error:
            outcomes.append(_outcome(issue, "technical_failed", error))
            continue
        patches = item["patches"]
        target_by_id = {target["id"]: target for target in targets}
        ids = [patch.get("target_id") for patch in patches if isinstance(patch, dict) and isinstance(patch.get("target_id"), str)]
        if patches and (len(ids) != len(patches) or len(ids) != len(set(ids)) or set(ids) != set(target_by_id)):
            outcomes.append(_outcome(issue, "technical_failed", "missing_duplicate_or_unknown_target"))
            continue
        replacements = []
        for patch in patches:
            target = target_by_id[patch["target_id"]]
            if set(patch) != {"target_id", "old", "new"} or patch.get("old") != target["old"] or not isinstance(patch.get("new"), str) or damaged_text(patch["new"]):
                error = "invalid_target_patch"
                break
            replacements.append({**target, "new": patch["new"]})
        if error:
            outcomes.append(_outcome(issue, "technical_failed", error))
            continue
        # 表示相同的候选不值得重排原文，避免美元包装或Unicode转换制造补丁。
        for target in replacements:
            if proven_representation_equivalent(target["old"], target["new"]):
                target["new"] = target["old"]
        if not replacements or all(target["old"] == target["new"] for target in replacements):
            if not visual and any(target.get("native_new") != target["old"] for target in targets):
                outcomes.append(_outcome(issue, "unresolved", "native_disagreement_requires_vision", item["reason"]))
            elif issue.get("focus_conflicts"):
                outcomes.append(_outcome(issue, "unresolved", "native_conflict_requires_focused_review", item["reason"]))
            else:
                normalized = item["status"] == "corrected" or bool(patches)
                outcomes.append(_outcome(issue, "checked", "normalized_no_change" if normalized else "accepted", item["reason"]))
            continue
        if item["status"] == "unchanged":
            outcomes.append(_outcome(issue, "technical_failed", "conflicting_status"))
            continue
        if not visual and any(target["new"] != target.get("native_new") for target in replacements):
            outcomes.append(_outcome(issue, "technical_failed", "replacement_not_registered_native_span"))
            continue
        before = str(block.get("content") or "")
        after = _rebuild(before, replacements)
        error = _rebuilt_error(block, before, after, visual=visual, issue=issue, targets=replacements)
        if error:
            outcomes.append(_outcome(issue, "technical_failed", error))
            continue
        accepted.append((issue, replacements, item))
        outcome = _outcome(issue, "machine_corrected", "accepted", item["reason"])
        conflicting_ids = {entry["target_id"] for entry in issue.get("focus_conflicts", [])}
        remaining = [target["id"] for target in replacements if target["id"] in conflicting_ids and target["new"] == target["old"]]
        if remaining:
            outcome.update(
                remaining_conflicts=remaining, reason=outcome["reason"] + "；已修改其中一部分，其余差异仍需核对",
            )
        outcomes.append(outcome)
    # 同一块可有独立问题：先拒绝重叠，再一次重建，避免多个完整 before 补丁互相覆盖。
    overlapping = set()
    for index, (left, left_targets, _) in enumerate(accepted):
        for right, right_targets, _ in accepted[index + 1:]:
            if left["block_id"] == right["block_id"] and any(
                _overlapping(a, b) and (a["start"], a["end"], a["new"]) != (b["start"], b["end"], b["new"])
                for a in left_targets for b in right_targets
            ):
                overlapping.update((left["id"], right["id"]))
    groups = {}
    for issue, replacements, item in accepted:
        if issue["id"] in overlapping:
            outcome = next(outcome for outcome in outcomes if outcome["issue_id"] == issue["id"])
            outcome.update(
                status="technical_failed", reason=readable_review_reason("overlapping_targets"),
                validation="overlapping_targets",
            )
            continue
        groups.setdefault(issue["block_id"], []).append((issue, replacements, item))
    edits = []
    for block_id, entries in groups.items():
        block = by_id[block_id]
        before = str(block.get("content") or "")
        unique_targets = {(target["start"], target["end"]): target for _, targets, _ in entries for target in targets}
        after = _rebuild(before, list(unique_targets.values()))
        error = _rebuilt_error(block, before, after, visual=visual,
                               issue={"evidence": [entry for issue, _, _ in entries for entry in issue.get("evidence", [])]},
                               targets=list(unique_targets.values()))
        if error:
            for issue, _, _ in entries:
                outcome = next(outcome for outcome in outcomes if outcome["issue_id"] == issue["id"])
                outcome.update(status="technical_failed", reason=readable_review_reason(error), validation=error)
            continue
        issue_ids = [issue["id"] for issue, _, _ in entries]
        edits.append({
            "block_id": block_id, "before": before, "after": after, "page": block["page"], "bbox": block.get("bbox"),
            "kind": block["type"], "status": "machine_corrected", "critical": critical_signature(before) != critical_signature(after),
            "issue_id": issue_ids[0], "issue_ids": issue_ids, "reason": "; ".join(item["reason"] for _, _, item in entries)[:1000],
            "evidence_ids": list(dict.fromkeys(value for _, _, item in entries for value in item["evidence_ids"])), "source": "model",
        })
    if protocol_errors:
        for outcome in outcomes:
            outcome["protocol_errors"] = protocol_errors
    # 保存实际收到的建议与校验结局，不能只留下模型reason而丢失被拒new。
    for outcome in outcomes:
        item = next((item for item in results if isinstance(item, dict) and item.get("issue_id") == outcome["issue_id"]), None)
        if item:
            outcome["model_proposal"] = deepcopy(item)
        outcome["patch_accepted"] = outcome["status"] == "machine_corrected"
        outcome["applied"] = False
    return edits, outcomes


def _outcome(issue: dict, status: str, validation: str, reason: str = "") -> dict:
    """生成恰好对应一个登记问题的终态。"""
    return {
        "issue_id": issue["id"], "block_id": issue.get("block_id"), "status": status,
        "reason": readable_review_reason(reason[:1000] or validation), "validation": validation,
    }


def _snapshot_error(issue: dict, block: dict | None, targets: list[dict]) -> str:
    """核对程序登记的块版本、原始偏移与上下文。"""
    if not block or not targets or issue.get("status") not in {"unresolved", "not_checked", "pending", "running"}:
        return "unregistered_target"
    before = str(block.get("content") or "")
    digest = hashlib.sha256(before.encode()).hexdigest()
    if issue.get("before") != before:
        return "stale_block_hash"
    for target in targets:
        start, end = target.get("start"), target.get("end")
        if target.get("block_id") != block["id"] or target.get("block_hash") != digest:
            return "stale_block_hash"
        if type(start) is not int or type(end) is not int or not 0 <= start <= end <= len(before) or before[start:end] != target.get("old"):
            return "invalid_target_offset"
        if before[max(0, start - 64):start] != target.get("context_before") or before[end:end + 64] != target.get("context_after"):
            return "stale_target_context"
    if any(_overlapping(left, right) for index, left in enumerate(targets) for right in targets[index + 1:]):
        return "overlapping_targets"
    return ""


def _evidence_error(block: dict, targets: list[dict], evidence: list[dict], *, visual: bool) -> str:
    """以登记跨度的区域证明局部修改，不要求局部图覆盖整块。"""
    for target in targets:
        region = target.get("source_region", {})
        if region.get("page") != block["page"] or not _valid_bbox(region.get("bbox")):
            return "unbound_target_region"
        if visual:
            matching = [entry for entry in evidence if entry.get("source") == "visual_region" and entry.get("quality") == "high" and target["id"] in entry.get("target_ids", []) and entry.get("block_id") == block["id"] and entry.get("block_hash") == target["block_hash"] and entry.get("page") == region["page"] and _valid_bbox(entry.get("bbox")) and overlap_fraction(region["bbox"], entry["bbox"]) >= .98]
            if not matching:
                return "visual_region_does_not_cover_target"
        else:
            native_ids = target.get("native_evidence_ids", [])
            registered = [entry for entry in evidence if entry.get("id") in native_ids]
            if "native_new" not in target or not native_ids or len(registered) != len(native_ids) or any(entry.get("source") != "native" or entry.get("quality") != "high" or entry.get("page") != block["page"] or overlap_fraction(entry.get("bbox"), block.get("bbox")) < .98 for entry in registered):
                return "unreliable_native_span_requires_vision"
    return ""


def _rebuilt_error(block: dict, before: str, after: str, *, visual: bool,
                   issue: dict, targets: list[dict]) -> str:
    """重建后复用完整块局部性和保护校验，文字路线保留全部科学 token。"""
    if not preserves_protected(before, after):
        return "protected_content_changed"
    error = validate_inline_change(
        before, after,
        script_evidence=_native_baseline_proof(issue, targets),
        punctuation_evidence=_native_replacement_proof(issue, targets),
    )
    if error:
        return error
    if not visual and critical_signature(before) != critical_signature(after):
        return "scientific_change_requires_vision"
    if not visual and not is_local_candidate(before, after):
        return "native_absence_cannot_delete_words"
    valid, _ = validate_review_patch([block], {"edits": [{"block_id": block["id"], "before": before, "after": after}], "unresolved": []})
    return "" if valid else "full_block_local_validation_failed"


def _complete_changes(before: str, candidate: str) -> list[tuple[int, int, int, int]]:
    """扩展原文跨度后沿同一对齐投影候选，禁止把公式碎片当完整目标。"""
    opcodes = difflib.SequenceMatcher(a=before, b=candidate, autojunk=False).get_opcodes()
    spans = expand_target_spans(before, [(a, b) for op, a, b, c, d in opcodes
                                        if op != "equal" and (before[a:b].strip() or candidate[c:d].strip())])
    changes = []
    for start, end in spans:
        native_start, native_end = len(candidate), 0
        for op, a, b, c, d in opcodes:
            if op == "equal":
                left, right = max(start, a), min(end, b)
                if left < right:
                    native_start = min(native_start, c + left - a)
                    native_end = max(native_end, c + right - a)
            elif a < end and b > start or a == b and start <= a <= end:
                native_start, native_end = min(native_start, c), max(native_end, d)
        if native_start <= native_end:
            changes.append((start, end, native_start, native_end))
    return changes


def _native_replacement_proof(issue: dict, targets: list[dict]) -> bool:
    """仅登记的同区域高质量候选能支持原文标点的实际变化。"""
    changed = [target for target in targets if target["old"] != target["new"]]
    native = {entry["id"]: entry for entry in issue.get("evidence", []) if entry.get("source") == "native" and entry.get("quality") == "high"}
    return bool(changed) and all(target.get("native_new") == target["new"] and target.get("native_evidence_ids")
                                 and all(identity in native for identity in target["native_evidence_ids"])
                                 for target in changed)


def _native_baseline_proof(issue: dict, targets: list[dict]) -> bool:
    """脚本降级必须由真实原生字符的等字号同基线证明，缺几何时不猜。"""
    if not _native_replacement_proof(issue, targets):
        return False
    evidence_ids = {identity for target in targets if target["old"] != target["new"] for identity in target.get("native_evidence_ids", [])}
    for entry in issue.get("evidence", []):
        if entry.get("id") not in evidence_ids:
            continue
        chars = [char for char in entry.get("chars", []) if char.get("text", "").strip()]
        if not chars or any(not isinstance(char.get("font_size"), (int, float))
                            or not isinstance(char.get("origin_normal"), (int, float)) for char in chars):
            return False
        sizes = [char["font_size"] for char in chars]
        origins = [char["origin_normal"] for char in chars]
        if min(sizes) <= 0 or max(sizes) / min(sizes) > 1.1 or max(origins) - min(origins) > min(sizes) * .15:
            return False
    return bool(evidence_ids)


def _rebuild(before: str, targets: list[dict]) -> str:
    """从右往左替换已登记跨度，其余字符逐字守恒。"""
    after = before
    for target in sorted(targets, key=lambda target: (target["start"], target["end"]), reverse=True):
        after = after[:target["start"]] + target["new"] + after[target["end"]:]
    return after


def _overlapping(left: dict, right: dict) -> bool:
    """插入点与邻接跨度区分；相同插入点不能重复写入。"""
    if left["start"] == left["end"] and right["start"] == right["end"]:
        return left["start"] == right["start"]
    if left["start"] == left["end"]:
        return right["start"] < left["start"] < right["end"]
    if right["start"] == right["end"]:
        return left["start"] < right["start"] < left["end"]
    return left["start"] < right["end"] and right["start"] < left["end"]


def _valid_bbox(bbox) -> bool:
    """模型边界只接受归一化的有限正面积区域。"""
    return isinstance(bbox, (list, tuple)) and len(bbox) == 4 and all(type(value) in {int, float} and math.isfinite(value) and 0 <= value <= 1 for value in bbox) and bbox[0] < bbox[2] and bbox[1] < bbox[3]


def _native_span_region(evidence: list[dict], start: int, end: int) -> list | None:
    """仅使用真实逐字符几何；原生删除无可见字符时保留整块证据。"""
    if start == end:
        return None
    boxes, offset = [], 0
    for entry in evidence:
        content = str(entry.get("content") or "")
        chars = entry.get("chars", [])
        raw = "".join(char.get("text", "") for char in chars)
        if raw.strip() != content:
            return None
        cursor = offset - (len(raw) - len(raw.lstrip()))
        for char in chars:
            stop = cursor + len(char.get("text", ""))
            if cursor < end and stop > start and char.get("text", "").strip():
                if not _valid_bbox(char.get("bbox")):
                    return None
                boxes.append(char["bbox"])
            cursor = stop
        offset += len(content) + 1
    return [min(box[0] for box in boxes), min(box[1] for box in boxes), max(box[2] for box in boxes), max(box[3] for box in boxes)] if boxes else None


def _split_large_targets(issues):
    """将普通长文本按原块稳定偏移分成子问题，不截断未检查部分。"""
    expanded = []
    for issue in issues:
        targets = issue.get("targets", [])
        if not targets or sum(len(target["old"]) for target in targets) <= 2000:
            expanded.append(issue)
            continue
        if issue.get("block_type") in {"formula", "chemical_equation"}:
            issue.update(status="unresolved", review_route="none", targets=[], validation="indivisible_protected_target", reason="超长公式不能按字符拆断数学结构，保留原文待人工核对")
            expanded.append(issue)
            continue
        fragments = []
        blocked = False
        before = issue["before"]
        for target in targets:
            if len(target["old"]) <= 2000:
                fragments.append(target)
                continue
            protected = protected_spans(before, include_math=True) + [
                (atom["start"], atom["end"]) for atom in parse_inline_atoms(before)
            ]
            cursor = target["start"]
            while cursor < target["end"]:
                end = min(cursor + 2000, target["end"])
                if end < target["end"]:
                    for left, right in protected:
                        if left < end < right:
                            end = left if left > cursor else right
                            break
                    if end - cursor > 2000:
                        blocked = True
                        break
                    space = before.rfind(" ", cursor + 1000, end)
                    if space > cursor and not any(left < space < right for left, right in protected):
                        end = space + 1
                if end <= cursor:
                    blocked = True
                    break
                part = deepcopy(target)
                part.update(start=cursor, end=end, old=before[cursor:end],
                            id="target_" + hashlib.sha256(f"{target['id']}|{cursor}|{end}".encode()).hexdigest()[:24],
                            context_before=before[max(0, cursor-64):cursor], context_after=before[end:end+64],
                            native_evidence_ids=[])
                part.pop("native_new", None)
                part["source_region"]["scope"] = "block"
                fragments.append(part)
                cursor = end
            if blocked:
                break
        if blocked:
            issue.update(status="unresolved", review_route="none", targets=[], validation="indivisible_protected_target", reason="超长数学或保护结构无法安全分片，保留原文待人工复核")
            expanded.append(issue)
            continue
        for index, target in enumerate(fragments):
            child = deepcopy(issue)
            child.update(id=f"{issue['id']}_part{index+1}", parent_issue_id=issue["id"], targets=[target], review_route="vision")
            expanded.append(child)
    return expanded
