"""按区域构建独立证据并执行保守 R1，正文块始终是唯一渲染来源。"""

from __future__ import annotations

import difflib
import hashlib
import math
import re
from collections import Counter, defaultdict
from statistics import median

from yuxi.knowledge.parser.pdf_inline_atoms import parse_inline_atoms, validate_inline_change
from yuxi.knowledge.parser.pdf_repair_rules import (
    apply_block_edits,
    covers_block_region,
    clean_prose_whitespace,
    critical_signature,
    damaged_text,
    is_local_candidate,
    layout_text,
    overlap_fraction,
    protected_spans,
    proven_representation_equivalent,
)
from yuxi.knowledge.parser.result import ParsedDocument


RULE_VERSION = "yuxi-r2-2"


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
            "quality": _native_quality(native), "role": role, "chars": native.get("chars", []),
        })
    evidence_by_page = defaultdict(list)
    for entry in evidence:
        evidence_by_page[entry["page"]].append(entry)
    structure_rows = _repair_proven_structure(result, evidence)
    matches = {
        block["id"]: [entry for entry in evidence_by_page[block["page"]] if overlap_fraction(entry["bbox"], block.get("bbox")) >= 0.85]
        for block in result.blocks
    }
    owners = defaultdict(list)
    for block in result.blocks:
        for entry in matches[block["id"]]:
            owners[entry["id"]].append(block)
    rows = structure_rows
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
        if block["type"] in {"text", "caption", "footnote"}:
            whitespace = clean_prose_whitespace(content)
            if whitespace != content and apply_block_edits(result.blocks, [{"block_id": block["id"], "before": content, "after": whitespace}], allow_prose_cleanup=True):
                row = _row(block, "prose_whitespace", [], status="machine_corrected", reason="S01–S06仅清理普通正文的空白，保护公式、结构与有效换行", severity="low", route="none")
                row.update(before=content, after=whitespace, source="rule", source_quality="syntax", validation="accepted")
                row["candidates"]["ocr"] = content
                rows.append(row)
                content = whitespace
        fixed = _delimit_bare_math(content)
        if fixed != content and block["type"] in {"text", "caption", "footnote"}:
            if apply_block_edits(result.blocks, [{"block_id": block["id"], "before": content, "after": fixed}]):
                row = _row(block, "math_delimiter", [], status="machine_corrected", reason="仅给完整下标或指数 token 添加数学定界，可见 token 守恒", severity="medium")
                row.update(before=content, after=fixed, source="rule", source_quality="syntax", review_route="none")
                row["candidates"]["ocr"] = content
                rows.append(row)
                content = fixed
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
        if sources and layout_text(content) != candidate and block["type"] in {"text", "caption", "footnote"}:
            mixed = protected_spans(content, include_math=True) or re.search(r"[_^{}\\⁰-⁹₀-₟α-ωΑ-Ω]", content + candidate) or critical_signature(content) != critical_signature(candidate)
            region_proven = unique and covers_block_region(block.get("bbox"), sources) and all(entry["role"] in {"body", "article_metadata", "unknown"} for entry in sources)
            if mixed and region_proven:
                fixed, targets = _repair_local_words(content, sources)
                if fixed != content and apply_block_edits(result.blocks, [{"block_id": block["id"], "before": content, "after": fixed}]):
                    row = _row(block, "local_word_repair", sources, status="machine_corrected", reason="唯一同区域的原词及左右邻词对齐，局部逐字几何只证明普通词拼写", route="none", severity="medium")
                    row.update(before=content, after=fixed, source="rule", targets=targets, local_geometry_validation="accepted")
                    row["candidates"]["ocr"] = content
                    rows.append(row)
                    content = fixed
        if sources and layout_text(content) != candidate and strong and proven_representation_equivalent(content, candidate):
            rows.append(_row(block, "representation_equivalent", sources, status="proven_equivalent", reason="唯一同区域原生证据与有限排印语法证明等价，正文逐字保留", route="none", severity="low"))
        elif sources and layout_text(content) != candidate:
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
        lint = _math_problem(content)
        if lint:
            rows.append(_row(block, "math_syntax", sources, reason=lint, route="vision", severity="medium"))
        if block["type"] == "title":
            numbered = re.match(r"^\s*(\d+(?:\.\d+)*)(?:[.)]?\s+)\S", content)
            title_offset = 2 if block.get("title_hierarchy_document_title_id") else 1
            expected = min(6, numbered[1].count(".") + title_offset) if numbered else None
            if expected and expected != block.get("level"):
                body_sizes = [char["font_size"] for entry in evidence if entry["page"] == block["page"] and entry["role"] == "body" and entry not in sources for char in entry.get("chars", []) if isinstance(char.get("font_size"), (int, float))]
                title_sizes = [char["font_size"] for entry in sources for char in entry.get("chars", []) if isinstance(char.get("font_size"), (int, float))]
                if strong and candidate == layout_text(content) and body_sizes and title_sizes and min(title_sizes) > median(body_sizes) * 1.15:
                    old_level = block.get("level")
                    block["level"] = expected
                    row = _row(block, "title_hierarchy", sources, status="machine_corrected", reason="唯一原生标题字号和原文编号共同支持层级，标题文字保持", severity="medium", route="none")
                    row.update(source="rule", before_level=old_level, after_level=expected, original_block_ids=[block["id"]], final_block_ids=[block["id"]], original_order=[block.get("order")], final_order=[block.get("order")])
                else:
                    row = _row(block, "title_hierarchy", sources, reason="标题身份与编号存在层级冲突，缺少独立字体依据", severity="medium", route="none")
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



def _repair_proven_structure(result: ParsedDocument, evidence: list[dict]) -> list[dict]:
    """只按唯一原生行身份拆日期或合并同行碎块，跨页关系不猜测。"""
    rows, index = [], 0
    while index < len(result.blocks):
        block = result.blocks[index]
        content = str(block.get("content") or "")
        if block.get("type") != "text":
            index += 1
            continue
        sources = [entry for entry in evidence if entry["page"] == block["page"] and overlap_fraction(entry.get("bbox"), block.get("bbox")) >= .98]
        unique = all(sum(other["page"] == entry["page"] and overlap_fraction(entry.get("bbox"), other.get("bbox")) >= .98 for other in result.blocks) == 1 for entry in sources)
        date_count = 0
        for entry in sources:
            if not re.match(r"^(?:Received|Revised|Accepted|Published)(?:\s+online)?\s*:\s*.*\d", entry["content"], re.I):
                break
            date_count += 1
        if 0 < date_count < len(sources) and unique and all(entry["quality"] == "high" for entry in sources) and covers_block_region(block.get("bbox"), sources):
            metadata_text = layout_text("\n".join(entry["content"] for entry in sources[:date_count]))
            body_text = layout_text("\n".join(entry["content"] for entry in sources[date_count:]))
            boundary_match = re.match(r"\s*" + r"\s+".join(re.escape(word) for word in metadata_text.split()) + r"(?P<separator>\s+)", content)
            # 分隔空白归后块，避免后续普通正文清理删除前块的尾部空格。
            boundary = boundary_match.start("separator") if boundary_match and layout_text(content[boundary_match.end():]) == body_text else -1
            body_id = f"{block['id']}_body"
            if boundary > 0 and not any(start < boundary < end for start, end in protected_spans(content, include_math=True)) and all(other["id"] != body_id for other in result.blocks):
                metadata = {**block, "type": "footnote", "content": content[:boundary], "bbox": _region_envelope(sources[:date_count])}
                body = {**block, "id": body_id, "content": content[boundary:], "bbox": _region_envelope(sources[date_count:])}
                if metadata["content"] + body["content"] == content:
                    row = _row(block, "date_body_split", sources, status="machine_corrected", reason="唯一高质量原生行给出日期/正文边界，原始字符逐字守恒", route="none", severity="medium")
                    row.update(source="rule", original_block_ids=[block["id"]], final_block_ids=[block["id"], body_id], original_order=[block.get("order", index)], final_order=[index, index + 1], before=content, after=content)
                    rows.append(row)
                    result.blocks[index:index + 1] = [metadata, body]
                    index += 2
                    continue
        if index + 1 < len(result.blocks):
            right = result.blocks[index + 1]
            right_text = str(right.get("content") or "")
            if right.get("type") == "text" and right["page"] == block["page"] and content == content.strip() and right_text == right_text.strip() and not protected_spans(content, include_math=True) and not protected_spans(right_text, include_math=True):
                proof = []
                for entry in evidence:
                    text = entry["content"]
                    if entry["page"] != block["page"] or entry["quality"] != "high" or not text.startswith(content) or not text.endswith(right_text):
                        continue
                    middle = text[len(content):len(text) - len(right_text)] if right_text else ""
                    if middle and middle.isspace() and text.count(content) == 1 and text.count(right_text) == 1 and overlap_fraction(block.get("bbox"), entry.get("bbox")) >= .98 and overlap_fraction(right.get("bbox"), entry.get("bbox")) >= .98:
                        proof.append(entry)
                if len(proof) == 1:
                    after = proof[0]["content"]
                    if layout_text(content + " " + right_text) == layout_text(after):
                        row = _row(block, "same_line_join", proof, status="machine_corrected", reason="唯一原生同一行证明OCR碎块相邻，按原生空白连接且可见字符守恒", route="none", severity="medium")
                        row.update(source="rule", original_block_ids=[block["id"], right["id"]], final_block_ids=[block["id"]], original_order=[block.get("order", index), right.get("order", index + 1)], final_order=[index], before=content + " " + right_text, after=after)
                        rows.append(row)
                        result.blocks[index:index + 2] = [{**block, "content": after, "bbox": _region_envelope([block, right])}]
                        index += 1
                        continue
        index += 1
    if rows:
        final_orders = {}
        for order, block in enumerate(result.blocks):
            block["order"] = order
            final_orders[block["id"]] = order
        for row in rows:
            row["final_order"] = [final_orders[block_id] for block_id in row["final_block_ids"]]
    return rows


def _repair_local_words(content: str, sources: list[dict]) -> tuple[str, list[dict]]:
    """只替换独立邻词及字符几何均对齐的普通词，其余原字符不动。"""
    native = "\n".join(entry["content"] for entry in sources)
    old_atoms, new_atoms = parse_inline_atoms(content), parse_inline_atoms(native)
    old_words = [content[atom["start"]:atom["end"]] for atom in old_atoms]
    new_words = [native[atom["start"]:atom["end"]] for atom in new_atoms]
    old_protected = protected_spans(content, include_math=True)
    new_protected = protected_spans(native, include_math=True)
    targets = []
    for operation, a, b, c, d in difflib.SequenceMatcher(a=old_words, b=new_words, autojunk=False).get_opcodes():
        if operation != "replace" or b - a != 1 or d - c != 1 or not a or not c or b == len(old_atoms) or d == len(new_atoms):
            continue
        before, after = old_words[a], new_words[c]
        if not all(re.fullmatch(r"[A-Za-z]{4,}", word) and not critical_signature(word) for word in (before, after)) or not is_local_candidate(before, after):
            continue
        if old_words[a - 1] != new_words[c - 1] or old_words[b] != new_words[d]:
            continue
        old_group, new_group = old_atoms[a - 1:b + 1], new_atoms[c - 1:d + 1]
        if any(atom["kind"] != "word" for atom in old_group + new_group):
            continue
        old_start, old_end = old_group[0]["start"], old_group[-1]["end"]
        new_start, new_end = new_group[0]["start"], new_group[-1]["end"]
        if any(old_start < end and old_end > start for start, end in old_protected) or any(new_start < end and new_end > start for start, end in new_protected):
            continue
        if sum(old_words[index:index + 3] == old_words[a - 1:b + 1] for index in range(len(old_words) - 2)) != 1 or sum(new_words[index:index + 3] == new_words[c - 1:d + 1] for index in range(len(new_words) - 2)) != 1:
            continue
        left_gap = content[old_group[0]["end"]:old_atoms[a]["start"]]
        right_gap = content[old_atoms[a]["end"]:old_group[-1]["start"]]
        if layout_text(left_gap) != layout_text(native[new_group[0]["end"]:new_atoms[c]["start"]]) or layout_text(right_gap) != layout_text(native[new_atoms[c]["end"]:new_group[-1]["start"]]):
            continue
        evidence_ids = _local_character_proof(sources, new_start, new_end)
        if evidence_ids:
            targets.append({"start": old_atoms[a]["start"], "end": old_atoms[a]["end"], "old": before, "new": after, "native_start": new_atoms[c]["start"], "native_end": new_atoms[c]["end"], "native_evidence_ids": evidence_ids})
    if not targets or len(targets) > 4:
        return content, []
    after = content
    for target in reversed(targets):
        after = after[:target["start"]] + target["new"] + after[target["end"]:]
    return (content, []) if validate_inline_change(content, after) else (after, targets)


def _local_character_proof(sources: list[dict], start: int, end: int) -> list[str]:
    """逐字映射目标及邻词到真实原生字符，按所在原生行验证局部几何。"""
    offset, evidence_ids = 0, []
    for entry in sources:
        text = entry["content"]
        entry_start = offset
        left, right = max(start, entry_start), min(end, entry_start + len(text))
        offset += len(text) + 1
        if left >= right:
            continue
        characters = entry.get("chars", [])
        raw = "".join(char.get("text", "") for char in characters)
        if raw.strip() != text:
            return []
        trim = len(raw) - len(raw.lstrip())
        local_start, local_end = left - entry_start + trim, right - entry_start + trim
        selected, position = [], 0
        for char in characters:
            char_end = position + len(char.get("text", ""))
            if position < local_end and char_end > local_start:
                if position < local_start or char_end > local_end:
                    return []
                selected.append(char)
            position = char_end
        visible = [char for char in selected if char.get("text", "").strip()]
        if not visible or any(overlap_fraction(char.get("bbox"), entry.get("bbox")) < .98 for char in visible):
            return []
        local = {"content": "".join(char["text"] for char in selected), "chars": selected, "bbox": _region_envelope(visible)}
        if _native_quality(local) != "high":
            return []
        evidence_ids.append(entry["id"])
    return evidence_ids


def _region_envelope(regions: list[dict]) -> list:
    """合并已有真实区域，不为文字片段推造坐标。"""
    boxes = [entry["bbox"] for entry in regions]
    return [min(box[0] for box in boxes), min(box[1] for box in boxes), max(box[2] for box in boxes), max(box[3] for box in boxes)]


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
    if re.search(r"\bdoi\b|10\.\d{4,9}/|Supporting Information|Correspond(?:ing|ence)|^(?:Received|Revised|Accepted|Published)(?:\s+online)?\s*:|\bE[-\s]?mail\b|[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}|orcid\.org|Author|Acknowledg|Cite This", content, re.I):
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
