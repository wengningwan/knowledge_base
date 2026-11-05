"""用章节和完整证据单元组织论文，不推断未报告的实验事实。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from yuxi.knowledge.chunking.ragflow_like.nlp import count_tokens
from yuxi.knowledge.chunking.source import SourceDocument, source_sha256

PAPER_CHUNKER_VERSION = "paper_rules_v6"
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
CAPTION = re.compile(
    r"^(?:#{1,6}\s+)?(?:\*\*)?(table|figure|fig[.．]?|scheme|表|图)\s*(S?\d+(?:[.-]\d+)*)[.:：、．]?\s*",
    re.I,
)
DEFINITION = re.compile(
    r"^(?:式中|其中|这里|where\b|in which\b|here[,\s]|in\s+(?:eq\.?|equation)\s*\(?\d+\)?\s*[,，:：])",
    re.I,
)
INTRODUCTION = re.compile(
    r"(?:如下|可得|得到|写作|公式为|方程为|given by|expressed as|written as|defined as|as follows|following equation)"
    r"\s*[:：]?$",
    re.I,
)
IDENTIFIER = re.compile(
    r"\b(sample|specimen|experiment|run)\s+([A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*)\b"
    r"|(?:样品|实验)\s*([A-Za-z0-9]+(?:[-_][A-Za-z0-9]+)*)",
    re.I,
)


@dataclass
class Evidence:
    """保留原文单元及不连续来源范围。"""

    kind: str
    ranges: list[tuple[int, int]]
    headings: tuple[tuple[int, str, int, int], ...]
    warnings: list[str] = field(default_factory=list)


def chunk_paper(
    text: str,
    file_id: str,
    filename: str,
    config: dict[str, Any],
    *,
    parse_assets: dict | None = None,
) -> list[dict[str, Any]]:
    """直接生成含完整检索正文和显式来源的论文分块。"""
    target = max(1, int(config.get("chunk_token_num", 512) or 512))
    title, title_range, parsed_units = _parse_units(text)
    units = _group_evidence(text, parsed_units)

    groups: list[list[Evidence]] = []
    for unit in units:
        body = _body(text, unit)
        previous = groups[-1] if groups else []
        # 对象标识和章节是组合边界，图表与公式始终保留为完整证据。
        if (
            previous
            and unit.kind in {"text", "metadata"}
            and all(part.kind == unit.kind for part in previous)
            and unit.headings == previous[0].headings
            and _identifiers(body) == _identifiers("\n".join(_body(text, part) for part in previous))
            and count_tokens("\n\n".join(_body(text, part) for part in [*previous, unit])) <= target
        ):
            previous.append(unit)
        else:
            groups.append([unit])

    digest = source_sha256(text)
    source = SourceDocument(text, parse_assets)
    records = []
    for index, group in enumerate(groups):
        body = "\n\n".join(_body(text, unit) for unit in group)
        # 浮图在 PDF 中的位置不证明章节归属，图表保留独立上下文。
        visual = all(unit.kind in {"figure", "figure_caption", "table", "table_caption"} for unit in group)
        headings = () if visual or group[0].kind == "metadata" else group[0].headings
        section_path = [heading[1] for heading in headings]
        content = f"论文：{title or filename}\n章节：{' > '.join(section_path) or '正文'}\n\n{body}"
        ranges = [title_range] if title_range else []
        ranges.extend((heading[2], heading[3]) for heading in headings)
        ranges.extend(span for unit in group for span in unit.ranges)
        evidence_ids = [f"evidence_{unit.ranges[0][0]}" for unit in group]
        warnings = list(dict.fromkeys(warning for unit in group for warning in unit.warnings))
        if count_tokens(content) > target:
            warnings.append("over_target_tokens")
        kinds = {unit.kind for unit in group}
        metadata = {
            "source_sha256": digest,
            "chunker_version": PAPER_CHUNKER_VERSION,
            "section_path": section_path,
            "content_type": next(iter(kinds)) if len(kinds) == 1 else "mixed",
            "evidence_ids": evidence_ids,
            "source_spans": [source.span(start, end) for start, end in dict.fromkeys(ranges)],
            "warnings": warnings,
        }
        records.append(
            {
                "id": f"{file_id}_chunk_{index}",
                "chunk_id": f"{file_id}_chunk_{index}",
                "file_id": file_id,
                "filename": filename,
                "source": filename,
                "chunk_index": index,
                "content": content,
                "source_metadata": metadata,
                "start_char_pos": None,
                "end_char_pos": None,
                "start_token_pos": None,
                "end_token_pos": None,
                "extraction_result": None,
            }
        )
    return records


def _parse_units(text: str) -> tuple[str | None, tuple[int, int] | None, list[Evidence]]:
    """先扫描结构边界，公式和表格内部换行不触发切分。"""
    lines = text.splitlines(keepends=True)
    starts = []
    offset = 0
    for line in lines:
        starts.append(offset)
        offset += len(line)
    headings: list[tuple[int, str, int, int]] = []
    title = None
    title_range = None
    units = []
    first_content = next((
        index for index, line in enumerate(lines)
        if line.strip() and _line_kind(line.strip()) not in {"figure", "metadata"}
    ), None)
    cursor = 0
    while cursor < len(lines):
        line = lines[cursor].strip()
        body_start = None
        if not line:
            cursor += 1
            continue
        caption = CAPTION.match(line)
        heading = _heading(line) if not caption else None
        if heading:
            level, name, heading_length = heading
            line_start = starts[cursor] + len(lines[cursor]) - len(lines[cursor].lstrip())
            heading_range = (line_start, line_start + heading_length)
            # 解析器可能用 H2 标论文名、H1 标章节；文档题名只确定一次。
            if (
                cursor == first_content
                and HEADING.match(line)
                and not _is_back_matter_heading(name)
                and not re.match(r"^\d+(?:\.\d+)*\.?\s", name)
                and name.casefold()
                not in {
                    "abstract",
                    "introduction",
                    "methods",
                    "materials and methods",
                    "experimental",
                    "results",
                    "discussion",
                    "results and discussion",
                    "conclusion",
                    "conclusions",
                    "references",
                    "bibliography",
                    "摘要",
                    "引言",
                    "实验方法",
                    "材料与方法",
                    "结果",
                    "讨论",
                    "结论",
                    "参考文献",
                }
            ):
                title, title_range = name, heading_range
                cursor += 1
                continue
            while heading:
                level, name, heading_length = heading
                heading_range = (line_start, line_start + heading_length)
                headings = [entry for entry in headings if entry[0] < level]
                headings.append((level, name, *heading_range))
                remainder = line[heading_length:]
                line_start = heading_range[1] + len(remainder) - len(remainder.lstrip())
                line = remainder.strip()
                heading = _heading(line) if line else None
            if not line:
                cursor += 1
                continue
            body_start = line_start

        first = cursor
        markdown_table = _is_markdown_table(lines, cursor)
        kind = "table" if markdown_table else _line_kind(line)
        warnings = []
        cursor += 1
        if kind == "code":
            fence = re.match(r"(`{3,}|~{3,})", line)[0]
            while cursor < len(lines):
                closing = re.fullmatch(re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", lines[cursor].strip())
                cursor += 1
                if closing:
                    break
        elif kind == "formula":
            if line.startswith("$$"):
                closed = line.count("$$") >= 2
                closing = "$$"
            elif line.startswith(r"\["):
                closing = r"\]"
                closed = closing in line
            else:
                environment = re.match(r"\\begin\{([^}]+)\}", line)[1]
                closing = rf"\end{{{environment}}}"
                closed = closing in line
            while not closed and cursor < len(lines) and not _heading(lines[cursor].strip()):
                closed = closing in lines[cursor]
                cursor += 1
            if not closed:
                warnings.append("unclosed_formula")
        elif kind == "table" and re.match(r"<table\b", line, re.I):
            while "</table>" not in "".join(lines[first:cursor]).lower() and cursor < len(lines):
                if _heading(lines[cursor].strip()):
                    break
                cursor += 1
            if "</table>" not in "".join(lines[first:cursor]).lower():
                warnings.append("unclosed_table")
        elif kind == "table":
            while cursor < len(lines) and lines[cursor].strip() and "|" in lines[cursor]:
                cursor += 1
        elif kind not in {"figure", "metadata"}:
            while cursor < len(lines) and lines[cursor].strip():
                next_line = lines[cursor].strip()
                if _heading(next_line) or _line_kind(next_line) != "text" or _is_markdown_table(lines, cursor):
                    break
                cursor += 1
        start = body_start if body_start is not None else starts[first] + len(lines[first]) - len(lines[first].lstrip())
        end = starts[cursor - 1] + len(lines[cursor - 1].rstrip())
        if not any(
            re.sub(r"^\d+(?:\.\d+)*[.、]?\s*", "", heading[1]).strip(" ()（）:：").casefold()
            in {"references", "bibliography", "参考文献"}
            for heading in headings
        ):
            unit = Evidence(kind, [(start, end)], tuple(headings), warnings)
            adjacent = units[-1] if units else None
            if adjacent and adjacent.headings == unit.headings and adjacent.kind == "figure":
                if kind == "text" and re.fullmatch(r"\([a-z]\)", _body(text, unit)):
                    adjacent.ranges.extend(unit.ranges)
                    continue
                if kind == "figure" and re.fullmatch(r"\([a-z]\)", text[slice(*adjacent.ranges[-1])]):
                    adjacent.ranges.extend(unit.ranges)
                    continue
            # 浮动图表不拥有正文阅读流；只在同节未完句和明确续句同时成立时跨过它。
            previous = next(
                (part for part in reversed(units)
                 if part.kind not in {"figure", "figure_caption", "table", "table_caption", "metadata"}), None
            )
            if previous and _continues_paragraph(text, previous, unit):
                previous.ranges.extend(unit.ranges)
                continue
            units.append(unit)
    return title, title_range, units


def _heading(line: str) -> tuple[int, str, int] | None:
    """识别 Markdown、独立数字小节及同行正文前的小节，返回原文标题长度。"""
    heading = HEADING.match(line)
    if heading:
        run_in = _heading(heading[2])
        if run_in and run_in[2] < len(heading[2]):
            return run_in[0], run_in[1], heading.start(2) + run_in[2]
        number = re.match(r"^(\d+(?:\.\d+)*)\.\s", heading[2])
        level = number[1].count(".") + 1 if number else len(heading[1])
        if _is_back_matter_heading(heading[2]):
            level = 1
        return level, heading[2], len(line)
    numbered = re.fullmatch(r"(\d+(?:\.\d+)+)(\.)?\s+([^\n]+)", line)
    if not numbered or not re.match(r"[A-Z\u4e00-\u9fff]", numbered[3]):
        return None
    if not numbered[2] and re.match(r"(?:K|Pa|kPa|MPa|GPa)(?:\s|$)", numbered[3]):
        return None
    title = re.match(r"([^.!?。！？\n]{1,120})\.(?:\s+|$)", numbered[3])
    if title and numbered[2]:
        return numbered[1].count(".") + 1, f"{numbered[1]}. {title[1]}", numbered.start(3) + title.end(1) + 1
    if len(numbered[3]) <= 120 and not numbered[3].endswith(("。", ".", "!", "?")):
        return numbered[1].count(".") + 1, f"{numbered[1]}. {numbered[3]}", len(line)
    return None


def _group_evidence(text: str, units: list[Evidence]) -> list[Evidence]:
    """先组织唯一图表说明，再绑定公式引导与定义。"""
    units = _bind_captions(units)
    grouped: list[Evidence] = []
    for unit in units:
        previous = grouped[-1] if grouped else None
        formula_owner = next(
            (part for part in reversed(grouped) if part.kind not in {"figure", "figure_caption", "metadata"}), None
        )
        same_formula_section = formula_owner is not None and formula_owner.headings == unit.headings
        body = _body(text, unit)
        footnote = re.match(r"^\$\^\{([a-z])\}\$", body) if unit.kind == "text" else None
        # 浮动正文可能已移入表前证据，脚注的紧邻关系必须回查原文。
        if (
            previous is not None and previous.kind == "table"
            and previous.headings == unit.headings and footnote
            and not text[max(end for _, end in previous.ranges) : unit.ranges[0][0]].strip()
            and any(
                re.search(rf"\^\{{{footnote[1]}\}}", text[start:end])
                for start, end in previous.ranges
                if _line_kind(text[start:end]) == "table"
                or _is_markdown_table(text[start:end].splitlines(), 0)
            )
        ):
            previous.ranges.extend(unit.ranges)
        elif (
            same_formula_section and formula_owner.kind == "formula"
            and unit.kind == "text" and DEFINITION.match(body)
        ):
            reference = re.match(r"^in\s+(?:eq\.?|equation)\s*\(?(\d+)\)?", body, re.I)
            labels = re.findall(r"\\tag\{(\d+)\}", _body(text, formula_owner))
            if reference and reference[1] not in labels:
                unit.warnings.append("unresolved_formula_reference")
                grouped.append(unit)
            else:
                formula_owner.ranges.extend(unit.ranges)
        elif (
            same_formula_section
            and unit.kind == "formula"
            and formula_owner.kind == "text"
            and INTRODUCTION.search(
                re.sub(r"(?:\$\^\{[\d\s,\[\]–-]+\}\$|[⁰¹²³⁴⁵⁶⁷⁸⁹]+)\s*$", "", _body(text, formula_owner))
            )
            and not DEFINITION.match(_body(text, formula_owner))
        ):
            formula_owner.kind = "formula"
            formula_owner.ranges.extend(unit.ranges)
            formula_owner.warnings.extend(unit.warnings)
        else:
            grouped.append(unit)
    return grouped


def _bind_captions(units: list[Evidence]) -> list[Evidence]:
    """仅绑定整条相邻图注链中唯一的一对一归属，不用扫描方向消除竞争。"""
    neighbors: dict[int, list[int]] = {}
    captions = [index for index, unit in enumerate(units) if unit.kind in {"figure_caption", "table_caption"}]
    for index in captions:
        neighbors.setdefault(index, [])
        for owner in _caption_candidates(units, index):
            neighbors[index].append(owner)
            neighbors.setdefault(owner, []).append(index)
    visited: set[int] = set()
    bound: set[int] = set()
    for index in captions:
        if index in visited:
            continue
        component: set[int] = set()
        pending = [index]
        while pending:
            current = pending.pop()
            if current in component:
                continue
            component.add(current)
            pending.extend(neighbors[current])
        visited.update(component)
        chain = sorted(component)
        caption_indices = [position for position in chain if units[position].kind.endswith("_caption")]
        # 候选只来自左右紧邻，所以连通分量是交替链；数量相等时才有唯一完整配对。
        if len(chain) == 2 * len(caption_indices):
            for left, right in zip(chain[::2], chain[1::2]):
                caption_index, owner_index = (left, right) if left in caption_indices else (right, left)
                caption, owner = units[caption_index], units[owner_index]
                owner.ranges = sorted([*owner.ranges, *caption.ranges])
                owner.warnings.extend(caption.warnings)
                bound.add(caption_index)
        else:
            for position in caption_indices:
                units[position].warnings.append("ambiguous_caption" if neighbors[position] else "unresolved_caption")
    return [unit for index, unit in enumerate(units) if index not in bound]


def _caption_candidates(units: list[Evidence], index: int) -> list[int]:
    """只接受同节紧邻的同类图表，不凭距离或图号猜归属。"""
    caption = units[index]
    owner_kind = "figure" if caption.kind == "figure_caption" else "table"
    return [
        position for position in (index - 1, index + 1)
        if 0 <= position < len(units)
        and units[position].kind == owner_kind
        and units[position].headings == caption.headings
    ]


def _is_back_matter_heading(name: str) -> bool:
    """用明确的文末栏目重置章节根，避免作者和致谢继承结论。"""
    return re.fullmatch(
        r"associated content|author information|acknowledg(?:e)?ments?|"
        r"appendix(?:\s+[A-Z])?(?:[.:]\s*.+)?|致谢|作者信息|附录(?:\s*[A-Z一二三四五六七八九十])?",
        name, re.I,
    ) is not None


def _line_kind(line: str) -> str:
    """识别需要完整保留的行级结构。"""
    if re.match(r"`{3,}|~{3,}", line):
        return "code"
    if re.match(r"(?:Received|Revised|Accepted|Published)(?:\s+online)?\s*:\s*.+\b(?:19|20)\d{2}\b", line, re.I):
        return "metadata"
    if line.startswith(("$$", r"\[")) or re.match(r"\\begin\{(?:equation|align|gather|displaymath)\*?\}", line):
        return "formula"
    if line.startswith("|") or re.match(r"<table\b", line, re.I):
        return "table"
    if line.startswith("![") or re.match(r"<img\b", line, re.I):
        return "figure"
    caption = CAPTION.match(line)
    if caption:
        # 正文中“Figure 1 shows ...”不是图注，避免吸走下一段讨论。
        remainder = line[caption.end() :]
        if not caption[0][-1].isspace() and re.match(r"[a-z]\b(?![.:：、])", remainder, re.I):
            return "text"
        remainder = re.sub(r"^(?:\([a-z]\)|[a-z])(?:[–-][a-z])?\s+", "", remainder, flags=re.I)
        if re.match(
            r"(?:shows?|provides?|lists?|compares?|presents?|indicates?|illustrates?|depicts?|"
            r"delineates?|reveals?|displays?|demonstrates?|summari[sz]es|summaries|is|was)\b|显示|表明|给出|中",
            remainder,
            re.I,
        ):
            return "text"
        return "table_caption" if caption[1].lower() in {"table", "表"} else "figure_caption"
    return "text"


def _is_markdown_table(lines: list[str], index: int) -> bool:
    """按紧随的分隔行识别没有外侧竖线的 Markdown 表头。"""
    return (
        index + 1 < len(lines)
        and "|" in lines[index]
        and re.fullmatch(r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*", lines[index + 1]) is not None
    )


def _body(text: str, unit: Evidence) -> str:
    """由已记录的范围装配证据正文。"""
    return "\n\n".join(text[start:end] for start, end in unit.ranges)


def _continues_paragraph(text: str, previous: Evidence, current: Evidence) -> bool:
    """用同节、对象字面标识和未完句约束续接，保持独立原文范围。"""
    if previous.kind != "text" or current.kind != "text" or previous.headings != current.headings:
        return False
    before, after = _body(text, previous), _body(text, current)
    if re.match(r"(?:Received|Revised|Accepted|Published)(?:\s+online)?\s*:", before, re.I):
        return False
    before_samples, after_samples = _sample_names(before), _sample_names(_opening_sentence(after))
    if before_samples and after_samples and not (
        _same_sample_family(before_samples, after_samples)
        or _same_sample_family(after_samples, before_samples)
    ):
        return False
    ending = re.sub(r"(?:\$\^\{[\d\s,\[\]–-]+\}\$|[⁰¹²³⁴⁵⁶⁷⁸⁹]+)\s*$", "", before).rstrip()
    numeric_pair = (
        re.search(r"(?:^|[\s(])[-+−]?(?:\d+(?:\.\d+)?|\.\d+)\s+(?:and|or)$", ending, re.I)
        and re.match(r"[-+−]?(?:\d+(?:\.\d+)?|\.\d+)(?=[\s,;:%)]|$)", after)
    )
    return bool(
        re.search(r"[A-Za-z0-9$)},]$", ending)
        and (re.match(r"[a-z]|\(\s*[<>≤≥]?\s*\d", after) or numeric_pair)
        and _identifiers(before) == _identifiers(after)
    )


def _identifiers(text: str) -> set[str]:
    """提取明确对象标识作为切分边界，排除泛称及另一个实验。"""
    result = set()
    for match in IDENTIFIER.finditer(text):
        identifier = match[2] or match[3]
        if re.fullmatch(r"[A-Z]|(?=[A-Za-z0-9_-]*\d)[A-Z0-9][A-Za-z0-9_-]*", identifier) is None:
            continue
        prefix = text[max(0, match.start() - 24) : match.start()]
        if re.search(r"\b(?:another|separate|different)\s+$|(?:另一个|另一项|另外一个|不同的)\s*$", prefix, re.I):
            continue
        category = (match[1] or ("sample" if match[0].startswith("样品") else "experiment")).lower()
        result.add(f"{category}:{identifier.lower()}")
    return result


def _sample_names(body: str) -> set[str]:
    """识别样品字面名称，仅用于阻止不同对象的正文误续接。"""
    names = {
        "sample:" + name.split(":", 1)[1]
        for name in _identifiers(body) if name.split(":", 1)[0] in {"sample", "specimen"}
    }
    for match in re.finditer(r"\b[A-Z][A-Za-z]*(?:-[A-Za-z0-9_]+)+\b", body):
        if re.search(r"\d|(?:^|-)x$", match[0]):
            names.add(f"sample:{match[0].lower()}")
    return names


def _opening_sentence(body: str) -> str:
    """仅取开头续句判断样品冲突，跳过数学与论文缩写内的句点。"""
    abbreviations = {"fig", "figs", "eq", "eqs", "ref", "refs", "dr", "mr", "mrs", "ms", "al", "vs", "no"}
    for match in re.finditer(r"[.!?。！？](?=\s+[A-Z][a-z]|\s*$)", body):
        prefix = body[:match.end()]
        if prefix.count("$") % 2:
            continue
        word = re.search(r"\b([A-Za-z]+)\.$", prefix)
        if word and (len(word[1]) == 1 or word[1].casefold() in abbreviations):
            continue
        return prefix
    return body


def _same_sample_family(left: set[str], right: set[str]) -> bool:
    """按字面系列前缀检查续句身份，具体样品之间不互相替代。"""
    return bool(left & right) or any(
        re.fullmatch(re.escape(name[:-1]) + r"\d+[A-Za-z0-9]*", sample)
        for name in left if name.endswith("-x")
        for sample in right
    )
