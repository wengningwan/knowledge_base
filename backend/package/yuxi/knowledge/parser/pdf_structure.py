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
    # SoMark 官方 API 使用另一套类型词表（figure、figure_caption、table_caption、equation、reference 等）。
    # 未列出的类型会退化为 unknown 并按原图区域保底，因此这里必须覆盖官方词表，
    # 否则正文文字块会被当成插图追加截图。
    # reference 归入 text：下游规则校对与复核白名单只认识既有类型，新增类型会让参考文献条目失去原生文字核对。
    "figure": "picture", "figure_caption": "caption", "table_caption": "caption",
    "equation": "formula", "reference": "text",
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
    blocks_by_position: dict[tuple[int, int], dict] = {}
    cross_page_marks: list[tuple[dict, tuple[int, int]]] = []
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
            font_size = source.get("font_size")
            if isinstance(font_size, (int, float)) and not isinstance(font_size, bool) and math.isfinite(font_size) and font_size > 0:
                block["font_size"] = font_size
            if isinstance(source.get("role"), str):
                block["role"] = source["role"]
            if source.get("paragraph_start") is True:
                block["paragraph_start"] = True
            if source.get("continues_previous") is True:
                block["continues_previous"] = True
            image_ocr = _image_ocr_texts(source)
            if image_ocr:
                block["image_ocr"] = image_ocr
            target = _cross_page_target(source)
            if target is not None:
                cross_page_marks.append((block, target))
            blocks_by_position[(page, idx)] = block
            _extract_images(block, source, result)
            result.blocks.append(block)
            if block_type in {"picture", "table", "chemical_structure", "chemical_equation"} or block.get("asset_ids"):
                figure = {
                    key: block[key]
                    for key in ("id", "page", "bbox", "type", "asset_id", "asset_ids", "source_urls", "image_ocr")
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
    for marked, target in cross_page_marks:
        # 官方把跨页/跨栏续句做成指针而非直接合并文本。落到既有 continues_previous 语义上，
        # 让数字或大写开头的续句（"…(2:1," + "3:1, 4:1…"）也能接上，几何判据仍由 _safe_continuation 把关。
        continued = blocks_by_position.get(target)
        if continued is None:
            result.diagnostics.append({"kind": "cross_page_target_missing", "block_id": marked["id"], "target": list(target)})
            continue
        continued["continues_previous"] = True
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
    references = False
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
            references = _is_reference_heading(content)
            content = f"{'#' * int(block.get('level', 2))} {content.lstrip('#').strip()}"
        elif kind in {"formula", "chemical_equation"} and block.get("format", "").lower() in {"latex", "tex"}:
            if not (content.startswith("$$") or content.startswith(r"\[")):
                content = f"$$\n{content}\n$$"
        elif kind == "code_block" and not content.startswith("```"):
            content = f"```\n{content}\n```"
        boundaries = _reference_boundaries(content) if references and kind == "text" else []
        if boundaries:
            starts, ends = [0, *boundaries], [*boundaries, len(content)]
            parts.extend(content[start:end].strip() for start, end in zip(starts, ends, strict=True))
        elif previous is not None and parts and not references and _safe_continuation(previous, block):
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


def restore_reading_flow(blocks: list[dict], diagnostics: list[dict]) -> list[dict]:
    """按确证层级与续句恢复阅读流，保留块身份、原文和成组浮图。"""
    ordered = _restore_title_hierarchy(blocks, diagnostics)
    references = False
    for block in ordered:
        if block["type"] == "title":
            references = _is_reference_heading(str(block.get("content", "")))
        elif references and block["type"] == "text":
            boundaries = _reference_boundaries(str(block.get("content", "")))
            if boundaries:
                diagnostics.append({
                    "kind": "reference_boundaries", "block_id": block["id"], "boundaries": boundaries,
                    "reason": "参考文献区内连续编号、作者样式与年份共同确认段落边界；块及原文保持",
                })
    index = 0
    references = False
    while index < len(ordered):
        previous = ordered[index]
        if previous["type"] == "title":
            references = _is_reference_heading(str(previous.get("content", "")))
        if previous["type"] != "text" or references:
            index += 1
            continue
        cursor = index + 1
        while cursor < len(ordered) and ordered[cursor]["type"] in {"picture", "caption", "page_header", "page_footer"}:
            cursor += 1
        between = ordered[index + 1:cursor]
        if between and cursor < len(ordered) and _safe_continuation(previous, ordered[cursor]) and _confirmed_float_group(between, ordered[cursor]["page"]):
            original_ids = [block["id"] for block in ordered[index:cursor + 1]]
            current = ordered.pop(cursor)
            ordered.insert(index + 1, current)
            diagnostics.append({
                "kind": "floating_figure_after_continuation", "block_id": current["id"],
                "original_block_ids": original_ids,
                "final_block_ids": [block["id"] for block in ordered[index:cursor + 1]],
                "reason": "跨页/跨栏位置与未完句共同支持续接；原图图注保持成组，块字符与身份保持",
            })
        index += 1
    references = False
    for index, block in enumerate(ordered):
        if block["type"] == "title":
            references = _is_reference_heading(str(block.get("content", "")))
        if index and not references and _safe_continuation(ordered[index - 1], block):
            diagnostics.append({
                "kind": "paragraph_continuation", "block_id": block["id"],
                "original_block_ids": [ordered[index - 1]["id"], block["id"]],
                "final_block_ids": [ordered[index - 1]["id"], block["id"]],
                "reason": "仅渲染时接续同一未完句；跨页或跨栏几何、栏宽与段首共同校验，块原文保持",
            })
    return ordered


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


def _image_ocr_texts(source: dict) -> list[str]:
    """归一官方图内文字识别结果，只保留有序文字。

    官方每项形如 {"text": "...", "polygon": [[x, y], ...]}。坐标多边形不参与正文渲染，
    图内文字也只进 figures 侧车，避免把插图里的字当正文。
    """
    raw = source.get("image_ocr")
    if not isinstance(raw, list):
        return []
    texts = []
    for item in raw:
        value = item.get("text") if isinstance(item, dict) else item
        if not isinstance(value, str):
            continue
        text = " ".join(value.split())
        if text:
            texts.append(text)
    return texts


def _cross_page_target(source: dict) -> tuple[int, int] | None:
    """把官方 text_cross_page 指针归一为 (1 基页码, idx)，指向本块之后的续句块。"""
    raw = source.get("text_cross_page")
    if not isinstance(raw, dict):
        return None
    page_num, block_idx = raw.get("page_num"), raw.get("block_idx")
    for value in (page_num, block_idx):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return None
    return (page_num + 1, block_idx)


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
    if previous["type"] != "text" or current["type"] != "text" or _is_article_metadata(previous) or _is_article_metadata(current):
        return False
    before, after = str(previous.get("content", "")).rstrip(), str(current.get("content", "")).lstrip()
    # 句末引文是排印附件，不得遮蔽其前方的句末标点。
    ending = re.sub(r"(?:\$\s*\^\{[^{}]*\}\s*\$|<sup>[^<]*</sup>|[⁰¹²³⁴⁵⁶⁷⁸⁹]+)\s*$", "", before).rstrip(" \t\"'”’)]}")
    if not before or not after or ending.endswith(("-", "‐", "‑", ".", "。", "!", "！", "?", "？", ":", "：", ";", "；")):
        return False
    if current.get("paragraph_start") is True or re.match(r"^(?:#{1,6}\s|[-+*]\s|\(?\d+(?:\.\d+)*[.)]\s|\[\d+\]\s|!\[|<)", after):
        return False
    if not previous.get("bbox") or not current.get("bbox"):
        return False
    old_font, new_font = previous.get("font_size"), current.get("font_size")
    if old_font and new_font and max(old_font, new_font) > min(old_font, new_font) * 1.15:
        return False
    old, new = previous["bbox"], current["bbox"]
    old_width, new_width = old[2] - old[0], new[2] - new[0]
    matched_width = min(old_width, new_width) > 0 and max(old_width, new_width) <= min(old_width, new_width) * 1.35
    same_column = abs(old[0] - new[0]) < 0.025 and abs(old[2] - new[2]) < 0.035
    page_turn = current["page"] == previous["page"] + 1 and old[3] >= 0.80 and new[1] <= 0.30
    column_turn = current["page"] == previous["page"] and old[0] < 0.40 and new[0] > 0.45 and old[3] >= 0.75 and new[1] <= 0.30
    explicit = current.get("continues_previous") is True
    next_page_first_column = old[0] > 0.45 and new[0] < 0.40
    return matched_width and (page_turn and (same_column or next_page_first_column) or column_turn) and (explicit or after[0].islower())


def _restore_title_hierarchy(blocks: list[dict], diagnostics: list[dict]) -> list[dict]:
    """首个标题的字体和位置确证主标题后，将编号章节置于其下。"""
    ordered = list(blocks)
    first = next((block for block in ordered if block["type"] == "title"), None)
    if first is None or first["page"] != 1 or re.match(r"^\s*\d", str(first.get("content", ""))):
        return ordered
    body_sizes = [block["font_size"] for block in ordered if block["type"] == "text" and block["page"] == first["page"] and block.get("font_size")]
    title_size, box = first.get("font_size"), first.get("bbox")
    if not body_sizes or not title_size or title_size < median(body_sizes) * 1.6 or not box or box[1] > .25:
        return ordered
    for index, block in enumerate(ordered):
        if block["type"] != "title":
            continue
        numbered = re.match(r"^\s*(\d+(?:\.\d+)*)(?:[.)]?\s+)\S", str(block.get("content", "")))
        level = 1 if block is first else min(6, numbered[1].count(".") + 2) if numbered else None
        if level is not None:
            block = {**block, "title_hierarchy_document_title_id": first["id"]}
            ordered[index] = block
        if level is not None and block.get("level") != level:
            ordered[index] = {**block, "level": level}
            diagnostics.append({
                "kind": "title_hierarchy", "block_id": block["id"], "before_level": block.get("level"), "after_level": level,
                "document_title_id": first["id"], "reason": "首页顶部显著字号确证主标题；编号深度确定其下的章节层级",
            })
    return ordered


def _is_article_metadata(block: dict) -> bool:
    """显式作者出版身份或完整出版标签阻断正文续接，不凭人名猜身份。"""
    role = str(block.get("role") or block.get("source_type") or "").replace("-", "_").lower()
    if role in {"author", "authors", "article_metadata", "publication_metadata", "publication_info", "date"}:
        return True
    content = str(block.get("content", ""))
    if re.match(r"^\s*(?:(?:Received|Revised|Accepted|Published)(?:\s+online)?\s*:|Published\s+as\s+part\s+of\b|Cite\s+This\s*:|DOI\s*:)", content, re.I):
        return True
    month = r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
    day, year = r"(?:0?[1-9]|[12]\d|3[01])", r"(?:18|19|20)\d{2}"
    date = rf"(?:{day}\s+{month}\s+{year}|{month}\s+{day},?\s+{year}|{year}[-/](?:0?[1-9]|1[0-2])[-/]{day})\b"
    return bool(re.match(rf"^\s*(?:Received|Revised|Accepted|Published)(?:\s+online)?\s+{date}", content, re.I))


def _confirmed_float_group(blocks: list[dict], page: int) -> bool:
    """仅允许目标页上有真实几何的图组越过续句，孤立图注不作证据。"""
    figures = [block for block in blocks if block["type"] in {"picture", "caption"}]
    if not figures or any(block["page"] != page or not block.get("bbox") for block in figures):
        return False
    for index, block in enumerate(figures):
        if block["type"] != "caption":
            continue
        if not index or figures[index - 1]["type"] != "picture":
            return False
        image, caption = figures[index - 1]["bbox"], block["bbox"]
        overlap = max(0, min(image[2], caption[2]) - max(image[0], caption[0]))
        if caption[1] < image[3] - .01 or caption[1] - image[3] > .06 or overlap < min(image[2] - image[0], caption[2] - caption[0]) * .5:
            return False
    return True


def _is_reference_heading(content: str) -> bool:
    """仅明确的参考文献标题开启条目边界识别。"""
    return bool(re.fullmatch(r"[\s#■▪●]*\s*(?:\d+[.)]?\s+)?(?:REFERENCES|BIBLIOGRAPHY|参考文献)\s*", content, re.I))


def _reference_boundaries(content: str) -> list[int]:
    """按行首连续编号、作者和年份确证文献分段，保留所有原文字符。"""
    entries = list(re.finditer(r"(?m)^[ \t]*(?:\((\d+)\)|\[(\d+)\])[ \t]+(?=[A-ZÀ-ÖØ-Þ][\w'’−–-]+,\s*[A-Z])", content))
    if len(entries) < 2 or content[:entries[0].start()].strip():
        return []
    boundaries = []
    for previous, current in zip(entries, entries[1:]):
        consecutive = int(current[1] or current[2]) == int(previous[1] or previous[2]) + 1
        entry = content[previous.end():current.start()].strip()
        if consecutive and re.search(r"\b(?:18|19|20)\d{2}\b", entry) and entry.endswith((".", "。")):
            boundaries.append(current.start())
    return boundaries


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
