"""展开实验表格并保留每个逻辑单元格的原文坐标。"""

from __future__ import annotations

import re
from dataclasses import dataclass

type Span = tuple[int, int]
type CellPosition = tuple[int, int]

_HTML_TABLE_TAG = re.compile(r"</?(?:table|thead|tbody|tfoot|tr|th|td)\b", re.I)
_PIPE_DIVIDER = re.compile(r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*")
_FOOTNOTE = re.compile(r"\[\^|[†‡]|\d\s*\*|<sup\b", re.I)
_UNIT_POWER = re.compile(r"(?<=[A-Za-zµμÅ])\s*<sup\b[^>]*>\s*[+−-]?\d+\s*</sup\s*>", re.I)
_ATTR = re.compile(r"""\s+([^\s"'=<>`]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?""")


@dataclass
class TableGrid:
    """保留原行序、展开后的列及合并格的起始位置。"""

    rows: list[list[Span]]
    header_rows: int
    origins: list[list[CellPosition]]


def table_grid(text: str, start: int, end: int) -> TableGrid | None:
    """区分普通原文、待核表格和带绝对坐标的数表。"""
    block = text[start:end]
    if _HTML_TABLE_TAG.search(block):
        grid = _html_grid(block)
    elif any(line.lstrip().startswith("|") or _PIPE_DIVIDER.fullmatch(line) for line in block.splitlines()):
        rows = _pipe_cells(block)
        grid = TableGrid(rows, 1, [[(row, column) for column in range(len(cells))] for row, cells in enumerate(rows)])
    else:
        return None
    if (
        grid is None or len(grid.rows) <= grid.header_rows or len(grid.rows[0]) < 2
        or any(len(row) != len(grid.rows[0]) for row in grid.rows)
        or _FOOTNOTE.search(_UNIT_POWER.sub("", block))
    ):
        return TableGrid([], 0, [])
    if start:
        grid.rows = [[(start + left, start + right) for left, right in row] for row in grid.rows]
    return grid


def table_cells(text: str, start: int, end: int) -> list[list[Span]] | None:
    """返回每行展开后的原文坐标。"""
    grid = table_grid(text, start, end)
    return grid.rows if grid is not None else None


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


def _html_grid(block: str) -> TableGrid | None:
    """展开 HTML 表头和合并格，拒绝重叠及跨表头边界的布局。"""
    outer = re.fullmatch(r"\s*<table\b[^>]*>(.*?)</table\s*>\s*", block, flags=re.I | re.S)
    if outer is None:
        return None
    body = outer.group(1)
    head = re.match(r"\s*<thead\b[^>]*>(.*?)</thead\s*>", body, re.I | re.S)
    if head and re.search(r"</?thead\b", body[head.start(1):head.end(1)] + body[head.end():], re.I):
        return None

    rows, header_tags = [], []
    row_end = header_rows = 0
    section_tags = r"(?:\s*</?(?:thead|tbody)\b[^>]*>)*\s*" if head else r"(?:\s*</?tbody\b[^>]*>)*\s*"
    for row in re.finditer(r"<tr\b[^>]*>(.*?)</tr\s*>", body, re.I | re.S):
        if not re.fullmatch(section_tags, body[row_end:row.start()]):
            return None
        inner = row.group(1)
        cells = []
        all_headers = True
        cell_end = 0
        offset = outer.start(1) + row.start(1)
        for cell in re.finditer(r"<(th|td)\b([^>]*)>(.*?)</\1\s*>", inner, re.I | re.S):
            if (
                inner[cell_end:cell.start()].strip() or _HTML_TABLE_TAG.search(cell[3])
                or re.search(r"<br\b", cell[3], re.I)
            ):
                return None
            dimensions = _span_dimensions(cell[2], len(block))
            if dimensions is None:
                return None
            source = _trim_span(block, offset + cell.start(3), offset + cell.end(3))
            cells.append((source, *dimensions))
            all_headers &= cell[1].lower() == "th"
            cell_end = cell.end()
        if inner[cell_end:].strip():
            return None
        rows.append(cells)
        header_tags.append(bool(cells) and all_headers)
        if head and row.end() <= head.end(1):
            header_rows += 1
        row_end = row.end()
    if not rows or head and not header_rows or not re.fullmatch(section_tags, body[row_end:]):
        return None
    if not head:
        header_rows = next((index for index in range(1, len(rows)) if not header_tags[index]), len(rows))

    slots: dict[CellPosition, tuple[Span, CellPosition]] = {}
    width = 0
    for row_index, cells in enumerate(rows):
        column = 0
        for source, row_span, col_span in cells:
            while (row_index, column) in slots:
                column += 1
            bottom = row_index + row_span
            if (
                bottom > len(rows) or row_index < header_rows < bottom
                or row_span * col_span > len(block) - len(slots)
            ):
                return None
            origin = (row_index, column)
            for logical_row in range(row_index, bottom):
                for logical_column in range(column, column + col_span):
                    position = (logical_row, logical_column)
                    if position in slots:
                        return None
                    slots[position] = source, origin
            column += col_span
            width = max(width, column)
    if len(slots) != len(rows) * width:
        return None
    return TableGrid(
        [[slots[row, column][0] for column in range(width)] for row in range(len(rows))], header_rows,
        [[slots[row, column][1] for column in range(width)] for row in range(len(rows))],
    )


def _span_dimensions(attrs: str, limit: int) -> tuple[int, int] | None:
    """读取原表的正整数跨行、跨列值。"""
    spans = {}
    end = 0
    for match in _ATTR.finditer(attrs):
        if attrs[end:match.start()].strip():
            return None
        end = match.end()
        name = match[1].lower()
        if name not in {"rowspan", "colspan"}:
            continue
        value = next((value for value in match.groups()[1:] if value is not None), "")
        if name in spans or len(value) > len(str(limit)) or not re.fullmatch(r"[1-9][0-9]*", value):
            return None
        spans[name] = int(value)
    if attrs[end:].strip():
        return None
    return spans.get("rowspan", 1), spans.get("colspan", 1)


def _trim_span(text: str, start: int, end: int) -> Span:
    """去除单元格布局空白，保留实际原文范围。"""
    value = text[start:end]
    left = start + len(value) - len(value.lstrip())
    return left, max(left, start + len(value.rstrip()))
