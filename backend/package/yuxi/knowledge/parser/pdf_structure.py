"""将 OXR 块序列转为可审阅、可保存的正文与图片资产。"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
from html import escape
from html.parser import HTMLParser
from statistics import median

from yuxi.knowledge.parser.result import ParsedDocument, safe_metadata


TYPE_MAP = {
    "text": "text", "title": "title", "caption": "caption", "formula": "formula",
    "picture": "picture", "image": "picture", "table": "table", "footnote": "footnote",
    "chemicalequation": "chemical_equation", "chemical_equation": "chemical_equation",
    "chemicalstructure": "chemical_structure", "chemical_structure": "chemical_structure",
    "cs": "chemical_structure", "codeblock": "code_block", "code_block": "code_block",
    "code": "code_block",
    "stamp": "stamp", "pageheader": "page_header", "page_header": "page_header",
    "header": "page_header", "pagefooter": "page_footer", "page_footer": "page_footer", "footer": "page_footer",
}


def normalize_oxr_result(payload: dict) -> ParsedDocument:
    """接受完整响应、data.result 或 outputs 文档，保持 pages/blocks 原阅读顺序。"""
    if not isinstance(payload, dict):
        raise ValueError("OXR 结果必须是对象")
    document = payload
    upstream_metadata = {}
    for _ in range(4):
        if isinstance(document.get("metadata"), dict):
            upstream_metadata.update(safe_metadata(document["metadata"]))
        if "outputs" in document or "pages" in document:
            break
        child = document.get("data") if isinstance(document.get("data"), dict) else document.get("result")
        if not isinstance(child, dict):
            raise ValueError("OXR 成功响应缺少 outputs.json.pages")
        document = child
    outputs = document.get("outputs", document)
    if not isinstance(outputs, dict):
        raise ValueError("OXR outputs 必须是对象")
    structure = outputs.get("json", outputs)
    if isinstance(structure, str):
        structure = json.loads(structure)
    if not isinstance(structure, dict) or not isinstance(structure.get("pages"), list):
        raise ValueError("OXR 结果缺少 pages 数组")
    result = ParsedDocument(metadata={"oxr": upstream_metadata, "parsed_pages": []})
    used_ids = set()
    order = 0
    for page_index, source_page in enumerate(structure["pages"]):
        if not isinstance(source_page, dict):
            raise ValueError("OXR page 必须是对象")
        page = int(source_page.get("page_num", source_page.get("page_idx", page_index))) + 1
        if page < 1:
            raise ValueError("OXR page_num 必须是 0 基非负页码")
        result.metadata["parsed_pages"].append(page)
        size = source_page.get("page_size", {})
        if isinstance(size, (list, tuple)) and len(size) == 2:
            width, height = size
        elif isinstance(size, dict):
            width, height = size.get("w", size.get("width")), size.get("h", size.get("height"))
        else:
            raise ValueError("OXR page_size 必须是对象或二元数组")
        source_blocks = source_page.get("blocks", [])
        if not isinstance(source_blocks, list):
            raise ValueError("OXR blocks 必须是数组")
        fonts = [
            float(item["font_size"])
            for item in source_blocks
            if isinstance(item, dict)
            and isinstance(item.get("font_size"), (int, float))
            and item["font_size"] > 0
        ]
        body_font = median(fonts) if fonts else None
        for position, source in enumerate(source_blocks):
            if not isinstance(source, dict):
                raise ValueError("OXR block 必须是对象")
            idx = source.get("idx", position)
            if not isinstance(idx, int) or isinstance(idx, bool) or idx < 0:
                raise ValueError("OXR idx 必须是非负整数")
            block_id = f"p{page}_b{idx}"
            if block_id in used_ids:
                block_id += f"_at{position}"
                result.diagnostics.append({"kind": "duplicate_idx", "page": page, "idx": idx})
            used_ids.add(block_id)
            source_type = str(source.get("type", "text"))
            block_type = TYPE_MAP.get(source_type.replace(" ", "").replace("-", "_").lower(), "unknown")
            content = source.get("content", "")
            if content is None:
                content = ""
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            bbox = _normalize_bbox(source.get("bbox"), width, height)
            block = {
                "id": block_id,
                "page": page,
                "idx": idx,
                "order": order,
                "type": block_type,
                "bbox": bbox,
                "content": content,
                "format": str(source.get("format") or "text"),
            }
            if source_type.replace(" ", "").replace("-", "_").lower() not in TYPE_MAP:
                block["source_type"] = source_type
                result.diagnostics.append({"kind": "unknown_type", "block_id": block_id, "source_type": source_type})
            if bbox is None:
                result.diagnostics.append({"kind": "missing_bbox", "block_id": block_id})
            if block_type == "title":
                block["level"] = _title_level(source, content, body_font)
            if source.get("continues_previous") is True:
                block["continues_previous"] = True
            _extract_images(block, source, result)
            result.blocks.append(block)
            if block_type in {"picture", "table", "chemical_structure", "chemical_equation"} or block.get("asset_ids"):
                figure = {
                    key: block[key]
                    for key in ("id", "page", "bbox", "type", "asset_id", "asset_ids", "source_urls")
                    if key in block
                }
                figure.update(
                    {
                        "image_url": f"asset://{block['asset_id']}" if block.get("asset_id") else "",
                        "caption": "",
                        "analysis": "",
                        "analysis_status": "pending",
                        "model": None,
                    }
                )
                result.figures.append(figure)
            order += 1
    result.metadata["upstream_markdown"] = str(outputs.get("markdown") or "")
    result.blocks = restore_reading_flow(result.blocks, result.diagnostics)
    result.markdown = render_document(result.blocks)
    for position, block in enumerate(result.blocks):
        if block["type"] == "caption" and position > 0:
            previous = result.blocks[position - 1]
            for figure in result.figures:
                if figure["id"] == previous["id"] and figure["page"] == block["page"]:
                    figure["caption"] = block["content"]
    if not result.blocks and outputs.get("markdown"):
        result.markdown = str(outputs["markdown"])
        result.diagnostics.append({"kind": "unstructured_markdown", "message": "服务未提供可定位块，仅保留原 Markdown"})
    return result


def render_document(blocks: list[dict], urls: dict[str, str] | None = None) -> str:
    """按块顺序渲染正文，保留公式、HTML、图片与原图注。"""
    parts = []
    previous = None
    for block in blocks:
        kind = block["type"]
        if kind in {"page_header", "page_footer"}:
            continue
        content = str(block.get("content", "")).strip()
        if not content and block.get("asset_id"):
            content = f"![](asset://{block['asset_id']})"
        if not content:
            previous = block
            continue
        content = _replace_asset_urls(content, urls or {})
        if kind == "title":
            content = f"{'#' * int(block.get('level', 2))} {content.lstrip('#').strip()}"
        elif kind in {"formula", "chemical_equation"} and block.get("format", "").lower() in {"latex", "tex"}:
            if not (content.startswith("$$") or content.startswith(r"\[")):
                content = f"$$\n{content}\n$$"
        elif kind == "code_block" and not content.startswith("```"):
            content = f"```\n{content}\n```"
        if previous is not None and parts and _safe_continuation(previous, block):
            parts[-1] += " " + content
        else:
            parts.append(content)
        previous = block
    return "\n\n".join(parts).strip()


def finalize_document(result: ParsedDocument, urls: dict[str, str]) -> None:
    """上传资产后原位替换占位链接，同时更新块、图结构和正文。"""
    for asset_id, url in urls.items():
        if asset_id in result.assets:
            result.assets[asset_id]["url"] = url
    for block in result.blocks:
        block["content"] = _replace_asset_urls(str(block.get("content", "")), urls)
    result.figures = _replace_nested_urls(result.figures, urls)
    result.reviews = _replace_nested_urls(result.reviews, urls)
    result.diagnostics = _replace_nested_urls(result.diagnostics, urls)
    result.markdown = (
        render_document(result.blocks, urls)
        if result.blocks
        else _replace_asset_urls(result.markdown, urls)
    )


def _normalize_bbox(value, width, height) -> list[float] | None:
    """把 OXR 像素区域转为左上原点归一坐标；缺失位置保持显式未知。"""
    if not isinstance(value, (list, tuple)) or len(value) != 4 or not width or not height:
        return None
    try:
        x0, y0, x1, y1 = map(float, value)
        width, height = float(width), float(height)
    except (ValueError, TypeError):
        return None
    if (
        not all(math.isfinite(item) for item in (x0, y0, x1, y1, width, height))
        or width <= 0 or height <= 0 or x1 <= x0 or y1 <= y0
    ):
        return None
    bbox = [max(0.0, min(1.0, item)) for item in (x0 / width, y0 / height, x1 / width, y1 / height)]
    return bbox if bbox[2] > bbox[0] and bbox[3] > bbox[1] else None


def _title_level(source: dict, content: str, body_font: float | None) -> int:
    """仅使用明确编号、显式层级或显著字体差异估计标题层级。"""
    explicit = source.get("title_level", source.get("level"))
    if isinstance(explicit, int) and not isinstance(explicit, bool) and 1 <= explicit <= 6:
        return explicit
    numbered = re.match(r"^\s*(\d+(?:\.\d+)*)(?:[.)]?\s+)\S", content)
    if numbered:
        return min(6, numbered.group(1).count(".") + 1)
    font = source.get("font_size")
    if body_font and isinstance(font, (int, float)) and font >= body_font * 1.6:
        return 1
    return 2


def _extract_images(block: dict, source: dict, result: ParsedDocument) -> None:
    """解码内嵌图片并保留外部 URL 位置，不获取任何远端资源。"""
    references = []
    parser = _ImageSources()
    parser.feed(block["content"])
    references.extend(parser.sources)
    references.extend(re.findall(r"!\[[^\]]*\]\(\s*(data:image/[^)]*|https?://[^\s)]+)\s*\)", block["content"], re.I))
    for key in ("img_url", "image_url", "source_url"):
        if isinstance(source.get(key), str) and source[key]:
            references.append(source[key])
    if block["content"].strip().lower().startswith("data:image/"):
        references.append(block["content"].strip())
    if (
        block["type"] in {"picture", "chemical_structure"}
        and re.fullmatch(r"https?://\S+", block["content"].strip())
    ):
        references.append(block["content"].strip())
        block["content"] = f"![]({block['content'].strip()})"
    block_assets = []
    for reference in dict.fromkeys(references):
        if not reference.lower().startswith("data:image/"):
            block.setdefault("source_urls", []).append(reference)
            continue
        match = re.fullmatch(r"data:(image/[a-zA-Z0-9.+-]+);base64,([\s\S]+)", reference)
        if not match or len(reference) > 48 * 1024 * 1024:
            result.diagnostics.append({"kind": "invalid_image", "block_id": block["id"]})
            continue
        try:
            data = base64.b64decode(re.sub(r"\s+", "", match.group(2)), validate=True)
        except (ValueError, binascii.Error):
            result.diagnostics.append({"kind": "invalid_image", "block_id": block["id"]})
            continue
        if not data:
            result.diagnostics.append({"kind": "empty_image", "block_id": block["id"]})
            continue
        asset_id = f"{block['id']}_img{len(block_assets) + 1}"
        result.assets[asset_id] = {
            "data": data,
            "mime": match.group(1).lower(),
            "page": block["page"],
            "bbox": block["bbox"],
            "role": block["type"],
        }
        block_assets.append(asset_id)
        block["content"] = block["content"].replace(reference, f"asset://{asset_id}").replace(escape(reference, quote=True), f"asset://{asset_id}")
    if block_assets:
        block["asset_id"], block["asset_ids"] = block_assets[0], block_assets
    if block["content"].startswith("asset://"):
        block["content"] = f"![]({block['content']})"
    elif not block["content"] and block_assets:
        block["content"] = "\n".join(f"![](asset://{asset_id})" for asset_id in block_assets)
    elif not block["content"] and block.get("source_urls"):
        block["content"] = f"![]({block['source_urls'][0]})"
    for asset_id in block_assets:
        if f"asset://{asset_id}" not in block["content"]:
            block["content"] += f"\n![](asset://{asset_id})"


def _replace_asset_urls(content: str, urls: dict[str, str]) -> str:
    """以完整资产 ID 替换链接，避免前缀相近的 ID 相互覆盖。"""
    return re.sub(r"asset://([A-Za-z0-9_.-]+)", lambda match: urls.get(match.group(1), match.group(0)), content)


def _replace_nested_urls(value, urls):
    """递归替换结构记录中的显式资产引用，不接触二进制内容。"""
    if isinstance(value, str):
        return _replace_asset_urls(value, urls)
    if isinstance(value, list):
        return [_replace_nested_urls(item, urls) for item in value]
    if isinstance(value, dict):
        return {key: _replace_nested_urls(item, urls) for key, item in value.items()}
    return value


def _safe_continuation(previous: dict, current: dict) -> bool:
    """只连接有版面和句法依据的续句，化学连字符原样保留。"""
    if previous["type"] != "text" or current["type"] != "text":
        return False
    before, after = str(previous.get("content", "")).rstrip(), str(current.get("content", "")).lstrip()
    if not before or not after or before.endswith(("-", "‐", "‑", ".", "。", "!", "?", ":", ";")):
        return False
    if not previous.get("bbox") or not current.get("bbox"):
        return False
    old, new = previous["bbox"], current["bbox"]
    same_column = abs(old[0] - new[0]) < 0.12 and abs(old[2] - new[2]) < 0.15
    page_turn = current["page"] == previous["page"] + 1 and old[3] >= 0.80 and new[1] <= 0.30
    column_turn = current["page"] == previous["page"] and old[0] < 0.40 and new[0] > 0.45 and old[3] >= 0.75 and new[1] <= 0.30
    explicit = current.get("continues_previous") is True
    next_page_first_column = old[0] > 0.45 and new[0] < 0.40
    return (page_turn and (same_column or next_page_first_column or explicit) or column_turn) and (explicit or after[0].islower())


def restore_reading_flow(blocks: list[dict], diagnostics: list[dict]) -> list[dict]:
    """续句跨过页首浮图时先连段落，再保留浮图与图注的相对次序。"""
    ordered = list(blocks)
    index = 0
    while index < len(ordered):
        previous = ordered[index]
        if previous["type"] != "text":
            index += 1
            continue
        cursor = index + 1
        while cursor < len(ordered) and ordered[cursor]["type"] in {"picture", "caption", "page_header", "page_footer"}:
            cursor += 1
        if cursor > index + 1 and cursor < len(ordered) and _safe_continuation(previous, ordered[cursor]):
            current = ordered.pop(cursor)
            ordered.insert(index + 1, current)
            diagnostics.append({"kind": "floating_figure_after_continuation", "block_id": current["id"], "reason": "根据跨页/跨栏位置与未完句恢复阅读次序，原图图注保持成组"})
        index += 1
    return ordered


class _ImageSources(HTMLParser):
    """只读取 HTML 图片引用，保留原 HTML 的全部结构。"""

    def __init__(self):
        """建立单块图片引用容器。"""
        super().__init__(convert_charrefs=False)
        self.sources = []

    def handle_starttag(self, tag, attrs):
        """收集 img 的 src，不重写表格或其它标签。"""
        if tag.lower() == "img":
            self.sources.extend(value for key, value in attrs if key.lower() == "src" and value)
