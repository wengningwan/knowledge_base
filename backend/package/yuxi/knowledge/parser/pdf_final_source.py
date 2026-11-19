"""用最终正文的位置双向核对 PDF，产出需人工确认的差异。"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
from collections import defaultdict
from difflib import SequenceMatcher
from itertools import accumulate
from statistics import median

from yuxi.knowledge.parser.pdf_recognition_source import printed_source_rows, same_printed_source_line
from yuxi.knowledge.parser.pdf_repair_rules import damaged_text, overlap_fraction, preserves_protected, protected_spans

_PAGE_BOX = [0.0, 0.0, 1.0, 1.0]
_MAX_BLOCK = 6_000
_MAX_FINDINGS = 12
_PAGE_CHROME = {"page_header", "page_footer"}
_IMAGE_KINDS = {"picture", "chemical_structure"}
_SCRIPT_GLYPH = re.compile(r"[²³¹⁰-ⁿ₀-ₜ]|[\^_]\{")
_SCIENTIFIC_TOKEN = re.compile(r"[A-Za-z0-9²³¹⁰-ⁿ₀-ₜ·+−-]+")
_TEXT_STYLES = str.maketrans({
    "。": ".", "−": "-", "–": "-", "‐": "-", "‑": "-", "\x02": "-", "\x03": "-",
    "“": '"', "”": '"', "‘": "'", "’": "'", "■": "", "\u00ad": "",
})


def check_final_source_page(page: int, blocks: list[dict], native: list[dict], images: list[dict]) -> list[dict]:
    """逐块检查最终文字，并登记未被任何已核对位置覆盖的原文。"""
    rows = printed_source_rows(native)
    chrome = [
        (_comparison(block["content"])[0], block["bbox"])
        for block in blocks if block["type"] in _PAGE_CHROME and block.get("bbox")
    ]
    sources = []
    keys = []
    source_positions = []
    for row in rows:
        key, positions = _comparison(row["content"])
        _, top, _, bottom = row["bbox"]
        confirmed_chrome = (bottom <= 0.08 or top >= 0.93) and any(
            key == text and overlap_fraction(row["bbox"], region) >= 0.75 for text, region in chrome
        )
        if not key or confirmed_chrome:
            continue
        sources.append(row)
        keys.append(key)
        source_positions.append(positions)

    if not any(not damaged_text(key) and any(char.isalnum() for char in key) for key in keys):
        return [_finding(page, "page", _PAGE_BOX, "本页缺少可靠的 PDF 文字，请对照整页原图核对正文和遗漏")]

    claims = defaultdict(list)
    findings = []
    visual_regions = []
    script_regions = []
    previous = None
    for block in blocks:
        kind = block["type"]
        if kind in _PAGE_CHROME | _IMAGE_KINDS:
            continue
        before = block.get("content", "")
        box = block.get("bbox") or _PAGE_BOX
        key, positions = _comparison(before)
        if kind in {"formula", "table"} or protected_spans(before, include_math=True):
            detail = {"formula": "公式的符号及上下标", "table": "表格的行列及数值"}.get(kind, "本段的公式或特殊排版")
            x0, y0, x1, y1 = box
            visual_box = [x0, max(0, y0 - 0.10), x1, min(1, y1 + 0.10)]
            findings.append(_finding(page, block["id"], visual_box, f"{detail}请对照原图核对", block))
            if block.get("bbox"):
                visual_regions.append(box)
            continue
        indexes = [
            i for i, row in enumerate(sources)
            if _near_block(row["bbox"], block.get("bbox")) and not damaged_text(keys[i])
        ]
        aligned = _align(key, [keys[i] for i in indexes]) if indexes else None
        if aligned is None:
            findings.append(_finding(page, block["id"], box, "这段文字无法和 PDF 原文可靠对齐，请核对位置和正文", block))
            continue
        offsets, start, end, evidence = aligned
        windows = [
            (index, max(0, start - offsets[i]), min(len(keys[index]), end - offsets[i]))
            for i, index in enumerate(indexes)
            if offsets[i] < end and offsets[i + 1] > start
        ]
        source_boxes = [sources[i]["bbox"] for i, _, _ in windows]
        source_box = _bounds(source_boxes)
        if any(
            (_column_overlap(left, right) < 0.45 or right[1] < left[1] - 0.02)
            and not same_printed_source_line(left, right)
            for left, right in zip(source_boxes, source_boxes[1:])
        ):
            findings.append(_finding(
                page, block["id"], source_box, "正文对应多个栏或原文行序不明，请对照原图核对", block,
            ))
            visual_regions.append(source_box)
            continue
        if any(
            left < claimed_right and right > claimed_left
            for index, left, right in windows for claimed_left, claimed_right, _ in claims[index]
        ):
            findings.append(_finding(
                page, block["id"], source_box,
                "多段正文对应同一处原文，可能重复或位置不明确，请对照原图核对", block,
            ))
            continue
        for i, left, right in windows:
            claims[i].append((left, right, block))
        if previous and _column_overlap(previous, source_box) >= 0.75 and source_box[3] < previous[1] - 0.01:
            findings.append(_finding(
                page, f"{block['id']}-order", _bounds([previous, source_box]),
                "正文顺序与同栏原文位置有冲突，请核对段落顺序", block,
            ))
        previous = source_box
        if _SCRIPT_GLYPH.search(before) or any(_has_positioned_scripts(sources[i]) for i, _, _ in windows):
            script_regions.append(source_box)
        if finding := _text_finding(
            page, block, key, positions, evidence, indexes, offsets, start, sources, source_positions,
        ):
            findings.append(finding)

    findings.extend(_missing_source_findings(page, sources, keys, claims, visual_regions))
    figure_boxes = [
        block["bbox"] for block in blocks if block["type"] in _IMAGE_KINDS and block.get("asset_id") and block.get("bbox")
    ]
    for image in images:
        box = image["bbox"]
        area = (box[2] - box[0]) * (box[3] - box[1])
        covered = any(overlap_fraction(box, figure) >= 0.8 for figure in figure_boxes)
        if area >= 0.45 or (area >= 0.003 and not covered):
            findings.append(_finding(
                page, image["id"], box, "这处栅格原图的内容无法用 PDF 文字层核对，请检查是否有遗漏",
            ))
    if script_regions:
        findings.append(_finding(
            page, "inline-scripts", _bounds(script_regions),
            "这些段落含上下标或上标写法，请对照原图核对符号的位置",
        ))
    if len(findings) > _MAX_FINDINGS:
        # 文字候选保留，其他未覆盖的位置用一个整页工单收敛，避免大量大图。
        located = [row for row in findings if row.get("candidates")]
        findings = located[: _MAX_FINDINGS - 1] + [
            _finding(page, "page", _PAGE_BOX, "本页有多处差异或未能对齐的原文，请核对其余内容"),
        ]
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
    if (exact := native.find(key)) >= 0 and exact == native.rfind(key):
        return offsets, exact, exact + len(key), key
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
    first_row = bisect_right(offsets, first.a) - 1
    last_row = bisect_right(offsets, last_end - 1)
    start = max(offsets[first_row], first.a - first.b)
    end = min(offsets[last_row], last_end + len(key) - last.b - last.size)
    return offsets, start, end, native[start:end]


def _text_finding(page, block, key, positions, evidence, indexes, offsets, base, sources, source_positions) -> dict | None:
    """保留差异原图，并在仅涉及安全文字片段时提供整段候选。"""
    before = block["content"]
    patches = []
    boxes = []
    protected = [
        match.span() for match in _SCIENTIFIC_TOKEN.finditer(before) if _SCRIPT_GLYPH.search(match[0])
    ]
    skipped = False
    changes = (
        [("equal", 0, len(key), 0, len(key))] if key == evidence
        else SequenceMatcher(None, key, evidence, autojunk=False).get_opcodes()
    )
    for op, a, b, c, d in changes:
        if op == "equal":
            for pos in range(a, b - 1):
                if not (key[pos:pos + 2].isascii() and key[pos:pos + 2].isalpha()):
                    continue
                native_pos = base + c + pos - a
                i = bisect_right(offsets, native_pos) - 1
                if native_pos + 1 == offsets[i + 1]:
                    continue
                source_index = indexes[i]
                origin = source_positions[source_index]
                offset = native_pos - offsets[i]
                native_gap = sources[source_index]["content"][origin[offset] + 1:origin[offset + 1]]
                result_gap = before[positions[pos] + 1:positions[pos + 1]]
                if native_gap.isspace() != result_gap.isspace():
                    boxes.append(sources[source_index]["bbox"])
                    skipped = True
            continue
        source_start = base + c
        source_end = base + d
        first = bisect_right(offsets, min(source_start, offsets[-1] - 1)) - 1
        last = bisect_right(offsets, min(max(source_start, source_end - 1), offsets[-1] - 1)) - 1
        boxes.extend(sources[indexes[i]]["bbox"] for i in range(first, last + 1))
        left = positions[a] if a < len(positions) else len(before)
        right = positions[b - 1] + 1 if a < b else left
        patch = evidence[c:d]
        if (
            not patch and key[a:b] == "," and 0 < a <= b < len(key)
            and key[a - 1].isdigit() and key[b].isdigit() and before[right:positions[b]].isspace()
        ):
            right = positions[b]
        if (
            first != last or max(b - a, d - c) > 12
            or _comparison(before[left:right])[0] != key[a:b]
            or not re.fullmatch(r"[\w.,:;()[\]%+−·-]*", key[a:b] + patch)
            or any(left <= end and right >= start for start, end in protected)
        ):
            skipped = True
            continue
        patches.append((left, right, patch))
    if not boxes:
        return None
    row = _finding(
        page, block["id"], _bounds(boxes) if boxes else block["bbox"],
        "最终正文与同页原文有差异，请对照原图确认", block,
    )
    if patches:
        candidate = before
        for left, right, patch in reversed(patches):
            candidate = candidate[:left] + patch + candidate[right:]
        if candidate != before and preserves_protected(before, candidate):
            row["candidates"] = {"native": candidate}
            if skipped:
                row["reason"] = "候选仅覆盖能定位的文字，其他差异请对照原图核对"
    return row


def _missing_source_findings(page, sources, keys, claims, visual_regions) -> list[dict]:
    """从 PDF 向正文反查未覆盖的原文，连续行合用一张裁图。"""
    findings = []
    pending = []
    for i, (row, key) in enumerate(zip(sources, keys, strict=True)):
        start = 0
        uncovered = []
        owned = sorted(claims.get(i, ()), key=lambda item: item[0])
        for left, right, _ in owned:
            uncovered.append(key[start:left])
            start = max(start, right)
        uncovered.append(key[start:])
        visible_elsewhere = any(overlap_fraction(row["bbox"], box) >= 0.85 for box in visual_regions)
        if not any(uncovered) or visible_elsewhere or owned:
            if pending:
                findings.append(_missing_finding(page, sources, pending))
                pending = []
            if owned and any(uncovered) and not visible_elsewhere:
                block = owned[0][2]
                findings.append(_finding(
                    page, f"source-{i}", row["bbox"], "这行原文有未对齐的片段，请检查是否漏写",
                    block if len(owned) == 1 else None,
                ))
            continue
        if pending and not _next_print_row(sources[pending[-1]]["bbox"], row["bbox"]):
            findings.append(_missing_finding(page, sources, pending))
            pending = []
        pending.append(i)
    if pending:
        findings.append(_missing_finding(page, sources, pending))
    return findings


def _missing_finding(page, sources, indexes) -> dict:
    """定位遗漏的原文行，只有完整且可读的行提供补入候选。"""
    row = _finding(
        page, f"source-{indexes[0]}", _bounds([sources[i]["bbox"] for i in indexes]),
        "这处 PDF 原文未能与最终正文完整对应，请检查是否遗漏",
    )
    candidate = "\n".join(sources[i]["content"] for i in indexes)
    if not damaged_text(candidate) and len(candidate) <= _MAX_BLOCK and not protected_spans(candidate, include_math=True):
        row["candidates"] = {"native": candidate}
    return row


def _next_print_row(previous: list, current: list) -> bool:
    """按相邻高度和水平重合判断两行属于同一栏的同一原文区域。"""
    height = max(previous[3] - previous[1], current[3] - current[1])
    return (
        0 <= current[1] - previous[1] <= 2.6 * height
        and _column_overlap(previous, current) >= 0.65
    )


def _column_overlap(left: list, right: list) -> float:
    """用较窄一侧的宽度衡量两处是否仍属同一栏。"""
    width = min(left[2] - left[0], right[2] - right[0])
    overlap = min(left[2], right[2]) - max(left[0], right[0])
    return max(0.0, overlap) / width if width > 0 else 0.0


def _has_positioned_scripts(row: dict) -> bool:
    """原文包含上下标字形或显著偏移的小号字时，保留原图核对。"""
    if _SCRIPT_GLYPH.search(row["content"]):
        return True
    glyphs = [
        char for char in row["chars"]
        if char["text"].isalnum() and char["font_size"] and char["origin_normal"] is not None
    ]
    if len(glyphs) < 3:
        return False
    size = median(char["font_size"] for char in glyphs)
    baseline = median(char["origin_normal"] for char in glyphs if char["font_size"] >= size)
    return any(
        char["font_size"] < size * 0.85 and abs(char["origin_normal"] - baseline) >= size * 0.12
        for char in glyphs
    )


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
