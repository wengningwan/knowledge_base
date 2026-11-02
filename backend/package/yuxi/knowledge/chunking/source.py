"""Markdown 原文坐标与保守的同次解析版面映射。"""

from __future__ import annotations

import hashlib
import re
from bisect import bisect_right
from typing import Any


def source_sha256(text: str) -> str:
    """计算当前 Markdown UTF-8 字节的指纹。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SourceDocument:
    """一次扫描行边界，复用于同一文档的所有显式范围。"""

    def __init__(self, text: str, parse_assets: dict | None = None):
        """建立字符行索引和真实解析块的精确文本索引。"""
        self.text = text
        self.line_starts = [0] + [match.end() for match in re.finditer("\n", text)]
        self.blocks: dict[str, list[dict]] = {}
        for block in (parse_assets or {}).get("blocks", []):
            if isinstance(block, dict) and isinstance(original := block.get("content") or block.get("text"), str):
                self.blocks.setdefault(original, []).append(block)

    def span(self, start: int, end: int) -> dict[str, Any]:
        """保留 Unicode 坐标；仅唯一、逐字匹配时附加真实 PDF 位置。"""
        span = {
            "line_start": bisect_right(self.line_starts, start),
            "line_end": bisect_right(self.line_starts, max(start, end - 1)),
            "start_char": start,
            "end_char": end,
        }
        original = self.text[start:end]
        matches = self.blocks.get(original, [])
        if not original or len(matches) != 1 or self.text.count(original) != 1:
            return span
        block = matches[0]
        page = block.get("page")
        if isinstance(page, int) and not isinstance(page, bool) and page > 0:
            span["page"] = page
            if block.get("block_id") or block.get("id"):
                span["block_id"] = block.get("block_id") or block["id"]
            if block.get("bbox") is not None:
                span["bbox"] = block["bbox"]
        return span
