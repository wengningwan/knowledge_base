"""从规范吸附记录投影可逐条核对的样品卡和原文。"""

from __future__ import annotations

import re

from yuxi.knowledge.adsorption_contract import (
    Notice,
    filter_record_references,
    observation_result_entries,
    operation_sample_ids,
)
from yuxi.knowledge.adsorption_quantities import UnverifiedQuantity, ground_quantity, quantity_from_text, quantity_text

_METRIC_LABELS = {
    "textural.surface_area.specific": "BET 比表面积",
    "textural.pore_volume.total": "总孔容",
}


def filter_observation_values(record: dict, sources: dict, markdown: str, notices: list[Notice]) -> dict:
    """逐个保留可核对结果，拒绝候选保留完整目标和出处。"""
    filter_machine_quantity_values(record, sources, markdown, notices)
    filter_record_references(record, notices)
    states = {item["sample_state_id"]: item for item in record["sample_states"]}
    entities = {item["entity_id"]: item for item in record["chemical_entities"]}
    kept = []
    for observation in record["observations"]:
        sample_ids = _sample_ids(observation)
        if any(sample_id not in states for sample_id in sample_ids):
            notices.append(("unverified_observation", "观测样品的数量出处尚未成立。",
                            {"collection": "observations", "record": observation}))
            continue
        evidence = [sources[ref] for ref in observation["evidence_ids"]]
        names = list(dict.fromkeys(states[sample_id]["reported_name"] for sample_id in sample_ids))
        other_names = tuple({
            state["reported_name"] for sample_id, state in states.items()
            if sample_id not in sample_ids and state["reported_name"] not in names
        })
        if _ambiguous_sample(observation, states, evidence):
            notices.append((
                "unverified_observation", "同名样品状态无法由所引原文区分。",
                {"collection": "observations", "record": observation},
            ))
            continue
        accepted = []
        for result in observation["results"]:
            result_id, metric, value = result["result_id"], result["metric_id"], result["value"]
            species = _observed_species(observation, metric, entities)
            other_species = tuple({
                item.get("formula_ascii") or item["reported_name"] for item in entities.values()
                if (item.get("formula_ascii") or item["reported_name"]) not in species
            }) if species else ()
            metric_name = observation["observation_type"] if metric in entities else metric
            try:
                ground_quantity(
                    metric_name, value, evidence, names, markdown, species=species,
                    other_samples=other_names, other_species=other_species,
                )
            except UnverifiedQuantity as error:
                notices.append((
                    "unverified_observation", f"结果 {result_id} 未采用。{error}",
                    {"collection": "observations", "record": {**observation, "results": [result]}},
                ))
            else:
                accepted.append(result)
        if accepted:
            kept.append({**observation, "results": accepted})
        elif not observation["results"]:
            notices.append((
                "empty_observation_value", "观测没有可用结果。",
                {"collection": "observations", "record": observation},
            ))
    record["observations"] = kept
    return project_observation_values(record)


def filter_machine_quantity_values(record: dict, sources: dict, markdown: str, notices: list[Notice]) -> None:
    """机器条件和实体数量逐个回查自身出处，人工来源保留人工责任边界。"""
    states = {item["sample_state_id"]: item for item in record["sample_states"]}
    for collection in (
        "chemical_entities", "sample_batches", "sample_states", "synthesis_routes", "experiment_runs", "observations",
    ):
        accepted = []
        for item in record[collection]:
            if collection == "synthesis_routes":
                operations = []
                for operation in item["operations"]:
                    try:
                        _ground_owned_quantities(operation, sources, markdown, states)
                    except UnverifiedQuantity as error:
                        notices.append(("unverified_quantity", f"制备操作数量未采用。{error}",
                                        {"collection": collection, "record": {**item, "operations": [operation]}}))
                    else:
                        operations.append(operation)
                item["operations"] = operations
            try:
                _ground_owned_quantities(item, sources, markdown, states)
            except UnverifiedQuantity as error:
                notices.append(("unverified_quantity", f"实体或条件数量未采用。{error}",
                                {"collection": collection, "record": item}))
            else:
                accepted.append(item)
        record[collection] = accepted


def project_observation_values(record: dict) -> dict:
    """从当前活动结果投影显示值，保留每个结果自己的身份。"""
    entities = {item["entity_id"]: item["reported_name"] for item in record["chemical_entities"]}
    return {
        observation["observation_id"]: [
            {"result_id": result_id, "metric": _METRIC_LABELS.get(metric, entities.get(metric, metric)),
             "text": quantity_text(value)}
            for result_id, metric, value in observation_result_entries(observation)
        ]
        for observation in record["observations"]
    }


def project_experiment_cards(record: dict, sources: dict, measurement_values: dict) -> tuple[list[dict], list[dict]]:
    """按样品状态编号投影当前制备、实验、结果和出处。"""
    review_ids = {ref: item.get("review_evidence_id", f"e{index}") for index, (ref, item) in enumerate(sources.items(), 1)}
    evidence = [
        {
            **{key: value for key, value in item.items() if key != "review_evidence_id"},
            "evidence_id": review_ids[ref], "canonical_evidence_id": ref,
        }
        for ref, item in sources.items()
    ]
    states = {item["sample_state_id"]: item for item in record["sample_states"]}
    batches = {item["sample_batch_id"]: item for item in record["sample_batches"]}
    entities = {item["entity_id"]: item["reported_name"] for item in record["chemical_entities"]}
    runs = {item["run_id"]: item for item in record["experiment_runs"]}

    def field(text, refs):
        """将规范出处编号映射到审核契约。"""
        return {"raw_text": str(text), "evidence_ids": [review_ids[ref] for ref in dict.fromkeys(refs) if ref in review_ids]}

    cards = {
        sample_id: {
            "sample_state_id": sample_id, "sample": field(state["reported_name"], state["evidence_ids"]),
            "state": state.get("state"), "physical_form": state.get("physical_form"),
            "sample_batch_id": state.get("sample_batch_id"),
            "batch_number": batches.get(state.get("sample_batch_id"), {}).get("batch_number"),
            "preparation_route_id": state.get("preparation_route_id"),
            "preparation_steps": [], "measurements": [], "warnings": [], "review_status": "draft",
        }
        for sample_id, state in states.items()
    }

    def add(sample_id, key, item, refs):
        """登记当前卡片的样品视角及整条记录的出处。"""
        assignment_refs = [*states[sample_id]["evidence_ids"], *refs]
        cards[sample_id][key].append({
            **item, "sample_state_id": sample_id, "assignment_evidence_ids": field("", assignment_refs)["evidence_ids"],
        })

    for route in record["synthesis_routes"]:
        for operation in route["operations"]:
            refs = operation["evidence_ids"]
            sample_ids = operation_sample_ids(operation, record, route)
            for sample_id in sample_ids:
                conditions = dict(operation.get("conditions", {}))
                by_sample = conditions.pop("temperature_by_sample", {})
                if sample_temperature := by_sample.get(states[sample_id]["sample_batch_id"]):
                    conditions["temperature"] = sample_temperature
                add(sample_id, "preparation_steps", {
                    "canonical_id": operation["operation_id"], "operation_id": operation["operation_id"],
                    "sample_state_ids": sample_ids, "applies_to_sample_state_ids": sample_ids,
                    "scope": "group" if len(sample_ids) > 1 else "sample",
                    "route_id": route["route_id"], "operation": field(operation["operation_type"], refs),
                    "source": operation.get("source"),
                    "parameters": conditions,
                    "conditions": [
                        {"name": name, "value": field(text, refs)}
                        for name, value in conditions.items() if (text := quantity_text(value)) is not None
                    ],
                }, refs)

    for observation in record["observations"]:
        refs = observation["evidence_ids"]
        sample_ids = _sample_ids(observation)
        run_id = observation.get("run_id")
        run = runs.get(run_id, {})
        method = observation.get("method") or run.get("method") or run.get("measurement_method") or run.get("technique") or run.get("run_type")
        parameters = _observation_conditions(observation)
        conditions = dict(parameters)
        if entity_id := observation.get("entity_id"):
            conditions["species"] = entities[entity_id]
        if pair := observation.get("ordered_species_pair"):
            conditions["species_pair"] = f'{entities[pair["numerator_entity_id"]]}/{entities[pair["denominator_entity_id"]]}'
        results = {item["result_id"]: item for item in observation["results"]}
        for item in measurement_values.get(observation["observation_id"], []):
            result_id = item["result_id"]
            measurement = {
                "canonical_id": observation["observation_id"], "result_id": result_id,
                "sample_state_ids": sample_ids, "scope": "group" if len(sample_ids) > 1 else "sample",
                "run_id": run_id, "run_ids": observation.get("run_ids") or ([run_id] if run_id else []),
                "run_missing_reason": observation.get("run_missing_reason"),
                "property": field(item["metric"], refs), "result": field(item["text"], refs),
                "method": field(method, run.get("evidence_ids", refs)) if method else None,
                "source": results[result_id].get("source") or observation.get("source"),
                "parameters": parameters,
                "conditions": [
                    {"name": name, "value": field(text, refs)}
                    for name, value in conditions.items() if (text := quantity_text(value)) is not None
                ],
            }
            for sample_id in sample_ids:
                add(sample_id, "measurements", measurement, refs)
    return list(cards.values()), evidence


def _sample_ids(observation: dict) -> list[str]:
    """读取观测明确登记的单个样品或样品集合。"""
    return [observation["sample_state_id"]] if "sample_state_id" in observation else observation.get("sample_state_ids", [])


def _ground_owned_quantities(owner: dict, sources: dict, markdown: str, states: dict) -> None:
    """沿最近的出处及样品边界核对数量，不借相邻对象的值。"""
    aliases = {
        "temperatures": "temperature", "input_temperatures": "temperature", "temperature_by_sample": "temperature",
        "pressure_max_explicit_in_results": "pressure", "pressure_domain": "pressure", "purity_value": "purity",
    }

    def verify(metric, quantity, refs, samples):
        """共同方法未逐个点名样品时，仍拒绝引用其他样品的专属条件。"""
        evidence = [sources[ref] for ref in refs if ref in sources]
        named = samples if any(
            re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", source["quote"], re.I)
            for name in samples for source in evidence
        ) else []
        other_names = tuple({state["reported_name"] for state in states.values()} - set(samples))
        ground_quantity(metric, quantity, evidence, named, markdown, parameter=True, other_samples=other_names)

    def visit(value, metric, refs, samples, manual=False):
        """递归保留数量Owner，结果由专用结果核对处理。"""
        if isinstance(value, dict):
            manual = manual or value.get("source", {}).get("kind") == "manual"
            if manual:
                return
            refs = value.get("evidence_ids", refs)
            ids = _sample_ids(value) or value.get("applies_to_sample_state_ids") or []
            samples = list(dict.fromkeys(states[ref]["reported_name"] for ref in ids if ref in states)) or samples
            if value.get("kind") in {"scalar", "range", "uncertainty", "text"}:
                verify(metric, value, refs, samples)
                return
            if metric == "relative_pressure_interval" and {"minimum", "maximum"} <= value.keys():
                left = "[" if value["minimum_inclusive"] else "("
                right = "]" if value["maximum_inclusive"] else ")"
                raw = (
                    f'{value["minimum"]}–{value["maximum"]}' if value["minimum_inclusive"] and value["maximum_inclusive"]
                    else f'{left}{value["minimum"]}, {value["maximum"]}{right}'
                )
                verify(metric, quantity_from_text(raw), refs, samples)
                return
            for key, child in value.items():
                if key in {"results", "operations", "source", "sequence", "evidence_ids"}:
                    continue
                field = aliases.get(key, key)
                if key in {"pressure_basis", "mass_basis", "loading_definition"} and isinstance(child, str) and child.strip():
                    if child.casefold() not in {"not_reported", "unresolved", "unknown", "not reported"} and not any(
                        "".join(child.casefold().split()) in "".join(sources[ref]["quote"].casefold().split())
                        for ref in refs if ref in sources
                    ):
                        raise UnverifiedQuantity(f"{key} 基准未能在自身出处中定位。")
                if metric == "temperature" and key in {state.get("sample_batch_id") for state in states.values()}:
                    field = "temperature"
                if type(child) in {int, float}:
                    unit = value.get(f'{key.removesuffix("_value")}_unit') or value.get("unit")
                    quantity = quantity_from_text(f'{child} {unit or ""}')
                    verify(field, quantity, refs, samples)
                else:
                    visit(child, field, refs, samples)
        elif isinstance(value, list):
            for member in value:
                visit(member, metric, refs, samples, manual)

    visit(owner, "", owner.get("evidence_ids", []), [])


def _observation_conditions(observation: dict) -> dict:
    """保留观测明确绑定的条件；缺失条件不从相邻实验补齐。"""
    conditions = dict(observation.get("conditions", {}))
    for key in ("temperature", "pressure", "loading_domain", "condition_context"):
        if key in observation:
            conditions[key] = observation[key]
    return conditions


def _observed_species(observation: dict, metric: str, entities: dict) -> tuple[str, ...]:
    """按观测声明读取需在结果出处核对的气体或有序气体对。"""
    if pair := observation.get("ordered_species_pair"):
        return tuple(entities[pair[field]].get("formula_ascii") or entities[pair[field]]["reported_name"] for field in (
            "numerator_entity_id", "denominator_entity_id",
        ))
    entity_id = observation.get("entity_id") or (metric if metric in entities else None)
    if entity_id:
        entity = entities[entity_id]
        return (entity.get("formula_ascii") or entity["reported_name"],)
    return ()


def _ambiguous_sample(observation: dict, states: dict, evidence: list[dict]) -> bool:
    """同名状态需要独立的出处或明确写出的状态说明。"""
    selected = set(_sample_ids(observation))
    refs = set(observation["evidence_ids"])
    for sample_id in selected:
        state = states[sample_id]
        alternatives = [
            item for key, item in states.items() if key not in selected
            and item["reported_name"].casefold() == state["reported_name"].casefold()
        ]
        if not alternatives:
            continue
        other_refs = {ref for item in alternatives for ref in item["evidence_ids"]}
        if refs.intersection(state["evidence_ids"]) - other_refs:
            continue
        descriptor = state.get("state")
        if descriptor and all(descriptor != item.get("state") for item in alternatives) and any(
            re.search(rf"(?<!\w){re.escape(descriptor)}(?!\w)", source["quote"], re.I) for source in evidence
        ):
            continue
        return True
    return False
