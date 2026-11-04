"""用章节和完整证据单元组织论文，不推断未报告的实验事实。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from yuxi.knowledge.chunking.ragflow_like.nlp import count_tokens
from yuxi.knowledge.chunking.source import SourceDocument, source_sha256

PAPER_CHUNKER_VERSION = "paper_rules_v3"
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
CAPTION = re.compile(
    r"^(?:#{1,6}\s+)?(?:\*\*)?(table|figure|fig\.?|scheme|表|图)\s*(S?\d+(?:[.-]\d+)*)(?![A-Za-z0-9_])[.:：、]?\s*",
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
CONDITION = re.compile(
    r"\b(?:prepared|treated|heated|annealed|synthesi[sz]ed|conducted)\b|制备|处理|加热|退火|合成|实验条件",
    re.I,
)
RESULT = re.compile(
    r"\b(?:yield(?:ed)?|result(?:s)?|achieved|increased|decreased|showed|obtained|reached|reported)\b"
    r"|结果|产率|达到|提高|降低|获得",
    re.I,
)


@dataclass
class Evidence:
    """保留结构化单元及不连续原文范围。"""

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
    caption_counts: dict[str, int] = {}
    for unit in parsed_units:
        caption = CAPTION.match(_body(text, unit)) if unit.kind == "figure_caption" else None
        if caption and caption[1].lower() in {"figure", "fig", "fig.", "图"}:
            number = caption[2].upper()
            caption_counts[number] = caption_counts.get(number, 0) + 1
    units = _group_evidence(text, parsed_units)
    condition_sources: dict[str, list[Evidence]] = {}
    for unit in units:
        body = _body(text, unit)
        if CONDITION.search(body) and re.search(r"\d", body):
            for identifier in _identifiers(body):
                condition_sources.setdefault(identifier, []).append(unit)

    groups: list[list[Evidence]] = []
    for unit in units:
        body = _body(text, unit)
        previous = groups[-1] if groups else []
        # 实验身份和章节是组合边界，图表与公式始终保留为完整证据。
        if (
            previous
            and unit.kind == "text"
            and all(part.kind == "text" for part in previous)
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
        headings = group[0].headings
        section_path = [heading[1] for heading in headings]
        content = f"论文：{title or filename}\n章节：{' > '.join(section_path) or '正文'}\n\n{body}"
        context_ranges = [title_range] if title_range else []
        context_ranges.extend((heading[2], heading[3]) for heading in headings)
        ranges = [span for unit in group for span in unit.ranges]
        evidence_ids = [f"evidence_{unit.ranges[0][0]}" for unit in group]
        associations = []
        if RESULT.search(body):
            for identifier in sorted(_identifiers(body)):
                candidates = condition_sources.get(identifier, [])
                status = "ambiguous" if len(candidates) > 1 else "unresolved"
                if len(candidates) == 1:
                    status = "linked"
                    condition = candidates[0]
                    if condition not in group:
                        content += f"\n\n实验条件（{identifier}）：\n{_body(text, condition)}"
                        ranges.extend(condition.ranges)
                        evidence_ids.append(f"evidence_{condition.ranges[0][0]}")
                associations.append({"identifier": identifier, "status": status})

        warnings = list(dict.fromkeys(warning for unit in group for warning in unit.warnings))
        if count_tokens(content) > target:
            warnings.append("over_target_tokens")
        kinds = {unit.kind for unit in group}
        metadata = {
            "source_sha256": digest,
            "chunker_version": PAPER_CHUNKER_VERSION,
            "section_path": section_path,
            "section_id": source_sha256(f"{file_id}:{digest}:{headings[-1][2]}") if headings else None,
            "content_type": next(iter(kinds)) if len(kinds) == 1 else "mixed",
            "evidence_ids": evidence_ids,
            "source_spans": [source.span(start, end) for start, end in dict.fromkeys(ranges)],
            "context_source_spans": [source.span(start, end) for start, end in dict.fromkeys(context_ranges)],
            "figure_ids": [],
            "figure_references": [],
            "warnings": warnings,
            "associations": associations,
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
    # 只有实际绑定完整图片的唯一图注才能成为目标，不能从相邻图片猜测归属。
    figures: dict[str, list[str]] = {}
    for record, group in zip(records, groups):
        for unit in group:
            if unit.kind != "figure" or "ambiguous_caption" in unit.warnings:
                continue
            captions = [CAPTION.match(text[start:end]) for start, end in unit.ranges]
            numbers = [
                caption[2].upper()
                for caption in captions
                if caption and caption[1].lower() in {"figure", "fig", "fig.", "图"}
            ]
            if len(numbers) == 1:
                record["source_metadata"]["figure_ids"] = numbers
                figures.setdefault(numbers[0], []).append(record["chunk_id"])
    for record, group in zip(records, groups):
        body = "\n\n".join(
            text[start:end]
            for unit in group
            for start, end in unit.ranges
            if unit.kind not in {"figure", "figure_caption", "table_caption"}
            and _line_kind(text[start:end]) not in {"figure_caption", "table_caption"}
        )
        for number in _figure_references(body):
            targets = figures.get(number, [])
            unique = len(targets) == 1 and caption_counts.get(number) == 1
            reference = {
                "figure_id": number,
                "status": "linked" if unique else "ambiguous" if caption_counts.get(number) else "missing",
            }
            if unique:
                reference["target_chunk_id"] = targets[0]
            record["source_metadata"]["figure_references"].append(reference)
    return records


def _figure_references(text: str) -> list[str]:
    """识别显式图号、有限数字区间与面板，补充材料编号独立归一。"""
    token = r"S?\d+(?:\.\d+)*(?:[a-z](?:\s*[–—-]\s*[a-z])?)?(?![A-Za-z0-9_]|\.\d)"
    separator = r"(?:[,，、]\s*(?:(?:and|or)\b\s*)?|\b(?:and|or)\b|&|[–—-])"
    pattern = rf"(?:\bFig(?:ure)?s?\.?\s*|图\s*)({token}(?:\s*{separator}\s*{token})*)"
    numbers = []
    for match in re.finditer(pattern, text, re.I):
        sequence = match[1]
        parts = list(re.finditer(r"(S?)(\d+(?:\.\d+)*)(?:[a-z](?:\s*[–—-]\s*[a-z])?)?", sequence, re.I))
        for index, part in enumerate(parts):
            label = ".".join(str(int(value)) for value in part[2].split("."))
            number = f"{part[1].upper()}{label}"
            if index:
                previous = parts[index - 1]
                separator = sequence[previous.end() : part.start()].strip()
                if previous[2].isdigit() and part[2].isdigit():
                    first, last = int(previous[2]), int(part[2])
                    if (
                        separator in {"-", "–", "—"}
                        and previous[1].upper() == part[1].upper()
                        and 0 < last - first <= 20
                    ):
                        numbers.extend(f"{part[1].upper()}{value}" for value in range(first + 1, last))
            numbers.append(number)
    return list(dict.fromkeys(numbers))


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
    first_content = next((index for index, line in enumerate(lines) if line.strip()), None)
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
            headings = [entry for entry in headings if entry[0] < level]
            headings.append((level, name, *heading_range))
            remainder = line[heading_length:]
            if not remainder.strip():
                cursor += 1
                continue
            body_start = heading_range[1] + len(remainder) - len(remainder.lstrip())
            line = remainder.strip()

        first = cursor
        markdown_table = _is_markdown_table(lines, cursor)
        kind = "table" if markdown_table else _line_kind(line)
        warnings = []
        cursor += 1
        if kind == "formula":
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
        elif kind not in {"figure"}:
            while cursor < len(lines) and lines[cursor].strip():
                next_line = lines[cursor].strip()
                if _heading(next_line) or _line_kind(next_line) != "text" or _is_markdown_table(lines, cursor):
                    break
                cursor += 1
        start = body_start if body_start is not None else starts[first] + len(lines[first]) - len(lines[first].lstrip())
        end = starts[cursor - 1] + len(lines[cursor - 1].rstrip())
        if not any(
            re.sub(r"^\d+(?:\.\d+)*[.、]?\s*", "", heading[1]).strip().casefold()
            in {"references", "bibliography", "参考文献"}
            for heading in headings
        ):
            unit = Evidence(kind, [(start, end)], tuple(headings), warnings)
            previous = units[-1] if units else None
            # 仅续接同节、同身份且以英文小写开头的相邻半句，不跨图表或公式重排原文。
            if previous and previous.kind == kind == "text" and previous.headings == unit.headings:
                previous_body = re.sub(
                    r"(?:\$\^\{[\d\s,\[\]–-]+\}\$|[⁰¹²³⁴⁵⁶⁷⁸⁹]+)\s*$", "", _body(text, previous)
                ).rstrip()
                if (
                    re.match(r"[a-z]", _body(text, unit))
                    and re.search(r"[A-Za-z0-9$)}]$", previous_body)
                    and _identifiers(_body(text, previous)) == _identifiers(_body(text, unit))
                ):
                    previous.ranges.extend(unit.ranges)
                    continue
            units.append(unit)
    return title, title_range, units


def _heading(line: str) -> tuple[int, str, int] | None:
    """识别 Markdown、独立数字小节及同行正文前的小节，返回原文标题长度。"""
    heading = HEADING.match(line)
    if heading:
        number = re.match(r"^(\d+(?:\.\d+)*)\.\s", heading[2])
        level = number[1].count(".") + 1 if number else len(heading[1])
        return level, heading[2], len(line)
    numbered = re.fullmatch(r"(\d+(?:\.\d+)+)(\.)?\s+([^\n]+)", line)
    if not numbered or not re.match(r"[A-Z\u4e00-\u9fff]", numbered[3]):
        return None
    title = re.match(r"([^.!?。！？\n]{1,120})\.(?:\s+|$)", numbered[3])
    if title and numbered[2]:
        return numbered[1].count(".") + 1, f"{numbered[1]}. {title[1]}", numbered.start(3) + title.end(1) + 1
    if len(numbered[3]) <= 120 and not numbered[3].endswith(("。", ".", "!", "?")):
        return numbered[1].count(".") + 1, f"{numbered[1]}. {numbered[3]}", len(line)
    return None


def _group_evidence(text: str, units: list[Evidence]) -> list[Evidence]:
    """先绑定上一式定义，再绑定显式引导、图注和表标题。"""
    grouped: list[Evidence] = []
    for index, unit in enumerate(units):
        previous = grouped[-1] if grouped else None
        same_section = previous is not None and previous.headings == unit.headings
        body = _body(text, unit)
        if unit.kind in {"figure_caption", "table_caption"}:
            owner_kind = "figure" if unit.kind == "figure_caption" else "table"
            previous_candidate = (
                same_section
                and previous.kind == owner_kind
                and not any(CAPTION.match(text[start:end]) for start, end in previous.ranges)
            )
            next_candidate = (
                index + 1 < len(units)
                and units[index + 1].kind == owner_kind
                and units[index + 1].headings == unit.headings
            )
            if previous_candidate and next_candidate:
                unit.warnings.append("ambiguous_caption")
                grouped.append(unit)
                continue
        if same_section and previous.kind == "formula" and unit.kind == "text" and DEFINITION.match(body):
            reference = re.match(r"^in\s+(?:eq\.?|equation)\s*\(?(\d+)\)?", body, re.I)
            labels = re.findall(r"\\tag\{(\d+)\}", _body(text, previous))
            if reference and reference[1] not in labels:
                unit.warnings.append("unresolved_formula_reference")
                grouped.append(unit)
            else:
                previous.ranges.extend(unit.ranges)
        elif (
            same_section
            and unit.kind == "formula"
            and previous.kind == "text"
            and INTRODUCTION.search(_body(text, previous))
            and not DEFINITION.match(_body(text, previous))
        ):
            previous.kind = "formula"
            previous.ranges.extend(unit.ranges)
            previous.warnings.extend(unit.warnings)
        elif (
            same_section
            and not any(CAPTION.match(text[start:end]) for start, end in previous.ranges)
            and (
                (previous.kind == "figure" and unit.kind == "figure_caption")
                or (
                    previous.kind == "table"
                    and unit.kind == "table_caption"
                    and not (
                        index + 1 < len(units)
                        and units[index + 1].kind == "table"
                        and units[index + 1].headings == unit.headings
                    )
                )
            )
        ):
            previous.ranges.extend(unit.ranges)
        elif (
            same_section
            and (
                (previous.kind == "table_caption" and unit.kind == "table")
                or (previous.kind == "figure_caption" and unit.kind == "figure")
            )
            and "ambiguous_caption" not in previous.warnings
        ):
            previous.kind = unit.kind
            previous.ranges.extend(unit.ranges)
            previous.warnings.extend(unit.warnings)
        else:
            grouped.append(unit)
    return grouped


def _line_kind(line: str) -> str:
    """识别需要完整保留的行级结构。"""
    if line.startswith(("$$", r"\[")) or re.match(r"\\begin\{(?:equation|align|gather|displaymath)\*?\}", line):
        return "formula"
    if line.startswith("|") or re.match(r"<table\b", line, re.I):
        return "table"
    if line.startswith("![") or re.match(r"<img\b", line, re.I):
        return "figure"
    caption = CAPTION.match(line)
    if caption:
        if re.match(r"\s*(?:[,，、]\s*(?:S?\d|and\b)|(?:and|or)\b|[–—-]\s*S?\d)", line[caption.end(2) :], re.I):
            return "text"
        # 正文中“Figure 1 shows ...”不是图注，避免吸走下一段讨论。
        if re.match(
            r"(?:shows?|provides?|lists?|compares?|presents?|indicates?|is|was)\b|显示|表明|给出|中",
            line[caption.end() :],
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


def _identifiers(text: str) -> set[str]:
    """仅关联单个大写字母或含数字的明确标识，排除泛称及另一个实验。"""
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
