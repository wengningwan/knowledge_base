"""汇总两轮论文抽取后可定位的校验问题。"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict

from yuxi.knowledge.adsorption_contract import REGISTRIES, Notice


def paper_validation_issues(
    plans: list[dict],
    record: dict,
    notices: list[Notice],
    sources: dict,
    covered_tables: set[tuple[str, str]],
    targets: list[dict],
) -> list[dict]:
    """按原文合并缺项和候选剔除原因，保留有效的关联编号。"""
    findings = {}
    runs = {item["run_id"] for item in record["experiment_runs"]}
    ids = {item[key] for name, key in REGISTRIES.items() for item in record[name]}
    names = {item["entity_id"]: item["reported_name"] for item in record["chemical_entities"]}
    fields = defaultdict(set)
    for observation in record["observations"]:
        for result in observation.get("results", [observation]):
            metric = result["metric_id"]
            labels = [metric, result.get("value", {}).get("basis")]
            if entity := names.get(result.get("entity_id") or observation.get("entity_id")):
                labels.extend((entity + metric, metric + entity))
            for ref in result["evidence_ids"]:
                fields[observation["run_id"], ref].update(_metric_key(label) for label in labels if label)

    for plan in plans:
        run_id = plan["run_id"]
        if run_id not in runs:
            _add_finding(findings, sources, plan["evidence_ids"], f'未登记计划中的实验「{plan["run_type"]}」。')
            continue
        for expected in plan["results"]:
            ref = expected["evidence_ids"][0]
            if (run_id, ref) in covered_tables:
                continue
            missing = [name for name in expected["fields"] if _metric_key(name) not in fields[run_id, ref]]
            if missing:
                _add_finding(
                    findings, sources, [ref],
                    f'「{plan["run_type"]}」此处尚未核实结果：{"、".join(dict.fromkeys(missing))}。', run_id,
                )

    for series in record["series_sets"]:
        if series["source"]["data_availability"] == "needs_review":
            refs = [ref for ref in series["evidence_ids"] if sources[ref]["origin"] == "table"]
            if not refs and series.get("axis_consistency_status") == "needs_review":
                refs = series["evidence_ids"]
            _add_finding(findings, sources, refs, "曲线轴或数表坐标未能核实。", series["series_set_id"])

    for kind, message, *details in notices:
        if kind in {"unverified_printed_pages", "printed_page_conflict"} or not details:
            continue
        detail = details[0]
        candidate = detail.get("record")
        if not isinstance(candidate, dict):
            continue
        key = REGISTRIES.get(detail.get("collection"))
        record_id = candidate.get(key) if key else None
        if not isinstance(record_id, str) or record_id not in ids:
            run_id = candidate.get("run_id")
            record_id = run_id if isinstance(run_id, str) and run_id in runs else None
        _add_finding(findings, sources, _candidate_refs(candidate), message, record_id)

    for target in targets:
        _add_finding(findings, sources, target["evidence_ids"], target["message"])

    return [
        {
            "issue_id": f"issue:filtered:{index}",
            "issue_type": "unverified_paper_evidence",
            "severity": "warning",
            "message": " ".join(messages),
            "record_ids": list(record_ids) or [record["record_id"]],
            "evidence_ids": list(refs),
        }
        for index, (refs, (messages, record_ids)) in enumerate(findings.items(), 1)
    ]


def _add_finding(findings, sources, refs, message, record_id=None):
    """只接受本次原文目录的出处并合并重复说明。"""
    key = tuple(sorted({ref for ref in refs if isinstance(ref, str) and ref in sources}))
    if key:
        messages, record_ids = findings.setdefault(key, ({}, {}))
        messages[message] = None
        if record_id:
            record_ids[record_id] = None


def _candidate_refs(candidate):
    """候选只剔除部分成员时优先采用该部分的出处。"""
    refs = []
    for field in ("results", "operations"):
        if isinstance(members := candidate.get(field), list):
            for member in members:
                if isinstance(member, dict) and isinstance(evidence := member.get("evidence_ids"), list):
                    refs.extend(evidence)
    evidence = candidate.get("evidence_ids")
    return refs or (evidence if isinstance(evidence, list) else [])


def _metric_key(metric):
    """统一原文指标名的排版空白。"""
    return re.sub(r"[\s_$(){}]", "", unicodedata.normalize("NFKC", metric).casefold())
