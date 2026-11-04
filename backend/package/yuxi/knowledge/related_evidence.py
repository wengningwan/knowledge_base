"""在主排序完成后补取同源证据，并统一模型可见的证据装配。"""

from typing import Any
from copy import deepcopy

from yuxi.repositories.knowledge_chunk_repository import KnowledgeChunkRepository

RELATED_CHUNK_LIMIT = 24
RELATED_CHAR_LIMIT = 24_000


async def attach_related_evidence(kb_id: str, hits: list[dict]) -> list[dict]:
    """所有主命中先占去重身份，补充块保持原子性并共享固定预算。"""
    primary_identities = {_identity(kb_id, hit) for hit in hits}
    included = set(primary_identities)
    count = chars = 0
    repository = KnowledgeChunkRepository()
    for hit in hits:
        metadata = hit.get("metadata") or {}
        source = metadata.get("source_metadata") or {}
        if source.get("chunker_version") != "paper_rules_v3":
            continue
        hit["related_evidence"] = []
        hit["related_primary_evidence"] = []
        candidates, status = await repository.get_paper_related_evidence(
            kb_id=kb_id, anchor=hit, limit=RELATED_CHUNK_LIMIT + 1
        )
        hit["related_evidence_status"] = status
        omitted = 0
        for candidate in candidates:
            identity = _identity(kb_id, candidate)
            if identity in primary_identities:
                hit["related_primary_evidence"].append(
                    {"chunk_id": candidate["metadata"]["chunk_id"], "evidence_role": candidate["evidence_role"]}
                )
                continue
            if identity in included:
                continue
            size = len(candidate["content"])
            if count >= RELATED_CHUNK_LIMIT or chars + size > RELATED_CHAR_LIMIT:
                omitted += 1
                continue
            included.add(identity)
            count += 1
            chars += size
            hit["related_evidence"].append(candidate)
        if omitted:
            status["budget"] = "truncated"
            status["omitted_chunks_at_least"] = status.get("omitted_chunks_at_least", 0) + omitted
        else:
            status.setdefault("budget", "complete")
    return hits


def pack_retrieval_evidence(kb_id: str, hits: list[dict], max_docs: int | None = None) -> tuple[list[dict], list[dict]]:
    """Search 与评估共用主块、补充块和来源角色的装配规则。"""
    selected = hits if max_docs is None else hits[:max_docs]
    primary = [_pack_chunk(kb_id, hit, index) for index, hit in enumerate(selected) if isinstance(hit, dict)]
    contexts = []
    context_chars = 0
    seen = {_identity(kb_id, hit) for hit in selected}
    all_primary = {(hit.get("metadata") or {}).get("chunk_id"): hit for hit in hits if isinstance(hit, dict)}
    for hit, packed in zip((hit for hit in selected if isinstance(hit, dict)), primary):
        if "related_evidence_status" in hit:
            packed["metadata"]["evidence_status"] = deepcopy(hit["related_evidence_status"])
        related_chunks = list(hit.get("related_evidence") or [])
        for relation in hit.get("related_primary_evidence") or []:
            existing = all_primary.get(relation["chunk_id"])
            if existing:
                related_chunks.append(
                    {
                        "content": existing["content"],
                        "metadata": {
                            key: value
                            for key, value in existing["metadata"].items()
                            if key
                            not in {"score", "distance", "rerank_score", "hybrid_score", "graph_score", "bm25_score"}
                        },
                        "evidence_role": relation["evidence_role"],
                    }
                )
        related_chunks.sort(key=lambda chunk: (chunk.get("metadata") or {}).get("chunk_index", 0))
        for related in related_chunks:
            identity = _identity(kb_id, related)
            if identity in seen:
                continue
            size = len(related["content"])
            if len(contexts) >= RELATED_CHUNK_LIMIT or context_chars + size > RELATED_CHAR_LIMIT:
                status = packed["metadata"].setdefault("evidence_status", {})
                status["budget"] = "truncated"
                status["omitted_chunks_at_least"] = status.get("omitted_chunks_at_least", 0) + 1
                continue
            seen.add(identity)
            context_chars += size
            context = _pack_chunk(kb_id, related, len(contexts))
            context["metadata"].update({"anchor_chunk_id": packed["id"], "evidence_role": related["evidence_role"]})
            contexts.append(context)
    return primary, contexts


def public_source_metadata(source: dict | None) -> dict:
    """剥离只用于校验发布身份的内部字段。"""
    return {key: value for key, value in (source or {}).items() if not key.startswith("_")}


def _pack_chunk(kb_id: str, chunk: dict, index: int) -> dict[str, Any]:
    """规范检索身份并移除对象存储内部位置。"""
    metadata = {
        key: value
        for key, value in (chunk.get("metadata") or {}).items()
        if key not in {"filepath", "parsed_path", "path", "markdown_file", "parse_assets_file"}
        and not key.startswith("_")
    }
    if isinstance(metadata.get("source_metadata"), dict):
        metadata["source_metadata"] = public_source_metadata(metadata["source_metadata"])
    file_id = metadata.get("file_id") or chunk.get("file_id") or chunk.get("full_doc_id") or ""
    chunk_id = metadata.get("chunk_id") or chunk.get("chunk_id") or chunk.get("id")
    if chunk.get("chunk_index") is not None:
        metadata.setdefault("chunk_index", chunk["chunk_index"])
    for key in ("score", "distance"):
        if chunk.get(key) is not None:
            metadata.setdefault(key, chunk[key])
    return {
        "id": str(chunk_id or f"{file_id}:{index + 1}"),
        "kb_id": str(kb_id),
        "file_id": str(file_id),
        "content": str(chunk.get("content") or ""),
        "metadata": metadata,
    }


def _identity(kb_id: str, chunk: dict) -> tuple:
    """去重身份绑定知识库、文件、块与原文版本。"""
    metadata = chunk.get("metadata") or {}
    return (
        kb_id,
        metadata.get("file_id") or chunk.get("file_id"),
        metadata.get("chunk_id") or chunk.get("chunk_id") or chunk.get("id"),
        (metadata.get("source_metadata") or {}).get("source_sha256"),
    )
