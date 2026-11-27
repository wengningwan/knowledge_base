"""汇总两轮论文抽取后可定位的校验问题。"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from uuid import uuid4

from yuxi.knowledge.adsorption_contract import REGISTRIES, Notice


def paper_validation_issues(
    plans: list[dict],
    record: dict,
    notices: list[Notice],
    sources: dict,
    targets: list[dict],
) -> list[dict]:
    """按原文合并缺项和候选剔除原因，保留有效的关联编号。"""
    findings = {}
    runs = {item["run_id"] for item in record["experiment_runs"]}
    ids = {item[key] for name, key in REGISTRIES.items() for item in record[name]}
    located = _published_fields(record, sources)

    for plan in plans:
        run_id = plan["run_id"]
        if run_id not in runs:
            _add_finding(findings, sources, plan["evidence_ids"], f'未登记计划中的实验「{plan["run_type"]}」。')
            continue
        fields_by_ref = defaultdict(list)
        for expected in plan["results"]:
            fields_by_ref[expected["evidence_ids"][0]].extend(expected["fields"])
        for ref, fields in fields_by_ref.items():
            missing = [
                field for field in fields
                if not _published(field, run_id, ref, located)
            ]
            if missing:
                names = list(dict.fromkeys(
                    f'{field["target"]}.{field["subject"]} · {field["name"]}' if "subject" in field
                    else f'{field["target"]}.{field["name"]}' for field in missing
                ))
                reason = f'「{plan["run_type"]}」此处尚未核实：{"、".join(names)}。'
                record["missing_information"].append({
                    "missing_id": f"missing:plan:{uuid4().hex}", "record_id": run_id, "fields": names,
                    "reason": reason, "status": "needs_review", "evidence_ids": [ref],
                })
                _add_finding(
                    findings, sources, [ref], reason, run_id,
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
        run_id = target.get("run_id")
        if target["kind"] == "gap" and any(
            _published(target["field"], run_id, ref, located) for ref in target["evidence_ids"]
        ):
            continue
        _add_finding(findings, sources, target["evidence_ids"], target["message"], run_id if run_id in runs else None)

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


def _published_fields(record, sources):
    """按栏目、实验及已经发布的原文定位查找候选。"""
    entries = defaultdict(list)
    runs = {run["run_id"]: run for run in record["experiment_runs"]}
    entities = {item["entity_id"]: item["reported_name"] for item in record["chemical_entities"]}
    states = {item["sample_state_id"]: item["reported_name"] for item in record["sample_states"]}
    observation_runs = {item["observation_id"]: item["run_id"] for item in record["observations"]}
    source_labels = {ref: _metric_key(source["quote"]) for ref, source in sources.items()}
    source_values = {ref: _raw_key(source["quote"]) for ref, source in sources.items()}

    def add(run_id, refs, target, names, subjects=(), raw=""):
        """为同一出处登记该字段的量名、对象和原文值。"""
        entry = (
            {_metric_key(name) for name in names if name},
            {_metric_key(subject) for subject in subjects if subject},
            _raw_key(raw),
        )
        for ref in refs:
            entries[(run_id, ref, target)].append(entry)

    def add_condition(run_id, refs, name, value, subjects=()):
        """在实验的多处引用中只登记出现该条件及原值的出处。"""
        raw = value["raw_text"]
        label, raw_value = _metric_key(name), _raw_key(raw)
        selected = [ref for ref in refs if label in source_labels[ref] and _raw_matches(raw_value, source_values[ref])]
        add(run_id, selected, "conditions", [name], subjects, raw)

    for run_id, run in runs.items():
        for name, value in run["conditions"].items():
            add_condition(run_id, run["evidence_ids"], name, value)
        for name, definition in run.get("parameter_definitions", {}).items():
            refs = [ref for ref in run["evidence_ids"] if _metric_key(name) in source_labels[ref]]
            add(run_id, refs, "parameter_definitions", [name], raw=definition)
    for series in record["series_sets"]:
        add(series["run_id"], series["evidence_ids"], "series_sets",
            [series["source"].get("figure"), series["source"].get("table"), series["series_kind"]])
    for claim in record["mechanism_claims"]:
        destinations = {observation_runs[ref] for ref in claim["supported_by_observation_ids"] if ref in observation_runs}
        for run_id in destinations or {None}:
            add(run_id, claim["evidence_ids"], "mechanism_claims", [claim["claim"], claim["claim_type"]], raw=claim["claim"])
    for observation in record["observations"]:
        run_id = observation["run_id"]
        state = states.get(observation.get("sample_state_id"))
        for name, value in observation["conditions"].items():
            add_condition(run_id, observation["evidence_ids"], name, value, [state])
        for result in observation.get("results", [observation]):
            proof = result.get("source_text", {})
            value = result.get("value", {})
            metric = result["metric_id"]
            basis = value.get("basis", "")
            entity = entities.get(result.get("entity_id") or observation.get("entity_id"))
            subjects = [proof.get("subject"), entity, state]
            raw = value.get("raw_text", result.get("result", ""))
            add(run_id, result["evidence_ids"], "observations", [metric, proof.get("metric"), basis, metric + basis],
                subjects, raw)
            if fit := result.get("fit_result"):
                column, name = fit["target"], fit["name"]
                reported = runs[run_id].get(column) or {}
                names = {_metric_key(name), *(_metric_key(f"{subject} · {name}") for subject in subjects if subject)}
                if any(_metric_key(key) in names and published == value for key, published in reported.items()):
                    add(run_id, result["evidence_ids"], column, [name, metric, proof.get("metric")], subjects, raw)
    return entries


def _published(field, run_id, ref, entries):
    """检查同一事实是否已经进入目标栏目。"""
    target = field["target"]
    name = _metric_key(field["name"])
    subject = _metric_key(field.get("subject", ""))
    raw = _raw_key(field.get("raw_text", ""))
    candidates = entries.get((run_id, ref, target), [])
    if target == "mechanism_claims":
        candidates = [*candidates, *entries.get((None, ref, target), [])]
    return any(
        name in names and (not subject or subject in subjects or any(item.startswith(subject + name) for item in names))
        and (not raw or _raw_matches(raw, actual))
        for names, subjects, actual in candidates
    )


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


def _raw_key(text):
    """统一原值排版，同时保留单位大小写与数量符号。"""
    return re.sub(r"\s", "", unicodedata.normalize("NFKC", text)).replace("−", "-").lstrip("~≈约")


def _raw_matches(raw, actual):
    """约束相邻的数值和单位字符，排除数值或单位前缀命中。"""
    return raw and re.search(rf"(?<![\d.]){re.escape(raw)}(?![\dA-Za-zµμ%‰°℃℉/·⋅^~]|[.-]\d)", actual)
