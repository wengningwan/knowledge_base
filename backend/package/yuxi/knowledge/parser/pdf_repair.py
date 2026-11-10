"""按区域构建独立证据并执行保守 R1，正文块始终是唯一渲染来源。"""

from __future__ import annotations

import difflib
import hashlib
import math
import re
import unicodedata
from collections import Counter, defaultdict
from statistics import median

from yuxi.knowledge.parser.pdf_inline_atoms import parse_inline_atoms, validate_inline_change
from yuxi.knowledge.parser.pdf_review_messages import readable_review_reason
from yuxi.knowledge.parser.pdf_repair_rules import (
    apply_block_edits,
    covers_block_region,
    clean_prose_whitespace,
    critical_signature,
    damaged_text,
    is_local_candidate,
    layout_text,
    normalized_visible_text,
    overlap_fraction,
    protected_spans,
    proven_representation_equivalent,
    scientific_symbol_spans,
)
from yuxi.knowledge.parser.result import ParsedDocument


RULE_VERSION = "yuxi-r2-9"

# 跨页浮图断裂自动合并阈值：齐行比与字号比来自真实案例（file_0fd24b 六条）验证值。
_SENTENCE_END = re.compile(r"[.!?。！？]\s*$")
_PHRASE_BLOCKER = re.compile(r"[:：;；，、]\s*$|\.{3,}\s*$|…\s*$")
_MATH_TOKEN = re.compile(r"[$\\^_{}]")
_CITATION_HEAD = re.compile(r"^\[\d")
_FULL_LINE_RATIO = 0.97
_FONT_MATCH_RATIO = 0.90
_COLUMN_TOLERANCE = 0.02
_BELT_HEIGHT_LINES = 1.5
_FLOW_UNMERGED = "跨页浮图可能插断正文；缺少原生阅读流和全部结构引用证明，未合并"
_FLOW_MERGED = "跨页断裂满足齐行末行、接续开头与配对平衡全部条件，浮图两侧正文自动合并，可见字符守恒"

# 内容等价比较只统一排印差异，保留句读，确保子串包含可判定。
_EQUIVALENCE_TRANSLATION = str.maketrans({"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl", "−": "-", "–": "-", "—": "-"})


def run_rule_repair(result: ParsedDocument, native_blocks: list[dict], page_count: int) -> list[dict]:
    """原位修复 blocks 并更新统计，返回审计项供调用方扩展 reviews。"""
    publication_regions = _publication_regions(result.blocks, native_blocks)
    evidence = []
    for native in native_blocks:
        role = _region_role(native, publication_regions)
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
        role = _region_role(block, publication_regions)
        block["repair_role"] = role
        if role == "publication_noise":
            rows.append(_row(block, "publication_element", sources, status="checked", reason="区域类型、页边位置与出版用途证据支持排除正文核对", classification="excluded", severity="low"))
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
        # 块内重复转写：编号样式副本删除后可见字符守恒才允许自动修复。
        surplus_repaired = False
        if sources and layout_text(content) != candidate and block["type"] in {"text", "caption", "footnote"}:
            fixed = _repair_surplus_citation_token(content, candidate, strong)
            if fixed is not None and apply_block_edits(result.blocks, [{"block_id": block["id"], "before": content, "after": fixed}]):
                row = _row(block, "ocr_duplicate_token", sources, status="machine_corrected", reason="同区域同一原生出现只支持一份 OCR 文本", severity="medium")
                row.update(before=content, after=fixed, source="rule", review_route="none")
                row["candidates"]["ocr"] = content
                row["validation"] = "accepted"
                rows.append(row)
                content = fixed
                surplus_repaired = True
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
        if surplus_repaired:
            rows.append(_row(block, "representation_equivalent", sources, status="proven_equivalent", reason="删除重复转写副本后与原生证据可见字符守恒，正文逐字保留", route="none", severity="low"))
        elif sources and layout_text(content) != candidate and strong and proven_representation_equivalent(content, candidate):
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
        if scientific_symbol_spans(content):
            symbol_issue = next((
                row for row in reversed(rows)
                if row.get("block_id") == block["id"]
                and row.get("status") in {"unresolved", "not_checked"}
                and row.get("kind") not in {"reading_flow", "title_hierarchy", "missing_region", "table_math"}
            ), None)
            if symbol_issue is None:
                symbol_issue = _row(
                    block, "scientific_notation", sources, route="vision", severity="medium",
                    reason="行内字母下标或化学键级需要逐字对照原图，不按领域常识替换",
                )
                rows.append(symbol_issue)
            symbol_issue.update(review_scientific_symbols=True, review_route="vision")
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
    page_text_by_page = {entry["page"]: _equivalence_normalize(" ".join(str(block.get("content") or "") for block in result.blocks if block["page"] == entry["page"])) for entry in evidence}
    content_covered_regions = 0
    uncovered_by_page = defaultdict(list)
    for entry in evidence:
        covered = owners.get(entry["id"], [])
        if entry["role"] in {"publication_noise", "decorative_metadata"}:
            if not covered:
                reason = "期刊装饰元数据单独记账，不计正文遗漏" if entry["role"] == "decorative_metadata" else "出版元素单独记账，不计正文遗漏"
                rows.append(_row({"id": None, "page": entry["page"], "bbox": entry["bbox"], "content": ""}, "publication_element", [entry], status="checked", classification="excluded", severity="low", reason=reason))
            continue
        if covered:
            continue
        if _content_covered(
                entry,
                page_text_by_page.get(entry["page"], ""),
                _equivalence_normalize(" ".join(
                    str(block.get("content") or "") for block in result.blocks
                    if block["page"] == entry["page"] and overlap_fraction(entry["bbox"], block.get("bbox")) > 0
                )),
        ):
            content_covered_regions += 1
            continue
        in_figure = any(block["page"] == entry["page"] and block["type"] in {"picture", "chemical_structure", "table"} and overlap_fraction(entry["bbox"], block.get("bbox")) >= 0.85 for block in result.blocks)
        if in_figure:
            continue
        if entry.get("bbox") is None:
            rows.append(_row({"id": None, "page": entry["page"], "bbox": None, "content": ""}, "missing_region", [entry], reason="原生区域未覆盖；缺少唯一正文插入锚点，未猜写新块", route="text" if entry["quality"] == "high" else "vision"))
            continue
        uncovered_by_page[entry["page"]].append(entry)
    for page, page_entries in sorted(uncovered_by_page.items()):
        for cluster in _cluster_uncovered(page_entries):
            if len(cluster) < 3:
                for entry in cluster:
                    rows.append(_row({"id": None, "page": page, "bbox": entry["bbox"], "content": ""}, "missing_region", [entry], reason="原生区域未覆盖；缺少唯一正文插入锚点，未猜写新块", route="text" if entry["quality"] == "high" else "vision"))
                continue
            hull = [min(entry["bbox"][0] for entry in cluster), min(entry["bbox"][1] for entry in cluster), max(entry["bbox"][2] for entry in cluster), max(entry["bbox"][3] for entry in cluster)]
            ordered = sorted(cluster, key=lambda item: (item["bbox"][1], item["bbox"][0]))
            row = _row({"id": None, "page": page, "bbox": hull, "content": ""}, "formula_fragment_cluster", cluster, route="vision", reason="同页多条未覆盖原生碎片聚簇，疑似漏识别的公式或图注区域，未猜写新块")
            row.update(cluster=True, fragment_count=len(cluster), fragment_texts=[entry["content"] for entry in ordered])
            rows.append(row)
    merge_plans: list[dict] = []
    pending_absorbed: set[str] = set()
    pending_content: dict[str, str] = {}
    for index, block in enumerate(result.blocks):
        if block["id"] in removed or block["type"] != "text":
            continue
        previous = next((item for item in reversed(result.blocks[:index]) if item["type"] == "text" and item["id"] not in removed and item["id"] not in pending_absorbed), None)
        if not (previous and previous["page"] < block["page"]):
            continue
        # 句末判定用累计拼接文本：链式吸收后的段落完成不再续接。
        head_so_far = str(pending_content.get(previous["id"]) or previous.get("content") or "")
        if _SENTENCE_END.search(head_so_far):
            continue
        between = [item for item in result.blocks[result.blocks.index(previous) + 1:index] if item["id"] not in removed and item["id"] not in pending_absorbed]
        if not (between and all(item["type"] in {"picture", "caption", "page_header", "page_footer"} for item in between)):
            continue
        merge_evidence = _reading_flow_evidence(evidence, previous, block, head_so_far)
        if merge_evidence is None:
            rows.append(_row(block, "reading_flow", matches[block["id"]], reason=_FLOW_UNMERGED, severity="medium"))
            continue
        row = _row(previous, "reading_flow", matches[previous["id"]], status="machine_corrected", reason=_FLOW_MERGED, severity="medium")
        # 同一前块链式吸收多个后块时掺入后块身份，避免审计 id 塌缩。
        row["id"] = "repair_" + hashlib.sha256(f"{row['id']}|{block['id']}".encode()).hexdigest()[:20]
        row.update(source="rule", before=previous["content"], after=merge_evidence["merged_content"],
                   original_block_ids=[previous["id"], block["id"]], final_block_ids=[previous["id"]],
                   original_order=[previous.get("order"), block.get("order")], final_order=[previous.get("order")], merge_evidence=merge_evidence)
        rows.append(row)
        merge_plans.append({"head": previous, "tail": block, "tail_content": block["content"]})
        pending_content[previous["id"]] = merge_evidence["merged_content"]
        pending_absorbed.add(block["id"])
    _execute_reading_flow_merges(result, merge_plans, rows, removed)
    pages_with_native = {entry["page"] for entry in evidence if entry["content"].strip()}
    for page in range(1, page_count + 1):
        if page not in pages_with_native:
            rows.append(_row({"id": None, "page": page, "bbox": None, "content": ""}, "native_evidence_unavailable", [], status="not_checked", reason="本页无原生文字证据；不启动全页视觉复核", route="none"))
    result.metadata["pdf_repair"] = {
        "rule_version": RULE_VERSION, "source_skill_version": "2.7.7", "native_regions": len(evidence),
        "evidence_pages": len(pages_with_native), "page_count": page_count,
        "counts": dict(Counter(row["status"] for row in rows if row["classification"] != "excluded")),
        "excluded_publication_elements": sum(row["classification"] == "excluded" for row in rows),
        "content_covered_regions": content_covered_regions,
        "model_requests": 0,
    }
    for row in rows:
        row["reason"] = readable_review_reason(row["reason"])
    return rows



def _equivalence_normalize(text: str) -> str:
    """统一连字、连接符、项目符号与大小写，供内容等价子串比较。"""
    text = unicodedata.normalize("NFC", str(text)).translate(_EQUIVALENCE_TRANSLATION).lower()
    text = re.sub(r"[•●▪◦‣■]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _content_covered(entry: dict, page_text: str, adjacent_text: str = "") -> bool:
    """几何失配区域的保守内容等价判定：长片段全页命中，中片段仅限版面邻近块。

    片段按归一化后长度分级：≥8 字符在同页任意块中命中即可；4-7 字符必须在与区域
    bbox 有几何重叠的邻近块中命中（短词全局易乱命中）；<4 字符不参与判定。
    任一有效片段未命中则仍报遗漏；无有效片段（公式单字符等）回退几何判定，
    不靠短子串抵消。
    """
    if not page_text or not entry["content"].strip():
        return False
    long_fragments, mid_fragments = [], []
    for piece in re.split(r"[•●▪◦‣■]|(?<=[.!?;])\s+", entry["content"]):
        fragment = _equivalence_normalize(piece).strip(" \t.,;:!?-'“”\"()[]")
        if len(fragment) >= 8:
            long_fragments.append(fragment)
        elif len(fragment) >= 4:
            mid_fragments.append(fragment)
    if not long_fragments and not mid_fragments:
        return False
    if any(fragment not in page_text for fragment in long_fragments):
        return False
    if mid_fragments:
        if not adjacent_text or any(fragment not in adjacent_text for fragment in mid_fragments):
            return False
    return True


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


_CITATION_TOKEN = re.compile(r"\[\d{1,3}\]")


def _repair_surplus_citation_token(content: str, candidate: str, strong: bool) -> str | None:
    """同一原生出现被转写两次时，删除无几何支撑的引用编号副本。

    唯一授权依据：候选为编号样式 token，且删除后归一化可见字符与原生完全守恒，
    原生证据 strong；任一不满足返回 None，保留差异交人工或视觉裁决。
    """
    if not strong:
        return None
    target = normalized_visible_text(candidate)
    if not target or len(normalized_visible_text(content)) <= len(target):
        return None
    for match in _CITATION_TOKEN.finditer(content):
        remove_end = match.end() + 1 if content[match.end():match.end() + 1] == " " else match.end()
        fixed = content[:match.start()] + content[remove_end:]
        if normalized_visible_text(fixed) == target:
            return fixed
    return None


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


def _cluster_uncovered(entries: list[dict]) -> list[list[dict]]:
    """同页碎片连通分量聚簇：两两判邻接（垂直近邻且水平重叠或小间隙并置），并查集合并。

    阈值来自真实案例（file_0fd24b 第 11 页）：碎片高度中位数约 0.004，并排字符列
    水平间隙 0.0225 必须聚合，栏间距大于 0.05 必须分开；若用水平重叠判定会碎成 32 簇。
    交错排列（Å/Ñ 并排列、跨栏双列）不依赖扫描顺序，传递闭包保证同列成员连通。
    """
    ordered = sorted(entries, key=lambda item: (item["bbox"][1], item["bbox"][0]))
    heights = sorted(item["bbox"][3] - item["bbox"][1] for item in ordered)
    median_height = heights[len(heights) // 2]
    v_threshold = max(median_height * 2.0, 0.02)
    h_threshold = max(median_height * 8.0, 0.03)
    parent = list(range(len(ordered)))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for i in range(len(ordered)):
        for j in range(i + 1, len(ordered)):
            a, b = ordered[i]["bbox"], ordered[j]["bbox"]
            v_gap = max(a[1], b[1]) - min(a[3], b[3])
            h_gap = max(a[0], b[0]) - min(a[2], b[2])
            if v_gap <= v_threshold and h_gap <= h_threshold:
                parent[find(j)] = find(i)
    groups = defaultdict(list)
    for index, entry in enumerate(ordered):
        groups[find(index)].append(entry)
    return [sorted(group, key=lambda item: (item["bbox"][1], item["bbox"][0])) for group in groups.values()]


def _visible_tokens(text: str) -> list[str]:
    """提取可见词 token，剔除含数学定界或上下标记号的片段。"""
    return [token for token in re.findall(r"\S+", text) if not _MATH_TOKEN.search(token)]


def _pair_balanced(text: str) -> bool:
    """拼接文本的括号与数学定界配对平衡，防止劈开的引用或公式永不闭合。"""
    if any(text.count(left) != text.count(right) for left, right in (("(", ")"), ("[", "]"), ("{", "}"))):
        return False
    return text.count("$") % 2 == 0


def _char_font_median(entry: dict) -> float | None:
    """区域字符字号中位数；缺少有效字号时 None。"""
    sizes = [char["font_size"] for char in entry.get("chars", []) if isinstance(char.get("font_size"), (int, float))]
    return median(sizes) if sizes else None


def _reading_flow_evidence(evidence: list[dict], previous: dict, block: dict, pending_head: str | None = None) -> dict | None:
    """评估跨页浮图断裂是否满足全部自动合并条件，任一不满足返回 None。

    C1 断裂开场与 C2 纯浮图间隔由调用方循环先行保证；此处证明 C3 前块末行
    与同栏右缘齐行、C4 接续形态加顶部带词流覆盖、C5 拼接配对平衡、C7 字号一致。
    pending_head 是同一前块此前已计划吸收的累计文本，保证链式合并的配对平衡
    检查覆盖全部拼接内容。顶部带子序列覆盖用于对抗基线法向分行把上标引用
    切出独立行且 (y,x) 排序与阅读顺序不一致的噪声（file_0fd24b 第 7/13 页实证）。
    """
    head_content = str(pending_head or previous.get("content") or "").rstrip()
    tail_content = str(block.get("content") or "")
    if not head_content or not tail_content.strip() or not previous.get("bbox") or not block.get("bbox"):
        return None
    if _PHRASE_BLOCKER.search(head_content):
        return None
    tail_tokens = _visible_tokens(tail_content)
    if not tail_tokens:
        return None
    first_word = tail_tokens[0]
    if not (first_word[:1].islower() or first_word[:1] in ")]}. ,;" or first_word[:1].isdigit() or _CITATION_HEAD.match(first_word)):
        return None
    merged = head_content + " " + tail_content.lstrip()
    if not _pair_balanced(merged):
        return None
    page_head = [entry for entry in evidence if entry["page"] == previous["page"] and entry["content"].strip() and entry.get("bbox")]
    page_tail = [entry for entry in evidence if entry["page"] == block["page"] and entry["content"].strip() and entry.get("bbox")]
    if not page_head or not page_tail:
        return None
    inside = [entry for entry in page_head if overlap_fraction(entry["bbox"], previous["bbox"]) > 0]
    if not inside:
        return None
    tail_row = max(inside, key=lambda entry: entry["bbox"][1])
    column_x1 = [entry["bbox"][2] for entry in page_head if abs(entry["bbox"][0] - tail_row["bbox"][0]) < _COLUMN_TOLERANCE]
    if len(column_x1) < 2:
        return None
    baseline_x1 = median(column_x1)
    if baseline_x1 <= 0:
        return None
    c3_ratio = tail_row["bbox"][2] / baseline_x1
    if c3_ratio < _FULL_LINE_RATIO:
        return None
    heights = sorted(entry["bbox"][3] - entry["bbox"][1] for entry in page_tail)
    belt_depth = heights[len(heights) // 2] * _BELT_HEIGHT_LINES
    belt = []
    for entry in page_tail:
        eb = entry["bbox"]
        if min(eb[2], block["bbox"][2]) - max(eb[0], block["bbox"][0]) >= (eb[2] - eb[0]) * 0.5 and eb[3] > block["bbox"][1] and eb[1] < block["bbox"][1] + belt_depth:
            belt.append(entry)
    stream = [token for entry in sorted(belt, key=lambda item: (item["bbox"][1], item["bbox"][0])) for token in _visible_tokens(entry["content"])]
    cursor, hits = 0, 0
    for token in [item.lstrip("[") for item in tail_tokens[:5]]:
        found = next((position for position in range(cursor, len(stream)) if stream[position] == token), None)
        if found is None:
            continue
        cursor = found + 1
        hits += 1
    if hits < 2:
        return None
    head_font = _char_font_median(tail_row)
    belt_fonts = [size for entry in belt for size in [char["font_size"] for char in entry.get("chars", []) if isinstance(char.get("font_size"), (int, float))]]
    tail_font = median(belt_fonts) if belt_fonts else None
    if not head_font or not tail_font:
        return None
    c7_ratio = min(tail_font / head_font, head_font / tail_font)
    if c7_ratio < _FONT_MATCH_RATIO:
        return None
    return {
        "c1_open": True, "c2_float_gap": True,
        "c3_ratio": round(c3_ratio, 4), "c4_first_word": first_word, "c4_top_belt_hits": hits,
        "c5_pair_balanced": True, "c7_ratio": round(c7_ratio, 4), "merged_content": merged,
    }


def _execute_reading_flow_merges(result: ParsedDocument, merge_plans: list[dict], rows: list[dict], removed: set) -> None:
    """按收集顺序执行合并：前块逐次累积吸收文本保持 id/page/bbox，后块删除，差异行挂靠重定向。"""
    for plan in merge_plans:
        head, tail = plan["head"], plan["tail"]
        head["content"] = head["content"].rstrip() + " " + str(plan["tail_content"]).lstrip()
        removed.add(tail["id"])
        result.blocks = [item for item in result.blocks if item["id"] != tail["id"]]
        for row in rows:
            if row.get("block_id") == tail["id"]:
                row["block_id"] = head["id"]
                row["final_block_id"] = head["id"]


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


def _region_role(region: dict, publication_regions: list[dict]) -> str:
    """按版面证据排除出版区域，保留科学内容和文章联系信息。"""
    content = layout_text(str(region.get("content") or ""))
    if re.search(r"Supporting Information|Correspond(?:ing|ence)|\bE[-\s]?mail\b|[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}|orcid\.org|Acknowledg", content, re.I):
        return "article_metadata"
    # 引用行与投稿时间线是首页装饰服务文字，漏提取不计为正文遗漏；联系信息仍按内容元数据处理。
    if re.search(r"^Cite This|^(?:Received|Revised|Accepted|Published)(?:\s+online)?\s*:", content, re.I):
        return "decorative_metadata"
    if re.search(r"Author", content, re.I) and not re.match(r"(?:©|\(c\)|copyright)\s*", content, re.I):
        return "article_metadata"
    if region.get("type") in {"picture", "table", "formula", "caption", "chemical_equation", "chemical_structure", "code_block"}:
        return region["type"]
    box = region.get("bbox")
    if region.get("type") in {"page_header", "page_footer"} or any(
        item["page"] == region["page"] and overlap_fraction(box, item.get("bbox")) >= .85
        for item in publication_regions
    ):
        return "publication_noise"
    if re.search(r"\bdoi\b|10\.\d{4,9}/", content, re.I):
        return "article_metadata"
    # 左侧竖排水印栏与右侧对称；徽标、卷页行与页码片段整区域 fullmatch，正文句子与轴标数字不会命中。
    margin = bool(box) and (box[3] <= 0.10 or box[1] >= 0.90 or box[0] >= 0.92 or box[2] <= 0.08)
    known_purpose = bool(re.search(r"Downloaded (?:via|from)|sharingguidelines|pubs\.acs\.org/sharing|Contents lists available at|journal homepage:", content, re.I))
    badge = bool(re.fullmatch(r"Article|Review|Mini[- ]Review|Letter(?: to the Editor)?|Communication|Perspective|Feature|Highlight|Tutorial|Account|Essay|Opinion|Rapid (?:Communication|Publication)", content, re.I))
    citation_line = bool(re.fullmatch(r"[A-Za-z][A-Za-z .,&\-']* (?:19|20)\d{2}, ?\d{1,4}, ?\d{1,6}(?:[−–-]\d{1,6})?", content))
    page_number = bool(re.fullmatch(r"[−–-]?\d{4,6}", content))
    if margin and (known_purpose or badge or citation_line or page_number):
        return "publication_noise"
    if box and 0.08 <= box[1] < box[3] <= 0.94:
        return "body"
    return "unknown"


def _publication_regions(blocks: list[dict], native_blocks: list[dict]) -> list[dict]:
    """收集明确页眉页脚与有跨页、版权邻接证据的原生页边行。"""
    regions = [block for block in blocks if block["type"] in {"page_header", "page_footer"}]
    margins = []
    for native in native_blocks:
        box = native.get("bbox")
        if not box or box[3] - box[1] > .04 or not (box[3] <= .10 or box[1] >= .90):
            continue
        # 图表、公式和明确脚注内的文字不能独立充当出版元素证据。
        if any(block["page"] == native["page"] and block["type"] in {"picture", "table", "formula", "caption", "chemical_equation", "chemical_structure", "code_block", "footnote"}
               and overlap_fraction(box, block.get("bbox")) >= .85 for block in blocks):
            continue
        margins.append(native)
    copyrights = [item for item in margins if re.fullmatch(
        r"(?:©|\(c\)|copyright)\s*(?:copyright\s*)?(?:19|20)\d{2}\b\s*\S.*",
        layout_text(item.get("content", "")), re.I,
    )]
    for native in margins:
        content = layout_text(native.get("content", ""))
        box = native["bbox"]
        number = bool(re.fullmatch(r"[0-9]{1,6}", content)) and box[2] - box[0] <= .18
        peers = [item for item in margins if item["page"] != native["page"] and all(
            abs(left - right) <= .02 for left, right in zip(box, item["bbox"], strict=True)
        )]
        standalone_doi = bool(re.fullmatch(r"(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)?10\.\d{4,9}/\S+", content, re.I))
        contains_doi = bool(re.search(r"\bdoi\b|10\.\d{4,9}/", content, re.I))
        repeated = (bool(content) and not content.isdecimal() and (not contains_doi or standalone_doi)
                    and any(layout_text(item.get("content", "")) == content for item in peers))
        page_sequence = number and any(
            abs(item["page"] - native["page"]) == 1
            and re.fullmatch(r"[0-9]{1,6}", layout_text(item.get("content", "")))
            and int(layout_text(item["content"])) - int(content) == item["page"] - native["page"]
            for item in peers
        )
        doi_by_copyright = standalone_doi and any(
            item["page"] == native["page"] and abs(item["bbox"][1] - box[1]) <= .06
            for item in copyrights
        )
        if native in copyrights or repeated or page_sequence or doi_by_copyright:
            regions.append(native)
    return regions


def _delimit_bare_math(text: str) -> str:
    """只包围完整简单下标/指数串与纯数字引用上标，不解释未知 LaTeX 语义。"""
    spans = protected_spans(text, include_math=True)
    atom = r"(?:[A-Z][a-z]?(?:[_^](?:\{[A-Za-z0-9+−-]+\}|[A-Za-z0-9]))?)+"
    unit = r"[a-zA-Z]{1,3}\^\{[+−-]?\d+\}"
    # 片段守卫：匹配后紧邻 ^、_ 或 \\ 说明它是更大公式的一部分，包裹会撕裂渲染。
    pattern = rf"(?<![\w/\\])(?:{atom}|{unit})(?:[·/](?:{atom}|{unit}))*(?![\w{{}}^_\\])"
    # 引用上标只认纯数字组且必须附着在词/括号/句点之后；连排与非数字内容留给视觉裁决。
    citation = rf"(?<=[\w.)\]])\^\{{[0-9][0-9,\s\-–-]*\}}(?![\w{{}}^_\\])"
    output, cursor = [], 0
    for match in re.finditer(rf"{pattern}|{citation}", text):
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
