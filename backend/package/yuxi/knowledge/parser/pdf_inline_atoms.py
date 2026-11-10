"""完整内联修改单元及重建后的排印损坏检查，不推断论文科学含义。"""

from __future__ import annotations

import difflib
import re
from collections import Counter


SUBS = "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ"
SUPERS = "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁱⁿ"


def parse_inline_atoms(text: str) -> list[dict]:
    """识别完整词、附着脚本及数学包装，未知数学内容整体不拆。"""
    atoms, position = [], 0
    while position < len(text):
        start = position
        math_end = _math_end(text, position)
        if math_end is not None:
            position = math_end
            kind = "math"
        else:
            word = re.match(r"[^\W_]+", text[position:], re.UNICODE)
            if not word:
                position += 1
                continue
            position += len(word[0])
            kind = "word"
        while position < len(text):
            attached_math = _math_end(text, position)
            if attached_math is not None:
                position = attached_math
                kind = "math"
                continue
            if text[position] in "_^":
                position = _script_end(text, position)
                kind = "math"
                continue
            if text[position] in "/·≡=−-" and position + 1 < len(text) and text[position + 1].isalnum():
                position += 1
                continue
            suffix = re.match(r"[^\W_]+", text[position:], re.UNICODE)
            if suffix:
                position += len(suffix[0])
                continue
            break
        atoms.append({"start": start, "end": position, "kind": kind})
    return atoms


def expand_target_spans(text: str, spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """将差异扩展到完整原子并合并重叠跨度，插入点携带相邻词。"""
    atoms = parse_inline_atoms(text)
    expanded = []
    for start, end in spans:
        touched = [
            atom for atom in atoms
            if (atom["start"] < end and atom["end"] > start)
            or (start == end and atom["start"] <= start <= atom["end"])
        ]
        if touched:
            start = min(start, touched[0]["start"])
            end = max(end, touched[-1]["end"])
        expanded.append((start, end))
    merged = []
    for start, end in sorted(expanded):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def validate_inline_change(before: str, after: str, *, script_evidence: bool = False,
                           punctuation_evidence: bool = False) -> str:
    """拒绝新增语法损坏及无独立依据的脚本降级、词界和标点清理。"""
    old_damage, new_damage = _math_damage(before), _math_damage(after)
    if any(count > old_damage[kind] for kind, count in new_damage.items()):
        return "introduced_math_damage"
    if not script_evidence and lost_scripts(scripts(before), scripts(after)):
        script_error = "script_scope_loss_requires_native_geometry"
    else:
        script_error = ""
    changes = [entry for entry in difflib.SequenceMatcher(a=before, b=after, autojunk=False).get_opcodes() if entry[0] != "equal"]
    for _, a, b, c, d in changes:
        left, right = before[a:b], after[c:d]
        if not punctuation_evidence and any(char.isspace() for char in left) and not any(char.isspace() for char in right):
            for gap in re.finditer(r"\s+", left):
                start, end = a + gap.start(), a + gap.end()
                if start and end < len(before) and before[end].isascii() and before[end].isalnum():
                    # 作者标记/数学包装不能替代原文词间空格。
                    around = after[max(0, c - 1):min(len(after), d + 1)]
                    if not any(char.isspace() for char in around):
                        return "introduced_word_join"
    if len(re.findall(r"(?<!\\)\*", after)) > len(re.findall(r"(?<!\\)\*", before)):
        without_math = _without_math(after)
        if re.search(r"(?<!\\)\*", without_math):
            return "introduced_markdown_emphasis"
    punctuation = r"[.,;:!?–—−-]"
    if not punctuation_evidence and any(re.findall(punctuation, before[a:b]) != re.findall(punctuation, after[c:d])
                                        for _, a, b, c, d in changes):
        return "punctuation_change_requires_native_evidence"
    # 脚本作用域错误最后返回：标点/词界等硬破坏优先，豁免路径不得绕过它们。
    return script_error


def _math_end(text: str, start: int) -> int | None:
    """识别未转义数学定界；未闭合表达保护到块末。"""
    if start and text[start - 1] == "\\":
        return None
    for opening, closing in (("$$", "$$"), (r"\(", r"\)"), (r"\[", r"\]"), ("$", "$")):
        if text.startswith(opening, start):
            cursor = start + len(opening)
            while cursor < len(text):
                if text.startswith(closing, cursor) and text[cursor - 1] != "\\":
                    return cursor + len(closing)
                cursor += 1
            return len(text)
    return None


def _script_end(text: str, start: int) -> int:
    """消费完整脚本参数，复杂分组保持不透明。"""
    cursor = start + 1
    while cursor < len(text) and text[cursor].isspace():
        cursor += 1
    if cursor >= len(text):
        return cursor
    if text[cursor] != "{":
        command = re.match(r"\\[A-Za-z]+", text[cursor:])
        return cursor + (len(command[0]) if command else 1)
    depth = 0
    while cursor < len(text):
        if text[cursor] == "{" and (not cursor or text[cursor - 1] != "\\"):
            depth += 1
        elif text[cursor] == "}" and text[cursor - 1] != "\\":
            depth -= 1
            if depth == 0:
                return cursor + 1
        cursor += 1
    return len(text)


def scripts(text: str) -> list[tuple[str, str, str]]:
    """提取文本全部脚本（角色、参数、基字），供作用域守卫与几何保险丝复用。"""
    result = []
    subs = str.maketrans(SUBS, "0123456789+-=()aehijklmnoprstuvx")
    supers = str.maketrans(SUPERS, "0123456789+-=()in")
    for match in re.finditer(r"(?<!\\)[_^]", text):
        end = _script_end(text, match.start())
        value = text[match.end():end].strip()
        if value.startswith("{") and value.endswith("}"):
            value = value[1:-1]
        result.append((match.start(), match[0], value, _script_base(text, match.start())))
    for alphabet, role, table in ((SUBS, "_", subs), (SUPERS, "^", supers)):
        result.extend((match.start(), role, match[0].translate(table), _script_base(text, match.start()))
                      for match in re.finditer("[" + re.escape(alphabet) + "]+", text))
    return [(role, value, base) for _, role, value, base in sorted(result)]


def lost_scripts(old: list, new: list) -> list[tuple[str, str, str]]:
    """返回 old 中未在 new 里保住基字与作用范围的脚本，供几何保险丝逐项裁决。"""
    remaining = list(new)
    lost = []
    for role, value, base in old:
        matched = next((index for index, (kind, argument, anchor) in enumerate(remaining)
                        if kind == role and anchor == base and _script_argument_size(argument) >= _script_argument_size(value)), None)
        if matched is None:
            lost.append((role, value, base))
        else:
            remaining.pop(matched)
    return lost


def _script_argument_size(value: str) -> int:
    """比较实际脚本范围，引用方括号和已知显示包装不冒充作用字符。"""
    value = re.sub(r"\\(?:mathrm|textrm|mathit|text)\b", "", value)
    return len(re.sub(r"[{}\[\]\s]", "", value))


def _script_base(text: str, start: int) -> str:
    """记录简单脚本的直接基字，不能把同一个下标移动到另一个元素。"""
    prefix = text[:start].rstrip("$ ")
    base = re.search(r"[A-Za-z]+$", prefix)
    if not base:
        return ""
    # 同一元素在不同位置重复出现时，下标也不能从第一次转移到第二次。
    occurrences = len(re.findall(r"(?<![A-Za-z])" + re.escape(base[0]) + r"(?![A-Za-z])", prefix))
    return f"{base[0]}:{occurrences}"


def _without_math(text: str) -> str:
    """去掉定界数学，仅用于检查新增的正文强调符。"""
    return re.sub(r"(?<!\\)\$\$.*?\$\$|(?<!\\)\$.*?\$|\\\(.*?\\\)|\\\[.*?\\\]", "", text, flags=re.S)


def _math_damage(text: str) -> Counter:
    """度量新增的未闭合定界和数学参数，不把普通集合字面量当错误。"""
    damage = Counter()
    position = 0
    while position < len(text):
        end = _math_end(text, position)
        if end is None:
            position += 1
            continue
        opening = next(marker for marker in ("$$", r"\(", r"\[", "$") if text.startswith(marker, position))
        closing = {"$$": "$$", "$": "$", r"\(": r"\)", r"\[": r"\]"}[opening]
        chunk = text[position:end]
        if len(chunk) < len(opening) + len(closing) or not chunk.endswith(closing):
            damage["delimiter"] += 1
        depth = 0
        for index, char in enumerate(chunk):
            if index and chunk[index - 1] == "\\":
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                if depth:
                    depth -= 1
                else:
                    damage["brace"] += 1
        damage["brace"] += depth
        position = end
    # 处理由删除整个数学包装后遗留的孤立右括号；原有普通括号不加重。
    plain = _without_math(text)
    damage["orphan_brace"] = max(0, len(re.findall(r"(?<!\\)}", plain)) - len(re.findall(r"(?<!\\){", plain)))
    return damage
