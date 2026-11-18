"""知识库问答中候选论文的当前实验记录读取。"""

from __future__ import annotations

from urllib.parse import urlencode

from yuxi.permissions import ResourcePermission
from yuxi.repositories.knowledge_structure_repository import KnowledgeStructureRepository
from yuxi.services.knowledge_structure_service import read_experiment_cards
from yuxi.storage.postgres.manager import pg_manager


async def read_paper_experiments(
    *, kb_id: str, file_id: str, uid: str, sample: str = "", offset: int = 0, limit: int = 20,
) -> dict:
    """读取当前正文下的有效记录，人工覆盖即时优先，并返回同版本出处。"""
    snapshot = await read_experiment_cards(kb_id, file_id, uid=uid)
    async with pg_manager.get_async_session_context() as db:
        file, _kb = await KnowledgeStructureRepository().require_file(
            db, kb_id=kb_id, file_id=file_id, uid=uid, permission=ResourcePermission.READ,
        )
        filename = file.filename
    result = {
        "kb_id": kb_id, "file_id": file_id, "filename": filename,
        "status": "available" if snapshot["status"] == "draft" else snapshot["status"],
        "revision_id": snapshot.get("revision_id"), "source_sha256": snapshot.get("source_sha256"),
        "review_version": snapshot.get("review", {}).get("version", 0),
        "message": snapshot.get("message", ""),
        "partial_result": bool(snapshot.get("coverage", {}).get("partial_result")),
        "source_coverage": snapshot.get("coverage", {}).get("source_coverage"),
        "records": [], "excluded_records": [], "results": [],
        "total": 0, "offset": offset, "has_more": False,
    }
    if result["status"] != "available":
        result["message"] = result["message"] or "当前实验记录不可用，请从论文原文查证。"
        return result

    scoped_records = [
        item for item in snapshot["review"]["records"]
        if not sample or item["sample"].casefold() == sample.strip().casefold()
    ]
    result["excluded_records"] = [
        {key: item.get(key) for key in ("record_id", "kind", "sample", "metric", "evidence_ids")}
        for item in scoped_records if item["status"] == "excluded"
    ]
    records = [item for item in scoped_records if item["status"] in {"machine", "edited", "confirmed"}]
    selected = records[offset:offset + limit]
    source_url = "/knowledge-source?" + urlencode({"kb_id": kb_id, "file_id": file_id})
    for item in selected:
        result["records"].append({
            **{key: value for key, value in item.items() if key != "status"},
            "origin": "user_edit" if item["status"] in {"edited", "confirmed"} else "extracted",
            "source_url": source_url,
        })
    evidence_ids = {ref for item in selected for ref in item.get("evidence_ids", [])}
    result["results"] = [
        {
            "id": f'{snapshot["revision_id"]}:{evidence["evidence_id"]}',
            "kb_id": kb_id, "file_id": file_id, "content": evidence["quote"],
            "metadata": {
                **evidence, "source": filename, "source_url": source_url,
                "revision_id": snapshot["revision_id"], "source_sha256": snapshot["source_sha256"],
            },
        }
        for evidence in snapshot["evidence"] if evidence["evidence_id"] in evidence_ids
    ]
    result.update(total=len(records), has_more=offset + len(selected) < len(records))
    return result
