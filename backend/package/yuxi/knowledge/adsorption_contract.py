"""吸附分离 JSON 的字段契约和同文档引用约束。"""

from __future__ import annotations

import json
from collections import defaultdict
from functools import cache
from importlib.resources import files

from jsonschema import Draft202012Validator

STRUCTURE_VERSION = 2
STRUCTURE_PROMPT_VERSION = "adsorption-canonical-v3"
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
_DYNAMIC_MAPS = _BATCH_MAPS | _CHEMICAL_MAPS | {"pdf_page_to_printed_page"}
_REFERENCE_ALIASES = {
    "carbon_source_entity_id": "entity_id", "numerator_entity_id": "entity_id",
    "denominator_entity_id": "entity_id", "preferential_component_entity_id": "entity_id",
    "parent_state_id": "sample_state_id", "target_sample_state_id": "sample_state_id",
    "input_sample_state_id": "sample_state_id", "output_sample_state_id": "sample_state_id",
    "preparation_route_id": "route_id", "input_run_id": "run_id", "source_run_id": "run_id",
    "source_model_run_id": "run_id", "supported_by_observation_id": "observation_id",
    "input_intermediate_id": "intermediate_id",
}
_TYPED_REFERENCES = set(REGISTRIES.values()) | {"document_id", "intermediate_id"}
_INDEPENDENT_MEMBERS = {"observations": "results", "synthesis_routes": "operations"}
type Notice = tuple[str, str]
type RecordPosition = tuple[str, int]


@cache
def output_schema() -> dict:
    """读取随后端包发布的规范 JSON Schema。"""
    return json.loads(files(__package__).joinpath("adsorption_schema.json").read_text(encoding="utf-8"))


def stage_schema(fields: tuple[str, ...], observation_types: tuple[str, ...] = ()) -> dict:
    """从同一契约截取当前提取阶段及其引用的定义。"""
    schema = output_schema()
    properties = {name: schema["properties"][name] for name in fields}
    if observation_types:
        variants = properties["observations"]["items"]["anyOf"]
        selected = [
            variant for variant in variants
            if schema["$defs"][variant["$ref"].rsplit("/", 1)[-1]]["properties"]["observation_type"]["const"]
            in observation_types
        ]
        properties["observations"] = {"type": "array", "items": {"anyOf": selected}}

    definitions = {}

    def include(value):
        """递归收集所选字段引用的定义。"""
        if isinstance(value, dict):
            if "$ref" in value:
                name = value["$ref"].rsplit("/", 1)[-1]
                if name not in definitions:
                    definitions[name] = schema["$defs"][name]
                    include(definitions[name])
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
                            ))
                    item = {**item, child_field: kept}
            if not validator.is_valid(item):
                notices.append(("invalid_record_shape", f"{location}缺少字段、含多余字段或类型错误。"))
            elif evidence_references(item) - source_ids:
                notices.append(("invalid_evidence_reference", f"{location}引用了无法定位的原文块。"))
            elif name == "observations" and not item["observation_id"].startswith(f"obs:{stage}:"):
                notices.append(("invalid_observation_id", f"{location}的编号不属于当前观测阶段。"))
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
            notices.append(("invalid_record_reference", f"{collection} 第 {index + 1} 条已剔除。{reason}"))
        for collection in REGISTRIES:
            record[collection] = [
                item for index, item in enumerate(record[collection]) if (collection, index) not in problems
            ]
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
                        register("intermediate_id", intermediate, step_position)
                    for reagent in operation.get("reagent_uses", []):
                        register("reagent_use_id", reagent["reagent_use_id"], step_position)
    for positions in owners.values():
        if len(positions) > 1:
            if positions.count(None) > 1:
                raise ValueError("规范 JSON 的文档或原文编号重复")
            for position in positions:
                if position is None:
                    continue
                if notices is not None and len(position) == 3:
                    duplicate_steps[position[:2]].add(position[2])
                else:
                    problems[position[:2]] = "记录编号重复，无法确定归属。"
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
            child_field = _INDEPENDENT_MEMBERS.get(collection)
            if notices is not None and child_field and isinstance(item.get(child_field), list):
                kept = []
                for member_index, member in enumerate(item[child_field], 1):
                    reason = (
                        "步骤编号重复，无法确定归属。" if member_index in duplicate_steps.get(position, ())
                        else _reference_error(member, ids, owners, formulas)
                    )
                    if reason:
                        notices.append((
                            "invalid_member_reference",
                            f"{collection} 第 {index + 1} 条的 {child_field} 第 {member_index} 项已剔除。{reason}",
                        ))
                    else:
                        kept.append(member)
                item[child_field] = kept
            ignore_series = notices is not None and collection == "experiment_runs"
            if reason := _reference_error(item, ids, owners, formulas, ignore_series=ignore_series):
                problems[position] = reason
    return problems


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


def _fields(value):
    """遍历嵌套记录的字段和值。"""
    if isinstance(value, dict):
        for key, child in value.items():
            yield key, child
            if key not in _DYNAMIC_MAPS:
                yield from _fields(child)
    elif isinstance(value, list):
        for child in value:
            yield from _fields(child)
