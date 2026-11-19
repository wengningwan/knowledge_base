"""用同页原生文字核对模型转写中的局部差异。"""

from __future__ import annotations

import re
from collections import defaultdict
from difflib import SequenceMatcher

from yuxi.knowledge.parser.pdf_repair_rules import (
    damaged_text,
    fold_equivalent_typography,
    overlap_fraction,
    protected_spans,
)
from yuxi.knowledge.parser.result import ParsedDocument

_PRINT_FORMS = str.maketrans(
    "０１２３４５６７８９［］，．：；（）－₀₁₂₃₄₅₆₇₈₉",
    "0123456789[],.:;()-0123456789",
)
_CITATION = re.compile(r"\[([0-9]{1,5})\]")
_TRAILING_CITATION = re.compile(r"\[([0-9]{1,5})\]$")
_CITATION_DIGITS = re.compile(r"[0-9０-９]{1,5}")
_REFERENCE_MARKUP = re.compile(r"!?\[[^\]\n]+\][ \t]*\[[^\]\n]*\]|^[ \t]*\[[^\]\n]+\]:", re.M)
_SLASH_CHARGE = re.compile(r"(?<![A-Za-z0-9])((?:[A-Z][a-z]?[0-9]*){2,})([+-]?)/([A-Z][a-z]?)")


def check_recognition_source(document: ParsedDocument, native_blocks: list[dict]) -> dict:
    """用同一页的原生文字登记可定位的引文、文字和电荷疑点。"""
    page_rows = [(row, _compact(row["content"])) for row in native_blocks]
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
        sources = [
            (row, source) for row, source in page_rows
            if len(source) <= 240 and not damaged_text(source) and overlap_fraction(row["bbox"], block["bbox"]) >= 0.8
        ]
        if not sources:
            continue
        coverage["compared_blocks"] += 1
        review = {
            "kind": "text_mismatch", "status": "unresolved", "block_id": block["id"],
            "page": block["page"], "before": before,
        }
        if block["type"] == "formula":
            if not _formula_has_charge_conflict(before, [row for row, _ in sources]):
                continue
            review.update(
                kind="scientific_notation", critical=True,
                reason="PDF 同区域斜线前的电荷符号可能有差异，请对照原图确认",
            )
        else:
            positions = [index for index, char in enumerate(before) if not char.isspace()]
            compact = _compact(before)
            citation_edits = _citation_edits(compact, sources, page_rows, block["bbox"])
            letter_edits = _text_conflicts(compact, [source for _, source in sources])
            markup = protected_spans(before) + [match.span() for match in _REFERENCE_MARKUP.finditer(before)]
            math = protected_spans(before, include_math=True)
            # 候选保留 Markdown 结构。数学区的差异只登记原图待核。
            edits = []
            math_conflict = False
            for start, end, replacement, citation in citation_edits + letter_edits:
                left, right = positions[start], positions[end - 1] + 1
                if right - left != end - start or any(left < b and right > a for a, b in markup):
                    continue
                if citation and not _CITATION_DIGITS.fullmatch(before[left:right]):
                    continue
                if any(left < b and right > a for a, b in math):
                    math_conflict = True
                    continue
                edits.append((left, right, replacement, citation))
            if not edits and not math_conflict:
                continue
            candidate = before
            for start, end, replacement, _ in sorted(edits, reverse=True):
                candidate = candidate[:start] + replacement + candidate[end:]
            review.update(
                reason="同页原生文字和转写存在局部差异，请核对原图",
                critical=math_conflict or any(citation for _, _, _, citation in edits),
            )
            if edits:
                review["candidates"] = {"native": candidate}
        boxes = [block["bbox"], *(row["bbox"] for row, _ in sources)]
        review["bbox"] = [
            min(box[0] for box in boxes), min(box[1] for box in boxes),
            max(box[2] for box in boxes), max(box[3] for box in boxes),
        ]
        document.reviews.append({**review, "id": f"native-conflict-{block['id']}"})
        coverage["flagged_blocks"] += 1
    return coverage


def _compact(text: str) -> str:
    """比较时忽略空白和常见全角形式，保留连字符和点号差异。"""
    return "".join(char for char in text.translate(_PRINT_FORMS) if not char.isspace())


def _citation_edits(
    text: str, sources: list[tuple[dict, str]], page_rows: list[tuple[dict, str]], bbox: list
) -> list[tuple]:
    """引文数字要求前文一致、后文唯一，原生行首可以核对左侧同一行的片段。"""
    edits = []
    for match in _CITATION.finditer(text):
        right = text[match.end():match.end() + 10]
        if len(right) < 10 or sum(char.isalnum() for char in right) < 7 or text.count(right) != 1:
            continue
        line_matches = [(row, source) for row, source in sources if right in source]
        if len(line_matches) != 1 or sum(source.count(right) for _, source in page_rows) != 1:
            continue
        row, source = line_matches[0]
        end = source.index(right)
        citation = _TRAILING_CITATION.search(source, max(0, end - 7), end)
        if citation is None or citation[1] == match[1]:
            continue
        start = citation.start()
        left = text[max(0, match.start() - 5):match.start()]
        if not _citation_left_matches(left, row, source[:start], sources):
            continue
        digits = [char for char in row["chars"] if not char["text"].isspace()][start + 1:end - 1]
        if not _CITATION_DIGITS.fullmatch("".join(char["text"] for char in digits)) or any(
            not char["font_size"] or overlap_fraction(char["bbox"], bbox) < 0.95 for char in digits
        ):
            continue
        edits.append((match.start(1), match.end(1), citation[1], True))
    return edits


def _citation_left_matches(left: str, row: dict, prefix: str, sources: list[tuple[dict, str]]) -> bool:
    """只在版面同一行内补足被分开的引用前文。"""
    if not left:
        return not prefix
    if prefix.endswith(left):
        return True
    if not left.endswith(prefix):
        return False
    required = left[:len(left) - len(prefix)]
    x0, y0, _, y1 = row["bbox"]
    height, center = y1 - y0, (y0 + y1) / 2
    return sum(
        other is not row and text.endswith(required)
        and -0.5 * height <= x0 - other["bbox"][2] <= 1.5 * height
        and abs(center - (other["bbox"][1] + other["bbox"][3]) / 2) <= 0.75 * height
        for other, text in sources
    ) == 1


def _text_conflicts(text: str, sources: list[str]) -> list[tuple]:
    """登记两侧相同且在所比对区域唯一的一字和点号差异。"""
    candidates = defaultdict(set)
    for source in sources:
        if len(source) < 13 or source in text:
            continue
        changes = SequenceMatcher(None, text, source, autojunk=False).get_opcodes()
        for previous, change, following in zip(changes, changes[1:], changes[2:]):
            op, start, end, a, b = change
            if op != "replace" or end - start != 1 or b - a != 1:
                continue
            old, new = text[start], source[a]
            if not (old.isalpha() and new.isalpha() or {old, new} == {".", "·"}):
                continue
            left = min(8, start - previous[1])
            right = min(12, following[2] - end)
            if previous[0] != "equal" or following[0] != "equal" or min(left, right) < 3 or left + right < 12:
                continue
            context = text[start - left:end + right]
            native = source[a - left:b + right]
            if (text.count(context) == 1 and sum(row.count(native) for row in sources) == 1
                    and not any(context in row for row in sources)):
                candidates[start].add(new)
    return [(start, start + 1, next(iter(values)), False) for start, values in candidates.items() if len(values) == 1]


def _formula_has_charge_conflict(formula: str, rows: list[dict]) -> bool:
    """只登记可定位的电荷疑点，原生文字线性顺序不能裁定上下标。"""
    model = _SLASH_CHARGE.findall(_compact(fold_equivalent_typography(formula)))
    native = [
        match
        for row in rows
        for match in _SLASH_CHARGE.findall(_compact(fold_equivalent_typography(row["content"])))
    ]
    for left, charge, right in model:
        model_charges = [sign for a, sign, b in model if (a, b) == (left, right)]
        native_charges = [sign for a, sign, b in native if (a, b) == (left, right)]
        if len(model_charges) == len(native_charges) == 1 and charge != native_charges[0]:
            return True
    return False
