"""将规范论文记录投影为样品卡和可回查原文。"""

from __future__ import annotations

from yuxi.knowledge.structure_tables import CellPosition

type Measurement = tuple[str, str, dict[str, CellPosition], list[str]]


def project_experiment_cards(record: dict, sources: dict, measurement_values: dict) -> tuple[list[dict], list[dict]]:
    """把已校验的观测和原文位置映射到审核工作台。"""
    review_ids = {ref: f"e{index}" for index, ref in enumerate(sources, 1)}
    evidence = [
        {
            "evidence_id": review_ids[ref], "canonical_evidence_id": ref,
            **{key: value for key, value in item.items()
               if key not in {"section", "location", "origin", "figure_text"}},
        }
        for ref, item in sources.items()
    ]
    states = {item["sample_state_id"]: item for item in record["sample_states"]}
    entities = {item["entity_id"]: item["reported_name"] for item in record["chemical_entities"]}
    runs = {item["run_id"]: item for item in record["experiment_runs"]}
    cards = {}

    def field(text, refs):
        """将规范出处编号映射到审核契约。"""
        return {"raw_text": str(text), "evidence_ids": [review_ids[ref] for ref in dict.fromkeys(refs)]}

    def add(sample_id, key, item, refs):
        """用规范样品状态登记审核记录。"""
        state = states[sample_id]
        name = state["reported_name"]
        assignment_refs = [*state["evidence_ids"], *refs]
        card = cards.setdefault(name, {
            "sample": field(name, state["evidence_ids"]), "preparation_steps": [], "measurements": [],
            "warnings": [], "review_status": "draft",
        })
        card[key].append({**item, "assignment_evidence_ids": field(name, assignment_refs)["evidence_ids"]})

    for route in record["synthesis_routes"]:
        for operation in route["operations"]:
            refs = operation["evidence_ids"]
            sample_ids = operation.get("output_sample_state_ids")
            if sample_ids is None:
                output = operation.get("output_intermediate_id")
                sample_ids = [output] if output in states else route["target_sample_state_ids"]
            for sample_id in sample_ids:
                conditions = dict(operation.get("conditions", {}))
                by_sample = conditions.pop("temperature_by_sample", {})
                if sample_temperature := by_sample.get(states[sample_id]["sample_batch_id"]):
                    conditions["temperature"] = sample_temperature
                step = {
                    "canonical_id": operation["operation_id"],
                    "operation": field(operation["operation_type"], refs),
                    "conditions": [
                        {"name": name, "value": field(text, refs)}
                        for name, value in conditions.items()
                        if (text := _display_value(value)) is not None
                    ],
                }
                add(sample_id, "preparation_steps", step, refs)

    for observation in record["observations"]:
        refs = observation["evidence_ids"]
        sample_ids = _sample_ids(observation)
        run = runs.get(observation.get("run_id"), {})
        method = run.get("technique") or run.get("method") or run.get("run_type")
        differences = _observation_conditions(observation, entities)
        conditions = [
            {"name": name, "value": field(text, refs if name in differences else run["evidence_ids"])}
            for name, value in {**run.get("conditions", {}), **differences}.items()
            if (text := _display_value(value)) is not None
        ]
        for label, text, cells, value_refs in measurement_values[observation["observation_id"]]:
            measurement = {
                "canonical_id": observation["observation_id"],
                "property": field(label, value_refs), "result": field(text, value_refs),
                "method": field(method, run["evidence_ids"]) if method else None, "conditions": conditions,
            }
            if cells:
                measurement["table_cells"] = {review_ids[ref]: list(cell) for ref, cell in cells.items()}
            for sample_id in sample_ids:
                add(sample_id, "measurements", measurement, value_refs)
    return list(cards.values()), evidence


def _sample_ids(observation: dict) -> list[str]:
    """读取观测显式关联的样品状态编号。"""
    if "sample_state_id" in observation:
        return [observation["sample_state_id"]] if observation["sample_state_id"] is not None else []
    return observation.get("sample_state_ids", [])


def _observation_conditions(observation: dict, entities: dict) -> dict:
    """保留单条观测显式绑定的物种、条件和取值基准。"""
    conditions = dict(observation.get("conditions", {}))
    for key in ("temperature", "pressure", "loading_domain", "condition_context"):
        if key in observation:
            conditions[key] = observation[key]
    if observation["observation_type"] == "isosteric_heat" and "loading" in observation:
        conditions["loading"] = observation["loading"]
    if entity_id := observation.get("entity_id"):
        conditions["species"] = entities[entity_id]
    if pair := observation.get("ordered_species_pair"):
        conditions["species_pair"] = f'{entities[pair["numerator_entity_id"]]}/{entities[pair["denominator_entity_id"]]}'
    return conditions


def _display_value(value):
    """将有直接文字或数量的值转为审核页面显示文本。"""
    if isinstance(value, dict):
        if "raw_text" in value:
            return value["raw_text"]
        if "minimum" in value and "maximum" in value:
            return f'{value["minimum"]}–{value["maximum"]} {value.get("unit") or ""}'.strip()
        if value.get("value") is not None and not isinstance(value["value"], (dict, list)):
            return f'{value["value"]} {value.get("unit") or ""}'.strip()
        return None
    return value if isinstance(value, (str, int, float)) and not isinstance(value, bool) else None
