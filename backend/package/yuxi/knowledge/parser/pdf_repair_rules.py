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


_STRUCTURE_PATTERN = r" {2,}(?=\r?\n)|\r?\n|^[ \t]*(?:[-+*]|\d+[.)])[ \t]+"


def preserves_protected(before: str, after: str) -> bool:
    """要求保护区逐字且按出现次数守恒，拒绝插入新保护内容。"""
    return (
        Counter(before[start:end] for start, end in protected_spans(before)) == Counter(after[start:end] for start, end in protected_spans(after))
        and re.findall(_STRUCTURE_PATTERN, before, re.M) == re.findall(_STRUCTURE_PATTERN, after, re.M)
    )


def whitespace_structure_divergence(before: str, after: str) -> bool:
    """保护内容逐字守恒而换行/缩进等空白结构失衡：差异只在排版结构，
    代码、链接等保护内容本身并未被触碰。"""
    return (
        Counter(before[start:end] for start, end in protected_spans(before)) == Counter(after[start:end] for start, end in protected_spans(after))
        and re.findall(_STRUCTURE_PATTERN, before, re.M) != re.findall(_STRUCTURE_PATTERN, after, re.M)
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


_SUBSCRIPT_TRANSLATION = str.maketrans("₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎", "0123456789+-=()")
_SUPERSCRIPT_TRANSLATION = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁱⁿ", "0123456789+-=()in")
_MATH_WRAPPER = re.compile(r"\$\$?(.*?)\$\$?", re.S)


def normalized_visible_text(text: str) -> str:
    """归一化为可比对的可见字符流：剥数学包装、折合上下标与连字、删除全部空白。

    仅用于字符守恒比较；大小写与孤立 ^ _ 保留，避免把数值或符号差异当排印差异。
    """
    text = unicodedata.normalize("NFC", str(text))
    text = _MATH_WRAPPER.sub(lambda match: match[1], text).replace("$", "")
    text = re.sub(r"[\^_]\{([^{}]*)\}", lambda match: match[1], text)
    text = text.translate(str.maketrans({"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", "−": "-", "–": "-", "—": "-"}))
    text = text.translate(_SUBSCRIPT_TRANSLATION).translate(_SUPERSCRIPT_TRANSLATION)
    return re.sub(r"\s+", "", text)


def visible_surplus(ocr: str, native: str) -> str:
    """返回 OCR 归一化后比原生文字多出的可见字符排序串；无多出为空。"""
    surplus = Counter(normalized_visible_text(ocr)) - Counter(normalized_visible_text(native))
    return "".join(sorted(surplus.elements()))


_LATEX_SYMBOL_FOLD = {
    "dagger": "†", "ddagger": "‡", "S": "§", "P": "¶",
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "varepsilon": "ε",
    "epsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ",
    "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ",
    "pi": "π", "rho": "ρ", "sigma": "σ", "varsigma": "ς", "tau": "τ",
    "upsilon": "υ", "phi": "φ", "varphi": "ϕ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π",
    "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    "ell": "ℓ", "hbar": "ℏ", "imath": "ı", "jmath": "ȷ",
    "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥", "ne": "≠", "neq": "≠",
    "approx": "≈", "sim": "∼", "simeq": "≃", "cong": "≅", "equiv": "≡",
    "propto": "∝", "infty": "∞", "partial": "∂", "nabla": "∇",
    "pm": "±", "mp": "∓", "times": "×", "div": "÷", "cdot": "·", "circ": "∘",
    "ast": "∗", "star": "⋆", "bullet": "•",
    "to": "→", "rightarrow": "→", "leftarrow": "←", "Rightarrow": "⇒",
    "Leftarrow": "⇐", "leftrightarrow": "↔", "uparrow": "↑", "downarrow": "↓",
    "mapsto": "↦", "in": "∈", "notin": "∉", "ni": "∋", "subset": "⊂", "supset": "⊃",
    "subseteq": "⊆", "supseteq": "⊇", "cup": "∪", "cap": "∩", "emptyset": "∅",
    "forall": "∀", "exists": "∃", "neg": "¬", "land": "∧", "lor": "∨",
    "sum": "∑", "prod": "∏", "coprod": "∐", "int": "∫", "iint": "∬", "iiint": "∭",
    "oint": "∮", "sqrt": "√", "prime": "′",
    "langle": "⟨", "rangle": "⟩", "lfloor": "⌊", "rfloor": "⌋", "lceil": "⌈", "rceil": "⌉",
    "angle": "∠", "perp": "⊥", "parallel": "∥", "mid": "∣",
    "ldots": "…", "dots": "…", "cdots": "⋯", "vdots": "⋮", "ddots": "⋱",
    "triangle": "△", "bigcirc": "○", "square": "□", "diamond": "◇",
    "checkmark": "✓", "degree": "°", "textdegree": "°",
}
_LATEX_DROP_COMMANDS = (
    "left", "right", "Bigg", "bigg", "Big", "big", "biggl", "biggr", "Bigl", "Bigr",
    "bigl", "bigr", "mathrm", "text", "textrm", "textit", "textbf", "texttt", "textsf", "textsc",
    "mathit", "mathbf", "mathcal", "mathsf", "mathtt", "mathbb", "mathfrak", "mathord",
    "operatorname", "mathopen", "mathclose", "mbox", "hbox", "displaystyle", "textstyle",
    "limits", "nolimits", "quad", "qquad", "hspace", "vspace",
    "rm", "it", "bf", "sf", "tt", "sc", "sl", "cal", "frak", "boldsymbol",
)
_LATEX_FOLD_PATTERN = re.compile(
    r"\\(" + "|".join(re.escape(name) for name in sorted(set(_LATEX_SYMBOL_FOLD) | set(_LATEX_DROP_COMMANDS), key=len, reverse=True)) + r")(?![a-zA-Z])"
)
_EQUIVALENT_PUNCTUATION = str.maketrans({
    "’": "'", "‘": "'", "“": '"', "”": '"',
    "\u0002": "-", "\u0003": "-",
    "∼": "~", "～": "~",
    "•": "●", "∙": "●",
    "¨": "", "ˆ": "", "˜": "", "ˇ": "", "˘": "", "¯": "", "´": "",
})
_GARBLED_EVIDENCE = re.compile(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f]|[ÄÅÇÉÑÖ]{3}")
_DISPLAY_MATH_COMMAND = re.compile(r"\\(?:begin\{|[dt]?frac|sqrt|left|Bigg|bigg|mathrm|partial|nabla)")
_MATH_SYMBOL_RUN = re.compile(r"[∂∑∏∫∇∞≪≫≡∝]{2}")


def _fold_latex_commands(text: str) -> str:
    """按已知命令名最长匹配折算 LaTeX 命令；后随字母视为更长命令名，不折算。"""
    return _LATEX_FOLD_PATTERN.sub(lambda match: _LATEX_SYMBOL_FOLD.get(match[1], ""), text)


def fold_equivalent_typography(text: str) -> str:
    """把已知等价排印折算为统一可见形态，供内容级守恒比较。

    LaTeX 命令须在空白删除前折算（命令边界依赖空白与标点）；直弯引号、波浪
    形态与原生层缺陷字符随后映射；预组合变音符经 NFD 剥组合符，与基字符加
    独立变音符形态归一；最后剥数学包装并删除残迹 ^ _ { } 与孤立反斜杠。
    """
    text = unicodedata.normalize("NFC", str(text))
    text = _fold_latex_commands(text)
    text = text.translate(_EQUIVALENT_PUNCTUATION)
    decomposed = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[{}^_\\]", "", normalized_visible_text(text))


def native_evidence_garbled(native: str) -> bool:
    """原生候选是否为公式乱码或控制字符主导：数学字体 ToUnicode 缺失特征。

    此类原生文字层不能作为字符守恒的 oracle，视觉复核是公式区域的终审。
    """
    return bool(_GARBLED_EVIDENCE.search(str(native)))


def unresolved_surplus(ocr: str, native: str) -> str:
    """字符守卫专用：返回需要人工裁决的内容级多出字符，空表示无差异或不可拦。

    原生候选缺失时视觉复核是唯一证据源；公式乱码与数学符号流的原生文字层
    不能作为字符守恒的 oracle；等价排印折算后守恒及纯标点差异不构成内容
    差异。三类均以视觉复核结论为准，仅数字、字母词与编号幻觉保留人工核对。
    """
    ocr, native = str(ocr), str(native)
    if not native.strip() or native_evidence_garbled(native):
        return ""
    if _DISPLAY_MATH_COMMAND.search(ocr) or _MATH_SYMBOL_RUN.search(native):
        return ""
    surplus = visible_surplus(fold_equivalent_typography(ocr), fold_equivalent_typography(native))
    if surplus and not any(ch.isalnum() for ch in surplus):
        return ""
    return surplus


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
