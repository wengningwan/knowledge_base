"""按区域构建独立证据并执行保守 R1，正文块始终是唯一渲染来源。"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict
from statistics import median

from yuxi.knowledge.parser.pdf_repair_rules import (
    apply_block_edits,
    covers_block_region,
    critical_signature,
    damaged_text,
    is_local_candidate,
    layout_text,
    overlap_fraction,
    protected_spans,
)
from yuxi.knowledge.parser.result import ParsedDocument


RULE_VERSION = "yuxi-r1-1"


def run_rule_repair(result: ParsedDocument, native_blocks: list[dict], page_count: int) -> list[dict]:
    """原位修复 blocks 并更新统计，返回审计项供调用方扩展 reviews。"""
    repeats = defaultdict(set)
    for native in native_blocks:
        repeats[layout_text(native.get("content", ""))].add(native["page"])
    evidence = []
    for native in native_blocks:
        role = _region_role(native, repeats)
        evidence.append({
            "id": native["id"], "page": native["page"], "bbox": native.get("bbox"),
            "content": str(native.get("content") or ""), "source": "native",
            "quality": _native_quality(native), "role": role,
        })
    evidence_by_page = defaultdict(list)
    for entry in evidence:
        evidence_by_page[entry["page"]].append(entry)
    matches = {
        block["id"]: [entry for entry in evidence_by_page[block["page"]] if overlap_fraction(entry["bbox"], block.get("bbox")) >= 0.85]
        for block in result.blocks
    }
    owners = defaultdict(list)
    for block in result.blocks:
        for entry in matches[block["id"]]:
            owners[entry["id"]].append(block)
    rows = []
    removed = set()
    for block in result.blocks:
        content = str(block.get("content") or "")
        sources = matches[block["id"]]
        role = _region_role(block, repeats)
        block["repair_role"] = role
        if role == "publication_noise":
            rows.append(_row(block, "publication_element", sources, status="checked", reason="几何位置与重复或下载用途共同确认出版元素", classification="excluded", severity="low"))
            continue
        if block["type"] in {"picture", "table", "code_block", "chemical_structure"}:
            if block["type"] == "table" and re.search(r"<(?:eq|th)\b", content, re.I) and re.search(r"[_^{}]", content):
                rows.append(_row(block, "table_math", sources, reason="表格单元数学需专用结构证明，本版保留原表", severity="medium"))
            continue
        if damaged_text(content):
            rows.append(_row(block, "damaged_glyph", sources, reason="控制、私用或替换字形需原页视觉裁决", route="vision"))
            continue
        if not sources and content.strip() and block["type"] in {"text", "caption", "footnote", "title", "formula", "chemical_equation"}:
            rows.append(_row(block, "native_region_unavailable", [], status="not_checked", reason="本区域无原生文字证据；保留OCR，不以同页其他区域代替核对", route="none"))
        candidate = layout_text("\n".join(entry["content"] for entry in sources))
        unique = all(len(owners[entry["id"]]) == 1 for entry in sources)
        strong = bool(sources) and unique and covers_block_region(block.get("bbox"), sources) and all(entry["quality"] == "high" and entry["role"] in {"body", "article_metadata", "unknown"} for entry in sources)
        # 仅同位置、同内容、同一原生出现可证明 OCR 多出一份；原生真重复保留。
        if sources and not unique and block["type"] == "text" and candidate == layout_text(content):
            peers = owners[sources[0]["id"]]
            source_ids = tuple(entry["id"] for entry in sources)
            same = []
            for peer in peers:
                peer_source_ids = tuple(entry["id"] for entry in matches[peer["id"]])
                if peer["type"] == "text" and peer.get("bbox") == block.get("bbox") and peer.get("content") == content and peer_source_ids == source_ids:
                    same.append(peer)
            if len(same) > 1 and same[0]["id"] != block["id"] and covers_block_region(block.get("bbox"), sources) and all(entry["quality"] == "high" for entry in sources) and not protected_spans(content):
                if apply_block_edits(result.blocks, [{"block_id": block["id"], "before": content, "after": ""}]):
                    removed.add(block["id"])
                    row = _row(block, "ocr_duplicate", sources, status="machine_corrected", reason="同区域同一原生出现只支持一份 OCR 文本", severity="medium")
                    row.update(before=content, after="", source="rule", review_route="none")
                    row["candidates"]["ocr"] = content
                    row["validation"] = "accepted"
                    rows.append(row)
                    continue
        if sources and layout_text(content) != candidate:
            critical = critical_signature(content) != critical_signature(candidate)
            row = _row(block, "critical_mismatch" if critical else "text_mismatch", sources, reason="同区域候选存在精确差异", route="vision" if critical or not strong else "text")
            plain = not protected_spans(content, include_math=True) and not re.search(r"[_^{}\\]", content + candidate)
            symbolic_change = re.findall(r"[=<>+−×÷±¼½¾]", content) != re.findall(r"[=<>+−×÷±¼½¾]", candidate)
            if strong and plain and not critical and not symbolic_change and block["type"] in {"text", "caption", "footnote"} and is_local_candidate(content, candidate) and not re.search(r"[⁰-⁹₀-₟α-ωΑ-Ω]", content + candidate):
                if apply_block_edits(result.blocks, [{"block_id": block["id"], "before": content, "after": candidate}]):
                    row.update(status="machine_corrected", source="rule", after=candidate, review_route="none", validation="accepted", reason="唯一同区域逐字符原生证据支持局部精确修复")
                    content = candidate
            elif not strong:
                row["reason"] = "原生字形质量、区域归属或阅读流不足，禁止覆盖 OCR"
            elif critical:
                row["reason"] = "数值、单位、引用或科学 token 有差异，本版 R1 不自动改写，需视觉裁决"
            rows.append(row)
        fixed = _delimit_bare_math(content)
        if fixed != content and block["type"] in {"text", "caption", "footnote"}:
            if apply_block_edits(result.blocks, [{"block_id": block["id"], "before": content, "after": fixed}]):
                row = _row(block, "math_delimiter", [], status="machine_corrected", reason="仅给完整下标或指数 token 添加数学定界，可见 token 守恒", severity="medium")
                row.update(before=content, after=fixed, source="rule", source_quality="syntax", review_route="none")
                row["candidates"]["ocr"] = content
                rows.append(row)
                content = fixed
        lint = _math_problem(content)
        if lint:
            rows.append(_row(block, "math_syntax", sources, reason=lint, route="vision", severity="medium"))
        if block["type"] == "title":
            numbered = re.match(r"^\s*(\d+(?:\.\d+)*)(?:[.)]?\s+)\S", content)
            expected = min(6, numbered[1].count(".") + 1) if numbered else None
            if expected and expected != block.get("level"):
                row = _row(block, "title_hierarchy", sources, reason="标题身份与编号存在层级冲突，缺少独立字体依据", severity="medium")
                rows.append(row)
    # 遗漏检测按区域和精确 token 执行，中文和短数字不经过英文指纹过滤。
    for entry in evidence:
        covered = owners.get(entry["id"], [])
        if entry["role"] == "publication_noise":
            if not covered:
                rows.append(_row({"id": None, "page": entry["page"], "bbox": entry["bbox"], "content": ""}, "publication_element", [entry], status="checked", classification="excluded", severity="low", reason="出版元素单独记账，不计正文遗漏"))
            continue
        if covered:
            continue
        in_figure = any(block["page"] == entry["page"] and block["type"] in {"picture", "chemical_structure", "table"} and overlap_fraction(entry["bbox"], block.get("bbox")) >= 0.85 for block in result.blocks)
        if in_figure:
            continue
        rows.append(_row({"id": None, "page": entry["page"], "bbox": entry["bbox"], "content": ""}, "missing_region", [entry], reason="原生区域未覆盖；缺少唯一正文插入锚点，未猜写新块", route="text" if entry["quality"] == "high" else "vision"))
    for index, block in enumerate(result.blocks):
        if block["id"] in removed or block["type"] != "text":
            continue
        previous = next((item for item in reversed(result.blocks[:index]) if item["type"] == "text" and item["id"] not in removed), None)
        if previous and previous["page"] < block["page"] and not re.search(r"[.!?。！？]\s*$", previous.get("content", "")):
            between = result.blocks[result.blocks.index(previous) + 1:index]
            if between and all(item["type"] in {"picture", "caption", "page_header", "page_footer"} for item in between):
                rows.append(_row(block, "reading_flow", matches[block["id"]], reason="跨页浮图可能插断正文；缺少原生阅读流和全部结构引用证明，未合并", severity="medium"))
    pages_with_native = {entry["page"] for entry in evidence if entry["content"].strip()}
    for page in range(1, page_count + 1):
        if page not in pages_with_native:
            rows.append(_row({"id": None, "page": page, "bbox": None, "content": ""}, "native_evidence_unavailable", [], status="not_checked", reason="本页无原生文字证据；不启动全页视觉复核", route="none"))
    result.metadata["pdf_repair"] = {
        "rule_version": RULE_VERSION, "source_skill_version": "2.7.7", "native_regions": len(evidence),
        "evidence_pages": len(pages_with_native), "page_count": page_count,
        "counts": dict(Counter(row["status"] for row in rows if row["classification"] != "excluded")),
        "excluded_publication_elements": sum(row["classification"] == "excluded" for row in rows),
        "model_requests": 0,
    }
    return rows


def _row(block, kind, sources, *, status="unresolved", reason, route="text", classification="content", severity="high") -> dict:
    """生成稳定审计身份，保持原文候选和最终状态可追溯。"""
    key = f"{block.get('id')}|{block['page']}|{kind}|{','.join(entry['id'] for entry in sources)}"
    return {
        "id": "repair_" + hashlib.sha256(key.encode()).hexdigest()[:20],
        "block_id": block.get("id"), "original_block_id": block.get("id"), "final_block_id": block.get("id"),
        "page": block["page"], "bbox": block.get("bbox"), "kind": kind, "severity": severity,
        "status": status, "reason": reason, "before": block.get("content", ""),
        "candidates": {"ocr": block.get("content", ""), "native": "\n".join(entry["content"] for entry in sources)},
        "evidence": sources, "evidence_ids": [entry["id"] for entry in sources], "source": "detector",
        "source_quality": "high" if sources and all(entry["quality"] == "high" for entry in sources) else "weak",
        "review_route": route if status in {"unresolved", "not_checked"} else "none", "classification": classification,
        "validation": "pending" if status in {"unresolved", "not_checked"} else "accepted",
    }


def _native_quality(native: dict) -> str:
    """只有字形、字符内容和几何信息完整时给出区域强证据。"""
    text = str(native.get("content") or "")
    chars = native.get("chars") or []
    if not text or damaged_text(text) or not native.get("bbox") or not chars:
        return "weak"
    if layout_text("".join(char.get("text", "") for char in chars)) != layout_text(text):
        return "weak"
    visible = [char for char in chars if char.get("text", "").strip()]
    if not visible or any(not char.get("bbox") or not char.get("baseline") for char in visible):
        return "weak"
    if any(overlap_fraction(char["bbox"], native["bbox"]) < 0.98 for char in visible):
        return "weak"
    if not covers_block_region(native["bbox"], visible):
        return "weak"
    sizes = [char.get("font_size") for char in visible]
    normals = [char.get("origin_normal") for char in visible]
    if any(not isinstance(value, (int, float)) or not math.isfinite(value) for value in sizes + normals):
        return "weak"
    height = median(sizes)
    if height <= 0 or min(sizes) < height * 0.85 or max(sizes) > height * 1.15 or max(normals) - min(normals) > height * 0.20:
        return "weak"
    return "high"


def _region_role(region: dict, repeats) -> str:
    """出版用途需位置佐证，唯一 DOI、SI 和通讯信息始终保留。"""
    content = layout_text(str(region.get("content") or ""))
    if re.search(r"\bdoi\b|10\.\d{4,9}/|Supporting Information|Correspond(?:ing|ence)|\bE[-\s]?mail\b|[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}|orcid\.org|Author|Acknowledg|Cite This", content, re.I):
        return "article_metadata"
    box = region.get("bbox")
    margin = bool(box) and (box[3] <= 0.10 or box[1] >= 0.90 or box[0] >= 0.92)
    known_purpose = bool(re.search(r"Downloaded (?:via|from)|sharingguidelines|pubs\.acs\.org/sharing|Contents lists available at|journal homepage:", content, re.I))
    repeated_margin = margin and len(repeats.get(content, set())) >= 2
    if (margin and known_purpose) or repeated_margin:
        return "publication_noise"
    if region.get("type") in {"picture", "table", "formula", "caption", "chemical_equation", "chemical_structure"}:
        return region["type"]
    if box and 0.08 <= box[1] < box[3] <= 0.94:
        return "body"
    return "unknown"


def _delimit_bare_math(text: str) -> str:
    """只包围完整简单下标/指数串，不解释未知 LaTeX 语义。"""
    spans = protected_spans(text, include_math=True)
    atom = r"(?:[A-Z][a-z]?(?:[_^](?:\{[A-Za-z0-9+−-]+\}|[A-Za-z0-9]))?)+"
    unit = r"[a-zA-Z]{1,3}\^\{[+−-]?\d+\}"
    pattern = rf"(?<![\w/\\])(?:{atom}|{unit})(?:[·/](?:{atom}|{unit}))*(?![\w{{}}])"
    output, cursor = [], 0
    for match in re.finditer(pattern, text):
        if "_" not in match[0] and "^" not in match[0]:
            continue
        if any(match.start() < end and match.end() > start for start, end in spans):
            continue
        output.extend([text[cursor:match.start()], "$" + match[0] + "$"])
        cursor = match.end()
    output.append(text[cursor:])
    return "".join(output)


def _math_problem(text: str) -> str:
    """发现空组与定界不配对，保留科学内容等待局部裁决。"""
    protected = protected_spans(text)
    plain = "".join(" " if any(start <= index < end for start, end in protected) else char for index, char in enumerate(text))
    if re.search(r"[_^]\s*\{\s*\}|\\(?:frac|sqrt|mathrm|text)\s*\{\s*\}", plain):
        return "存在空数学组，语法与科学含义均未证明"
    dollars = re.findall(r"(?<!\\)\$\$|(?<!\\)\$", plain)
    opened = None
    for delimiter in dollars:
        if opened is None:
            opened = delimiter
        elif opened != delimiter:
            return "数学定界不配对，未猜测补写位置"
        else:
            opened = None
    if opened or plain.count(r"\(") != plain.count(r"\)") or plain.count(r"\[") != plain.count(r"\]"):
        return "数学定界不闭合，未猜测补写位置"
    return ""
