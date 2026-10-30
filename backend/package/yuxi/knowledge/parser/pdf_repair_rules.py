"""证据约束的块补丁；借鉴 pdf-md-quad-repair 2.7.7 保护区与事务机制。

只移植内存规则，不执行原 skill CLI、宽松摘句校验或任意正则规则。
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections import Counter

from yuxi.knowledge.parser.pdf_review import validate_review_patch


def validate_evidence_patch(
    blocks: list[dict], payload, issues: list[dict], *, visual: bool
) -> tuple[list[dict], list[dict]]:
    """校验登记问题、块快照和同区域证据，返回补丁而不应用。"""
    if not isinstance(payload, dict) or set(payload) != {"edits", "unresolved"}:
        return [], [{"kind": "invalid_patch_payload", "reason": "必须包含 edits/unresolved 数组"}]
    if not isinstance(payload["edits"], list) or not isinstance(payload["unresolved"], list):
        return [], [{"kind": "invalid_patch_payload", "reason": "edits/unresolved 必须为数组"}]
    unresolved_ids = [item.get("issue_id") for item in payload["unresolved"] if isinstance(item, dict) and isinstance(item.get("issue_id"), str)]
    edited_ids = {item.get("issue_id") for item in payload["edits"] if isinstance(item, dict) and isinstance(item.get("issue_id"), str)}
    if len(unresolved_ids) != len(set(unresolved_ids)) or edited_ids.intersection(unresolved_ids):
        return [], [{"kind": "invalid_patch_payload", "reason": "同一问题不能重复或同时修订与未决"}]
    by_id = {block["id"]: block for block in blocks}
    registered = {issue["id"]: issue for issue in issues}
    counts = Counter(item.get("block_id") for item in payload["edits"] if isinstance(item, dict) and isinstance(item.get("block_id"), str))
    edits, pending = [], []
    allowed = {"block_id", "issue_id", "evidence_ids", "before", "after", "reason", "kind", "confidence", "critical"}
    if len(payload["edits"]) > min(1000, len(blocks) * 2 + 10) or len(payload["unresolved"]) > 1000:
        return [], [{"kind": "invalid_patch_payload", "reason": "补丁数量超过上限"}]
    for item in payload["edits"]:
        block = by_id.get(item.get("block_id")) if isinstance(item, dict) and isinstance(item.get("block_id"), str) else None
        issue = registered.get(item.get("issue_id")) if isinstance(item, dict) and isinstance(item.get("issue_id"), str) else None
        reason = ""
        if not isinstance(item, dict) or set(item) - allowed:
            reason = "补丁含未知字段或不是对象"
        elif not block or not issue or issue.get("block_id") != block["id"]:
            reason = "补丁必须绑定已登记问题和目标块"
        elif counts[block["id"]] != 1:
            reason = "同一块的多个补丁全部拒绝"
        elif issue.get("status") not in {"unresolved", "not_checked"}:
            reason = "问题已处理，不允许再次修改"
        elif issue.get("before") != block.get("content", "") or item.get("before") != issue.get("before"):
            reason = "问题快照或 before 已过期"
        elif not isinstance(item.get("evidence_ids"), list) or not item["evidence_ids"] or any(not isinstance(value, str) for value in item["evidence_ids"]):
            reason = "必须引用程序登记的 evidence_ids"
        elif not set(item["evidence_ids"]).issubset(set(issue.get("evidence_ids", []))):
            reason = "补丁引用了问题之外的证据"
        elif block.get("type") not in {"text", "caption", "title", "footnote", "formula", "chemical_equation"}:
            reason = "此类块不能由文字补丁修改"
        if reason:
            pending.append({"block_id": block.get("id") if block else None, "kind": "rejected_patch", "reason": reason})
            continue
        stripped = {key: value for key, value in item.items() if key not in {"issue_id", "evidence_ids"}}
        valid, refused = validate_review_patch([block], {"edits": [stripped], "unresolved": []})
        pending.extend(refused)
        if not valid:
            continue
        before, after = item["before"], item["after"]
        evidence = [entry for entry in issue.get("evidence", []) if entry.get("id") in item["evidence_ids"]]
        same_region = evidence and len(evidence) == len(set(item["evidence_ids"])) and covers_block_region(block.get("bbox"), evidence) and all(
            entry.get("page") == block["page"] and overlap_fraction(entry.get("bbox"), block.get("bbox")) >= 0.85
            for entry in evidence
        )
        if not same_region:
            reason = "证据没有唯一绑定目标区域"
        elif not preserves_protected(before, after):
            reason = "补丁修改了代码、HTML、链接或原图保护区"
        elif visual:
            if not any(entry.get("source") == "visual_region" and entry.get("quality") == "high" for entry in evidence):
                reason = "视觉补丁缺少程序登记的原页区域证据"
        elif critical_signature(before) != critical_signature(after):
            reason = "文字模型不能改变数字、单位、符号、化学式或上下标"
        elif set(item["evidence_ids"]) != {entry["id"] for entry in issue.get("evidence", []) if entry.get("source") == "native"}:
            reason = "文字补丁必须引用完整原生行集合，不可挑选摘句删除中部"
        elif not is_local_candidate(before, after):
            reason = "文字补丁不得删除词句或改写整段"
        elif not all(entry.get("quality") == "high" and entry.get("source") == "native" for entry in evidence):
            reason = "文字补丁需要高质量原生证据"
        elif layout_text(after) != layout_text("\n".join(entry["content"] for entry in evidence)):
            reason = "after 必须精确选择同区域原生候选，摘句存在不能证明任意改写"
        if reason:
            pending.append({"block_id": block["id"], "issue_id": issue["id"], "kind": "rejected_patch", "reason": reason})
            continue
        edits.append({**valid[0], "issue_id": issue["id"], "evidence_ids": item["evidence_ids"], "source": "model"})
    for item in payload["unresolved"]:
        issue = registered.get(item.get("issue_id")) if isinstance(item, dict) and isinstance(item.get("issue_id"), str) else None
        if issue and item.get("block_id") == issue.get("block_id"):
            pending.append({"block_id": issue["block_id"], "issue_id": issue["id"], "kind": "model_unresolved", "reason": str(item.get("reason") or "模型未能确认")[:1000]})
        else:
            pending.append({"kind": "invalid_patch_payload", "reason": "unresolved 必须绑定已登记问题和目标块"})
    return edits, pending


def apply_block_edits(blocks: list[dict], edits: list[dict]) -> bool:
    """先校验全部 before，再原子应用同组受控文字补丁。"""
    by_id = {block["id"]: block for block in blocks}
    if len({edit["block_id"] for edit in edits}) != len(edits):
        return False
    for edit in edits:
        block = by_id.get(edit["block_id"])
        if not block or block.get("content", "") != edit["before"] or not preserves_protected(edit["before"], edit["after"]):
            return False
    for edit in edits:
        by_id[edit["block_id"]]["content"] = edit["after"]
    return True


def layout_text(text: str) -> str:
    """只规整空白，不消除大小写、数字、标点或数学符号。"""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()


def damaged_text(text: str) -> bool:
    """发现控制、私用、替换和未分配字形，不猜测对应文字。"""
    return any(char == "\ufffd" or unicodedata.category(char) in {"Co", "Cs", "Cn"} or (unicodedata.category(char) == "Cc" and char not in "\n\r\t") for char in text)


def overlap_fraction(inner, outer) -> float:
    """按原生区域面积计算覆盖率，坐标仍使用现有 PDFium 契约。"""
    if not inner or not outer:
        return 0.0
    x0, y0, x1, y1 = inner
    a0, b0, a1, b1 = outer
    area = max(0, x1 - x0) * max(0, y1 - y0)
    intersection = max(0, min(x1, a1) - max(x0, a0)) * max(0, min(y1, b1) - max(y0, b0))
    return intersection / area if area else 0.0


def covers_block_region(block_bbox, evidence: list[dict]) -> bool:
    """原生行集合必须覆盖块的二维范围，禁止短行证明整段替换。"""
    boxes = [entry.get("bbox") for entry in evidence]
    if not block_bbox or not boxes or not all(boxes):
        return False
    envelope = [min(box[0] for box in boxes), min(box[1] for box in boxes), max(box[2] for box in boxes), max(box[3] for box in boxes)]
    return overlap_fraction(block_bbox, envelope) >= 0.85 and overlap_fraction(envelope, block_bbox) >= 0.85


def protected_spans(text: str, *, include_math: bool = False) -> list[tuple[int, int]]:
    """保护代码、HTML、图片和链接，未闭合围栏保护到块末。"""
    spans = []
    fence = None
    offset = 0
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})", line)
        if marker and fence is None:
            fence = (marker[1][0], len(marker[1]), offset)
        elif marker and fence and marker[1][0] == fence[0] and len(marker[1]) >= fence[1] and not line[marker.end():].strip():
            spans.append((fence[2], offset + len(line)))
            fence = None
        elif re.match(r"^(?: {4}|\t)", line):
            spans.append((offset, offset + len(line)))
        offset += len(line)
    if fence:
        spans.append((fence[2], len(text)))
    patterns = [r"<!--.*?(?:-->|\Z)", r"(?P<t>`+).*?(?P=t)", r"<(?:script|style|pre|code|table)\b.*?(?:</(?:script|style|pre|code|table)>|\Z)", r"</?[A-Za-z][^>]*>", r"!?\[[^\]]*\]\([^)]*\)", r"(?:https?://|asset://)\S+"]
    if include_math:
        patterns.extend([r"(?<!\\)\$\$.*?(?:(?<!\\)\$\$|\Z)", r"(?<![\\$])\$(?!\$).*?(?:(?<!\\)\$|\Z)", r"\\\[.*?(?:\\\]|\Z)", r"\\\(.*?(?:\\\)|\Z)"])
    for pattern in patterns:
        spans.extend((match.start(), match.end()) for match in re.finditer(pattern, text, re.S | re.I))
    return spans


def preserves_protected(before: str, after: str) -> bool:
    """要求保护区逐字且按出现次数守恒，拒绝插入新保护内容。"""
    return Counter(before[start:end] for start, end in protected_spans(before)) == Counter(after[start:end] for start, end in protected_spans(after))


def critical_signature(text: str) -> list[str]:
    """保守保留数值、科学 token 与符号，普通词拼写允许精确候选修复。"""
    numeric = r"(?<![\w.])[+−-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+−-]?\d+)?"
    units = r"\b(?:[munpkMGTµμ]?(?:g|mol|J|L|m|s)|kcal|cal|eV|Hz|Pa|bar|W|V|A|K)\b"
    pattern = rf"\\[A-Za-z]+|[_^](?:\{{[^}}]*\}}|\w)|{numeric}|[A-Za-z]*[0-9₀-₉⁰-⁹][A-Za-z0-9₀-₉⁰-⁹]*|\b[A-Z][A-Za-z]{{0,2}}\b|\b[A-Z][A-Za-z]?(?:[A-Z][A-Za-z]?)+\b|{units}|[α-ωΑ-Ωₐ-₟ⁱⁿµ°%]|[^\w\s,.;:!?()\[\]'\"，。；：！？、]"
    return re.findall(pattern, text)


def is_local_candidate(before: str, after: str) -> bool:
    """只接受少量局部差异，不把全段候选选择变成任意重写。"""
    if len(after.split()) < len(before.split()):
        return False
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    changes = [(i2 - i1, j2 - j1) for op, i1, i2, j1, j2 in matcher.get_opcodes() if op != "equal"]
    return bool(changes) and len(changes) <= 4 and sum(max(a, b) for a, b in changes) <= 24 and matcher.ratio() >= 0.80
