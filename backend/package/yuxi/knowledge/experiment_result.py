"""由同次机器原稿和人工覆盖生成页面与导出的共同结果。"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256

from yuxi.knowledge.adsorption_contract import (
    REGISTRIES,
    evidence_references,
    normalize_legacy_quantities,
    normalize_observation_results,
    operation_sample_ids,
    output_schema,
    validate_references,
    validate_shape,
)
from yuxi.knowledge.adsorption_quantities import (
    canonical_unit, check_quantity, ground_quantity, quantity_from_text, quantity_text,
)
from yuxi.knowledge.adsorption_relations import observation_assignment_error
from yuxi.knowledge.adsorption_review import project_experiment_cards, project_observation_values
from yuxi.knowledge.experiment_review import experiment_record_id, experiment_review_records, review_parameters

_ACTIVE = {"edited", "confirmed"}
_ENTITIES = {
    "sample_batch": ("sample_batches", "sample_batch_id", {"reported_name", "batch_number"}),
    "sample_state": ("sample_states", "sample_state_id", {"reported_name", "state", "physical_form", "sample_batch_id"}),
    "synthesis_route": ("synthesis_routes", "route_id", {"yield_text", "replicate_count"}),
    "experiment_run": ("experiment_runs", "run_id", {"method", "parameters"}),
}


@dataclass
class ExperimentResult:
    """封装同一正文与人工版本的工作台结果。"""

    paper: dict | None
    cards: list[dict]
    records: dict[str, dict]
    review: dict
    evidence: list[dict]
    export_error: str | None = None


def assemble_experiment_result(
    coverage: dict, cards: list[dict], evidence: list[dict], markdown: str, *, revision_id: str, source_hash: str,
) -> ExperimentResult:
    """在副本中应用结构化人工修改，不改变原稿或持久化活动结果。"""
    review = deepcopy(coverage.get("manual_review") or {})
    review.pop("unresolved_dependencies", None)
    review.pop("unresolved_entities", None)
    raw = coverage.get("canonical")
    if not isinstance(raw, dict):
        return ExperimentResult(None, cards, experiment_review_records(cards, coverage), review, evidence)

    paper = deepcopy(raw)
    legacy = paper["schema_version"] == "1.0.0-draft"
    sources, evidence_refs = _source_index(paper, evidence, markdown)
    if legacy:
        normalize_observation_results(paper, revision_id)
        normalize_legacy_quantities(paper)
        paper["schema_version"] = "2.0.0-draft"
        paper["extraction_metadata"]["base_schema_version"] = "1.0.0-draft"
        _legacy_missing_fields(paper)
    for route in paper["synthesis_routes"]:
        for operation in route["operations"]:
            operation["applies_to_sample_state_ids"] = operation_sample_ids(operation, paper, route)
    mapping_errors = {}
    if legacy:
        for record_id, entry in review.get("items", {}).items():
            target = entry.get("target") or entry["record"].get("target") or {}
            if not target.get("legacy_operation_mapping"):
                continue
            sample_id = target["sample_state_id"]
            assigned = False
            for route in paper["synthesis_routes"]:
                if sample_id not in route["target_sample_state_ids"]:
                    continue
                for operation in route["operations"]:
                    if operation["operation_id"] == target["operation_id"] and not operation["applies_to_sample_state_ids"]:
                        operation["applies_to_sample_state_ids"] = [sample_id]
                        assigned = True
                        break
            if not assigned:
                mapping_errors[record_id] = "所选样品不属于该步骤所在的制备路线"

    base_cards, _ = project_experiment_cards(paper, sources, project_observation_values(paper))
    base = experiment_review_records(base_cards, {})
    result_counts = {row["observation_id"]: len(row["results"]) for row in paper["observations"]}
    for row in base.values():
        row["original_result_count"] = result_counts.get(row.get("canonical_id"), 0)
    if legacy:
        review["items"] = _adapt_legacy_reviews(review.get("items", {}), cards, base, paper)
    items = review.get("items", {})
    errors = []
    entity_order = {kind: index for index, kind in enumerate(_ENTITIES)}
    for entity in sorted(review.get("entities", {}).values(), key=lambda item: entity_order[item["change"]["kind"]]):
        candidate = deepcopy(paper)
        try:
            _apply_entity(candidate, entity, sources, evidence_refs)
        except ValueError as error:
            errors.append(f"登记修改尚未生效：{error}")
            review.setdefault("unresolved_entities", []).append({"change": entity["change"], "reason": str(error)})
        else:
            paper = candidate

    edited_steps = {}
    for entry in items.values():
        record = entry["record"]
        target = entry.get("target") or record.get("target") or {}
        if target.get("operation_id") and target.get("sample_state_id") and not record.get("legacy_mapping_required"):
            edited_steps.setdefault(target["sample_state_id"], set()).add(target["operation_id"])
    before_ids = _dependent_ids(paper)
    paths_before = deepcopy(paper)
    split_problem = None
    derived_steps = {}
    try:
        step_ids, derived_steps = _split_sample_paths(paper, edited_steps)
    except ValueError as error:
        paper = paths_before
        step_ids = {}
        split_problem = str(error)
        errors.append(str(error))
    view_ids = {}
    inactive = {}
    replacements = {}
    covered_base_ids = set(items)
    for record_id, entry in sorted(items.items(), key=lambda pair: (pair[1].get("version", 0), pair[0])):
        record = deepcopy(entry["record"])
        record["target"] = entry.get("target") or record.get("target") or {}
        entry = {**entry, "record": record}
        if record.get("legacy_mapping_required"):
            inactive[record_id] = record
            continue
        target = record.get("target") or {}
        original_id = experiment_record_id({**target, "kind": "preparation"}) if target.get("legacy_operation_mapping") else record_id
        covered_base_ids.add(original_id)
        if record_id in mapping_errors:
            inactive[record_id] = {**record, "status": "pending", "issue": mapping_errors[record_id]}
            errors.append(mapping_errors[record_id])
            continue
        if target.get("operation_id") and split_problem:
            inactive[record_id] = {**record, "status": "pending", "issue": split_problem}
            continue
        candidate = deepcopy(paper)
        try:
            _require_target(candidate, target, step_ids)
            if record["status"] not in _ACTIVE:
                if target.get("result_id"):
                    _remove_result(candidate, target)
                if target.get("operation_id"):
                    _remove_step(candidate, step_ids.get((target["operation_id"], target.get("sample_state_id")), target["operation_id"]))
                inactive[record_id] = record
            elif record["kind"] == "measurement":
                if not (record.get("run_id") or record.get("run_ids") or record.get("run_missing_reason")):
                    record["run_missing_reason"] = "unresolved"
                if target.get("operation_id"):
                    _remove_step(candidate, step_ids.get((target["operation_id"], target.get("sample_state_id")), target["operation_id"]))
                result_id, moved = _apply_measurement(candidate, record, base.get(record_id), entry, sources, evidence_refs)
                replacements.update(moved)
                view_ids[("measurement", result_id)] = record_id
            else:
                if target.get("result_id"):
                    _remove_result(candidate, target)
                if target.get("operation_id") and record.get("sample_state_id") != target.get("sample_state_id"):
                    _remove_step(candidate, step_ids.get((target["operation_id"], target["sample_state_id"]), target["operation_id"]))
                operation_id = _apply_preparation(candidate, record, base.get(original_id), entry, step_ids, sources, evidence_refs)
                view_ids[("preparation", operation_id, record["sample_state_id"])] = record_id
        except ValueError as error:
            pending = {**record, "status": "pending", "issue": str(error)}
            inactive[record_id] = pending
            errors.append(str(error))
            # 不再把已改动但归属不明的旧值作为有效结果输出。
            if target.get("result_id"):
                _remove_result(paper, target)
            elif target.get("operation_id"):
                operation_id = step_ids.get((target["operation_id"], target.get("sample_state_id")), target["operation_id"])
                operation = next((op for route in paper["synthesis_routes"] for op in route["operations"] if op["operation_id"] == operation_id), None)
                if operation and operation["applies_to_sample_state_ids"] == [target.get("sample_state_id")]:
                    _remove_step(paper, operation_id)
        else:
            paper = candidate
    unresolved = _detach_removed_references(paper, before_ids - _dependent_ids(paper), replacements)
    if unresolved:
        review["unresolved_dependencies"] = unresolved
    run_methods = {
        run["run_id"]: run.get("method") or run.get("measurement_method") or run.get("technique") or run["run_type"]
        for run in paper["experiment_runs"]
    }
    for observation in paper["observations"]:
        if observation["observation_type"] == "reported_measurement" and observation.get("run_id") in run_methods:
            observation["method"] = run_methods[observation["run_id"]]
    paper["extraction_metadata"].update(
        view="effective", revision_id=revision_id, source_sha256=source_hash, review_version=review.get("version", 0),
    )
    if derived_steps:
        active_step_ids = _reference_ids(paper["synthesis_routes"])
        paper["extraction_metadata"]["derived_preparation_ids"] = {
            derived: original for derived, original in derived_steps.items() if derived in active_step_ids
        }

    export_error = errors[0] if errors else None
    if any(row.get("legacy_mapping_required") for row in inactive.values()):
        export_error = "部分旧人工修改尚未指定目标，请在人工校对中处理后再下载"
    missing_sources = evidence_references(paper) - sources.keys()
    if missing_sources:
        export_error = export_error or f"有 {len(missing_sources)} 处实验出处无法与本次正文唯一核对，当前结果暂不能下载"
    try:
        validate_shape(paper, output_schema())
        validate_references(paper)
        if legacy:
            _check_legacy_evidence(paper, sources, markdown)
    except ValueError as exc:
        export_error = export_error or f"当前结果仍需核对或重新入库：{exc}"

    if missing_sources:
        paper["evidence"] = [item for item in paper["evidence"] if item["evidence_id"] not in missing_sources]
    current_cards, current_evidence = project_experiment_cards(paper, sources, project_observation_values(paper))
    records = {}
    for record in experiment_review_records(current_cards, {}).values():
        key = (
            ("measurement", record.get("result_id")) if record["kind"] == "measurement"
            else ("preparation", record.get("operation_id"), record.get("sample_state_id"))
        )
        record_id = view_ids.get(key, record["record_id"])
        override = items.get(record_id, {}).get("record")
        if override:
            record["status"] = override["status"]
            record["target"] = override.get("target")
            if override.get("observation_context"):
                record["observation_context"] = override["observation_context"]
            if override.get("fragment_id"):
                record["fragment_id"] = override["fragment_id"]
        records[record_id] = {**record, "record_id": record_id}
    active_ids = _dependent_ids(paper)
    for record_id, original in base.items():
        if record_id in covered_base_ids or original["kind"] != "preparation":
            continue
        actual_id = step_ids.get((original.get("operation_id"), original.get("sample_state_id")), original.get("operation_id"))
        if actual_id not in active_ids:
            inactive[record_id] = {
                **original, "status": "pending", "target": review_target(original),
                "issue": "上游步骤已修改或排除，请核对该样品的制备路径",
            }
            if original.get("operation_id"):
                operation = next((op for route in paths_before["synthesis_routes"] for op in route["operations"] if op["operation_id"] == original["operation_id"]), None)
                if operation:
                    review.setdefault("unresolved_dependencies", []).append({
                        "collection": "synthesis_routes", "record": deepcopy(operation), "record_id": record_id,
                        "reason": inactive[record_id]["issue"],
                    })
    records.update(inactive)
    if legacy:
        for route in paper["synthesis_routes"]:
            for operation in route["operations"]:
                if operation["applies_to_sample_state_ids"]:
                    continue
                record_id = _derived_id("legacy-operation", operation["operation_id"])
                records[record_id] = {
                    "record_id": record_id, "kind": "preparation", "status": "pending", "scope": "sample",
                    "canonical_id": operation["operation_id"], "operation_id": operation["operation_id"],
                    "sample": "", "sample_state_id": None, "sample_state_ids": [], "test": "",
                    "metric": operation["operation_type"], "result": "", "conditions": "",
                    "parameters": operation.get("conditions", {}),
                    "evidence_ids": [sources[ref]["review_evidence_id"] for ref in operation["evidence_ids"] if ref in sources],
                    "legacy_operation_mapping": True,
                    "legacy_operation_sample_ids": route["target_sample_state_ids"],
                    "issue": "旧操作没有可唯一确定的适用样品，请选择该制备路线中的样品",
                }
    for record in records.values():
        record.pop("original_result_count", None)
        record["parameters"] = review_parameters(record.get("parameters", {}))
    return ExperimentResult(paper, current_cards, records, review, current_evidence, export_error)


def sample_options(paper: dict | None) -> list[dict]:
    """同名样品用状态、批次及必要的编号后缀区分。"""
    if not paper:
        return []
    batches = {item["sample_batch_id"]: item for item in paper["sample_batches"]}
    counts = Counter(item["reported_name"].casefold() for item in paper["sample_states"])
    options = []
    for state in paper["sample_states"]:
        name = state["reported_name"]
        batch = batches.get(state.get("sample_batch_id"), {})
        label = name
        if counts[name.casefold()] > 1:
            label = " · ".join(filter(None, [name, state.get("state"), batch.get("batch_number") or batch.get("reported_name")]))
        options.append({
            **{key: state.get(key) for key in ("sample_state_id", "sample_batch_id", "reported_name", "state")},
            "label": label,
        })
    labels = Counter(item["label"] for item in options)
    for item in options:
        if labels[item["label"]] > 1:
            item["label"] += f' · {item["sample_state_id"][-8:]}'
    return options


def review_target(record: dict | None) -> dict:
    """固定修改所作用的机器结果或原样品步骤，不接受客户端传入目标路径。"""
    if not record:
        return {}
    if "target" in record:
        return record["target"] or {}
    return {
        key: record[key] for key in ("canonical_id", "operation_id", "result_id", "sample_state_id", "sample_state_ids", "scope")
        if record.get(key)
    }


def _source_index(paper, evidence, markdown):
    """按同版原文核对证据，旧审查编号仅用于保留人工选择。"""
    canonical = {item["evidence_id"]: item for item in paper["evidence"]}
    by_quote = {}
    for item in canonical.values():
        by_quote.setdefault((item["quote_raw"], item["pdf_page"]), []).append(item)

    matches = []
    for item in evidence:
        start, end, quote = item.get("start_char"), item.get("end_char"), item.get("quote")
        if type(start) is not int or type(end) is not int or not (0 <= start < end <= len(markdown)) or markdown[start:end] != quote:
            continue
        candidates = [
            row["evidence_id"] for row in by_quote.get((quote, item.get("page")), [])
            if row.get("start_char", start) == start and row.get("end_char", end) == end
        ]
        ref = item.get("canonical_evidence_id")
        if ref:
            if ref in canonical and ref not in candidates:
                ref = None
        else:
            ref = candidates[0] if len(candidates) == 1 else None
        matches.append((item, ref))

    counts = Counter(ref for _, ref in matches if ref)
    sources, refs = {}, {}
    for item, ref in matches:
        evidence_id = item["evidence_id"]
        if not ref or counts[ref] != 1:
            ref = f"ev:review:{evidence_id}"
        refs[evidence_id] = ref
        sources[ref] = {**item, "review_evidence_id": evidence_id}
    return sources, refs


def _require_target(paper, target, step_ids):
    """已有覆盖必须定位仍存在的同一结果或样品步骤。"""
    if target.get("result_id"):
        observation = next((row for row in paper["observations"] if row["observation_id"] == target.get("canonical_id")), None)
        if observation is None or not any(row["result_id"] == target["result_id"] for row in observation["results"]):
            raise ValueError("被修改的原始结果已不存在，请重新指定本次结果")
    elif target.get("operation_id"):
        sample_id = target.get("sample_state_id")
        if sample_id is None:
            raise ValueError("制备修改缺少原样品作用域")
        op_id = step_ids.get((target["operation_id"], sample_id), target["operation_id"])
        operation = next((op for route in paper["synthesis_routes"] for op in route["operations"] if op["operation_id"] == op_id), None)
        if operation is None or sample_id not in operation["applies_to_sample_state_ids"]:
            raise ValueError("被修改的原样品步骤已不存在，请核对下游依赖")
    elif target:
        raise ValueError("原记录缺少稳定结果或操作身份，须指定目标后再应用")


def _evidence_refs(paper, ids, defaults, sources, refs):
    """人工选定出处在有效结果内物化，仍复用同版原文范围。"""
    if not ids:
        if defaults:
            return defaults
        raise ValueError("请为新增实验数据选择原文出处")
    known = {item["evidence_id"] for item in paper["evidence"]}
    result = []
    for key in dict.fromkeys(ids):
        ref = refs.get(key)
        if ref is None:
            raise ValueError("所选出处不属于本次实验结果")
        item = sources[ref]
        page = item.get("page")
        if ref not in known:
            paper["evidence"].append({
                "evidence_id": ref, "document_id": paper["document"]["document_id"], "provenance_scope": "this_work",
                "pdf_page": page, "printed_page": paper["document"]["pdf_page_to_printed_page"].get(str(page)),
                "section": item.get("section") or "article",
                "location": item.get("location") or f'Markdown lines {item["line_start"]}-{item["line_end"]}',
                "quote_raw": item["quote"], "extraction_origin": item.get("origin") or "text",
                "start_char": item["start_char"], "end_char": item["end_char"],
            })
            known.add(ref)
        result.append(ref)
    return result


def _apply_entity(paper, entry, sources, refs):
    """按固定字段补录样品或实验，不猜测批次、对象或科学方法。"""
    change = entry["change"]
    kind = change["kind"]
    collection, id_field, allowed = _ENTITIES[kind]
    entity_id = change["entity_id"]
    updates = {key: value for key, value in change.items() if key not in {"kind", "entity_id", "evidence_ids", "create"}}
    if change.get("create"):
        if kind == "sample_state":
            if not updates.get("reported_name"):
                raise ValueError("新增样品必须填写名称")
            allowed = allowed | {"preparation_route_id"}
            entity = {
                "sample_state_id": entity_id, "reported_name": updates["reported_name"], "state": "",
                "physical_form": None, "sample_batch_id": None, "preparation_route_id": None, "parent_state_ids": [],
            }
        elif kind == "sample_batch":
            if not updates.get("reported_name"):
                raise ValueError("新增批次必须填写名称")
            entity = {
                "sample_batch_id": entity_id, "material_id": None, "series_id": None,
                "factor_assignments": {}, "batch_number": None,
            }
        elif kind == "experiment_run":
            entity = {
                "run_id": entity_id, "run_type": "reported_experiment", "method": "",
                "sample_state_ids": [], "species_entity_ids": [], "conditions": {},
            }
        else:
            raise ValueError("该登记类型不支持独立新增")
        paper[collection].append(entity)
    else:
        entity = next((item for item in paper[collection] if item[id_field] == entity_id), None)
        if entity is None:
            raise ValueError("要修改的实验登记不属于本次结果")
    if kind == "experiment_run" and entity["run_type"] == "reported_experiment":
        allowed = allowed | {"experiment_type", "sample_state_ids", "species_entity_ids"}
    if updates.keys() - allowed:
        raise ValueError("提交了该实验登记不支持的字段")
    if "reported_name" in updates and not updates["reported_name"]:
        raise ValueError("名称不能为空")
    if "yield_text" in updates:
        text = updates.pop("yield_text")
        updates["yield"] = quantity_from_text(text) if text else None
    if "parameters" in updates:
        parameters = _quantities(updates.pop("parameters"))
        visible = review_parameters(_entity_conditions(entity))
        if parameters != _quantities(visible):
            _apply_conditions(entity, parameters)
    if "method" in updates:
        if "measurement_method" in entity:
            entity["measurement_method"] = updates["method"] or ""
        updates["method"] = updates["method"] or ""
    if "state" in updates and not updates["state"]:
        updates["state"] = ""
    entity.update(updates)
    entity["source"] = _manual_source(entry)
    entity["evidence_ids"] = _evidence_refs(paper, change.get("evidence_ids"), entity.get("evidence_ids"), sources, refs)
    if kind == "experiment_run" and entity["run_type"] == "reported_experiment":
        if not entity.get("experiment_type") or not entity.get("method") or not entity["sample_state_ids"]:
            raise ValueError("补录实验须填写实验类型、方法和样品")
        if not entity["evidence_ids"]:
            raise ValueError("补录实验须选择原文出处")
        if not set(entity["sample_state_ids"]) <= {row["sample_state_id"] for row in paper["sample_states"]}:
            raise ValueError("实验样品不属于本次已登记样品")
        if not set(entity["species_entity_ids"]) <= {row["entity_id"] for row in paper["chemical_entities"]}:
            raise ValueError("实验物种不属于本次化学实体")
    if kind == "sample_state" and entity.get("sample_batch_id") and not any(
        row["sample_batch_id"] == entity["sample_batch_id"] for row in paper["sample_batches"]
    ):
        raise ValueError("所选样品批次不属于本次结果")
    if change.get("create") and entity.get("preparation_route_id"):
        route = next((row for row in paper["synthesis_routes"] if row["route_id"] == entity["preparation_route_id"]), None)
        if route is None:
            raise ValueError("所选制备路线不属于本次结果")
        route["target_sample_state_ids"] = list(dict.fromkeys([*route["target_sample_state_ids"], entity_id]))
    _update_missing_fields(paper, entity_id, updates)
    _validate_member(entity, collection)


def _apply_measurement(paper, record, original, entry, sources, refs):
    """按子结果更新；只有作用域改变时才分离该结果。"""
    target = record.get("target") or {}
    observation = next((item for item in paper["observations"] if item["observation_id"] == target.get("canonical_id")), None)
    member = next((item for item in (observation or {}).get("results", []) if item["result_id"] == target.get("result_id")), None)
    source = _manual_source(entry)
    group = bool(observation and "sample_state_ids" in observation and len(observation["sample_state_ids"]) > 1)
    if group:
        if record.get("scope") != "group" or set(record.get("sample_state_ids", [])) != set(observation["sample_state_ids"]):
            raise ValueError("组观察必须明确修改其完整样品集合，不能从单个样品入口缩小范围")
    else:
        if record.get("scope") == "group" or len(record.get("sample_state_ids", [])) > 1:
            raise ValueError("单样品结果不能隐式改为整组观察")
        if not any(row["sample_state_id"] == record.get("sample_state_id") for row in paper["sample_states"]):
            raise ValueError("测量必须关联本次已登记样品")
        if record.get("sample_state_ids") and set(record["sample_state_ids"]) != {record["sample_state_id"]}:
            raise ValueError("测量的单样品身份与样品集合不一致")
    metric = record["metric"]
    if member and original and metric == original["metric"]:
        metric = member["metric_id"]
    result_id = member["result_id"] if member else _derived_id("result", record["record_id"])
    parameters = _quantities(record.get("parameters", {}))
    condition_changed = parameters != _quantities(review_parameters((original or {}).get("parameters", {})))
    if record.get("conditions", "") != (original or {}).get("conditions", ""):
        if record.get("conditions", "").strip():
            parameters["condition_context"] = {"kind": "text", "raw_text": record["conditions"].strip()}
        condition_changed = True
    subject_changed = any(record.get(key) != (original or {}).get(key) for key in ("sample_state_id", "run_id")) if not group else record.get("run_id") != (original or {}).get("run_id")
    method_changed = not record.get("run_id") and record.get("test", "") != (original or {}).get("test", "")
    metric_changed = original is not None and record["metric"] != original["metric"]
    evidence_changed = record.get("evidence_ids") != (original or {}).get("evidence_ids")
    previous_value = member["value"] if member else {}
    same_scope = member is not None and not (subject_changed or metric_changed or condition_changed)
    if same_scope and original and record["result"] == original["result"]:
        value = deepcopy(previous_value)
    else:
        value = quantity_from_text(record["result"])
        if same_scope and value["kind"] != "text" and previous_value.get("basis") and not value.get("basis") and (
            (value.get("unit_canonical") or value.get("unit_raw")) == (previous_value.get("unit_canonical") or previous_value.get("unit_raw"))
        ):
            value["basis"] = previous_value["basis"]
    check_quantity(value)
    result = {"result_id": result_id, "metric_id": metric, "value": value, "source": source}
    if same_scope and "reported_assignment" in member:
        result["reported_assignment"] = member["reported_assignment"]
    run = next((row for row in paper["experiment_runs"] if row["run_id"] == record.get("run_id")), None)
    if record.get("run_id") and run is None:
        raise ValueError("所选实验不属于本次结果")
    if member and not (subject_changed or condition_changed or evidence_changed or method_changed or metric_changed) and (
        group or observation.get("run_ids") or value["kind"] != "text" or observation["observation_type"] == "reported_measurement"
    ):
        member.update(result)
        _validate_member(observation, "observations")
        return result_id, {}

    previous = deepcopy(observation) if observation else None
    previous_id = observation["observation_id"] if observation else None
    if member:
        _remove_result(paper, target)
    new_refs = _evidence_refs(paper, record.get("evidence_ids"), (previous or {}).get("evidence_ids"), sources, refs)
    preserve_kind = previous and value["kind"] != "text" and not (metric_changed or method_changed) and (
        record.get("run_id") or previous.get("run_ids")
    ) and (not run or run["run_type"] != "reported_experiment")
    if group or preserve_kind or (previous and previous.get("run_ids")):
        updated = previous
        updated["observation_id"] = _derived_id("obs", record["record_id"])
        if group:
            updated["sample_state_ids"] = list(previous["sample_state_ids"])
        elif "sample_state_id" in updated:
            updated["sample_state_id"] = record["sample_state_id"]
        elif "sample_state_ids" in updated:
            updated["sample_state_ids"] = [record["sample_state_id"]]
        if subject_changed and "run_ids" not in updated:
            if not record.get("run_id"):
                raise ValueError("专用观测缺少实验关联，请明确原文未报告或保持待处理")
            updated["run_id"] = record["run_id"]
        if condition_changed:
            _apply_conditions(updated, parameters)
        if metric_changed and "metric_id" in updated:
            updated["metric_id"] = metric
        if method_changed and not record.get("run_id"):
            raise ValueError("组观察的方法必须通过其对应实验修改")
    else:
        missing_reason = None if record.get("run_id") else record.get("run_missing_reason") or "unresolved"
        if missing_reason == "unresolved":
            raise ValueError("实验关联尚未确定，该结果保持待处理")
        updated = {
            "observation_id": _derived_id("obs", record["record_id"]), "observation_type": "reported_measurement",
            "sample_state_id": record["sample_state_id"], "run_id": record.get("run_id"),
            "run_missing_reason": missing_reason, "metric_raw": record["metric"],
            "method": record.get("test") or None, "conditions": parameters,
        }
        context = previous or record.get("observation_context")
        if context:
            original_type = context.get("reported_observation_type") or context.get("observation_type")
            if original_type and original_type != "reported_measurement":
                updated["reported_observation_type"] = original_type
            for key in ("entity_id", "ordered_species_pair"):
                if key in context:
                    updated[key] = context[key]
            if "entity_id" not in updated and member and any(
                entity["entity_id"] == member["metric_id"] for entity in paper["chemical_entities"]
            ):
                updated["entity_id"] = member["metric_id"]
    updated.update(results=[result], source=source, evidence_ids=new_refs)
    if reason := observation_assignment_error(updated, paper):
        raise ValueError(reason)
    _validate_member(updated, "observations")
    paper["observations"].append(updated)
    moved = {previous_id: updated["observation_id"]} if previous_id and (original or {}).get("original_result_count") == 1 and not any(
        row["observation_id"] == previous_id for row in paper["observations"]
    ) else {}
    return result_id, moved


def _split_sample_paths(paper, edited_steps):
    """仅隔离实际修改的共享操作和依赖它的下游步骤。"""
    step_ids = {}
    origins = {}
    state_ids = {row["sample_state_id"] for row in paper["sample_states"]}
    for route in paper["synthesis_routes"]:
        original = deepcopy(route["operations"])
        retained = {op["operation_id"]: deepcopy(op) for op in original}
        replacements = []
        shared_steps = set()
        for sample_id in sorted(edited_steps.keys() & set(route["target_sample_state_ids"])):
            operations = {op["operation_id"]: op for op in original if sample_id in op["applies_to_sample_state_ids"]}
            affected = edited_steps[sample_id] & operations.keys()
            pending = list(affected)
            while pending:
                output = operations[pending.pop()].get("output_intermediate_id")
                if output is None:
                    continue
                downstream = {
                    key for key, op in operations.items()
                    if key not in affected and output in op.get("input_intermediate_ids", [])
                }
                affected.update(downstream)
                pending.extend(downstream)
            shared_ids = {key for key in affected if len(operations[key]["applies_to_sample_state_ids"]) > 1}
            shared_steps.update(shared_ids)
            intermediate = {
                op["output_intermediate_id"]: _derived_id("intermediate", op["output_intermediate_id"], sample_id)
                for op in operations.values() if op["operation_id"] in shared_ids
                and op.get("output_intermediate_id") and op["output_intermediate_id"] not in state_ids
            }
            if intermediate and any(
                sample_id in op["applies_to_sample_state_ids"] and intermediate.keys() & set(op.get("input_intermediate_ids", []))
                for other in paper["synthesis_routes"] if other is not route for op in other["operations"]
            ):
                raise ValueError("该样品的下游步骤跨越制备路线，请先核对中间体归属")
            origins.update({derived: base for base, derived in intermediate.items()})
            for key, op in operations.items():
                if key not in affected:
                    continue
                clone = deepcopy(op)
                if op["operation_id"] in shared_ids:
                    clone["operation_id"] = _derived_id("operation", op["operation_id"], sample_id)
                    origins[clone["operation_id"]] = op["operation_id"]
                    clone["applies_to_sample_state_ids"] = [sample_id]
                    if "output_sample_state_ids" in clone:
                        clone["output_sample_state_ids"] = [ref for ref in clone["output_sample_state_ids"] if ref == sample_id]
                        if not clone["output_sample_state_ids"]:
                            raise ValueError("共享步骤的明确产物无法分离到当前样品，请先核对制备链")
                    if clone.get("output_intermediate_id") in intermediate:
                        clone["output_intermediate_id"] = intermediate[clone["output_intermediate_id"]]
                    for reagent in clone.get("reagent_uses", []):
                        original_id = reagent["reagent_use_id"]
                        reagent["reagent_use_id"] = _derived_id("reagent", original_id, sample_id)
                        origins[reagent["reagent_use_id"]] = original_id
                    replacements.append(clone)
                    kept = retained[op["operation_id"]]
                    kept["applies_to_sample_state_ids"] = [ref for ref in kept["applies_to_sample_state_ids"] if ref != sample_id]
                    if "output_sample_state_ids" in kept:
                        kept["output_sample_state_ids"] = [ref for ref in kept["output_sample_state_ids"] if ref != sample_id]
                else:
                    retained[op["operation_id"]] = clone
                if "input_intermediate_ids" in clone:
                    clone["input_intermediate_ids"] = [intermediate.get(ref, ref) for ref in clone["input_intermediate_ids"]]
                step_ids[(op["operation_id"], sample_id)] = clone["operation_id"]
        route["operations"] = sorted(
            [op for op in retained.values() if op["applies_to_sample_state_ids"] or op["operation_id"] not in shared_steps] + replacements,
            key=lambda op: (op["sequence"], op["operation_id"]),
        )
    return step_ids, origins


def _apply_preparation(paper, record, original, entry, step_ids, sources, refs):
    """用当前样品步骤覆盖制备信息；补录保留实际报告条件。"""
    target = record.get("target") or {}
    sample_id = record["sample_state_id"]
    base_id = target.get("operation_id")
    op_id = step_ids.get((base_id, sample_id), base_id)
    step = next(
        (op for route in paper["synthesis_routes"] for op in route["operations"] if op["operation_id"] == op_id), None,
    )
    if target.get("sample_state_id") != sample_id:
        step = None
    source = _manual_source(entry)
    if record.get("scope") == "group":
        raise ValueError("制备修改只作用于明确的单个样品")
    params = _quantities(record.get("parameters", {}))
    if original is None and record.get("conditions", "").strip():
        params["condition_context"] = {"kind": "text", "raw_text": record["conditions"].strip()}
    if step is not None:
        step["operation_type"] = record["metric"]
        changed = params != _quantities(review_parameters((original or {}).get("parameters", {})))
        if record.get("conditions", "") != (original or {}).get("conditions", ""):
            if record.get("conditions", "").strip():
                params["condition_context"] = {"kind": "text", "raw_text": record["conditions"].strip()}
            changed = True
        if changed:
            step["conditions"] = params
            _reported_operation(step)
        step["evidence_ids"] = _evidence_refs(paper, record.get("evidence_ids"), step["evidence_ids"], sources, refs)
        step["source"] = source
        route = next(row for row in paper["synthesis_routes"] if any(op["operation_id"] == step["operation_id"] for op in row["operations"]))
        _validate_member(route, "synthesis_routes")
        return step["operation_id"]

    state = next((item for item in paper["sample_states"] if item["sample_state_id"] == sample_id), None)
    if state is None:
        raise ValueError("制备步骤必须关联本次已登记样品")
    new_refs = _evidence_refs(paper, record.get("evidence_ids"), state["evidence_ids"], sources, refs)
    route = next((item for item in paper["synthesis_routes"] if item["route_id"] == state.get("preparation_route_id")), None)
    if route is None:
        route = {
            "route_id": _derived_id("route", sample_id), "route_name": "人工补录",
            "target_sample_state_ids": [sample_id], "operations": [], "yield": None, "replicate_count": None,
            "evidence_ids": new_refs, "source": source,
        }
        paper["synthesis_routes"].append(route)
        state["preparation_route_id"] = route["route_id"]
    operation_id = _derived_id("operation", record["record_id"])
    route["operations"].append({
        "operation_id": operation_id, "operation_type": record["metric"],
        "sequence": max((op["sequence"] for op in route["operations"]), default=0) + 1,
        "applies_to_sample_state_ids": [sample_id], "conditions": params, "evidence_ids": new_refs, "source": source,
        "output_sample_state_ids": [sample_id],
    })
    _validate_member(route, "synthesis_routes")
    return operation_id


def _remove_result(paper, target):
    """只移除目标子结果，保留同一观测的独立结果。"""
    for observation in paper["observations"]:
        if observation["observation_id"] == target.get("canonical_id"):
            observation["results"] = [row for row in observation["results"] if row["result_id"] != target["result_id"]]
            if not observation["results"]:
                paper["observations"].remove(observation)
            return


def _remove_step(paper, operation_id):
    """移出指定步骤与失去唯一输入的后续步骤，不重接制备路径。"""
    for route in paper["synthesis_routes"]:
        pending = {operation_id}
        missing_intermediates = set()
        while pending:
            removed = [op for op in route["operations"] if op["operation_id"] in pending]
            if not removed:
                break
            route["operations"] = [op for op in route["operations"] if op["operation_id"] not in pending]
            missing_intermediates.update(op["output_intermediate_id"] for op in removed if op.get("output_intermediate_id"))
            pending = {
                op["operation_id"] for op in route["operations"]
                if missing_intermediates.intersection(op.get("input_intermediate_ids", []))
            }


def _apply_conditions(item, parameters):
    """替换当前条件，避免同一字段在旧顶层和值表中同时活动。"""
    values = dict(parameters)
    for name in ("temperature", "pressure", "pressure_domain", "loading_domain", "condition_context"):
        if name not in item:
            continue
        if name in values:
            value = values.pop(name)
            item[name] = quantity_text(value) if isinstance(item[name], str) else value
        else:
            item.pop(name)
    if "temperatures" in item:
        item["temperatures"] = [values.pop("temperature")] if "temperature" in values else []
    if "probe" in item and "temperature" in item["probe"]:
        if "temperature" in values:
            item["probe"]["temperature"] = values.pop("temperature")
        else:
            item["probe"].pop("temperature")
    item["conditions"] = values


def _quantities(parameters):
    """把人工填写的完整文字转为明确数量或文字，并拒绝字段自相矛盾。"""
    quantities = {}
    for key, value in parameters.items():
        if not isinstance(value, dict):
            raise ValueError(f"条件 {key} 需要原文数量或文字")
        if value.get("kind") in {"scalar", "range", "uncertainty", "text"}:
            quantity = deepcopy(value)
        else:
            quantity = quantity_from_text(value["raw_text"])
            unit = value.get("unit")
            if unit:
                if quantity.get("kind") != "text" and not quantity.get("unit_raw"):
                    quantity.update(unit_raw=unit, unit_canonical=canonical_unit(unit))
                elif unit != quantity.get("unit_raw") and (
                    canonical_unit(unit) is None or canonical_unit(unit) != quantity.get("unit_canonical")
                ):
                    raise ValueError(f"条件 {key} 的单位与原文写法不一致")
            submitted = value.get("value")
            if submitted is not None and submitted not in (quantity.get("value"), quantity["raw_text"]):
                raise ValueError(f"条件 {key} 的数值与原文写法不一致")
        check_quantity(quantity)
        quantities[key] = quantity
    return quantities


def _reported_operation(step):
    """条件改写后使用固定原文操作变体，不保留旧类型的隐藏条件。"""
    allowed = {
        "operation_id", "sequence", "operation_type", "operation_labels", "input_intermediate_ids",
        "input_sample_state_ids", "output_intermediate_id", "output_sample_state_ids", "reagent_uses",
        "equipment", "simultaneous_processes", "conditions", "evidence_ids", "applies_to_sample_state_ids", "source",
    }
    for key in tuple(step):
        if key not in allowed:
            step.pop(key)


def _entity_conditions(entity):
    """读取当前实验可编辑的明确条件，避免空表单清理未显示的科学字段。"""
    parameters = dict(entity.get("conditions", {}))
    for key in ("temperature", "pressure", "pressure_domain", "loading_domain", "condition_context"):
        if key in entity:
            parameters[key] = entity[key]
    if len(entity.get("temperatures", [])) == 1:
        parameters["temperature"] = entity["temperatures"][0]
    if entity.get("probe", {}).get("temperature") is not None:
        parameters["temperature"] = entity["probe"]["temperature"]
    return parameters


def _manual_source(entry):
    """记录操作者与人工版本，人工值不冒充原文核验。"""
    return {
        "kind": "manual", "operator_id": entry["operator_id"], "updated_at": entry["updated_at"],
        "review_version": entry.get("version", 0),
    }


def _validate_member(value, collection):
    """人工变更立即使用当前集合契约校验，不依赖最后一次导出才发现。"""
    schema = output_schema()
    validate_shape(value, {**schema["properties"][collection]["items"], "$defs": schema["$defs"]})


def _derived_id(kind, *parts):
    """同一次原稿中稳定派生编号，后续保存不因排序变化。"""
    return f'{kind}:manual:{sha256(chr(0).join(parts).encode()).hexdigest()[:32]}'


def _dependent_ids(paper):
    """收集可能被人工排除的观测及步骤身份。"""
    return {
        *(row["observation_id"] for row in paper["observations"]),
        *(result["result_id"] for row in paper["observations"] for result in row["results"]),
        *(op["operation_id"] for route in paper["synthesis_routes"] for op in route["operations"]),
        *(op["output_intermediate_id"] for route in paper["synthesis_routes"] for op in route["operations"] if op.get("output_intermediate_id")),
    }


def _detach_removed_references(paper, removed, replacements):
    """失去引用的旧说明转为待核对说明，不导出悬空关系。"""
    unresolved = []
    for collection in ("mechanism_claims", "missing_information", "validation_issues"):
        kept = []
        for row in paper[collection]:
            original = deepcopy(row)
            _replace_references(row, replacements)
            dependencies = _reference_ids(row)
            if not removed.intersection(dependencies):
                kept.append(row)
            else:
                unresolved.append({"collection": collection, "record": original, "reason": "原依据已被修改或排除"})
        if len(kept) != len(paper[collection]):
            paper[collection] = kept
            issue = {
                "issue_id": _derived_id("issue", "review_dependencies", collection),
                "severity": "warning", "issue_type": "review_dependency_removed",
                "message": "人工修改使原记录依据不再适用，请核对原稿中的相关说明。",
            }
            paper["validation_issues"].append(issue)
    return unresolved


def _reference_ids(value):
    """递归收集依赖，嵌套依据不能在排除后继续悬空。"""
    refs = set()
    if isinstance(value, dict):
        for key, member in value.items():
            if key.endswith("_id") and isinstance(member, str):
                refs.add(member)
            elif key.endswith("_ids") and isinstance(member, list):
                refs.update(ref for ref in member if isinstance(ref, str))
            else:
                refs.update(_reference_ids(member))
    elif isinstance(value, list):
        for member in value:
            refs.update(_reference_ids(member))
    return refs


def _replace_references(value, mapping):
    """只在明确一对一替换时更新依赖编号。"""
    if isinstance(value, dict):
        for key, member in value.items():
            if key.endswith("_id") and isinstance(member, str):
                value[key] = mapping.get(member, member)
            elif key.endswith("_ids") and isinstance(member, list):
                value[key] = [mapping.get(ref, ref) for ref in member]
            else:
                _replace_references(member, mapping)
    elif isinstance(value, list):
        for member in value:
            _replace_references(member, mapping)


def _update_missing_fields(paper, entity_id, updates):
    """补齐后清除对应缺失项，明确置空时保留缺失原因。"""
    if not updates:
        return
    for item in paper["missing_information"]:
        if item["record_id"] == entity_id:
            item["fields"] = [field for field in item["fields"] if field not in updates or updates[field] is None]
    paper["missing_information"] = [item for item in paper["missing_information"] if item["fields"]]
    missing = [key for key, value in updates.items() if value is None]
    if missing and not any(item["record_id"] == entity_id and set(missing) <= set(item["fields"]) for item in paper["missing_information"]):
        paper["missing_information"].append({
            "missing_id": _derived_id("missing", entity_id), "record_id": entity_id,
            "fields": missing, "reason": "not_reported",
        })


def _legacy_missing_fields(paper):
    """旧 null 只表示未抽取，不冒充原文已经报告缺失。"""
    for collection, id_field in REGISTRIES.items():
        if collection in {"evidence", "missing_information", "validation_issues"}:
            continue
        for row in paper[collection]:
            fields = [key for key, value in row.items() if value is None]
            if fields:
                paper["missing_information"].append({
                    "missing_id": _derived_id("missing", "legacy", row[id_field]), "record_id": row[id_field],
                    "fields": fields, "reason": "legacy_not_extracted",
                })


def _adapt_legacy_reviews(items, cards, base, paper):
    """只迁移由冻结旧记录和新结果共同唯一识别的覆盖。"""
    original = experiment_review_records(cards, {})
    adapted, candidates = {}, {}
    for key, entry in items.items():
        row = entry["record"]
        if "target" in row or entry.get("target") or row.get("legacy_mapping_required"):
            adapted[key] = entry
            continue
        old = original.get(key)
        if old and not old.get("sample_state_id"):
            sample_ids = _legacy_sample_ids(old, paper)
            if len(sample_ids) == 1:
                old = {**old, "sample_state_id": sample_ids[0]}
        matches = [
            current for current in base.values() if old and current["kind"] == old["kind"]
            and current.get("canonical_id") == old.get("canonical_id") and old.get("canonical_id")
            and current.get("sample_state_id") == old.get("sample_state_id") and old.get("sample_state_id")
            and current["metric"] == old["metric"]
            and current["result"] == old["result"]
            and (not old.get("result_id") or old["result_id"] == current.get("result_id"))
            and (not old.get("operation_id") or old["operation_id"] == current.get("operation_id"))
        ]
        if len(matches) == 1:
            match = matches[0]
            sample_id = row.get("sample_state_id") or old["sample_state_id"]
            if sample_id == match["sample_state_id"] and row["sample"] == old["sample"] and match.get("scope") != "group":
                target_id = match["record_id"]
                candidates[key] = (target_id, {**entry, "record": {
                    **match, **row, "record_id": target_id, "sample_state_id": sample_id,
                    "target": review_target(match), "scope": "sample",
                }})
                continue
        adapted[key] = {**entry, "record": {**row, "status": "pending", "legacy_mapping_required": True, "legacy_record_id": key}}
    counts = Counter(target for target, _entry in candidates.values())
    for key, (target, entry) in candidates.items():
        if counts[target] == 1 and target not in adapted:
            adapted[target] = entry
        else:
            row = items[key]["record"]
            adapted[key] = {**items[key], "record": {**row, "status": "pending", "legacy_mapping_required": True, "legacy_record_id": key}}
    return adapted


def _legacy_sample_ids(old, paper):
    """从冻结原对象的明确作用域找旧卡片状态，名字只作最后的字面核对。"""
    canonical_id = old.get("canonical_id")
    if not canonical_id:
        return []
    if old["kind"] == "measurement":
        owner = next((row for row in paper["observations"] if row["observation_id"] == canonical_id), None)
        ids = (owner.get("sample_state_ids") or [owner.get("sample_state_id")]) if owner else []
    else:
        owner = next((op for route in paper["synthesis_routes"] for op in route["operations"] if op["operation_id"] == canonical_id), None)
        ids = owner["applies_to_sample_state_ids"] if owner else []
    states = {row["sample_state_id"]: row["reported_name"] for row in paper["sample_states"]}
    return [sample_id for sample_id in ids if sample_id in states and states[sample_id] == old["sample"]]


def _check_legacy_evidence(paper, sources, markdown):
    """升级旧结果时按同版原文复查仍活动的机器数量。"""
    states = {item["sample_state_id"]: item["reported_name"] for item in paper["sample_states"]}
    entities = {item["entity_id"]: item for item in paper["chemical_entities"]}
    for observation in paper["observations"]:
        sample_ids = observation.get("sample_state_ids") or [observation.get("sample_state_id")]
        quotes = [sources[ref] for ref in observation["evidence_ids"] if ref in sources]
        for result in observation["results"]:
            if (result.get("source") or {}).get("kind") != "manual":
                metric = observation["observation_type"] if result["metric_id"] in entities else result["metric_id"]
                species_ids = [observation["entity_id"]] if observation.get("entity_id") else []
                if pair := observation.get("ordered_species_pair"):
                    species_ids = [pair["numerator_entity_id"], pair["denominator_entity_id"]]
                elif result["metric_id"] in entities:
                    species_ids = [result["metric_id"]]
                species = tuple(entities[ref].get("formula_ascii") or entities[ref]["reported_name"] for ref in species_ids)
                ground_quantity(
                    metric, result["value"], quotes, [states[ref] for ref in sample_ids if ref in states], markdown,
                    species=species,
                )
