"""用章节和完整证据单元组织论文，不推断未报告的实验事实。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from yuxi.knowledge.chunking.ragflow_like.nlp import count_tokens
from yuxi.knowledge.chunking.source import SourceDocument, source_sha256

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
    r"(?:如下|可得|得到|写作|公式为|方程为|表达式为|表示为|定义为|按以下式计算|"
    r"given by|expressed as|written as|defined as|as follows|following (?:equation|(?:thermodynamic )?relationships?))"
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
    title, title_range, parsed_units = _parse_units(text, filename)
    units = _group_evidence(text, parsed_units)

    groups: list[list[Evidence]] = []
    for unit in units:
        body = _body(text, unit)
        previous = groups[-1] if groups else []
        previous_body = "\n\n".join(_body(text, part) for part in previous)
        # 对象标识和章节是组合边界，图表与公式始终保留为完整证据。
        if (
            previous
            and unit.kind in {"text", "metadata"}
            and all(part.kind == unit.kind for part in previous)
            and unit.headings == previous[0].headings
            and not unit.warnings and not any(part.warnings for part in previous)
            and _identifiers(body) == _identifiers(previous_body)
            and _sample_names(body) == _sample_names(previous_body)
            and count_tokens(f"{previous_body}\n\n{body}") <= target
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
            "chunk_preset_id": "paper",
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


def _parse_units(text: str, filename: str) -> tuple[str | None, tuple[int, int] | None, list[Evidence]]:
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
    body_headings = {
        "abstract", "introduction", "methods", "materials and methods", "experimental",
        "results", "discussion", "results and discussion", "conclusion", "conclusions",
        "references", "bibliography", "摘要", "引言", "实验方法", "材料与方法", "结果", "讨论", "结论", "参考文献",
    }
    body_section_seen = False
    front_title_repeated = False
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
        if (
            cursor == first_content and not HEADING.match(line)
            and re.sub(r"\s+", "", line).casefold()
            == re.sub(r"\s+", "", filename.rsplit(".", 1)[0]).casefold()
        ):
            start = starts[cursor] + len(lines[cursor]) - len(lines[cursor].lstrip())
            title, title_range = line, (start, starts[cursor] + len(lines[cursor].rstrip()))
            cursor += 1
            continue
        caption = CAPTION.match(line)
        heading = _heading(line) if not caption else None
        if heading:
            level, name, heading_length = heading
            line_start = starts[cursor] + len(lines[cursor]) - len(lines[cursor].lstrip())
            heading_range = (line_start, line_start + heading_length)
            body_section_seen |= (
                re.match(r"^\d+(?:\.\d+)*[.、]?\s", name) is not None
                or name.casefold() in body_headings - {"abstract", "摘要"}
            )
            if body_section_seen:
                front_title_repeated = False
            if title and not body_section_seen:
                title_key = re.sub(r"\s+", "", title).casefold()
                name_key = re.sub(r"\s+", "", name).casefold()
                repeated_end = cursor
                canonical_repeat = name_key == title_key
                if not canonical_repeat and cursor + 1 < len(lines):
                    continuation = lines[cursor + 1].strip()
                    if continuation and name_key + re.sub(r"\s+", "", continuation).casefold() == title_key:
                        canonical_repeat, repeated_end = True, cursor + 1
                alternate = None
                if front_title_repeated and len(name) >= 20:
                    alternate = next((
                        part for part in units if part.kind == "text" and not part.headings
                        and len(_body(text, part)) >= 20
                        and bool(re.search(r"[\u4e00-\u9fff]", _body(text, part)))
                        != bool(re.search(r"[\u4e00-\u9fff]", title))
                    ), None)
                if canonical_repeat or (
                    alternate and re.sub(r"\s+", "", _body(text, alternate)).casefold() == name_key
                ):
                    end = starts[repeated_end] + len(lines[repeated_end].rstrip())
                    units.append(Evidence("metadata", [(line_start, end)], ()))
                    headings = []
                    front_title_repeated = canonical_repeat
                    cursor = repeated_end + 1
                    continue
                front_title_repeated = False
            # 解析器可能用 H2 标论文名、H1 标章节；文档题名只确定一次。
            if (
                cursor == first_content
                and HEADING.match(line)
                and not _is_back_matter_heading(name)
                and not re.match(r"^\d+(?:\.\d+)*\.?\s", name)
                and name.casefold() not in body_headings
            ):
                title, title_range = name, heading_range
                cursor += 1
                continue
            while heading:
                level, name, heading_length = heading
                body_section_seen |= (
                    re.match(r"^\d+(?:\.\d+)*[.、]?\s", name) is not None
                    or name.casefold() in body_headings - {"abstract", "摘要"}
                )
                heading_range = (line_start, line_start + heading_length)
                # 参考文献结束正文父章节，后续同级栏目不再继承结论或作者信息。
                if _is_reference_heading(name):
                    headings = []
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
            closed = False
            while cursor < len(lines):
                closing = re.fullmatch(re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", lines[cursor].strip())
                cursor += 1
                if closing:
                    closed = True
                    break
            if not closed:
                warnings.append("unclosed_code")
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
        elif kind != "figure":
            while cursor < len(lines) and lines[cursor].strip():
                next_line = lines[cursor].strip()
                if _heading(next_line) or _line_kind(next_line) != "text" or _is_markdown_table(lines, cursor):
                    break
                cursor += 1
        start = body_start if body_start is not None else starts[first] + len(lines[first]) - len(lines[first].lstrip())
        end = starts[cursor - 1] + len(lines[cursor - 1].rstrip())
        if not any(_is_reference_heading(heading[1]) for heading in headings):
            units.append(Evidence(kind, [(start, end)], tuple(headings), warnings))
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
    """先组织图表及表注，再续接正文和绑定公式定义。"""
    units = _group_subfigures(text, units)
    units = _bind_captions(text, units)
    flow: list[Evidence] = []
    for unit in units:
        previous = flow[-1] if flow else None
        # 表注须在续句之前归属，紧邻关系仍按原文而非重组位置判断。
        if _is_table_note(text, previous, unit):
            previous.ranges.extend(unit.ranges)
            previous.warnings.extend(unit.warnings)
            continue
        previous = next(
            (part for part in reversed(flow)
             if part.kind not in {"figure", "figure_caption", "table", "table_caption", "metadata"}), None
        )
        if previous and _continues_paragraph(text, previous, unit):
            previous.ranges.extend(unit.ranges)
        else:
            flow.append(unit)
    grouped: list[Evidence] = []
    for unit in flow:
        formula_owner = next(
            (part for part in reversed(grouped) if part.kind not in {"figure", "figure_caption", "metadata"}), None
        )
        same_formula_section = formula_owner is not None and formula_owner.headings == unit.headings
        body = _body(text, unit)
        if (
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


def _is_table_note(text: str, table: Evidence | None, note: Evidence) -> bool:
    """仅接受原文紧邻且具有明确说明或同字母标记的表注。"""
    if table is None or table.kind != "table" or note.kind != "text" or table.headings != note.headings:
        return False
    if not _adjacent_in_source(text, table, note):
        return False
    body = _body(text, note)
    if re.match(r"^(?:注|Notes?)\s*[:：]", body, re.I):
        return True
    footnote = re.match(r"^(?:\$\^\{([a-z])\}\$|([a-z])[)）](?=\s|$))", body)
    if not footnote:
        return False
    marker = footnote[1] or footnote[2]
    for start, end in table.ranges:
        source = text[start:end]
        kind = _line_kind(source)
        if (
            (kind == "table" or _is_markdown_table(source.splitlines(), 0))
            and re.search(rf"\^\{{{marker}\}}", source)
        ):
            return True
        if footnote[2] and kind == "table_caption" and re.search(rf"(?<![A-Za-z]){marker}[)）]\s*$", source):
            return True
    return False


def _group_subfigures(text: str, units: list[Evidence]) -> list[Evidence]:
    """仅组织连续字母标记、图片交替且以明确图注结束的多子图。"""
    grouped: list[Evidence] = []
    cursor = 0
    while cursor < len(units):
        first = units[cursor]
        position = cursor
        panels: list[Evidence] = []
        while position + 1 < len(units):
            picture, label = units[position:position + 2]
            if (
                picture.kind != "figure" or label.kind != "text"
                or picture.headings != first.headings or label.headings != first.headings
            ):
                break
            marker = re.fullmatch(r"[（(]([a-z])[)）][^\n]*", _body(text, label))
            if not marker or marker[1] != chr(ord("a") + len(panels) // 2):
                break
            if not _adjacent_in_source(text, picture, label):
                break
            if panels and not _adjacent_in_source(text, panels[-1], picture):
                break
            panels.extend((picture, label))
            position += 2
        if (
            len(panels) >= 4 and position < len(units)
            and units[position].kind == "figure_caption" and units[position].headings == first.headings
            and _adjacent_in_source(text, panels[-1], units[position])
        ):
            grouped.append(Evidence(
                "figure", [span for part in panels for span in part.ranges], first.headings,
                [warning for part in panels for warning in part.warnings],
            ))
            cursor = position
        else:
            grouped.append(first)
            cursor += 1
    return grouped


def _bind_captions(text: str, units: list[Evidence]) -> list[Evidence]:
    """仅绑定唯一图注归属，再组织已确认单图前的独立标记。"""
    units = _merge_bilingual_captions(text, units)
    neighbors: dict[int, list[int]] = {}
    captions = [index for index, unit in enumerate(units) if unit.kind in {"figure_caption", "table_caption"}]
    for index in captions:
        neighbors.setdefault(index, [])
        for owner in _caption_candidates(text, units, index):
            neighbors[index].append(owner)
            neighbors.setdefault(owner, []).append(index)
    visited: set[int] = set()
    bound: set[int] = set()
    single_figures: set[int] = set()
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
                marker_index = (caption_index + owner_index) // 2 if abs(caption_index - owner_index) == 2 else None
                if owner.kind == "figure" and len(owner.ranges) == 1 and marker_index is None:
                    single_figures.add(owner_index)
                ranges = [*owner.ranges, *caption.ranges]
                if marker_index is not None:
                    ranges.extend(units[marker_index].ranges)
                    owner.warnings.extend(units[marker_index].warnings)
                    bound.add(marker_index)
                owner.ranges = sorted(ranges)
                owner.warnings.extend(caption.warnings)
                bound.add(caption_index)
        else:
            for position in caption_indices:
                units[position].warnings.append("ambiguous_caption" if neighbors[position] else "unresolved_caption")
    grouped: list[Evidence] = []
    for index, unit in enumerate(units):
        if index in bound:
            continue
        previous = grouped[-1] if grouped else None
        if (
            index in single_figures and previous is not None and previous.kind == "text"
            and previous.headings == unit.headings
            and _is_figure_marker(_body(text, previous))
            and _adjacent_in_source(text, previous, unit)
        ):
            unit.ranges = [*previous.ranges, *unit.ranges]
            unit.warnings.extend(previous.warnings)
            grouped.pop()
        grouped.append(unit)
    for unit in grouped:
        if unit.kind == "text" and _is_figure_marker(_body(text, unit)):
            unit.warnings.append("unresolved_figure_marker")
    return grouped


def _merge_bilingual_captions(text: str, units: list[Evidence]) -> list[Evidence]:
    """只配对相邻同节、同类型同编号且一中一英的两个说明。"""
    merged: list[Evidence] = []
    for unit in units:
        previous = merged[-1] if merged else None
        if (
            previous is not None and unit.kind in {"figure_caption", "table_caption"}
            and previous.kind == unit.kind and previous.headings == unit.headings
            and len(previous.ranges) == 1 and len(unit.ranges) == 1
        ):
            before = CAPTION.match(_body(text, previous))
            after = CAPTION.match(_body(text, unit))
            if (
                before and after and before[2].upper() == after[2].upper()
                and before[1].casefold() != "scheme" and after[1].casefold() != "scheme"
                and (before[1] in {"图", "表"}) != (after[1] in {"图", "表"})
                and _adjacent_in_source(text, previous, unit)
            ):
                previous.ranges.extend(unit.ranges)
                previous.warnings.extend(unit.warnings)
                continue
        merged.append(unit)
    return merged


def _caption_candidates(text: str, units: list[Evidence], index: int) -> list[int]:
    """只接受原文同节紧邻的图表，图片前后可隔一个裸标记。"""
    caption = units[index]
    owner_kind = "figure" if caption.kind == "figure_caption" else "table"
    candidates = []
    for direction in (-1, 1):
        position = index + direction
        if (
            0 <= position < len(units) and owner_kind == "figure"
            and units[position].kind == "text" and units[position].headings == caption.headings
            and _is_figure_marker(_body(text, units[position]))
        ):
            position += direction
        if not (0 <= position < len(units)):
            continue
        if units[position].kind != owner_kind or units[position].headings != caption.headings:
            continue
        path = units[min(position, index):max(position, index) + 1]
        if any(not _adjacent_in_source(text, left, right) for left, right in zip(path, path[1:])):
            continue
        candidates.append(position)
    return candidates


def _is_figure_marker(body: str) -> bool:
    """识别尚需明确图片归属的独立单字母标记。"""
    return re.fullmatch(r"[（(][a-z][)）]", body) is not None


def _adjacent_in_source(text: str, left: Evidence, right: Evidence) -> bool:
    """按真实原文范围判断顺序及紧邻，不越过扫描跳过的内容。"""
    end = max(end for _, end in left.ranges)
    start = min(start for start, _ in right.ranges)
    return end <= start and not text[end:start].strip()


def _is_back_matter_heading(name: str) -> bool:
    """用明确的文末栏目重置章节根，避免作者和致谢继承结论。"""
    return re.fullmatch(
        r"associated content|author information|acknowledg(?:e)?ments?|"
        r"appendix(?:\s+[A-Z])?(?:[.:]\s*.+)?|致谢|作者信息|附录(?:\s*[A-Z一二三四五六七八九十])?",
        name, re.I,
    ) is not None


def _is_reference_heading(name: str) -> bool:
    """识别拥有独立范围的参考文献栏目。"""
    return re.sub(r"^\d+(?:\.\d+)*[.、]?\s*", "", name).strip(" ()（）:：").casefold() in {
        "references", "bibliography", "参考文献",
    }


def _line_kind(line: str) -> str:
    """识别需要完整保留的行级结构。"""
    if re.match(r"`{3,}|~{3,}", line):
        return "code"
    if re.match(r"(?:Received|Revised|Accepted|Published)(?:\s+online)?\s*:\s*.+\b(?:19|20)\d{2}\b", line, re.I):
        return "metadata"
    if re.match(r"(?:收稿日期|修回日期|改回日期|录用日期|发表日期|作者简介|基金项目|引用格式)\s*[:：]", line):
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
            r"delineates?|reveals?|displays?|demonstrates?|summari[sz]es|summaries|is|was)\b|"
            r"显示|表明|给出|中|所示|为|示出|展示了|可以看出",
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
    if previous.warnings or current.warnings:
        return False
    before, after = _body(text, previous), _body(text, current)
    if re.match(r"^(?:注|Notes?)\s*[:：]", after, re.I):
        return False
    if re.match(r"^[a-z][)）](?:\s|$)", after):
        return False
    if re.match(r"(?:Received|Revised|Accepted|Published)(?:\s+online)?\s*:", before, re.I):
        return False
    before_samples, after_samples = _sample_names(before), _sample_names(_opening_sentence(after))
    if before_samples and after_samples and not (
        _same_sample_family(before_samples, after_samples)
        or _same_sample_family(after_samples, before_samples)
    ):
        return False
    citation = r"(?:\$\^\{[\d\s,\[\]–-]+\}\$|\^\{[\d\s,\[\]–-]+\}|\[[\d\s,–-]+\]|[⁰¹²³⁴⁵⁶⁷⁸⁹]+)"
    ending = re.sub(rf"{citation}\s*$", "", before).rstrip()
    cited_opening = re.sub(rf"^{citation}\s*", "", after)
    cited_name = (
        cited_opening != after and re.search(r"\b(?:on|of|to|for|with|and|or|the|a|an)$", ending, re.I)
        and re.match(r"[A-Z][a-z]", cited_opening)
    )
    cited_author = (
        cited_opening != after and re.search(r"等人?$", ending)
        and re.match(r"[\u4e00-\u9fff]", cited_opening)
    )
    # 行首数字方括号也可能是列表，仅在明确作者或未完英文对象引导后越过它。
    opening = cited_opening if cited_name or cited_author else after
    numeric_pair = (
        re.search(r"(?:^|[\s(])[-+−]?(?:\d+(?:\.\d+)?|\.\d+)\s+(?:and|or)$", ending, re.I)
        and re.match(r"[-+−]?(?:\d+(?:\.\d+)?|\.\d+)(?=[\s,;:%)]|$)", opening)
    )
    chinese_pair = re.search(r"[\u4e00-\u9fff]$", ending) and re.match(r"[\u4e00-\u9fff]", opening)
    chinese_value = (
        re.search(r"(?:为|至|达到)$", ending)
        and re.match(r"[-+−]?\d+(?:\.\d+)?\s*(?:[%％、，,]|$)", opening)
    )
    parenthetical_case = re.match(r"\(case\s+[a-z0-9]+\)\s+[a-z]", opening)
    hyphenated_word = re.search(r"\b[A-Z]?[a-z]{2,}[-‐‑]$", ending) and re.match(r"[a-z]", opening)
    return bool(
        _identifiers(before) == _identifiers(after)
        and (
            hyphenated_word or (
                re.search(r"[A-Za-z0-9$)},\u4e00-\u9fff]$", ending)
                and (
                    re.match(r"[a-z]|\(\s*[<>≤≥]?\s*\d", opening)
                    or numeric_pair or chinese_pair or chinese_value or cited_name or parenthetical_case
                )
            )
        )
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
