"""从最终稿中寻找有同栏原文和前后文证据的局部差异。"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
from difflib import SequenceMatcher
from itertools import accumulate

from yuxi.knowledge.parser.pdf_recognition_source import printed_source_rows, same_printed_source_line
from yuxi.knowledge.parser.pdf_repair_rules import damaged_text, overlap_fraction, preserves_protected, protected_spans

_PAGE_BOX = [0.0, 0.0, 1.0, 1.0]
_MAX_BLOCK = 6_000
_PAGE_CHROME = {"page_header", "page_footer"}
_IMAGE_KINDS = {"picture", "chemical_structure"}
_SCRIPT_GLYPH = re.compile(r"[²³¹⁰-ⁿ₀-ₜ]|[\^_]\{")
_SCIENTIFIC_TOKEN = re.compile(r"[A-Za-z0-9²³¹⁰-ⁿ₀-ₜ·+−-]+")
_TEXT_STYLES = str.maketrans({
    "。": ".", "−": "-", "–": "-", "‐": "-", "‑": "-", "\x02": "-", "\x03": "-",
    "“": '"', "”": '"', "‘": "'", "’": "'", "■": "", "\u00ad": "",
})
_NUMERIC_CITATION = re.compile(r"\[[0-9]+(?:[,，\-–−][0-9]+)*\]")


def check_final_source_page(page: int, blocks: list[dict], native: list[dict]) -> list[dict]:
    """只把可定位的局部文字差异交给终审，复杂区域由模型直接看原图。"""
    excluded = [
        block["bbox"] for block in blocks
        if block["type"] in _PAGE_CHROME | _IMAGE_KINDS | {"formula", "table"} and block.get("bbox")
    ]
    sources = []
    for row in printed_source_rows(native):
        box = row["bbox"]
        key, positions = _comparison(row["content"])
        if (
            not key or damaged_text(key)
            or any(overlap_fraction(box, region) >= 0.8 for region in excluded)
        ):
            continue
        sources.append((row, key, positions))

    findings = []
    for block in blocks:
        if block["type"] not in {"text", "title"}:
            continue
        key, positions = _comparison(block["content"])
        nearby = [source for source in sources if _near_block(source[0]["bbox"], block.get("bbox"))]
        aligned = _align(key, [source[1] for source in nearby]) if nearby else None
        if aligned is None:
            continue
        offsets, start, end, evidence = aligned
        if evidence == key:
            continue
        boxes = [
            row["bbox"] for (row, _, _), left, right in zip(nearby, offsets[:-1], offsets[1:], strict=True)
            if left < end and right > start
        ]
        if any(
            (_column_overlap(left, right) < 0.45 or right[1] < left[1] - 0.02)
            and not same_printed_source_line(left, right)
            for left, right in zip(boxes, boxes[1:])
        ):
            continue
        if finding := _text_finding(page, block, key, positions, evidence, nearby, offsets, start):
            findings.append(finding)
    return findings


def check_final_page_joins(blocks: list[dict]) -> list[dict]:
    """登记跨页接续中页序、段落相邻关系或句尾证据冲突的项。"""
    positions = {block["id"]: i for i, block in enumerate(blocks)}
    findings = []
    for i, block in enumerate(blocks):
        join = block.get("recognition_join")
        if not join:
            continue
        prior = positions.get(join["block_id"], i)
        previous = blocks[prior]
        closed = previous["content"].rstrip(" \n\t”’\"')]").endswith(("。", "！", "？", ".", "!", "?"))
        between = blocks[prior + 1:i]
        separated = any(item["type"] not in _PAGE_CHROME | _IMAGE_KINDS | {"caption"} for item in between)
        old_box, new_box = previous.get("bbox"), block.get("bbox")
        away_from_edge = (old_box and old_box[3] < 0.65) or (new_box and new_box[1] > 0.40)
        joined_words = not join["separator"] and re.search(r"[A-Za-z]$", previous["content"].rstrip()) and re.match(
            r"[A-Za-z]", block["content"].lstrip(),
        )
        valid_seam = prior < i and previous["page"] + 1 == block["page"] and not (
            closed or separated or away_from_edge
        )
        if valid_seam and not joined_words:
            continue
        row = _finding(
            block["page"], f"{block['id']}-join", block.get("bbox") or _PAGE_BOX,
            "跨页接续的位置、句尾或词间分隔需要核对，请确认是否保留接续", block,
        )
        row.update(kind="model_recognition_seam", recognition_split=join)
        if valid_seam and joined_words:
            row["recognition_join"] = {**join, "separator": " "}
        if prior < i:
            row["previous_region"] = {"page": previous["page"], "bbox": previous.get("bbox") or _PAGE_BOX}
        findings.append(row)
    return findings


def _comparison(text: str) -> tuple[str, list[int]]:
    """统一排印字形并保留原串索引，引文标点和数字仍参与比较。"""
    key, positions = [], []
    for index, char in enumerate(text):
        if char == "\ufffe" and index and re.fullmatch(r"[A-Za-z]", text[index - 1]) and (
            index + 1 == len(text) or re.match(r"[A-Za-z]", text[index + 1])
        ):
            continue
        for visible in unicodedata.normalize("NFKC", char).translate(_TEXT_STYLES):
            if not visible.isspace():
                key.append(visible)
                positions.append(index)
    return "".join(key), positions


def _near_block(row: list, block: list | None) -> bool:
    """以栏宽限制横向匹配，容纳模型框的轻微纵向偏移。"""
    if not block:
        return False
    x0, y0, x1, y1 = block
    return overlap_fraction(row, [x0 - 0.025, y0 - 0.10, x1 + 0.025, y1 + 0.10]) >= 0.75


def _align(key: str, rows: list[str]) -> tuple | None:
    """跨同栏行连接上下文，以重复无歧义的前后锚点界定原文范围。"""
    offsets = list(accumulate(map(len, rows), initial=0))
    if not key or len(key) > _MAX_BLOCK or offsets[-1] > _MAX_BLOCK * 2:
        return None
    native = "".join(rows)
    if (exact := native.find(key)) >= 0:
        return (offsets, exact, exact + len(key), key) if exact == native.rfind(key) else None
    anchors = [
        match for match in SequenceMatcher(None, native, key, autojunk=False).get_matching_blocks()
        if match.size >= min(5, len(key))
    ]
    if sum(match.size for match in anchors) < max(min(len(key), 5), len(key) * 0.70):
        return None
    first, last = anchors[0], anchors[-1]
    for anchor in (first, last):
        sample = native[anchor.a:anchor.a + anchor.size]
        if native.find(sample) != native.rfind(sample):
            return None
    last_end = last.a + last.size
    return offsets, first.a, last_end, native[first.a:last_end]


def _text_finding(page, block, key, positions, evidence, sources, offsets, base) -> dict | None:
    """用两侧文字锁定局部候选，保留未参与改动的原段。"""
    before = block["content"]
    protected = protected_spans(before, include_math=True) + [
        match.span() for match in _SCIENTIFIC_TOKEN.finditer(before) if _SCRIPT_GLYPH.search(match[0])
    ]
    patches, boxes, details = [], [], []
    changes = SequenceMatcher(None, key, evidence, autojunk=False).get_opcodes()
    for previous, change, following in zip(changes, changes[1:], changes[2:]):
        op, a, b, c, d = change
        if (
            op == "equal" or previous[0] != "equal" or following[0] != "equal"
            or a - previous[1] < 3 or following[2] - b < 3 or max(b - a, d - c) > 12
        ):
            continue
        first = bisect_right(offsets, base + c) - 1
        last = bisect_right(offsets, base + max(c, d - 1)) - 1
        if first != last:
            continue
        row, _, source_positions = sources[first]
        left = positions[a]
        right = positions[b - 1] + 1 if a < b else left
        old = before[left:right]
        patch = ""
        if c < d:
            start = base + c - offsets[first]
            end = base + d - offsets[first]
            patch = row["content"][source_positions[start]:source_positions[end - 1] + 1]
        citation = any(match.start() < a <= b < match.end() for match in _NUMERIC_CITATION.finditer(key))
        if (
            _comparison(old)[0] != key[a:b]
            or not re.fullmatch(r"[\w .,:;()[\]%+−·-]*", old + patch)
            or _SCRIPT_GLYPH.search(old + patch)
            or any(left < end and right > start for start, end in protected)
            or not (any(char.isalnum() for char in old + patch) or citation and (old + patch) in {",", "，"})
        ):
            continue
        if citation and not patch and old == "," and before[right:positions[b]].isspace():
            right = positions[b]
        boxes.append(row["bbox"])
        patches.append((left, right, patch))
        details.append({
            "current": old, "original": patch, "citation": citation,
            "left": before[max(0, left - 20):left], "right": before[right:right + 20],
        })
    if not patches:
        return None
    candidate = before
    for left, right, patch in reversed(patches):
        candidate = candidate[:left] + patch + candidate[right:]
    if candidate == before or not preserves_protected(before, candidate):
        return None
    detail = details[0]
    left, right = detail["left"][-8:], detail["right"][:8]
    shown = f"「{left}{detail['current']}{right}」"
    expected = f"「{left}{detail['original']}{right}」"
    reason = f"同页原文候选为{expected}，成稿为{shown}，请对照原图。"
    if len(details) > 1:
        reason += f"这一段共有 {len(details)} 处待核对文字。"
    row = _finding(page, block["id"], _bounds(boxes), reason, block)
    row.update(candidates={"native": candidate}, final_source_details=details)
    return row


def _column_overlap(left: list, right: list) -> float:
    """用较窄一侧的宽度衡量两处是否仍属同一栏。"""
    width = min(left[2] - left[0], right[2] - right[0])
    overlap = min(left[2], right[2]) - max(left[0], right[0])
    return max(0.0, overlap) / width if width > 0 else 0.0


def _bounds(boxes: list[list]) -> list[float]:
    """合并同处原文的定位框以生成校对裁图。"""
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _finding(page, suffix, box, reason, block=None) -> dict:
    """使用已有人工校对记录契约保存最终核对结果。"""
    x0, y0, x1, y1 = box
    return {
        "id": f"final-source-{page}-{suffix}",
        "kind": "text_mismatch",
        "status": "unresolved",
        "page": page,
        "block_id": block["id"] if block else None,
        "before": block["content"] if block else "",
        "bbox": [max(0, x0 - 0.012), max(0, y0 - 0.008), min(1, x1 + 0.012), min(1, y1 + 0.008)],
        "reason": reason,
    }
