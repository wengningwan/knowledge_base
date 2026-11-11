"""识别可校验的简单实验表格并保留单元格的原文坐标。"""

from __future__ import annotations

import re

type Span = tuple[int, int]
type CellPosition = tuple[int, int]

_HTML_TABLE_TAG = re.compile(r"</?(?:table|thead|tbody|tfoot|tr|th|td)\b", re.I)
_PIPE_DIVIDER = re.compile(r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*")
_FOOTNOTE = re.compile(r"\[\^|[†‡]|\d\s*\*|<sup\b", re.I)
_QUANTITY_POWER = re.compile(r"(?:(?<=[mgshLÅ])|(?<=10))\s*<sup\b[^>]*>\s*[+−-]?\d+\s*</sup\s*>", re.I)


def table_cells(text: str, start: int, end: int) -> list[list[Span]] | None:
    """区分普通原文、待核表格和带绝对坐标的规则表格。"""
    block = text[start:end]
    if _HTML_TABLE_TAG.search(block):
        rows = _html_cells(block)
    elif any(line.lstrip().startswith("|") or _PIPE_DIVIDER.fullmatch(line) for line in block.splitlines()):
        rows = _pipe_cells(block)
    else:
        return None
    if len(rows) < 2 or len(rows[0]) < 2 or any(len(row) != len(rows[0]) for row in rows):
        return []
    return [[(start + left, start + right) for left, right in row] for row in rows]


def table_cell_has_note(text: str) -> bool:
    """区分数量幂次和未解释的单元格脚注。"""
    return bool(_FOOTNOTE.search(_QUANTITY_POWER.sub("", text)))


def _pipe_cells(block: str) -> list[list[Span]]:
    """按原文管道定位规则单表，分隔行不计作数据。"""
    lines = block.splitlines(keepends=True)
    if len(lines) < 3 or not _PIPE_DIVIDER.fullmatch(lines[1]) or r"\|" in block:
        return []
    rows = []
    offset = 0
    for index, line in enumerate(lines):
        if "|" not in line:
            return []
        raw = line.rstrip("\r\n")
        bounds = [-1, *(match.start() for match in re.finditer(r"\|", raw)), len(raw)]
        pairs = [(left + 1, right) for left, right in zip(bounds, bounds[1:])]
        if raw.lstrip().startswith("|"):
            pairs = pairs[1:]
        if raw.rstrip().endswith("|"):
            pairs = pairs[:-1]
        rows.append([_trim_span(block, offset + left, offset + right) for left, right in pairs])
        offset += len(line)
        if index > 1 and _PIPE_DIVIDER.fullmatch(line):
            return []
    if len(rows[0]) != len(rows[1]):
        return []
    return [rows[0], *rows[2:]]


def _html_cells(block: str) -> list[list[Span]]:
    """接收单层 HTML 表格，保留原始单元格文本的位置。"""
    outer = re.fullmatch(r"\s*<table\b[^>]*>(.*?)</table\s*>\s*", block, flags=re.I | re.S)
    if outer is None or re.search(r"\b(?:rowspan|colspan)\s*=|<tfoot\b", block, re.I):
        return []
    body = outer.group(1)
    head = re.search(r"<thead\b[^>]*>(.*?)</thead\s*>", body, re.I | re.S)
    if head and (body[:head.start()].strip() or len(re.findall(r"<tr\b", head.group(1), re.I)) != 1):
        return []
    rows = []
    row_end = 0
    section_tags = r"(?:\s*</?(?:thead|tbody)\s*>)*\s*"
    for row in re.finditer(r"<tr\b[^>]*>(.*?)</tr\s*>", body, re.I | re.S):
        if not re.fullmatch(section_tags, body[row_end:row.start()]):
            return []
        inner = row.group(1)
        cells = []
        cell_end = 0
        offset = outer.start(1) + row.start(1)
        for cell in re.finditer(r"<(th|td)\b[^>]*>(.*?)</\1\s*>", inner, re.I | re.S):
            if inner[cell_end:cell.start()].strip() or _HTML_TABLE_TAG.search(cell.group(2)):
                return []
            if rows and cells and cell.group(1).lower() == "th":
                return []
            if re.search(r"<br\b", cell.group(2), re.I):
                return []
            cells.append(_trim_span(block, offset + cell.start(2), offset + cell.end(2)))
            cell_end = cell.end()
        if not cells or inner[cell_end:].strip():
            return []
        rows.append(cells)
        row_end = row.end()
    return rows if re.fullmatch(section_tags, body[row_end:]) else []


def _trim_span(text: str, start: int, end: int) -> Span:
    """去除单元格布局空白，保留实际原文范围。"""
    value = text[start:end]
    left = start + len(value) - len(value.lstrip())
    return left, max(left, start + len(value.rstrip()))
