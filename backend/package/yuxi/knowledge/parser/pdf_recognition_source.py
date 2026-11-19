"""用同页原生文字核对模型转写中的局部差异。"""

from __future__ import annotations

import re
from collections import defaultdict
from difflib import SequenceMatcher

from yuxi.knowledge.parser.pdf_repair_rules import (
    damaged_text,
    fold_equivalent_typography,
    overlap_fraction,
    preserves_protected,
    protected_spans,
)
from yuxi.knowledge.parser.result import ParsedDocument

_PRINT_FORMS = str.maketrans(
    "０１２３４５６７８９［］，．：；（）－₀₁₂₃₄₅₆₇₈₉",
    "0123456789[],.:;()-0123456789",
)
_PRINT_FORMS.update(str.maketrans({"–": "-", "−": "-", "\x02": "-", "\x03": "-"}))
_CITATION = re.compile(r"\[([0-9]{1,5}(?:-[0-9]{1,5})?)\]")
_CITATION_DIGITS = re.compile(r"[0-9０-９]{1,5}(?:[-－–−][0-9０-９]{1,5})?")
_REFERENCE_MARKUP = re.compile(r"!?\[[^\]\n]+\][ \t]*\[[^\]\n]*\]|^[ \t]*\[[^\]\n]+\]:", re.M)
_SLASH_CHARGE = re.compile(r"(?<![A-Za-z0-9])((?:[A-Z][a-z]?[0-9]*){2,})([+-]?)/([A-Z][a-z]?)")
_GROUP_MARKS = str.maketrans("", "", "()[]")
_CHARGE_MARK = re.compile(r"(?<!\\)\^\s*(?:\{\s*([+-])\s*\}|([+-]))|([⁺⁻])")
_FORMULA_TOKEN = re.compile(r"\\[A-Za-z]+|\\[\s\S]|\s+|[^\s]")
_FORMULA_TEXT_STRUCTURES = re.compile(r"\\(?:text[A-Za-z]*|[fhm]box|operatorname|verb|begin|ce|pu)(?![A-Za-z])|(?<!\\)%")
_EQUATION_TAG = re.compile(r"\\tag\*?\s*\{(?:[0-9]{1,4}|\([0-9]{1,4}\))\}(?=\s*(?:\$\$)?\s*$)")
_GROUPING_COMMANDS = {
    r"\left", r"\right", r"\tag", r"\eta", r"\pi", r"\alpha", r"\beta", r"\Delta",
    r"\times", r"\div", r"\cdot", r"\quad", r"\qquad", r"\%", r"\,", r"\;",
}


def check_recognition_source(document: ParsedDocument, native_blocks: list[dict]) -> dict:
    """用同一页的原生文字登记可定位的引文、文字和电荷疑点。"""
    page_rows = [(row, _compact(row["content"])) for row in _printed_rows(native_blocks)]
    coverage = {
        "native_pages": int(bool(page_rows)), "eligible_blocks": 0, "compared_blocks": 0,
        "flagged_blocks": 0,
    }

    for block in document.blocks:
        if block["type"] not in {"text", "formula"}:
            continue
        coverage["eligible_blocks"] += 1
        before = block["content"]
        if not block.get("bbox") or len(before) > 6000:
            continue
        x0, y0, x1, y1 = block["bbox"]
        region = [x0, max(0, y0 - 0.1), x1, min(1, y1 + 0.1)]
        sources = [
            (row, source) for row, source in page_rows
            if len(source) <= 240 and not damaged_text(source) and overlap_fraction(row["bbox"], region) >= 0.8
        ]
        charge_rows = [
            row for row in native_blocks
            if overlap_fraction(row["bbox"], block["bbox"]) >= 0.8
            and len(_compact(row["content"])) <= 240 and not damaged_text(row["content"])
        ] if block["type"] == "formula" else []
        if not sources and not charge_rows:
            continue
        coverage["compared_blocks"] += 1
        review = {
            "kind": "text_mismatch", "status": "unresolved", "block_id": block["id"],
            "page": block["page"], "before": before,
        }
        if block["type"] == "formula":
            grouping = _formula_grouping_evidence(before, page_rows)
            if grouping and overlap_fraction(grouping[0], region) >= 0.8:
                box, expected = grouping
                evidence = {"kind": "grouping", "expected": expected}
                reason = "同页原文的算式括号位置与转写不同，请对照原图确认"
            elif evidence := _formula_charge_evidence(before, charge_rows):
                box = block["bbox"]
                reason = "PDF 同区域斜线前的电荷符号可能有差异，请对照原图确认"
            else:
                continue
            if _FORMULA_TEXT_STRUCTURES.search(before) or (
                evidence["kind"] == "grouping" and _grouping_key(before) is None
            ):
                evidence = {"kind": "image_only"}
            boxes = [box]
            review.update(
                kind="scientific_notation", critical=True, reason=reason,
                source_targets=[{**evidence, "bbox": box}],
            )
        else:
            positions = [index for index, char in enumerate(before) if not char.isspace()]
            compact = _compact(before)
            citation_edits = _citation_edits(compact, sources, page_rows, region)
            letter_edits = _text_conflicts(compact, sources, page_rows)
            markup = protected_spans(before) + [match.span() for match in _REFERENCE_MARKUP.finditer(before)]
            math = protected_spans(before, include_math=True)
            # 候选保留 Markdown 结构。数学区的差异只登记原图待核。
            edits, boxes, targets = [], [], []
            math_conflict = False
            for start, end, replacement, citation, source_bbox in citation_edits + letter_edits:
                left, right = positions[start], positions[end - 1] + 1
                if right - left != end - start or any(left < b and right > a for a, b in markup):
                    continue
                if citation and not _CITATION_DIGITS.fullmatch(before[left:right]):
                    continue
                boxes.append(source_bbox)
                if any(left < b and right > a for a, b in math) or not preserves_protected(
                    before, before[:left] + replacement + before[right:]
                ):
                    math_conflict = True
                    targets.append({"kind": "image_only", "bbox": source_bbox, "start": left, "end": right})
                    continue
                edits.append((left, right, replacement, citation))
                targets.append({
                    "kind": "text", "start": left, "end": right, "replacement": replacement, "bbox": source_bbox,
                })
            if not edits and not math_conflict:
                continue
            candidate = before
            for start, end, replacement, _ in sorted(edits, reverse=True):
                candidate = candidate[:start] + replacement + candidate[end:]
            review.update(
                reason="同页原生文字和转写存在局部差异，请核对原图",
                critical=math_conflict or any(citation for _, _, _, citation in edits),
                source_targets=targets,
            )
            if edits and preserves_protected(before, candidate):
                review["candidates"] = {"native": candidate}
        review["bbox"] = [
            min(box[0] for box in boxes), min(box[1] for box in boxes),
            max(box[2] for box in boxes), max(box[3] for box in boxes),
        ]
        document.reviews.append({**review, "id": f"native-conflict-{block['id']}"})
        coverage["flagged_blocks"] += 1
    return coverage


def matches_source_formula(before: str, after: str, target: dict) -> bool:
    """接受登记的括号或电荷变化，逐字保护公式的其余 LaTeX 和式号。"""
    if _FORMULA_TEXT_STRUCTURES.search(before):
        return False
    if target["kind"] == "grouping":
        if (
            _FORMULA_TEXT_STRUCTURES.search(after)
            or _linear_quotient(after) != target["expected"]
            or _EQUATION_TAG.findall(before) != _EQUATION_TAG.findall(after)
        ):
            return False
        before_key = _grouping_key(before)
        return before_key is not None and before_key == _grouping_key(after) and _valid_grouping(after)
    if target["kind"] == "charge":
        if _FORMULA_TEXT_STRUCTURES.search(after) or _compact(fold_equivalent_typography(after)) != target["expected"]:
            return False
        to_script = str.maketrans("+-", "⁺⁻")
        old, new = (
            _FORMULA_TOKEN.findall(
                _CHARGE_MARK.sub(lambda mark: (mark[1] or mark[2] or mark[3]).translate(to_script), raw)
            )
            for raw in (before, after)
        )
        changes = [change for change in SequenceMatcher(None, old, new, autojunk=False).get_opcodes() if change[0] != "equal"]
        if len(changes) != 1:
            return False
        _, start, end, new_start, new_end = changes[0]
        to_print = str.maketrans("⁺⁻", "+-")
        old_mark, new_mark = "".join(old[start:end]), "".join(new[new_start:new_end])
        return (
            old_mark.translate(to_print) == target["before_sign"]
            and new_mark.translate(to_print) == target["after_sign"]
            and len(_compact(fold_equivalent_typography("".join(old[:start])))) == target["sign_offset"]
            and all(part in ("", "⁺", "⁻") for part in (old_mark, new_mark))
        )
    return False


def _grouping_key(formula: str) -> list[str] | None:
    """比较独立的 TeX 符号，保留参数和上下标中的括号。"""
    key = []
    depth = 0
    script = False
    for token in _FORMULA_TOKEN.findall(formula):
        if token.isspace():
            continue
        if token.startswith("\\") and token not in _GROUPING_COMMANDS:
            return None
        if depth == 0 and not script and token in {"(", ")", "[", "]", r"\left", r"\right"}:
            continue
        key.append(token)
        depth += (token == "{") - (token == "}")
        script = token in {"^", "_"}
    return key if key.count(r"\tag") == len(_EQUATION_TAG.findall(formula)) else None


def _valid_grouping(formula: str) -> bool:
    """限制可调整的普通和可伸缩括号成对、顺序正确且位于同一参数组。"""
    pairs = {"(": ")", "[": "]"}
    stack: list[tuple[str, bool, int]] = []
    depth = 0
    tokens = re.finditer(r"\\(left|right)(?![A-Za-z])\s*(.)|\\[A-Za-z]+|\\[\s\S]|[()\[\]{}]", formula)
    for token in tokens:
        mark, bracket = token[1], token[2] if token[1] else token[0]
        if (mark == "left" and bracket not in pairs) or (mark == "right" and bracket not in pairs.values()):
            return False
        if bracket in pairs:
            stack.append((pairs[bracket], mark == "left", depth))
        elif bracket in pairs.values():
            if not stack or stack.pop() != (bracket, mark == "right", depth):
                return False
        elif bracket == "{":
            depth += 1
        elif bracket == "}":
            depth -= 1
    return not stack and depth == 0


def _compact(text: str) -> str:
    """比较时忽略空白和常见全角形式，保留连字符和点号差异。"""
    return "".join(char for char in text.translate(_PRINT_FORMS) if not char.isspace())


def _printed_rows(native_blocks: list[dict]) -> list[dict]:
    """接回同一印刷行内由上下标拆开的相邻原生片段。"""
    rows = []
    for row in native_blocks:
        previous = rows[-1] if rows else None
        if (
            previous
            and len(previous["chars"]) + len(row["chars"]) <= 240
            and previous["chars"][-1]["index"] + 1 == row["chars"][0]["index"]
            and _same_print_line(previous["bbox"], row["bbox"])
        ):
            chars = previous["chars"] + row["chars"]
            a, b = previous["bbox"], row["bbox"]
            rows[-1] = {
                **previous, "chars": chars, "content": "".join(char["text"] for char in chars).strip(),
                "bbox": [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])],
            }
        else:
            rows.append(row)
    return rows


def _citation_edits(
    text: str, sources: list[tuple[dict, str]], page_rows: list[tuple[dict, str]], bbox: list
) -> list[tuple]:
    """核对单号和编号范围，段尾用唯一前文定位。"""
    edits = []
    for match in _CITATION.finditer(text):
        left = text[max(0, match.start() - 5):match.start()]
        right = text[match.end():match.end() + 10]
        if len(right) == 10 and sum(char.isalnum() for char in right) >= 7:
            if text.count(right) != 1 or sum(source.count(right) for _, source in page_rows) != 1:
                continue
        elif len(left) < 5 or text.count(left) != 1:
            continue
        matches = [
            (row, citation)
            for row, source in page_rows
            for citation in _CITATION.finditer(source)
            if source[citation.end():].startswith(right)
            and _citation_left_matches(left, row, source[:citation.start()], page_rows)
        ]
        if len(matches) != 1 or matches[0][1][1] == match[1]:
            continue
        row, citation = matches[0]
        if not any(row is candidate for candidate, _ in sources):
            continue
        start, end = citation.span(1)
        digits = [char for char in row["chars"] if not char["text"].isspace()][start:end]
        if _CITATION_DIGITS.fullmatch("".join(char["text"] for char in digits)) and all(
            char["font_size"] and overlap_fraction(char["bbox"], bbox) >= 0.95 for char in digits
        ):
            edits.append((match.start(1), match.end(1), citation[1], True, row["bbox"]))
    return edits


def _citation_left_matches(left: str, row: dict, prefix: str, sources: list[tuple[dict, str]]) -> bool:
    """从同一行的左侧片段或同栏的上一行补足引用前文。"""
    if not left:
        return not prefix
    if prefix.endswith(left):
        return True
    if not left.endswith(prefix):
        return False
    required = left[:len(left) - len(prefix)]
    x0, y0, x1, y1 = row["bbox"]
    height, center = y1 - y0, (y0 + y1) / 2
    matches = 0
    for other, text in sources:
        if other is row or not text.endswith(required):
            continue
        a0, b0, a1, b1 = other["bbox"]
        distance = center - (b0 + b1) / 2
        adjacent = _same_print_line(other["bbox"], row["bbox"])
        above = (
            0.75 * height < distance <= 2.5 * max(height, b1 - b0)
            and min(x1, a1) - max(x0, a0) >= 0.8 * min(x1 - x0, a1 - a0)
        )
        matches += adjacent or above
    return matches == 1


def _same_print_line(left: list, right: list) -> bool:
    """判定横向相邻片段是否共用同一印刷行。"""
    height = max(left[3] - left[1], right[3] - right[1])
    gap = right[0] - left[2]
    return -0.5 * height <= gap <= 1.5 * height and abs(left[1] + left[3] - right[1] - right[3]) <= 1.7 * height


def _text_conflicts(text: str, sources: list[tuple[dict, str]], page_rows: list[tuple[dict, str]]) -> list[tuple]:
    """核对同页唯一的单字、点号和同行词间连字符。"""
    candidates = defaultdict(dict)
    for row, source in sources:
        if len(source) < 13 or source in text:
            continue
        changes = SequenceMatcher(None, text, source, autojunk=False).get_opcodes()
        for previous, change, following in zip(changes, changes[1:], changes[2:]):
            op, start, end, a, b = change
            if end - start != 1:
                continue
            old, new = text[start], source[a:b]
            if op == "delete" and old == "-" and _is_word_gap(row["content"], a):
                new = " "
            elif op != "replace" or len(new) != 1 or not (
                old.isalpha() and new.isalpha() or {old, new} == {".", "·"}
            ):
                continue
            left = min(8, start - previous[1])
            right = min(12, following[2] - end)
            if previous[0] != "equal" or following[0] != "equal" or min(left, right) < 3 or left + right < 12:
                continue
            context = text[start - left:end + right]
            native = source[a - left:b + right]
            if (text.count(context) == 1 and sum(line.count(native) for _, line in page_rows) == 1
                    and not any(context in line for _, line in page_rows)):
                candidates[start][new] = row["bbox"]
    return [
        (start, start + 1, replacement, False, bbox)
        for start, values in candidates.items() if len(values) == 1
        for replacement, bbox in values.items()
    ]


def _is_word_gap(line: str, index: int) -> bool:
    """只接受同一原生行内两个英文词之间实际存在的空白。"""
    positions = [i for i, char in enumerate(line) if not char.isspace()]
    if not 0 < index < len(positions):
        return False
    left, right = positions[index - 1] + 1, positions[index]
    return (
        line[left:right].isspace()
        and bool(re.search(r"[A-Za-z]{3,}$", line[:left]))
        and bool(re.match(r"[A-Za-z]{3,}", line[right:]))
    )


def _formula_grouping_evidence(formula: str, page_rows: list[tuple[dict, str]]) -> tuple[list, str] | None:
    """只在同页线性商式的主体序列唯一时定位括号差异。"""
    expected = _linear_quotient(formula)
    if not expected:
        return None
    key = expected.translate(_GROUP_MARKS)
    matches = []
    for row, _ in page_rows:
        native = _linear_quotient(row["content"])
        if native and native.translate(_GROUP_MARKS) == key:
            matches.append((row["bbox"], native))
    return matches[0] if len(matches) == 1 and matches[0][1] != expected else None


def _linear_quotient(text: str) -> str:
    """提取能以一行字符核对的等式，保留括号和运算符位置。"""
    compact = _compact(fold_equivalent_typography(_EQUATION_TAG.sub("", text)))
    compact = re.sub(r"\([0-9]{1,4}\)$", "", compact)
    if "/" in compact and re.fullmatch(r"[^\W\d]\w*=[\w.+*/×%()\[\]-]+", compact):
        return compact
    return ""


def _formula_charge_evidence(formula: str, rows: list[dict]) -> dict | None:
    """只登记可定位的电荷疑点，原生文字线性顺序不能裁定上下标。"""
    compact = _compact(fold_equivalent_typography(formula))
    model = list(_SLASH_CHARGE.finditer(compact))
    native = [
        match
        for row in rows
        for match in _SLASH_CHARGE.findall(_compact(fold_equivalent_typography(row["content"])))
    ]
    conflicts = []
    for match in model:
        left, charge, right = match.groups()
        model_matches = [other for other in model if (other[1], other[3]) == (left, right)]
        native_charges = [sign for a, sign, b in native if (a, b) == (left, right)]
        if len(model_matches) == len(native_charges) == 1 and charge != native_charges[0]:
            start, end = match.span(2)
            conflicts.append({
                "kind": "charge", "expected": compact[:start] + native_charges[0] + compact[end:],
                "sign_offset": start, "before_sign": charge, "after_sign": native_charges[0],
            })
    if len(conflicts) > 1:
        return {"kind": "image_only"}
    return conflicts[0] if conflicts else None
