"""吸附分离 JSON 的字段契约和同文档引用约束。"""

from __future__ import annotations

import json
from collections import defaultdict
from copy import deepcopy
from functools import cache
from hashlib import sha256
from importlib.resources import files

from jsonschema import Draft202012Validator

from yuxi.knowledge.adsorption_quantities import (
    UnverifiedQuantity,
    canonical_unit,
    check_quantity,
    quantity_from_text,
    quantity_text,
)
from yuxi.knowledge.adsorption_relations import observation_assignment_error, run_derivation_error

STRUCTURE_VERSION = 3
STRUCTURE_PROMPT_VERSION = "adsorption-canonical-v7"
REGISTRIES = {
    "evidence": "evidence_id",
    "chemical_entities": "entity_id",
    "material_identities": "material_id",
    "sample_batches": "sample_batch_id",
    "sample_states": "sample_state_id",
    "synthesis_routes": "route_id",
    "experiment_runs": "run_id",
    "series_sets": "series_set_id",
    "observations": "observation_id",
    "mechanism_claims": "claim_id",
    "missing_information": "missing_id",
    "validation_issues": "issue_id",
}
_BATCH_MAPS = {"temperature_by_sample", "per_sample_values"}
_CHEMICAL_MAPS = {"kinetic_diameter_angstrom", "polarizability_raw"}
_DYNAMIC_MAPS = _BATCH_MAPS | _CHEMICAL_MAPS | {"pdf_page_to_printed_page", "factor_assignments"}
_REFERENCE_ALIASES = {
    "carbon_source_entity_id": "entity_id", "numerator_entity_id": "entity_id",
    "denominator_entity_id": "entity_id", "preferential_component_entity_id": "entity_id",
    "parent_state_id": "sample_state_id", "target_sample_state_id": "sample_state_id",
    "input_sample_state_id": "sample_state_id", "output_sample_state_id": "sample_state_id",
    "preparation_route_id": "route_id", "input_run_id": "run_id", "source_run_id": "run_id",
    "source_model_run_id": "run_id", "supported_by_observation_id": "observation_id",
    "input_intermediate_id": "intermediate_id", "output_intermediate_id": "intermediate_id",
    "species_entity_id": "entity_id",
    "applies_to_sample_state_id": "sample_state_id",
    "sample_specific_assignment": "sample_state_id",
}
_TYPED_REFERENCES = set(REGISTRIES.values()) | {"document_id", "intermediate_id", "result_id"}
_INDEPENDENT_MEMBERS = {"observations": "results", "synthesis_routes": "operations"}
type Notice = tuple[str, str] | tuple[str, str, dict]
type RecordPosition = tuple[str, int]


@cache
def output_schema() -> dict:
    """读取随后端包发布的规范 JSON Schema。"""
    return json.loads(files(__package__).joinpath("adsorption_schema.json").read_text(encoding="utf-8"))


def ensure_result_ids(record: dict, namespace: str = "") -> None:
    """在首次冻结时固定结果身份；后续编辑保留结果自身编号。"""
    normalize_observation_results(record, namespace)


def normalize_observation_results(record: dict, namespace: str = "") -> None:
    """把旧稿的固定结果字段转成新版独立结果，不依据活动下标重定位。"""
    result_fields = (
        "value", "loading", "selectivity", "result", "reported_observation", "reported_order", "qualitative_result",
        "signed_value_from_figure", "magnitude_reported_in_prose", "interaction_distance_from_figure",
        "quantitative_particle_size", "numeric_mass_loss", "relative_component_fractions",
    )
    for observation in record.get("observations", []):
        previous = observation.get("results")
        normalized = []
        if isinstance(previous, list):
            for member in previous:
                if isinstance(member, dict) and "metric_id" in member and isinstance(member.get("value"), dict):
                    normalized.append(member)
                    continue
                if isinstance(member, dict):
                    metric = member.get("metric_id") or member.get("entity_id") or member.get("peak_name") or member.get("assignment")
                    if "binding_energy_eV" in member:
                        text = f'{member["binding_energy_eV"]} ± {member["uncertainty_eV"]} eV'
                        value = quantity_from_text(text)
                    else:
                        value = member.get("value", member.get("position", ""))
                else:
                    metric, value = observation["observation_type"], member
                item = {"metric_id": metric or observation["observation_type"], "value": value}
                if isinstance(member, dict) and "reported_assignment" in member:
                    item["reported_assignment"] = member["reported_assignment"]
                normalized.append(item)
        elif isinstance(previous, dict):
            for metric, value in previous.items():
                if isinstance(value, list):
                    normalized.extend({"metric_id": metric, "value": member} for member in value)
                else:
                    if metric.endswith("_percent") and type(value) in {int, float}:
                        value = {"value": value, "unit": "%"}
                    normalized.append({"metric_id": metric, "value": value})
        for key in result_fields:
            if key in observation:
                value = observation.pop(key)
                if key == "loading" and observation["observation_type"] == "isosteric_heat":
                    observation.setdefault("conditions", {})[key] = value
                    continue
                if value is not None:
                    metric = observation.get("metric_id") or (observation["observation_type"] if key in {
                        "value", "loading", "selectivity", "result",
                    } else key)
                    normalized.append({"metric_id": metric, "value": value})
        normalized.extend(
            {"metric_id": "qualitative_results", "value": value}
            for value in observation.pop("qualitative_results", [])
        )
        for index, member in enumerate(normalized):
            if not member.get("result_id"):
                identity = f'{namespace}:{observation["observation_id"]}:{index}'
                member["result_id"] = f'result:{sha256(identity.encode()).hexdigest()[:32]}'
            value = member["value"]
            if not isinstance(value, dict) or value.get("kind") not in {"scalar", "range", "uncertainty", "text"}:
                member["value"] = _legacy_quantity(value)
            elif value["kind"] == "range":
                value.setdefault("minimum_inclusive", True)
                value.setdefault("maximum_inclusive", True)
        observation["results"] = normalized


def normalize_legacy_quantities(record: dict) -> None:
    """把旧稿的固定数量形状转换为新版，保留不明原文供同版证据复核。"""
    shapes = (
        {"raw_text", "value", "unit"}, {"value", "unit"}, {"value", "unit", "pressure_type"},
        {"minimum", "maximum", "unit"}, {"value", "unit", "unit_status"},
    )

    def normalize(value):
        """只转换旧契约拥有的数量，不推断其他对象的单位。"""
        if isinstance(value, dict):
            if value.keys() in shapes or value.get("kind") in {"scalar", "range"}:
                return _legacy_quantity(value)
            for key, member in value.items():
                value[key] = normalize(member)
        elif isinstance(value, list):
            for index, member in enumerate(value):
                value[index] = normalize(member)
        return value

    normalize(record)


def observation_result_entries(observation: dict) -> list[tuple[str, str, dict]]:
    """按结果自身编号读取规范结果，不从活动列表位置推测身份。"""
    return [(item["result_id"], item["metric_id"], item["value"]) for item in observation.get("results", [])]


def operation_sample_ids(operation: dict, record: dict | None = None, route: dict | None = None) -> list[str]:
    """沿明确的样品产物与中间体下游链确定操作适用样品。"""
    explicit = operation.get("applies_to_sample_state_ids")
    if explicit is not None:
        return list(dict.fromkeys(explicit))
    states = {item["sample_state_id"] for item in (record or {}).get("sample_states", [])}
    operations = route.get("operations", []) if route else [
        item for parent in (record or {}).get("synthesis_routes", []) for item in parent["operations"]
    ]
    outputs = set(operation.get("output_sample_state_ids", []))
    pending = [operation.get("output_intermediate_id")]
    visited = set()
    while pending:
        output = pending.pop()
        if output is None or output in visited:
            continue
        visited.add(output)
        if output in states:
            outputs.add(output)
        for downstream in operations:
            if output in downstream.get("input_intermediate_ids", []):
                outputs.update(downstream.get("applies_to_sample_state_ids") or downstream.get("output_sample_state_ids", []))
                pending.append(downstream.get("output_intermediate_id"))
    return sorted(outputs)


def stage_schema(fields: tuple[str, ...], observation_types: tuple[str, ...] = ()) -> dict:
    """从同一契约截取当前提取阶段及其引用的定义。"""
    schema = output_schema()
    properties = {name: deepcopy(schema["properties"][name]) for name in fields}
    if "observations" in properties:
        variants = properties["observations"]["items"]["anyOf"]
        selected = []
        for variant in variants:
            definition = schema["$defs"][variant["$ref"].rsplit("/", 1)[-1]]
            kind = definition["properties"]["observation_type"]["const"]
            if kind != "reported_measurement" and (not observation_types or kind in observation_types):
                selected.append(variant)
        properties["observations"] = {"type": "array", "items": {"anyOf": selected}}
    if "experiment_runs" in properties:
        properties["experiment_runs"]["items"]["anyOf"] = [
            variant for variant in properties["experiment_runs"]["items"]["anyOf"]
            if variant["$ref"] != "#/$defs/experiment_runs_reported_experiment"
        ]

    definitions = {}

    def include(value):
        """递归收集所选字段引用的定义。"""
        if isinstance(value, dict):
            if "$ref" in value:
                name = value["$ref"].rsplit("/", 1)[-1]
                if name not in definitions:
                    definitions[name] = deepcopy(schema["$defs"][name])
                    include(definitions[name])
            if value.get("properties", {}).get("source", {}).get("$ref") == "#/$defs/source_attribution":
                del value["properties"]["source"]
                if "source" in value.get("required", []):
                    value["required"].remove("source")
            for child in value.values():
                include(child)
        elif isinstance(value, list):
            for child in value:
                include(child)

    include(properties)
    return {
        "type": "object", "properties": properties, "required": list(properties),
        "additionalProperties": False, "$defs": definitions,
    }


def validate_shape(value: dict, schema: dict) -> None:
    """拒绝缺失字段、额外字段或错误类型，诊断只包含字段路径和规则。"""
    validator = Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)
    error = next(validator.iter_errors(value), None)
    if error is not None:
        raise ValueError(f"规范 JSON 校验失败（{error.json_path}，{error.validator}）")


def filter_stage_records(value: dict, schema: dict, stage: str, source_ids: set[str]) -> tuple[dict, list[Notice]]:
    """保留合格候选，题录错误仍由调用方重试。"""
    if not isinstance(value, dict):
        raise ValueError("模型响应须为 JSON 对象")
    properties = schema["properties"]
    notices: list[Notice] = []
    if value.keys() - properties.keys():
        notices.append(("invalid_stage_field", f"{stage} 阶段包含模板以外的字段，已忽略。"))
    result = {}
    for name, shape in properties.items():
        candidate = value.get(name)
        if shape.get("type") != "array":
            validate_shape(candidate, {**shape, "$defs": schema["$defs"]})
            result[name] = candidate
            continue
        result[name] = []
        if not isinstance(candidate, list):
            notices.append(("invalid_collection", f"{stage} 阶段的 {name} 缺失或类型错误。"))
            continue
        validator = Draft202012Validator(
            {**shape["items"], "$defs": schema["$defs"]}, format_checker=Draft202012Validator.FORMAT_CHECKER,
        )
        child_field = _INDEPENDENT_MEMBERS.get(name)
        for index, item in enumerate(candidate, 1):
            location = f"{stage} 的 {name} 第 {index} 条"
            if child_field and isinstance(item, dict) and isinstance(item.get(child_field), list):
                if validator.is_valid({**item, child_field: []}):
                    kept = []
                    for member_index, member in enumerate(item[child_field], 1):
                        if validator.is_valid({**item, child_field: [member]}) and not (
                            evidence_references(member) - source_ids
                        ):
                            kept.append(member)
                        else:
                            notices.append((
                                "invalid_member", f"{location}的 {child_field} 第 {member_index} 项格式或出处错误。",
                                {"collection": name, "record": {**item, child_field: [member]}},
                            ))
                    item = {**item, child_field: kept}
            if not validator.is_valid(item):
                notices.append(("invalid_record_shape", f"{location}缺少字段、含多余字段或类型错误。",
                                {"collection": name, "record": item}))
            elif evidence_references(item) - source_ids:
                notices.append(("invalid_evidence_reference", f"{location}引用了无法定位的原文块。",
                                {"collection": name, "record": item}))
            elif name == "observations" and not item["observation_id"].startswith(f"obs:{stage}:"):
                notices.append(("invalid_observation_id", f"{location}的编号不属于当前观测阶段。",
                                {"collection": name, "record": item}))
            else:
                result[name].append(item)
    return result, notices


def filter_record_references(record: dict, notices: list[Notice]) -> None:
    """逐轮剔除歧义编号及其依赖，依据曲线记录重建实验曲线反向引用。"""
    while True:
        notice_count = len(notices)
        problems = _reference_problems(record, notices=notices)
        if not problems and len(notices) == notice_count:
            break
        for (collection, index), reason in problems.items():
            notices.append(("invalid_record_reference", f"{collection} 第 {index + 1} 条已剔除。{reason}",
                            {"collection": collection, "record": record[collection][index]}))
        for collection in REGISTRIES:
            record[collection] = [
                item for index, item in enumerate(record[collection]) if (collection, index) not in problems
            ]
    for route in record["synthesis_routes"]:
        for operation in route["operations"]:
            operation["applies_to_sample_state_ids"] = operation_sample_ids(operation, record, route)
    series_by_run = _series_by_run(record)
    for index, run in enumerate(record["experiment_runs"], 1):
        if "series_set_ids" not in run:
            continue
        expected = series_by_run.get(run["run_id"], [])
        if run["series_set_ids"] != expected:
            if set(run["series_set_ids"]) != set(expected) or len(run["series_set_ids"]) != len(expected):
                notices.append(("inconsistent_series_reference", f"第 {index} 条实验的曲线编号已按有效曲线整理。"))
            run["series_set_ids"] = expected


def validate_references(record: dict) -> None:
    """拒绝重复编号、悬空引用、类别错配和不一致的实验曲线关联。"""
    if problems := _reference_problems(record):
        raise ValueError(next(iter(problems.values())))
    series_by_run = _series_by_run(record)
    for run in record["experiment_runs"]:
        if set(run.get("series_set_ids", [])) != set(series_by_run.get(run["run_id"], [])):
            raise ValueError("规范 JSON 中实验和曲线的双向编号不一致")


def evidence_references(value: object) -> set[str]:
    """收集需从本次解析正文物化的出处编号。"""
    return {ref for key, values in _fields(value) if key == "evidence_ids" for ref in values}


def _reference_problems(record: dict, *, notices: list[Notice] | None = None) -> dict[RecordPosition, str]:
    """找出编号声明或引用有问题的记录位置。"""
    ids = defaultdict(set)
    owners = defaultdict(list)
    problems = {}
    duplicate_steps = defaultdict(set)

    def register(field, value, position=None):
        """登记编号类别，并保留重复声明的位置。"""
        ids[field].add(value)
        owners[value].append(position)

    register("record_id", record["record_id"])
    register("document_id", record["document"]["document_id"])
    for collection, field in REGISTRIES.items():
        for index, item in enumerate(record[collection]):
            position = (collection, index)
            register(field, item[field], None if collection == "evidence" else position)
            if collection == "synthesis_routes":
                for step_index, operation in enumerate(item["operations"], 1):
                    step_position = (*position, step_index)
                    register("operation_id", operation["operation_id"], step_position)
                    if intermediate := operation.get("output_intermediate_id"):
                        if intermediate in ids["sample_state_id"]:
                            # 同一物料已由样品登记拥有身份，制备产物只引用该状态。
                            ids["intermediate_id"].add(intermediate)
                        else:
                            register("intermediate_id", intermediate, step_position)
                    for reagent in operation.get("reagent_uses", []):
                        register("reagent_use_id", reagent["reagent_use_id"], step_position)
            elif collection == "observations":
                for result_index, result in enumerate(item.get("results", []), 1):
                    register("result_id", result["result_id"], (*position, result_index))
    for value, positions in owners.items():
        if len(positions) > 1:
            if positions.count(None) > 1:
                raise ValueError("规范 JSON 的文档或原文编号重复")
            for position in positions:
                if position is None:
                    continue
                if notices is not None and len(position) == 3:
                    duplicate_steps[position[:2]].add(position[2])
                else:
                    problems[position[:2]] = f"记录编号重复（{value}），无法确定归属。"
    formulas = {item.get("formula_ascii") for item in record["chemical_entities"]} - {None}
    series_runs = {run["run_id"] for run in record["experiment_runs"] if "series_set_ids" in run}
    for collection in REGISTRIES:
        for index, item in enumerate(record[collection]):
            position = (collection, index)
            if position in problems:
                continue
            if collection == "series_sets" and item["run_id"] not in series_runs:
                problems[position] = "对应实验没有可登记该曲线的字段。"
                continue
            if collection == "experiment_runs" and (reason := run_derivation_error(item, record)):
                problems[position] = reason
                continue
            if collection == "observations" and (reason := observation_assignment_error(item, record)):
                problems[position] = reason
                continue
            if collection == "observations" and not item["results"]:
                problems[position] = "观测没有可用的独立结果。"
                continue
            child_field = _INDEPENDENT_MEMBERS.get(collection)
            if notices is not None and child_field and isinstance(item.get(child_field), list):
                kept = []
                for member_index, member in enumerate(item[child_field], 1):
                    reason = (
                        "独立记录编号重复，无法确定归属。" if member_index in duplicate_steps.get(position, ())
                        else _reference_error(member, ids, owners, formulas) or _quantity_error(member)
                    )
                    if not reason and collection == "synthesis_routes":
                        reason = _operation_assignment_error(member, record, item)
                    if reason:
                        notices.append((
                            "invalid_member_reference",
                            f"{collection} 第 {index + 1} 条的 {child_field} 第 {member_index} 项已剔除。{reason}",
                            {"collection": collection, "record": {**item, child_field: [member]}},
                        ))
                    else:
                        kept.append(member)
                item[child_field] = kept
            elif collection == "synthesis_routes":
                for operation in item["operations"]:
                    if reason := _operation_assignment_error(operation, record, item):
                        problems[position] = reason
                        break
            ignore_series = notices is not None and collection == "experiment_runs"
            if reason := _reference_error(item, ids, owners, formulas, ignore_series=ignore_series) or _quantity_error(item):
                problems[position] = reason
    return problems


def _operation_assignment_error(operation: dict, record: dict, route: dict) -> str | None:
    """步骤的适用对象必须属于路线，产物必须属于步骤适用对象。"""
    sample_ids = set(operation_sample_ids(operation, record, route))
    if not sample_ids:
        return "操作没有明确的样品适用范围或通向样品的产物链。"
    if not sample_ids <= set(route["target_sample_state_ids"]):
        return "操作适用样品不属于对应制备路线。"
    if not set(operation.get("output_sample_state_ids", [])) <= sample_ids:
        return "操作产物与适用样品集合不一致。"
    return None


def _reference_error(item, ids, owners, formulas, *, ignore_series=False) -> str | None:
    """找出记录中第一个无法解析的编号。"""
    for key, value in _fields(item):
        if ignore_series and key == "series_set_ids":
            continue
        name = key[:-1] if key.endswith("_ids") else key
        field = _REFERENCE_ALIASES.get(name, name)
        if key in _BATCH_MAPS:
            invalid = value.keys() - ids["sample_batch_id"]
        elif key in _CHEMICAL_MAPS:
            invalid = value.keys() - formulas
        elif field == "record_id" or field in _TYPED_REFERENCES:
            allowed = owners if field == "record_id" else ids[field]
            refs = value if isinstance(value, list) else [value]
            invalid = any(ref is not None and ref not in allowed for ref in refs)
        else:
            continue
        if invalid:
            return f"{key} 包含不存在或类别错误的编号。"
    return None


def _series_by_run(record: dict) -> dict[str, list[str]]:
    """按实验收集当前曲线编号。"""
    result = defaultdict(list)
    for series in record["series_sets"]:
        result[series["run_id"]].append(series["series_set_id"])
    return result


def _quantity_error(value: dict) -> str | None:
    """所有固定数量字段均须保留可与原文表达核对的含义。"""
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            if current.get("kind") in {"scalar", "range", "uncertainty", "text"}:
                try:
                    check_quantity(current)
                except UnverifiedQuantity as error:
                    return str(error)
            else:
                pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
    return None


def _fields(value):
    """遍历嵌套记录的字段和值。"""
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"source", "legacy_mapping"}:
                continue
            if isinstance(child, dict) and (
                child.keys() == {"raw_text", "value", "unit"}
                or child.get("kind") in {"scalar", "range", "uncertainty", "text"}
            ):
                # 原文参数名属于论文内容，不按名称解释为编号引用。
                continue
            yield key, child
            if key not in _DYNAMIC_MAPS:
                yield from _fields(child)
    elif isinstance(value, list):
        for child in value:
            yield from _fields(child)


def _legacy_quantity(value: object) -> dict:
    """以旧原文和明示单位恢复数量，保留旧值供校验核对。"""
    if isinstance(value, dict):
        if value.get("kind") in {"scalar", "range"}:
            converted = dict(value)
            converted["unit_canonical"] = canonical_unit(value.get("unit_raw"))
            if converted["kind"] == "range":
                converted.setdefault("minimum_inclusive", True)
                converted.setdefault("maximum_inclusive", True)
            else:
                expression = quantity_from_text(converted["raw_text"])
                if expression.get("kind") == "scalar" and converted["operator"] in {"eq", "approx", "≈", "~", "≤", "≥"}:
                    converted["operator"] = expression["operator"]
            return converted
        if "minimum" in value and "maximum" in value:
            return quantity_from_text(f'{value["minimum"]}–{value["maximum"]} {value.get("unit") or ""}')
        if "unit_status" in value and not value.get("unit"):
            return {"kind": "text", "raw_text": str(value["value"])}
    text = quantity_text(value)
    if isinstance(value, dict) and value.get("unit") and "raw_text" in value:
        expression = quantity_from_text(text or "")
        if expression.get("kind") != "text" and not expression.get("unit_raw"):
            text = f'{text} {value["unit"]}'
    quantity = quantity_from_text(text or "")
    if isinstance(value, dict) and "pressure_type" in value and quantity["kind"] == "scalar":
        quantity["basis"] = value["pressure_type"]
    return quantity
