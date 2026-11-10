"""登记不可越界的复核目标，并将逐问题回执重建为完整块补丁。"""

from __future__ import annotations

import difflib
import hashlib
import math
import re
from collections import Counter
from copy import deepcopy
from statistics import median

from yuxi.knowledge.parser.pdf_inline_atoms import (
    expand_target_spans,
    lost_scripts,
    parse_inline_atoms,
    scripts,
    validate_inline_change,
)

from yuxi.knowledge.parser.pdf_repair_rules import (
    covers_block_region,
    critical_signature,
    damaged_text,
    fold_equivalent_typography,
    is_local_candidate,
    normalized_visible_text,
    overlap_fraction,
    preserves_protected,
    protected_spans,
    proven_representation_equivalent,
    scientific_symbol_spans,
    unresolved_surplus,
    whitespace_structure_divergence,
)
from yuxi.knowledge.parser.pdf_review import validate_review_patch
from yuxi.knowledge.parser.pdf_review_messages import readable_review_reason


_STRUCTURE_KINDS = {"reading_flow", "title_hierarchy", "missing_region", "table_math", "formula_fragment_cluster"}
_STRUCTURE_CAPTION = re.compile(r"^\s*(?:©|Copyright|Figure|Fig\.|Table|Scheme|Plate)\b", re.I)
_STRUCTURE_SHORT = 60


def _structure_auto_kept(kind: str, content: str) -> bool:
    """结构类分级：阅读顺序自动保留，公式碎片簇/长正文缺失留人工，图注/短缺失自动。

    公式碎片簇指示 OCR 缺失了原文公式内容（真实链路实证 underived 公式行），
    自动保留会静默丢失补录机会，必须人工；阅读顺序无内容损失可自动。
    """
    if kind == "formula_fragment_cluster":
        return False
    if kind != "missing_region":
        return True
    text = content.strip()
    return bool(text) and (bool(_STRUCTURE_CAPTION.match(text)) or len(text) <= _STRUCTURE_SHORT)


def prepare_review_issues(blocks: list[dict], issues: list[dict], *, vision_final: bool = False) -> list[dict]:
    """保留问题账，为唯一块快照登记全部差异；证据弱时转整块视觉。

    视觉终裁开启时结构类按影响分级处置（structure_auto_kept）：阅读顺序与
    图注/短缺失自动保留；公式碎片簇指示内容缺失必须人工；长正文缺失
    仍维持唯一锚定人工。
    """
    by_id = {block["id"]: block for block in blocks}
    prepared = deepcopy(issues)
    for issue in prepared:
        raw_targets = issue.get("targets") or []
        issue["targets"] = []
        if issue.get("status") not in {"unresolved", "not_checked", "pending", "running"} or issue.get("classification") == "excluded":
            continue
        if issue.get("kind") in _STRUCTURE_KINDS:
            # missing_region 的遗漏内容登记在 evidence 的 native entry（targets 为空），
            # 与 run_rule_repair 的 _row 装载结构对齐。
            content = "\n".join(str(entry.get("content") or "") for entry in issue.get("evidence") or []
                                if entry.get("source") == "native")
            if vision_final and _structure_auto_kept(issue["kind"], content):
                issue.update(status="checked", review_route="none", validation="structure_auto_kept",
                             reason="结构类差异按分级自动保留现状，未补写或未调整任何内容")
            else:
                issue.update(status="unresolved", review_route="none", validation="structure_requires_unique_proof")
            continue
        block = by_id.get(issue.get("block_id"))
        if not block or not _valid_bbox(block.get("bbox")) or block.get("type") not in {"text", "caption", "title", "footnote", "formula", "chemical_equation"}:
            issue.update(status="unresolved", review_route="none", validation="no_reliable_target", reason="缺少唯一正文块、可定位区域或可修改的文字目标，保留人工复核")
            continue
        before = str(block.get("content") or "")
        if not before.strip() or issue.get("before", before) != before:
            # 合并先行改块后快照过期：按内容找回承载快照的块继续复核，找不到才留人工。
            anchor = _reanchor_snapshot(issue.get("before"), blocks)
            if anchor is None:
                issue.update(status="unresolved", review_route="none", validation="stale_snapshot", reason="问题快照过期或没有可核对原文")
                continue
            anchor_block, containment = anchor
            issue["block_id"] = anchor_block["id"]
            issue["final_block_id"] = anchor_block["id"]
            if containment:
                # 仅被更大合并块包含时，区域级原生候选与整块目标错位，
                # 修正结论的字符守卫弹回改用诚实标签；无差异结论照常走守卫。
                issue["reanchored_containment"] = True
            block = anchor_block
            before = str(anchor_block.get("content") or "")
        issue["before"] = before
        issue["block_type"] = block["type"]
        native = [entry for entry in issue.get("evidence", []) if entry.get("source") == "native"]
        candidate = "\n".join(str(entry.get("content") or "") for entry in native)
        # 空白等价豁免：去空白后逐字一致即纯排版空白差异（PDF 断行折合出的
        # 假差异，整块 fallback 只会误撞保护区），不做任何修改直接关闭；
        # 真实字符差异绝不进入此分支。
        if candidate and "".join(before.split()) == "".join(candidate.split()):
            issue.update(status="proven_equivalent", review_route="none", validation="whitespace_equivalent",
                         reason="去空白后逐字一致，仅排版空白表示不同")
            issue["targets"] = []
            continue
        strong = bool(native) and covers_block_region(block["bbox"], native) and all(entry.get("quality") == "high" and entry.get("page") == block["page"] and overlap_fraction(entry.get("bbox"), block["bbox"]) >= .98 and sum(other["page"] == entry["page"] and overlap_fraction(entry.get("bbox"), other.get("bbox")) >= .85 for other in blocks) == 1 for entry in native)
        symbols = scientific_symbol_spans(before) if issue.get("review_scientific_symbols") else []
        changes = _complete_changes(before, candidate, extra_spans=symbols) if candidate else []
        # 未可靠对齐的整块保留原文；不从同页其他原生区域推断目标位置。
        if (
            not changes or not strong or len(changes) > 12
            or (candidate != before and not is_local_candidate(before, candidate))
        ):
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
                # 登记域原生证明文本同步剥换行，模型域与原生域在同一单行正文语义下对比。
                target["native_new"] = re.sub(r"[\r\n]+", "", candidate[native_start:native_end])
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


_REANCHORABLE_TYPES = {"text", "caption", "title", "footnote", "formula", "chemical_equation"}


def _reanchor_snapshot(snapshot, blocks):
    """快照与挂载块不一致时，在当前块集合中找回承载快照文本的块。

    候选必须是与主路径同款校验的正文块；空白差异按归一化包含吸收；命中多个时
    按“完全相等、最紧包含、块号字典序”确定性择优，找不到返回 None 保留人工。
    返回 (块, 是否仅包含)：仅包含意味着原块已合并删除，区域级证据与整块目标错位。
    """
    normalized = normalized_visible_text(str(snapshot or ""))
    if not normalized:
        return None
    best, best_key = None, None
    best_containment = False
    for block in blocks:
        if block.get("type") not in _REANCHORABLE_TYPES or not _valid_bbox(block.get("bbox")):
            continue
        content = normalized_visible_text(str(block.get("content") or ""))
        if not content or normalized not in content:
            continue
        key = (content != normalized, abs(len(content) - len(normalized)), str(block.get("id") or ""))
        if best_key is None or key < best_key:
            best, best_key = block, key
            best_containment = content != normalized
    return None if best is None else (best, best_containment)


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
        request = {
            "issue_id": issue["id"], "kind": issue.get("kind"), "targets": wire_targets,
            "block_type": block.get("type"), "evidence": evidence,
            "focus_conflicts": issue.get("focus_conflicts", []),
        }
        if issue.get("review_route") == "vision":
            geometry = _native_geometry(issue)
            if geometry:
                request["native_geometry"] = geometry
        requests.append(request)
    return {
        "schema_version": 4, "batch_id": batch_id, "image_order": image_order, "issues": requests,
        "instructions": (
            "每个 issue_id 恰好返回一次，status 只能 corrected/unchanged/unresolved。"
            "只逐字读取target所对应原图，focus_conflicts给出需要裁决的具体差异，两种文字都是候选。"
            "只检查目标及上下文，不复写无误整段。原图与现有文字一致或只有等价排印差异时返回unchanged、patches=[]。"
            "response_example不是预先结论。原图证明确有错误时必须corrected，看不清返回unresolved。"
            "corrected的patches覆盖该问题全部target_id，不变目标old==new；仅含target_id/old/new，"
            "old逐字复制target.old，new仅含目标替换。new不含换行符；PDF断行与行尾空白不是内容，照抄断行会被拒。unchanged/unresolved的patches为空。引用给出的evidence_ids，reason简短。"
            "逐字核对下标中的数字与字母、化学单键/双键/三键及其作用范围；原图证明OCR不同则修正，看不清不得猜改。"
            "native_geometry为原生文字层实测的字号与基线偏移，仅供对照，最终结论以裁图原图为准。"
            "保留完整公式和词间空格，作者上标星号使用数学上标，不用裸星号触发强调。"
            "原图确实采用的标点、双句点、连字符、化学键和重复句均保留，即使不符合领域常见写法也不得按科学常识改写。"
            "输出仅batch_id/results，每项仅issue_id/status/patches/evidence_ids/reason。"
        ),
        "response_example": {
            "batch_id": batch_id,
            "results": [{"issue_id": issue["issue_id"], "status": "unchanged", "patches": [],
                         "evidence_ids": [entry["id"] for entry in issue["evidence"]],
                         "reason": "逐目标检查原图后，无需修改。"} for issue in requests],
        },
    }


def validate_issue_results(blocks: list[dict], payload, issues: list[dict], *, batch_id: str, visual: bool,
                           vision_final: bool = False) -> tuple[list[dict], list[dict]]:
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
    outcomes, accepted, edits = [], [], []
    vision_flags = set()
    for issue in issues:
        if counts[issue["id"]] != 1:
            outcomes.append(_outcome(issue, "technical_failed", "duplicate_issue_id" if counts[issue["id"]] else "missing_issue_id"))
            continue
        item = next(item for item in results if isinstance(item, dict) and item.get("issue_id") == issue["id"])
        if set(item) != {"issue_id", "status", "patches", "evidence_ids", "reason"} or not isinstance(item.get("status"), str) or item.get("status") not in {"corrected", "unchanged", "unresolved"} or not isinstance(item.get("patches"), list) or not isinstance(item.get("evidence_ids"), list) or any(not isinstance(value, str) for value in item.get("evidence_ids", [])) or not isinstance(item.get("reason"), str) or not item["reason"].strip():
            outcomes.append(_outcome(issue, "technical_failed", "invalid_issue_result"))
            continue
        if item["status"] == "unresolved":
            cleaned = _reference_marker_cleanup(issue, by_id.get(issue.get("block_id")), visual=visual, vision_final=vision_final) if not item["patches"] else None
            if cleaned is not None:
                edits.append(cleaned["edit"])
                outcomes.append(cleaned["outcome"])
                vision_flags.add(issue["id"])
            else:
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
            replacements.append({**target, "new": re.sub(r"[\r\n]+", "", patch["new"])})
            # 剥除后退化（new==old）的补丁由下方表示等价化置零，有效修复不再被换行守恒拦截。
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
                # 例外：视觉终裁开启时，模型看着原图返回真零补丁 unchanged 意味着
                # 聚焦冲突已逐字裁决，人工兜底由视觉替代；与 surplus 自动关闭同族。
                if (visual and vision_final and item["status"] == "unchanged" and not patches
                        and any(entry.get("source") == "visual_region" and entry.get("quality") == "high" for entry in evidence)):
                    outcome = _outcome(issue, "checked", "vision_confirmed_no_change", item["reason"])
                    outcome["vision_final"] = True
                    outcomes.append(outcome)
                else:
                    outcomes.append(_outcome(issue, "unresolved", "native_conflict_requires_focused_review", item["reason"]))
            else:
                # 字符守卫：双候选显示 OCR 多出内容级字符时，无差异结论不得关闭问题；
                # 等价排印、公式乱码与原生缺失由 unresolved_surplus 分级放行。
                # 例外一：零补丁 unchanged 先过文字层仲裁，唯一短插入在 native
                # 归一化域证真后自动删除，视觉与文本路线同权。
                # 例外二：仲裁不成时，引用合格视觉证据的回执意味着模型已对原图
                # 逐字确认（native 覆盖不足的大缺口等形态），人工兜底由视觉替代。
                candidates = issue.get("candidates") or {}
                if candidates and unresolved_surplus(candidates.get("ocr", ""), candidates.get("native", "")):
                    # corrected+全等补丁与 unchanged+零补丁同属“无需修改”语义，
                    # 同样进文字层仲裁（真实链路 review 98512 单字符幻觉实证）。
                    no_change_shape = ((item["status"] == "unchanged" and not patches)
                                       or (item["status"] == "corrected" and bool(patches)
                                           and all(p.get("old") == p.get("new") for p in patches)))
                    arbitration = (_unchanged_native_arbitration(issue, block, vision_final=vision_final)
                                   if no_change_shape else None)
                    if arbitration is not None:
                        edits.append(arbitration["edit"])
                        outcomes.append(arbitration["outcome"])
                        vision_flags.add(issue["id"])
                    elif (visual and vision_final and item["status"] == "unchanged" and not patches
                            and any(entry.get("source") == "visual_region" and entry.get("quality") == "high" for entry in evidence)):
                        outcome = _outcome(issue, "checked", "vision_confirmed_no_change", item["reason"])
                        outcome["vision_final"] = True
                        outcomes.append(outcome)
                    else:
                        outcomes.append(_outcome(issue, "unresolved", "ocr_surplus_requires_human_review", item["reason"]))
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
        error = _rebuilt_error(block, before, after, visual=visual, issue=issue, targets=replacements,
                               vision_final=vision_final)
        typography_fix = _typography_exempt(error, before, after, visual=visual, vision_final=vision_final)
        if typography_fix:
            # 排版白名单：剥标点、空白与大小写后内容字符流完全一致，
            # 视觉终裁放行标点级差异；数学损伤与词界硬伤在上方已被拦截。
            error = ""
        if (error == "introduced_line_break" and visual and vision_final
                and "".join(before.split()) == "".join(after.split())):
            # 纯换行结构差异：剥换行后补丁与原文去空白逐字一致（入口剥 new
            # 换行的必达形态），模型未提出任何内容修改——按规则三语义自动
            # 保留 OCR 现状不占人工；去空白不等的混合意图仍转人工。
            outcome = _outcome(issue, "checked", "model_change_declined_kept", item["reason"])
            outcome["vision_final"] = True
            outcomes.append(outcome)
            continue
        if error:
            outcomes.append(_outcome(issue, "technical_failed", error))
            continue
        # 字符守卫同样约束部分修正路径：修正后正文仍多出内容级字符时不得关闭；
        # 包含型重锚定的整块 after 对区域级原生必然误报，改用诚实标签弹回人工。
        # 例外：视觉终裁开启时，纯删除登记 surplus 的部分修正可自动应用——删除集
        # 不超幻觉额度、直读字符层反证不触发才放行，残余 surplus 留审计可检索。
        candidates = issue.get("candidates") or {}
        surplus_after = unresolved_surplus(after, candidates.get("native", "")) if candidates else ""
        surplus_auto_fix = False
        deleted_chars = inserted_chars = ""
        if candidates and surplus_after:
            deleted_chars = _pure_deletion_chars(before, after) or ""
            replaced_deleted, inserted_chars = _replacement_chars(before, after)
            within = bool(replaced_deleted) and _within_registered_surplus(before, replaced_deleted, candidates.get("native", ""))
            refuted = bool(replaced_deleted) and _native_chars_prove_deletion(before, replaced_deleted, issue)
            proven_insert = bool(inserted_chars) and _native_chars_contain(issue, inserted_chars)
            surplus_auto_fix = (
                visual and vision_final
                and (
                    (bool(replaced_deleted) and not inserted_chars and within and not refuted)
                    # 规则三：替换型修正的对称证真——插入集在直读字符层
                    # 逐字符有存在证明，删除集不超登记 surplus 才放行。
                    or (bool(replaced_deleted) and bool(inserted_chars) and within and proven_insert and not refuted)
                )
            )
            if not surplus_auto_fix:
                if visual and vision_final and not issue.get("reanchored_containment"):
                    # 规则四：模型修改未获自动应用（插入无证、保险丝反证、
                    # 无字符层）时保留 OCR 现状，不再占用人工；含型重锚定
                    # 的误报结构不可靠，仍走诚实标签转人工。
                    outcome = _outcome(issue, "checked", "model_change_declined_kept", item["reason"])
                    outcome["vision_final"] = True
                    outcomes.append(outcome)
                else:
                    outcomes.append(_outcome(issue, "unresolved",
                                             "anchored_block_needs_human_review" if issue.get("reanchored_containment") else "ocr_surplus_requires_human_review",
                                             item["reason"]))
                continue
            if not deleted_chars:
                deleted_chars = replaced_deleted
        accepted.append((issue, replacements, item))
        if typography_fix:
            outcome = _outcome(issue, "machine_corrected", "typography_normalization", item["reason"])
            outcome["vision_final"] = True
            vision_flags.add(issue["id"])
        else:
            correction = "vision_final_surplus_correction"
            if surplus_auto_fix and inserted_chars:
                correction = "vision_final_replacement_correction"
            outcome = _outcome(issue, "machine_corrected", correction if surplus_auto_fix else "accepted", item["reason"])
            if surplus_auto_fix:
                outcome.update(vision_final=True, deleted_chars=deleted_chars, residual_surplus=surplus_after)
                if inserted_chars:
                    outcome["inserted_chars"] = inserted_chars
                vision_flags.add(issue["id"])
        if visual and vision_final and lost_scripts(scripts(before), scripts(after)):
            # 视觉终裁豁免了脚本作用域守卫，必须在回执上留下可追溯标记。
            outcome["vision_final"] = True
            if not _native_geometry_available(issue):
                outcome["geometry_reference"] = "unavailable"
            vision_flags.add(issue["id"])
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
    for block_id, entries in groups.items():
        block = by_id[block_id]
        before = str(block.get("content") or "")
        unique_targets = {(target["start"], target["end"]): target for _, targets, _ in entries for target in targets}
        after = _rebuild(before, list(unique_targets.values()))
        error = _rebuilt_error(block, before, after, visual=visual,
                               issue={"evidence": [entry for issue, _, _ in entries for entry in issue.get("evidence", [])]},
                               targets=list(unique_targets.values()), vision_final=vision_final)
        if _typography_exempt(error, before, after, visual=visual, vision_final=vision_final):
            # 合并重建与单问题路径同享排版白名单，豁免后不得再回落 technical_failed。
            error = ""
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
            "vision_final": any(issue["id"] in vision_flags for issue, _, _ in entries),
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


_TYPOGRAPHY_FOLD = re.compile(r"[^\w]+", re.UNICODE)
_REFERENCE_MARKER = re.compile(r" ?\[\d{1,3}\]")


def _typography_only_change(before: str, after: str) -> bool:
    """排版白名单判据：剥标点空白并小写归一后内容字符流必须完全一致。

    数学定界符数量必须一致：上标拍平类修改改变渲染语义，
    不属于标点/空白/大小写范畴，不得进白名单。
    """
    if before == after or before.count("$") != after.count("$"):
        return False
    fold = lambda text: _TYPOGRAPHY_FOLD.sub("", fold_equivalent_typography(text)).lower()
    return fold(before) == fold(after)


def _typography_exempt(error: str, before: str, after: str, *, visual: bool,
                       vision_final: bool) -> bool:
    """排版白名单豁免判定：标点守卫拦截 + 内容字符流不变 + 视觉终裁开启。

    单问题与合并重建两条校验路径都必须经过本判定，否则豁免会在合并
    重建时被二次校验覆盖回 technical_failed。
    """
    return (error == "punctuation_change_requires_native_evidence"
            and visual and vision_final and _typography_only_change(before, after))


def _replacement_chars(before: str, after: str) -> tuple[str, str]:
    """补丁效果的字符多重数差：返回（删除集，插入集），归一口径与守卫一致。"""
    folded_before = Counter(fold_equivalent_typography(before))
    folded_after = Counter(fold_equivalent_typography(after))
    deleted = folded_before - folded_after
    inserted = folded_after - folded_before
    return "".join(sorted(deleted.elements())), "".join(sorted(inserted.elements()))


def _native_chars_contain(issue: dict, inserted_chars: str) -> bool:
    """插入字符必须在原件直读字符层逐字符有存在证明（多重数额度）。"""
    folded = Counter(fold_equivalent_typography(inserted_chars))
    if not folded:
        return False
    texts = []
    for entry in issue.get("evidence", []):
        if entry.get("source") == "native":
            texts.append("".join(str(char.get("text") or "") for char in entry.get("chars") or []))
    layer = Counter(fold_equivalent_typography("".join(texts)))
    return bool(layer) and all(layer.get(char, 0) >= count for char, count in folded.items())


def _reference_marker_cleanup(issue: dict, block: dict | None, *, visual: bool,
                              vision_final: bool) -> dict | None:
    """unresolved 零补丁时的唯一 [数字] 引用标记清理；任何歧义或反证返回 None。

    判据三重锁：正文恰好一处匹配、native 层无该连续序列（真引用不清理）、
    删除后局部性与保护校验通过且不新增内容级 surplus。OCR 把行末文献
    编号误插正文是该规则唯一目标场景。
    """
    if not (visual and vision_final) or block is None:
        return None
    candidates = issue.get("candidates") or {}
    native = str(candidates.get("native") or "")
    before = str(block.get("content") or "")
    if not before or not native:
        return None
    registered = unresolved_surplus(before, native)
    if not registered or not any(char.isalnum() for char in registered):
        return None
    matches = list(_REFERENCE_MARKER.finditer(before))
    if len(matches) != 1:
        return None
    marker = re.sub(r"\s+", "", matches[0].group())
    texts = []
    for entry in issue.get("evidence", []):
        if entry.get("source") == "native":
            joined = "".join(str(char.get("text") or "") for char in entry.get("chars") or [])
            if joined:
                texts.append(joined)
    layer = fold_equivalent_typography("".join(texts)) if texts else fold_equivalent_typography(native)
    if marker in layer:
        return None
    after = before[:matches[0].start()] + before[matches[0].end():]
    if not preserves_protected(before, after) or validate_inline_change(before, after):
        return None
    residual = unresolved_surplus(after, native)
    if residual and any(char.isalnum() for char in residual) \
            and not _within_registered_surplus(before, marker, native):
        return None
    edit = {
        "block_id": block["id"], "before": before, "after": after, "page": block.get("page"),
        "bbox": block.get("bbox"), "kind": block.get("type"), "status": "machine_corrected",
        "critical": critical_signature(before) != critical_signature(after),
        "issue_id": issue["id"], "issue_ids": [issue["id"]],
        "reason": "唯一 [数字] 引用标记在 native 层无连续序列，自动清理误插",
        "evidence_ids": [], "source": "model", "vision_final": True,
    }
    outcome = _outcome(issue, "machine_corrected", "reference_marker_cleanup",
                       "模型无法核实但 native 层证明该引用标记为误插，已自动清理并保留审计")
    outcome.update(vision_final=True, deleted_chars=marker, residual_surplus=residual)
    return {"edit": edit, "outcome": outcome}


_ARBITRATION_MAX_CHARS = 12


def _unchanged_native_arbitration(issue: dict, block: dict | None, *,
                                  vision_final: bool) -> dict | None:
    """无需修改语义回执（unchanged+零补丁 或 corrected+全等补丁）的文字层仲裁。

    模型逐字确认无差异但字符守卫仍检出内容级 surplus 时，要求归一化文字层
    diff 恰好收敛为一个 ≤12 字符且含字母数字的多出块（OCR 比 native 多，
    即 delete 块），且在 OCR 原文中删除位置唯一（相邻同字符删除效果等价
    视作唯一）、删除后归一化恰等于 native、保护与局部性校验通过，才执行
    machine_corrected；任何歧义返回 None，交回视觉终裁或人工兑底。目标场
    景是模型无差异结论漏检的引用标记误插与词内单字符幻觉（review 98512
    实证 corrected+全等形态同样会漏检）。
    """
    if not vision_final or block is None:
        return None
    candidates = issue.get("candidates") or {}
    before = str(block.get("content") or "")
    ocr_seg = str(candidates.get("ocr") or "")
    native_seg = str(candidates.get("native") or "")
    if not before or not ocr_seg or not native_seg:
        return None
    norm_ocr = normalized_visible_text(ocr_seg)
    norm_native = normalized_visible_text(native_seg)
    if not norm_ocr or not norm_native:
        return None
    opcodes = difflib.SequenceMatcher(None, norm_ocr, norm_native).get_opcodes()
    surplus_blocks = [norm_ocr[i1:i2] for tag, i1, i2, _, _ in opcodes if tag == "delete"]
    if len(surplus_blocks) != 1 or any(tag in {"insert", "replace"} for tag, *_ in opcodes):
        return None
    surplus_text = surplus_blocks[0]
    if len(surplus_text) > _ARBITRATION_MAX_CHARS or not any(ch.isalnum() for ch in surplus_text):
        return None
    valid = [m.start() for m in re.finditer(re.escape(surplus_text), ocr_seg)
             if normalized_visible_text(ocr_seg[:m.start()] + ocr_seg[m.start() + len(surplus_text):]) == norm_native]
    if not valid or len({ocr_seg[:q] + ocr_seg[q + len(surplus_text):] for q in valid}) > 1:
        return None
    seg_start = before.find(ocr_seg)
    if seg_start < 0 or seg_start != before.rfind(ocr_seg):
        return None
    # 删除区间精确对齐多出块；邻接空白不并入（词界校验禁止吞原文词间空格）。
    start, end = seg_start + valid[0], seg_start + valid[0] + len(surplus_text)
    after = before[:start] + before[end:]
    if not preserves_protected(before, after) or validate_inline_change(before, after):
        return None
    edit = {
        "block_id": block["id"], "before": before, "after": after, "page": block.get("page"),
        "bbox": block.get("bbox"), "kind": block.get("type"), "status": "machine_corrected",
        "critical": critical_signature(before) != critical_signature(after),
        "issue_id": issue["id"], "issue_ids": [issue["id"]],
        "reason": "unchanged 回执经 native 文字层仲裁证明唯一多出内容为 OCR 误插，自动删除",
        "evidence_ids": [], "source": "model", "vision_final": True,
    }
    outcome = _outcome(issue, "machine_corrected", "vision_final_native_arbitration",
                       "模型确认无差异，native 文字层证明该唯一多出内容为 OCR 误插，已自动删除并保留审计")
    outcome.update(vision_final=True, deleted_chars=surplus_text)
    return {"edit": edit, "outcome": outcome}


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
                   issue: dict, targets: list[dict], vision_final: bool = False) -> str:
    """重建后复用完整块局部性和保护校验，文字路线保留全部科学 token。"""
    if not preserves_protected(before, after):
        # 保护内容逐字守恒而换行/缩进失衡时，代码、链接本身未被触碰，
        # 换用精确标签避免误导用户去找不存在的代码/链接差异。
        if whitespace_structure_divergence(before, after):
            return "introduced_line_break"
        return "protected_content_changed"
    error = validate_inline_change(
        before, after,
        script_evidence=_native_baseline_proof(issue, targets),
        punctuation_evidence=_native_replacement_proof(issue, targets),
    )
    if error == "script_scope_loss_requires_native_geometry":
        if not (visual and vision_final):
            return error
        # 视觉终裁：原生几何证明被删脚本真实存在时仍拒绝，否则豁免放行。
        if _vision_final_geometry_conflict(issue, before, after):
            return "vision_final_geometry_conflict"
        error = ""
    if error:
        return error
    if not visual and critical_signature(before) != critical_signature(after):
        return "scientific_change_requires_vision"
    if not visual and not is_local_candidate(before, after):
        return "native_absence_cannot_delete_words"
    valid, _ = validate_review_patch([block], {"edits": [{"block_id": block["id"], "before": before, "after": after}], "unresolved": []})
    return "" if valid else "full_block_local_validation_failed"


def _complete_changes(
    before: str, candidate: str, *, extra_spans: list[tuple[int, int]] | None = None
) -> list[tuple[int, int, int, int]]:
    """扩展原文跨度后沿同一对齐投影候选，禁止把公式碎片当完整目标。"""
    opcodes = difflib.SequenceMatcher(a=before, b=candidate, autojunk=False).get_opcodes()
    spans = [
        (a, b) for op, a, b, c, d in opcodes
        if op != "equal" and (before[a:b].strip() or candidate[c:d].strip())
    ]
    spans.extend(extra_spans or [])
    spans = expand_target_spans(before, spans)
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


def _vision_final_geometry_conflict(issue: dict, before: str, after: str) -> bool:
    """视觉终裁保险丝：任一被删脚本被原生字符几何证明真实存在即拒绝放行。"""
    lost = lost_scripts(scripts(before), scripts(after))
    if not lost:
        return False
    for entry in issue.get("evidence", []):
        if entry.get("source") != "native":
            continue
        for located in _located_scripts(entry.get("chars") or [], lost):
            if located["anchor"] is not None:
                peer_size, peer_origin = located["anchor"]["font_size"], located["anchor"]["origin_normal"]
            else:
                peer_size, peer_origin = located["peer"]
            if any(char["font_size"] / peer_size <= 0.9
                   and abs(char["origin_normal"] - peer_origin) >= 0.15 * peer_size
                   for char in located["chars"]):
                return True
    return False


def _located_scripts(chars: list, wanted: list[tuple[str, str, str]]) -> list[dict]:
    """在行级字符序列中定位每个脚本的出现，返回字符子集、基字与行内参照。

    原生 chars 的 text 可能是多字符片段，逐字符展开保证 compact 索引与
    列表切片一致；每个字符继承所属片段的实测几何。
    """
    indexed = []
    for char in chars:
        if not isinstance(char, dict):
            continue
        for text in str(char.get("text") or ""):
            indexed.append((text, char))
    if not indexed:
        return []
    compact = "".join(text for text, _ in indexed)
    measured_all = [char for _, char in indexed if _measurable(char)]
    located = []
    for role, value, base in wanted:
        for start, anchor in _script_candidates(compact, indexed, value, base):
            pieces, seen = [], set()
            for _, char in indexed[start:start + len(value)]:
                if id(char) not in seen:
                    seen.add(id(char))
                    pieces.append(char)
            measured = [char for char in pieces if _measurable(char)]
            if not measured:
                continue
            measured_ids = {id(char) for char in measured}
            peers = _peer_geometry([char for char in measured_all if id(char) not in measured_ids])
            located.append({"role": role, "value": value, "chars": measured,
                            "anchor": anchor if _measurable(anchor) else None, "peer": peers})
    return located


def _script_candidates(compact: str, indexed: list, value: str, base: str) -> list[tuple[int, dict | None]]:
    """返回脚本参数每个候选出现的起始位置；基字可锚定时只取其后首个匹配，否则全部出现。"""
    if not value:
        return []
    if base:
        name, _, occurrence = base.partition(":")
        anchors = list(re.finditer(r"(?<![A-Za-z])" + re.escape(name) + r"(?![A-Za-z])", compact))
        index = int(occurrence) - 1 if occurrence.isdigit() else 0
        if 0 <= index < len(anchors):
            hit = anchors[index]
            start = compact.find(value, hit.end(), hit.end() + len(value) + 6)
            if start >= 0:
                return [(start, indexed[hit.start()][1])]
    return [(match.start(), None) for match in re.finditer(re.escape(value), compact)]


def _measurable(char) -> bool:
    """字符具备可用的字号与基线实测值。"""
    return (isinstance(char, dict)
            and isinstance(char.get("font_size"), (int, float)) and char["font_size"] > 0
            and isinstance(char.get("origin_normal"), (int, float)) and math.isfinite(char["origin_normal"]))


def _peer_geometry(chars: list[dict]) -> tuple[float, float]:
    """行内参照几何：有效字符的字号与基线中位数；无参照时退回 1/0 使判定不冲突。"""
    if not chars:
        return 1.0, 0.0
    return median(char["font_size"] for char in chars), median(char["origin_normal"] for char in chars)


def _pure_deletion_chars(before: str, after: str) -> str | None:
    """补丁效果为纯删除时返回被删字符序列供审计，含插入或替换返回 None。"""
    deleted = []
    for op, a_start, a_end, _b_start, _b_end in difflib.SequenceMatcher(a=before, b=after, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        if op != "delete":
            return None
        deleted.append(before[a_start:a_end])
    return "".join(deleted)


def _within_registered_surplus(before: str, deleted_chars: str, native: str) -> bool:
    """删除集（归一可见流多重数）不得超出登记 surplus，防止夹带真字符。"""
    deleted = Counter(fold_equivalent_typography(deleted_chars))
    if not deleted:
        return False
    available = Counter(fold_equivalent_typography(unresolved_surplus(before, native)))
    return all(available.get(char, 0) >= count for char, count in deleted.items())


def _native_chars_prove_deletion(before: str, deleted_chars: str, issue: dict) -> bool:
    """保险丝：删除字符超出原件直读字符层的缺失额度即拒绝。

    直读字符拼接的多重数是原件字符的存在证明，OCR 文本相对它多出的部分
    才是可安全删除的幻觉额度；无可用字符层时无法反证，一律维持转人工。
    """
    folded_deleted = Counter(fold_equivalent_typography(deleted_chars))
    if not folded_deleted:
        return False
    texts = []
    for entry in issue.get("evidence", []):
        if entry.get("source") == "native":
            texts.append("".join(str(char.get("text") or "") for char in entry.get("chars") or []))
    layer = Counter(fold_equivalent_typography("".join(texts)))
    if not layer:
        return True
    budget = Counter(fold_equivalent_typography(before)) - layer
    return any(count > budget.get(char, 0) for char, count in folded_deleted.items())


def _native_geometry_available(issue: dict) -> bool:
    """问题是否携带至少一个可测几何的原生字符，供豁免审计标注。"""
    for entry in issue.get("evidence", []):
        if entry.get("source") == "native" and any(
                isinstance(char, dict) and _measurable(char) for char in entry.get("chars") or []):
            return True
    return False


def _native_geometry(issue: dict) -> dict | None:
    """汇总问题全部脚本的原生实测几何，作为中性参照随视觉请求下发。"""
    wanted = scripts(str(issue.get("before") or ""))
    if not wanted:
        return None
    observations = []
    for entry in issue.get("evidence", []):
        if entry.get("source") != "native":
            continue
        for located in _located_scripts(entry.get("chars") or [], wanted):
            if located["anchor"] is not None:
                peer_size, peer_origin = located["anchor"]["font_size"], located["anchor"]["origin_normal"]
            else:
                peer_size, peer_origin = located["peer"]
            observations.append({
                "role": located["role"], "value": located["value"],
                "font_size": round(median(char["font_size"] for char in located["chars"]), 2),
                "peer_font_size": round(peer_size, 2),
                "baseline_offset": round(median(char["origin_normal"] for char in located["chars"]) - peer_origin, 2),
            })
    return {"scripts": observations} if observations else None


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
