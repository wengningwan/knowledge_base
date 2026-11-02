from __future__ import annotations

from typing import Any

from yuxi.knowledge.chunking.ragflow_like.parsers import book, general, laws, qa, semantic, separator
from yuxi.knowledge.chunking.ragflow_like.presets import map_to_internal_parser_id, normalize_chunk_preset_id
from yuxi.knowledge.chunking.source import SourceDocument, source_sha256


def chunk_markdown(
    markdown_content: str,
    file_id: str,
    filename: str,
    processing_params: dict[str, Any],
    *,
    parse_assets: dict | None = None,
) -> list[dict[str, Any]]:
    """选择分块策略；论文分支直接产出显式来源记录。"""
    params = dict(processing_params or {})
    preset_id = normalize_chunk_preset_id(params.get("chunk_preset_id"))
    parser_config = params.get("chunk_parser_config") if isinstance(params.get("chunk_parser_config"), dict) else {}
    if preset_id == "paper":
        from yuxi.knowledge.chunking.paper import chunk_paper

        return chunk_paper(markdown_content, file_id, filename, parser_config, parse_assets=parse_assets)
    text_chunks = _dispatch_markdown_parser(preset_id, filename, markdown_content, parser_config)
    return _build_chunk_records(text_chunks, file_id, filename, markdown_content, parse_assets=parse_assets)


def chunk_file(
    file_content: str,
    file_id: str,
    filename: str,
    processing_params: dict[str, Any],
) -> list[dict[str, Any]]:
    """入库前内容已转换为 Markdown，复用同一策略入口。"""
    return chunk_markdown(file_content, file_id, filename, processing_params)


def _build_chunk_records(
    text_chunks: list[str],
    file_id: str,
    filename: str,
    source_text: str | None = None,
    *,
    parse_assets: dict | None = None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    search_from = 0
    digest = source_sha256(source_text) if source_text is not None else None
    source = SourceDocument(source_text, parse_assets) if source_text is not None else None

    for idx, chunk_content in enumerate(text_chunks):
        text = (chunk_content or "").strip()
        if not text:
            continue

        start_char_pos = None
        end_char_pos = None
        if source_text:
            found_at = source_text.find(text, search_from)
            if found_at >= 0:
                start_char_pos = found_at
                end_char_pos = found_at + len(text)
                search_from = end_char_pos

        records.append(
            {
                "id": f"{file_id}_chunk_{idx}",
                "content": text,
                "file_id": file_id,
                "filename": filename,
                "chunk_index": idx,
                "source": filename,
                "chunk_id": f"{file_id}_chunk_{idx}",
                "start_char_pos": start_char_pos,
                "end_char_pos": end_char_pos,
                "start_token_pos": None,
                "end_token_pos": None,
                "extraction_result": None,
                "source_metadata": {
                    "source_sha256": digest,
                    "chunker_version": "ragflow_like_v1",
                    "section_path": [],
                    "content_type": "text",
                    "evidence_ids": [],
                    "warnings": [],
                    "source_spans": [source.span(start_char_pos, end_char_pos)],
                }
                if start_char_pos is not None
                else None,
            }
        )

    return records


def _dispatch_markdown_parser(
    preset_id: str, filename: str, markdown_content: str, parser_config: dict[str, Any]
) -> list[str]:
    parser_id = map_to_internal_parser_id(preset_id)

    if parser_id == "naive":
        return general.chunk_markdown(markdown_content, parser_config)
    if parser_id == "qa":
        return qa.chunk_markdown(filename, markdown_content, parser_config)
    if parser_id == "book":
        return book.chunk_markdown(markdown_content, parser_config)
    if parser_id == "laws":
        return laws.chunk_markdown(filename, markdown_content, parser_config)
    if parser_id == "semantic":
        return semantic.chunk_markdown(markdown_content, parser_config)
    if parser_id == "separator":
        return separator.chunk_markdown(markdown_content, parser_config)

    return general.chunk_markdown(markdown_content, parser_config)
