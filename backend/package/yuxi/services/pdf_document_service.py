"""组合论文块、原图、局部复核和独立插图解释。"""

from __future__ import annotations

import asyncio
import io
import re
from collections import Counter
from html import escape
from urllib.parse import urljoin, urlsplit


async def build_native_document(file_path: str):
    """轻量模式按原生位置组合图文，保留不确定字符的独立诊断。"""
    from yuxi.knowledge.parser.pdf_native import read_native_blocks, read_native_image_regions
    from yuxi.knowledge.parser.pdf_structure import render_document
    from yuxi.knowledge.parser.result import ParsedDocument

    native = await asyncio.to_thread(read_native_blocks, file_path)
    images = await asyncio.to_thread(read_native_image_regions, file_path)
    blocks = []
    repeated = Counter(str(item.get("content", "")).strip() for item in native)
    for index, item in enumerate(native):
        box = item.get("bbox")
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        if box and (box[3] < 0.08 or box[1] > 0.94) and repeated[content] >= 2:
            continue
        if box and box[2] < 0.04 and box[3] - box[1] > 0.3:
            continue
        kind = "caption" if re.match(r"^Figure\s*\d+\.", content) else "text"
        if len(content) < 160 and "\n" not in content and re.match(r"^\d+(?:\.\d+)*\.\s+[A-Z]", content):
            kind = "title"
        blocks.append({
            **item, "id": f"p{item['page']}_native{index}", "idx": index,
            "type": kind, "format": "text", "content": content,
        })
    for index, item in enumerate(images):
        blocks.append({
            **item, "id": f"p{item['page']}_image{index}", "idx": index,
            "type": "picture", "content": "", "format": "markdown",
        })
    result = ParsedDocument(blocks=_native_reading_order(blocks), metadata={"engine": "disable", "review_status": "not_requested"})
    if any("\ufffd" in str(block.get("content", "")) for block in blocks):
        result.diagnostics.append({"kind": "native_text_damage", "reason": "原生文字包含未恢复字符，公式保真请使用带公式能力的解析方式"})
    result.markdown = render_document(result.blocks)
    return result


async def enrich_document(result, file_path: str, params: dict, control=None):
    """先确保原图自足，再分别运行可选局部校对和插图解释。"""
    from yuxi.knowledge.parser.pdf_native import render_pdf_region
    from yuxi.knowledge.parser.pdf_structure import render_document
    from yuxi.services.pdf_figure_analysis_service import analyze_document_figures
    from yuxi.services.pdf_review_service import review_pdf_document

    by_figure = {figure["id"]: figure for figure in result.figures}
    base_url = (params.get("_ocr_processor_kwargs") or {}).get("server_url") or ""
    for block in result.blocks:
        if control:
            await control.check_cancelled()
        kind = block.get("type")
        refs = list(dict.fromkeys(block.get("source_urls") or []))
        for index, reference in enumerate(refs):
            asset_id = f"{block['id']}_remote{index}"
            try:
                data, mime = await _read_service_image(reference, base_url)
            except Exception:
                # 外链无法取得时，按原PDF保留完整区域，不向任意主机取资源。
                if not block.get("bbox"):
                    raise ValueError("原图资源不可用且缺少原文定位") from None
                data = await asyncio.to_thread(render_pdf_region, file_path, block["page"], block["bbox"])
                mime = "image/png"
                result.diagnostics.append({"kind": "source_crop", "block_id": block["id"], "reason": "远程图片未取得，保留原PDF完整区域"})
                if kind == "table":
                    block["content"] = f"![](asset://{asset_id})"
                    block["format"] = "markdown"
                    result.diagnostics.append({"kind": "table_raster_fallback", "block_id": block["id"], "reason": "表格以原图保留，表格文字不可全文检索"})
                    refs = []
            result.assets[asset_id] = {"data": data, "mime": mime, "page": block["page"], "bbox": block.get("bbox"), "role": kind}
            block.setdefault("asset_ids", []).append(asset_id)
            block.setdefault("asset_id", asset_id)
            block["content"] = str(block.get("content", "")).replace(reference, f"asset://{asset_id}").replace(escape(reference, quote=True), f"asset://{asset_id}")
            if not refs:
                break
        if kind == "unknown" and not block.get("bbox"):
            if not str(block.get("content") or "").strip():
                raise ValueError("未知内容既无可用文字也无法定位原文")
            result.diagnostics.append({"kind": "unknown_region_unlocated", "block_id": block["id"], "reason": "保留未知类型原文文字，缺少定位，未猜测原图"})
        # 官方会把部分已知类型块返回为空文字（如 reference、footnote）。这些类型没有走 unknown 保底，
        # 正文会静默丢内容，因此按原PDF区域留一份可读截图；region_backup 把它排除出插图分析。
        blank_backup = (
            not str(block.get("content") or "").strip()
            and bool(block.get("bbox"))
            and kind not in {"page_header", "page_footer", "picture", "chemical_structure"}
        )
        if (kind in {"picture", "chemical_structure"} or (kind == "unknown" and block.get("bbox")) or blank_backup) and not block.get("asset_id"):
            if not block.get("bbox"):
                raise ValueError("插图缺少原图或可定位区域")
            asset_id = f"{block['id']}_source"
            data = await asyncio.to_thread(render_pdf_region, file_path, block["page"], block["bbox"])
            result.assets[asset_id] = {"data": data, "mime": "image/png", "page": block["page"], "bbox": block["bbox"], "role": kind}
            original = str(block.get("content") or "").strip() if kind == "unknown" else ""
            block.update(asset_id=asset_id, asset_ids=[asset_id], content=(original + "\n\n" if original else "") + f"![](asset://{asset_id})", format="markdown")
            if blank_backup:
                block["region_backup"] = True
                result.diagnostics.append({"kind": "blank_region_backup", "block_id": block["id"], "reason": "服务未返回该块文字，按原PDF区域保留图像证据"})
        if not block.get("region_backup") and (kind in {"picture", "chemical_structure"} or block.get("asset_ids")):
            figure = by_figure.get(block["id"], {"id": block["id"]})
            figure.update({key: block.get(key) for key in ("page", "bbox", "asset_id", "asset_ids", "type")})
            figure["image_url"] = f"asset://{block['asset_id']}" if block.get("asset_id") else ""
            figure.setdefault("caption", _nearby_caption(result.blocks, block))
            if not figure.get("caption"):
                figure["caption"] = _nearby_caption(result.blocks, block)
            figure.setdefault("analysis", "")
            figure.setdefault("analysis_status", "not_requested")
            by_figure[block["id"]] = figure
    # 模型与下载都只使用已解码、受大小限制的图像。
    for asset in result.assets.values():
        asset["data"], asset["mime"] = await asyncio.to_thread(_normalize_image, asset["data"])
    result.figures = [figure for figure in by_figure.values() if figure.get("asset_id")]
    if params.get("ocr_engine") == "oxr_ocr" and file_path.lower().endswith(".pdf"):
        await review_pdf_document(result, file_path, params, control)
    else:
        result.metadata.setdefault("review_status", "not_requested")
    model_spec = str(params.get("image_analysis_model") or "").strip()
    if params.get("_defer_figure_analysis") and model_spec and result.figures:
        for figure in result.figures:
            figure.update(analysis_status="pending", analysis="", model=model_spec)
        result.metadata["figure_analysis"] = {"status": "pending", "model": model_spec}
    else:
        await analyze_document_figures(result, params, control)
    result.markdown = render_document(result.blocks)
    return result


def _native_reading_order(blocks: list[dict]) -> list[dict]:
    """用跨栏块分隔页面，再在每个区域先左栏后右栏。"""
    ordered = []
    for page in sorted({block["page"] for block in blocks}):
        current = [block for block in blocks if block["page"] == page]
        valid = [block for block in current if block.get("bbox")]
        anchors = sorted([block for block in valid if block["bbox"][2] - block["bbox"][0] >= 0.62], key=lambda block: block["bbox"][1])
        remaining = [block for block in current if block not in anchors]
        for anchor in anchors:
            before = [block for block in remaining if block.get("bbox") and block["bbox"][3] <= anchor["bbox"][1] + 0.003]
            ordered.extend(_columns(before))
            remaining = [block for block in remaining if block not in before]
            ordered.append(anchor)
        ordered.extend(_columns(remaining))
    for index, block in enumerate(ordered):
        block["order"] = index
    return ordered


def _columns(blocks: list[dict]) -> list[dict]:
    """只在线性页区域内应用两栏次序。"""
    return sorted(blocks, key=lambda block: (
        0 if not block.get("bbox") or (block["bbox"][0] + block["bbox"][2]) / 2 < 0.5 else 1,
        (block.get("bbox") or [0, 0])[1], (block.get("bbox") or [0])[0],
    ))


def _nearby_caption(blocks: list[dict], image: dict) -> str:
    """图注仅作附属上下文，正文仍由自己的块渲染一次。

    题注既可能在目标下方，也可能紧贴上方（表格题注常在表格之上），
    两条几何带都匹配，取距离最近的一条。
    """
    box = image.get("bbox")
    if not box:
        return ""
    candidates = []
    for item in blocks:
        if item.get("type") != "caption" or item["page"] != image["page"] or not item.get("bbox"):
            continue
        for gap in (item["bbox"][1] - box[3], box[1] - item["bbox"][3]):
            if -0.015 <= gap <= 0.12:
                candidates.append((abs(gap), item))
                break
    if not candidates:
        return ""
    return min(candidates, key=lambda pair: pair[0])[1]["content"]


async def _read_service_image(reference: str, base_url: str) -> tuple[bytes, str]:
    """仅下载管理员配置服务同源的图片，不跟随跳转或转发密钥。"""
    import httpx

    target = urljoin(base_url.rstrip("/") + "/", reference)
    origin, parsed = urlsplit(base_url), urlsplit(target)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or (parsed.scheme, parsed.hostname, parsed.port) != (origin.scheme, origin.hostname, origin.port):
        raise ValueError("图片不属于已配置解析服务")
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        async with client.stream("GET", target) as response:
            response.raise_for_status()
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > 24 * 1024 * 1024:
                    raise ValueError("原图资源超过大小上限")
                chunks.append(chunk)
    return _normalize_image(b"".join(chunks))


def _normalize_image(data: bytes) -> tuple[bytes, str]:
    """验证尺寸并无损保存解码像素，统一成可携带PNG。"""
    from PIL import Image

    if not data or len(data) > 32 * 1024 * 1024:
        raise ValueError("解析图片为空或过大")
    with Image.open(io.BytesIO(data)) as image:
        if image.width * image.height > 40_000_000:
            raise ValueError("解析图片像素数过大")
        image.load()
        with image.convert("RGBA" if "A" in image.getbands() else "RGB") as decoded:
            output = io.BytesIO()
            decoded.save(output, format="PNG")
            return output.getvalue(), "image/png"
