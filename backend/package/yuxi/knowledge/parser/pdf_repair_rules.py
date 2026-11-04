"""证据约束的块补丁；借鉴 pdf-md-quad-repair 2.7.7 保护区与事务机制。

只移植内存规则，不执行原 skill CLI、宽松摘句校验或任意正则规则。
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections import Counter

def apply_block_edits(blocks: list[dict], edits: list[dict], *, allow_prose_cleanup: bool = False) -> bool:
    """先校验全部 before，再原子应用同组受控文字补丁。"""
    by_id = {block["id"]: block for block in blocks}
    if len({edit["block_id"] for edit in edits}) != len(edits):
        return False
    for edit in edits:
        block = by_id.get(edit["block_id"])
        if not block or block.get("content", "") != edit["before"] or not (preserves_protected(edit["before"], edit["after"]) or allow_prose_cleanup and edit["after"] == clean_prose_whitespace(edit["before"])):
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
    structures = r" {2,}(?=\r?\n)|\r?\n|^[ \t]*(?:[-+*]|\d+[.)])[ \t]+"
    return (
        Counter(before[start:end] for start, end in protected_spans(before)) == Counter(after[start:end] for start, end in protected_spans(after))
        and re.findall(structures, before, re.M) == re.findall(structures, after, re.M)
    )


def critical_signature(text: str) -> list[str]:
    """保守保留数值、科学 token 与符号，普通词拼写允许精确候选修复。"""
    numeric = r"(?<![\w.])[+−-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+−-]?\d+)?"
    prefix = r"(?:da|[YZEPTGMkhdcmµμunpfazy])?"
    units = rf"\b(?:{prefix}(?:mol|Pa|Hz|Wb|kat|rad|sr|eV|g|L|l|m|s|A|K|M|J|W|V|N|F|S|T|H|C)|kcal|cal|atm|bar|Torr|ppm|ppb)\b"
    pattern = rf"\\[A-Za-z]+|[_^](?:\{{[^}}]*\}}|\w)|{numeric}|[A-Za-z]*[0-9₀-₉⁰-⁹][A-Za-z0-9₀-₉⁰-⁹]*|\b[A-Z][A-Za-z]{{0,2}}\b|\b[A-Z][A-Za-z]?(?:[A-Z][A-Za-z]?)+\b|{units}|[α-ωΑ-Ωₐ-₟ⁱⁿµ°%]|[^\w\s,.;:!?()\[\]'\"，。；：！？、]"
    return re.findall(pattern, text)


def scientific_symbol_spans(text: str) -> list[tuple[int, int]]:
    """定位需原图核对的混合字母下标和化学键，跳过代码与链接。"""
    protected = protected_spans(text)
    pattern = r"[A-Za-z][ₐ-₟]+[A-Za-z]|\b[A-Z][a-z]?\s*(?:≡|=|\\equiv\b)\s*[A-Z][a-z]?\b"
    return [
        (match.start(), match.end())
        for match in re.finditer(pattern, text)
        if not any(start < match.end() and end > match.start() for start, end in protected)
    ]


def is_local_candidate(before: str, after: str) -> bool:
    """只接受少量局部差异，不把全段候选选择变成任意重写。"""
    if len(after.split()) < len(before.split()):
        return False
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    changes = [(i2 - i1, j2 - j1) for op, i1, i2, j1, j2 in matcher.get_opcodes() if op != "equal"]
    return bool(changes) and len(changes) <= 4 and sum(max(a, b) for a, b in changes) <= 24 and matcher.ratio() >= 0.80


def clean_prose_whitespace(text: str) -> str:
    """在代码、数学及结构行之外应用 S01–S06，不压缩缩进或有效换行。"""
    spans = protected_spans(text, include_math=True)
    lines, protected_lines, offset = [], [], 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        protected_lines.append(any(start < offset + len(line) and end > offset for start, end in spans))
        newline = line[len(body):]
        if not body.strip(" \t"):
            if not any(start < offset + len(body) and end > offset for start, end in spans):
                body = ""
        elif _ordinary_prose_line(body):
            indentation = re.match(r"[ \t]*", body)[0]
            cursor = len(indentation)
            pieces = [indentation]
            local_spans = sorted((max(cursor, start - offset), min(len(body), end - offset)) for start, end in spans if start < offset + len(body) and end > offset + cursor)
            for start, end in local_spans:
                if start < cursor:
                    start = cursor
                if end <= cursor:
                    continue
                pieces.extend([_clean_prose_segment(body[cursor:start]), body[start:end]])
                cursor = end
            pieces.append(_clean_prose_segment(body[cursor:]))
            body = "".join(pieces)
            suffix = re.search(r"[ \t]+$", body)
            if suffix and not any(start < offset + len(line.rstrip('\r\n')) and end > offset + len(line.rstrip('\r\n')) - len(suffix[0]) for start, end in spans):
                # 保留两个空格的 Markdown 强制换行；一空格与 Tab 不承担该语义。
                raw_suffix = re.search(r"[ \t]+$", line.rstrip("\r\n"))[0]
                body = body[:suffix.start()] + ("  " if newline and raw_suffix.endswith("  ") else "")
        lines.append(body + newline)
        offset += len(line)
    # S04 仅在普通正文段之间压缩空行，不跨列表、标题、表格或代码边界。
    index = 0
    while index < len(lines):
        if lines[index].strip():
            index += 1
            continue
        end = index
        while end < len(lines) and not lines[end].strip():
            end += 1
        if end - index > 1 and index and end < len(lines) and _ordinary_prose_line(lines[index - 1].rstrip('\r\n')) and _ordinary_prose_line(lines[end].rstrip('\r\n')):
            # 原始保护范围中的空行也不能改动。
            if not any(protected_lines[index:end]):
                lines[index:end] = ["\n"]
                protected_lines[index:end] = [False]
                end = index + 1
        index = end
    return "".join(lines)


def proven_representation_equivalent(left: str, right: str) -> bool:
    """只证明已知排印语法等价，不折合数字、上下标位置、符号或化学键。"""
    canonical_left = _representation_tokens(left)
    canonical_right = _representation_tokens(right)
    return canonical_left is not None and canonical_right is not None and canonical_left == canonical_right


def _ordinary_prose_line(line: str) -> bool:
    """排除有缩进或显式 Markdown/HTML 结构的行。"""
    return bool(line.strip()) and not re.match(r"^(?:[ \t]+|#{1,6}\s|[-+*]\s|\d+[.)]\s|>|\||```|~~~|<|!\[|\$\$|\\\[)", line) and not re.search(r"\|.*\||^[-=_*]{3,}\s*$", line)


def _clean_prose_segment(text: str) -> str:
    """清理普通文字片段，U+3000 及拉丁文/数字间的词界保持。"""
    text = text.translate(str.maketrans({"\u00a0": " ", "\u2007": " ", "\u202f": " "}))
    text = re.sub(r"[ \t]+", " ", text)
    han = r"\u3400-\u4dbf\u4e00-\u9fff"
    punctuation = "，。；：！？、（）《》〈〉【】「」『』“”‘’"
    text = re.sub(rf"(?<=[{han}]) +(?=[{han}{punctuation}])|(?<=[{punctuation}]) +(?=[{han}])", "", text)
    return text


def _representation_tokens(text: str) -> str | None:
    """解析有限数学包装；未知命令或未闭合语法没有等价证明。"""
    text = unicodedata.normalize("NFC", text)
    # 代码和链接以原始字节参与比较，不能被数学包装规则折合。
    if protected_spans(text):
        return text
    text = text.translate(str.maketrans({"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl"}))
    text = re.sub(r"(?<!\\)\$\$(.*?)\$\$|(?<![\\$])\$(?!\$)(.*?)(?<!\\)\$|\\\((.*?)\\\)|\\\[(.*?)\\\]", lambda match: next(group for group in match.groups() if group is not None), text, flags=re.S)
    if "$" in text or r"\(" in text or r"\[" in text:
        return None
    wrappers = {"mathrm", "textrm", "mathit", "text"}
    subs = str.maketrans("₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ", "0123456789+-=()aehijklmnoprstuvx")
    supers = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁱⁿ", "0123456789+-=()in")
    text = re.sub("[₀-₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ]+", lambda match: "_{" + match[0].translate(subs) + "}", text)
    text = re.sub("[⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁱⁿ]+", lambda match: "^{" + match[0].translate(supers) + "}", text)
    position = 0

    def parse_group(closing=False):
        """递归保留上下标分组，去除无语义的显示包装。"""
        nonlocal position
        result = []
        while position < len(text):
            char = text[position]
            position += 1
            if char == "}":
                return "".join(result) if closing else None
            if char == "{":
                # 普通花括号可能有集合/分组含义，不能按样式包装直接擦除。
                return None
            elif char == "\\":
                command = re.match(r"[A-Za-z]+", text[position:])
                if not command or command[0] not in wrappers:
                    return None
                position += len(command[0])
                while position < len(text) and text[position].isspace():
                    position += 1
                if position >= len(text) or text[position] != "{":
                    return None
                position += 1
                group = parse_group(True)
                if group is None or not group.strip():
                    return None
                result.append(group)
            elif char in "_^":
                while position < len(text) and text[position].isspace():
                    position += 1
                if position >= len(text):
                    return None
                if text[position] == "{":
                    position += 1
                    group = parse_group(True)
                else:
                    group = text[position]
                    position += 1
                if not group or any(symbol in group for symbol in "_^{}\\"):
                    return None
                result.append(char + "{" + group + "}")
            else:
                result.append(char)
        return None if closing else "".join(result)

    parsed = parse_group()
    return layout_text(parsed) if parsed is not None else None
