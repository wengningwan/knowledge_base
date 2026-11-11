"""将未通过提取校验的内容投影为有出处的人工待办。"""

from __future__ import annotations

from copy import deepcopy

from yuxi.knowledge.adsorption_quantities import quantity_from_text
from yuxi.knowledge.experiment_review import review_parameters


def project_rejected_corrections(record: dict, sources: dict, rejected: list[dict]) -> list[dict]:
    """保留被拒候选的明确字段，不猜测未知样品或出处。"""
    refs = {ref: f"e{index}" for index, ref in enumerate(sources, 1)}
    tasks = []
    for entry in rejected:
        candidate = entry["record"]
        if not isinstance(candidate, dict):
            continue
        collection = entry["collection"]
        if collection == "observations":
            for index, (result_id, metric, result) in enumerate(_candidate_values(candidate)):
                fields = _candidate_fields(candidate, record, refs)
                fields.update(metric=_text(metric), result=_text(result))
                context = {
                    key: candidate[key] for key in ("observation_type", "entity_id")
                    if isinstance(candidate.get(key), str) and candidate[key]
                }
                pair = candidate.get("ordered_species_pair")
                pair_fields = ("numerator_entity_id", "denominator_entity_id")
                if isinstance(pair, dict) and all(isinstance(pair.get(key), str) and pair[key] for key in pair_fields):
                    context["ordered_species_pair"] = {key: pair[key] for key in pair_fields}
                if context.get("observation_type") == "breakthrough_working_capacity" and isinstance(metric, str) and metric:
                    context["entity_id"] = metric
                tasks.append({
                    "task_id": f'{entry["issue_id"]}:{index}', "kind": "candidate", "fields": fields,
                    "canonical_id": candidate.get("observation_id"), "result_id": result_id,
                    "observation_context": context,
                    "reason": "模型提取的结果或归属未通过校验，请对照原文确认。",
                    "detail": entry["message"],
                })
        elif collection == "synthesis_routes":
            operations = candidate.get("operations")
            for index, operation in enumerate(operations if isinstance(operations, list) else []):
                if not isinstance(operation, dict):
                    continue
                fields = _candidate_fields({**candidate, **operation}, record, refs)
                fields.update(kind="preparation", metric=_text(operation.get("operation_type")))
                tasks.append({
                    "task_id": f'{entry["issue_id"]}:{index}', "kind": "candidate", "fields": fields,
                    "canonical_id": operation.get("operation_id"), "operation_id": operation.get("operation_id"),
                    "reason": "这条制备操作需要确认样品或条件。", "detail": entry["message"],
                })
        elif collection in {"chemical_entities", "sample_batches", "sample_states", "experiment_runs"}:
            fields = _candidate_fields(candidate, record, refs)
            if fields["evidence_ids"]:
                tasks.append({
                    "task_id": entry["issue_id"], "kind": "source", "fields": fields,
                    "reason": "这项实验登记的数量或条件未通过原文核对，请查看原文；被拒登记尚未进入有效数据。",
                    "detail": entry["message"], "rejected_record": deepcopy(candidate), "collection": collection,
                })
    return tasks


def experiment_correction_tasks(coverage: dict, evidence: list[dict], records: dict[str, dict]) -> list[dict]:
    """从同版本候选、缺失信息及人工回执读取当前待办。"""
    tasks = deepcopy(coverage.get("correction_tasks", []))
    resolutions = coverage.get("manual_review", {}).get("resolutions", {})
    canonical = coverage.get("canonical") or {}
    refs = {
        item["evidence_id"]: source["evidence_id"]
        for item in canonical.get("evidence", []) for source in evidence
        if item["quote_raw"] == source["quote"] and item["pdf_page"] == source.get("page")
    }
    linked_records = {
        item[key]: item
        for collection, key in (
            ("experiment_runs", "run_id"), ("observations", "observation_id"),
            ("sample_states", "sample_state_id"), ("series_sets", "series_set_id"),
            ("synthesis_routes", "route_id"), ("mechanism_claims", "claim_id"),
        ) for item in canonical.get(collection, [])
    }
    for item in canonical.get("missing_information", []):
        linked = linked_records.get(item["record_id"], {})
        for index, field in enumerate(item["fields"]):
            fields = _candidate_fields(linked, canonical, refs)
            fields["metric"] = _text(field)
            tasks.append({
                "task_id": f'{item["missing_id"]}:{index}', "kind": "missing", "fields": fields,
                "reason": "模型没有找到这项数据。原文未提供时，直接保持缺失即可。",
                "detail": item["reason"],
            })
    for item in canonical.get("validation_issues", []):
        if item["issue_id"].startswith(("issue:filtered:", "issue:pagination:")):
            continue
        ids = item.get("record_ids") or ([item["record_id"]] if item.get("record_id") else [])
        linked = [linked_records[ref] for ref in ids if ref in linked_records]
        fields = _candidate_fields(linked[0] if len(linked) == 1 else {}, canonical, refs)
        fields["evidence_ids"] = list(dict.fromkeys([
            *fields["evidence_ids"],
            *(refs[ref] for ref in item.get("evidence_ids", []) if ref in refs),
            *(refs[ref] for ref in ids if ref in refs),
            *(refs[ref] for parent in linked for ref in parent.get("evidence_ids", []) if ref in refs),
        ]))[:32]
        related = [candidate for candidate in records.values()
                   if candidate.get("canonical_id") in ids]
        for candidate in related or [None]:
            related_fields = {key: candidate.get(key, value) for key, value in fields.items()} if candidate else fields
            related_fields["evidence_ids"] = list(dict.fromkeys([
                *related_fields["evidence_ids"], *fields["evidence_ids"],
            ]))[:32]
            tasks.append({
                "task_id": item["issue_id"] + (f':{candidate["record_id"]}' if candidate else ""),
                "kind": "source", "fields": related_fields,
                **({"record_id": candidate["record_id"]} if candidate else {}),
                "reason": item["message"], "detail": item["issue_type"],
            })
    carried_records = {task.get("record_id") for task in tasks} | {
        entry.get("record_id") for entry in resolutions.values()
    }
    for record_id, candidate in records.items():
        if (
            candidate["status"] == "pending" or f"record:{record_id}" in resolutions
            or (candidate["status"] == "excluded" and record_id not in carried_records)
        ):
            tasks.append({
                "task_id": f"record:{record_id}", "kind": "candidate", "record_id": record_id,
                "fields": {key: candidate.get(key) for key in (
                    "kind", "sample", "metric", "test", "conditions", "result", "evidence_ids",
                    "sample_state_id", "sample_state_ids", "scope", "run_id", "run_missing_reason", "parameters",
                )},
                "legacy_mapping_required": bool(candidate.get("legacy_mapping_required")),
                "legacy_operation_sample_ids": candidate.get("legacy_operation_sample_ids"),
                "reason": candidate.get("issue") if candidate.get("legacy_operation_mapping") else (
                    "请指定旧修改的对应记录。" if candidate.get("legacy_mapping_required") else "请确认这条记录的样品、条件和结果。"
                ),
                "detail": candidate.get("issue", ""),
            })
    for fact in coverage.get("unassigned_facts", []):
        if f'fragment:{fact["fact_id"]}' not in resolutions and any(
            candidate.get("fragment_id") == fact["fact_id"] for candidate in records.values()
        ):
            continue
        fields = _candidate_fields({}, canonical, {})
        fields["evidence_ids"] = fact.get("evidence_ids", [])
        fields["result" if fact.get("kind") == "result" else "conditions"] = fact["raw_text"]
        tasks.append({
            "task_id": f'fragment:{fact["fact_id"]}', "kind": "candidate", "fragment_id": fact["fact_id"],
            "fields": fields, "reason": "原文片段尚未确定归属样品。", "detail": "",
        })
    for task in tasks:
        task["fields"]["parameters"] = review_parameters(task["fields"].get("parameters") or {})
        for key in ("evidence_ids", "sample_state_ids"):
            task["fields"][key] = task["fields"].get(key) or []
        for key in ("sample_state_id", "run_id", "run_missing_reason"):
            task["fields"][key] = task["fields"].get(key) or None
        task["fields"]["scope"] = task["fields"].get("scope") or "sample"
        resolution = resolutions.get(task["task_id"])
        record_id = resolution.get("record_id") if resolution else task.get("record_id")
        saved = records.get(record_id)
        excluded = saved and saved["status"] == "excluded"
        overridden = saved and resolution and (
            (excluded and resolution["outcome"] in {"saved", "checked"})
            or (not excluded and resolution["outcome"] in {"ignored", "unreported"})
        )
        if saved and ((excluded and not resolution) or overridden):
            entry = coverage.get("manual_review", {}).get("items", {}).get(record_id, {})
            resolution = {
                "outcome": "ignored" if excluded else "saved", "record_id": record_id,
                "operator_id": entry.get("operator_id"), "updated_at": entry.get("updated_at"),
            }
        task["resolution"] = resolution
        if resolution and saved:
            task["record_id"] = saved["record_id"]
            task["fields"] = {key: saved.get(key, value) for key, value in task["fields"].items()}
        if resolution and resolution.get("evidence_ids") is not None:
            task["fields"]["evidence_ids"] = resolution["evidence_ids"]
        task["fields"]["parameters"] = review_parameters(task["fields"].get("parameters") or {})
    return tasks


def _candidate_fields(candidate: dict, record: dict, refs: dict) -> dict:
    """只从显式登记的关联预填样品、方法和条件。"""
    ids = candidate.get("sample_state_ids", [])
    if candidate.get("sample_state_id"):
        ids = [candidate["sample_state_id"]]
    if not ids:
        ids = candidate.get("applies_to_sample_state_ids") or candidate.get("output_sample_state_ids", [])
    ids = ids if isinstance(ids, list) else []
    states = {item["sample_state_id"]: item["reported_name"] for item in record.get("sample_states", [])}
    ids = list(dict.fromkeys(item for item in ids if isinstance(item, str) and item in states))
    names = [states[sample_id] for sample_id in ids]
    run = next((item for item in record.get("experiment_runs", []) if item["run_id"] == candidate.get("run_id")), {})
    conditions = candidate.get("conditions")
    conditions = conditions if isinstance(conditions, dict) else {}
    conditions = {**conditions, **{key: candidate[key] for key in ("temperature", "pressure") if key in candidate}}
    evidence_ids = candidate.get("evidence_ids")
    evidence_ids = evidence_ids if isinstance(evidence_ids, list) else []
    return {
        "kind": "measurement", "sample": _text(" / ".join(names)),
        "sample_state_id": ids[0] if len(ids) == 1 else None, "sample_state_ids": ids,
        "scope": "group" if len(ids) > 1 else "sample", "run_id": run.get("run_id"),
        "run_missing_reason": None if run else candidate.get("run_missing_reason") or "unresolved",
        "metric": "", "test": _text(run.get("technique") or run.get("run_type") or candidate.get("technique")),
        "conditions": " · ".join(f"{key}: {_text(value)}" for key, value in conditions.items() if _text(value))[:16000],
        "parameters": {key: quantity_from_text(text) for key, value in conditions.items() if (text := _text(value))},
        "result": "", "evidence_ids": list(dict.fromkeys(
            refs[ref] for ref in evidence_ids if isinstance(ref, str) and ref in refs
        ))[:32],
    }


def _candidate_values(candidate: dict):
    """读取尚未通过格式校验的结果，跳过不可解释的嵌套结构。"""
    kind = candidate.get("observation_type", "")
    results = candidate.get("results")
    if isinstance(results, dict):
        for metric, value in results.items():
            yield None, metric, value
    elif isinstance(results, list):
        for value in results:
            if isinstance(value, str):
                yield None, kind, value
            elif isinstance(value, dict):
                if "binding_energy_eV" in value:
                    yield None, value.get("assignment") or kind, {
                        "value": value["binding_energy_eV"], "unit": "eV",
                    }
                else:
                    metric = value.get("metric_id") or value.get("entity_id") or value.get("peak_name") or kind
                    yield value.get("result_id"), metric, value.get("value", value.get("position", value))
    found = False
    for key in (
        "value", "loading", "selectivity", "result", "reported_observation", "reported_order", "qualitative_result",
        "signed_value_from_figure", "magnitude_reported_in_prose", "interaction_distance_from_figure",
    ):
        if key in candidate:
            found = True
            metric = candidate.get("metric_id") or (
                kind if key in {"value", "result", "loading", "selectivity"} else key
            )
            yield candidate.get("result_id"), metric or key, candidate[key]
    written = candidate.get("qualitative_results")
    for value in written if isinstance(written, list) else []:
        found = True
        yield None, kind, value
    if not results and not found and candidate.get("metric_id"):
        yield candidate.get("result_id"), candidate["metric_id"], ""


def _text(value) -> str:
    """保留候选中的字面数值与单位，超长或未知结构留空供人工填写。"""
    if isinstance(value, dict):
        raw = value.get("raw_text")
        unit = value.get("unit_raw") or value.get("unit")
        if isinstance(raw, str):
            value = raw + (f" {unit}" if isinstance(unit, str) and unit and unit not in raw else "")
        else:
            value = value.get("value")
            if isinstance(value, (str, int, float)) and not isinstance(value, bool) and isinstance(unit, str) and unit:
                value = f"{value} {unit}"
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return ""
    text = str(value)
    return text if len(text) <= 1000 else ""
