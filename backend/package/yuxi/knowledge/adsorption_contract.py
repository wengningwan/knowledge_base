"""论文规范 JSON 的共享形状与同文档引用约束。"""

from __future__ import annotations

import json
from collections import defaultdict
from functools import cache
from graphlib import CycleError, TopologicalSorter
from importlib.resources import files

from jsonschema import Draft202012Validator

STRUCTURE_VERSION = 3
READABLE_STRUCTURE_VERSIONS = (2, STRUCTURE_VERSION)
STRUCTURE_PROMPT_VERSION = "paper-general-v12"
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
_DYNAMIC_MAPS = _BATCH_MAPS | {
    "pdf_page_to_printed_page", "factor_assignments", "parameter_definitions", "model_parameters",
    "reported_background_values", "fit_quality_summary",
}
_REFERENCE_ALIASES = {
    "numerator_entity_id": "entity_id",
    "denominator_entity_id": "entity_id", "preferential_component_entity_id": "entity_id",
    "parent_state_id": "sample_state_id", "target_sample_state_id": "sample_state_id",
    "input_sample_state_id": "sample_state_id", "output_sample_state_id": "sample_state_id",
    "preparation_route_id": "route_id", "input_run_id": "run_id",
    "supported_by_observation_id": "observation_id",
    "input_intermediate_id": "intermediate_id", "output_intermediate_id": "intermediate_id",
    "species_entity_id": "entity_id",
}
_TYPED_REFERENCES = set(REGISTRIES.values()) | {"document_id", "intermediate_id"}
_INDEPENDENT_MEMBERS = {"observations": "results", "synthesis_routes": "operations"}
type Notice = tuple[str, str] | tuple[str, str, dict]
type RecordPosition = tuple[str, int]


@cache
def output_schema() -> dict:
    """读取随后端包发布的规范 JSON Schema。"""
    return json.loads(files(__package__).joinpath("adsorption_schema.json").read_text(encoding="utf-8"))


def stage_schema(fields: tuple[str, ...]) -> dict:
    """从导出契约派生阶段输出，程序负责生成派生字段。"""
    schema = output_schema()
    properties = {name: schema["properties"][name] for name in fields if name != "evidence_attributions"}
    if "evidence_attributions" in fields:
        evidence_name = schema["properties"]["evidence"]["items"]["$ref"].rsplit("/", 1)[-1]
        evidence_fields = schema["$defs"][evidence_name]["properties"]
        properties["evidence_attributions"] = {"type": "array", "items": {
            "type": "object", "required": ["evidence_id", "provenance_scope"], "additionalProperties": False,
            "properties": {name: evidence_fields[name] for name in ("evidence_id", "provenance_scope")},
        }}
    definitions = {}
    derived_fields = {
        "paper_run": {"series_set_ids"},
        "paper_series": {"observation_ids"},
        "paper_series_source": {"data_availability"},
        "paper_operation_conditions": {"temperature_by_sample"},
        "paper_reagent_ratio": {"per_sample_values"},
        "paper_scalar": {"normalized_value", "unit_canonical"},
        "paper_range": {"normalized_value", "unit_canonical"},
    }
    stage_fields = {
        "paper_operation_conditions": {"temperature_factor": {"type": "string", "minLength": 1}},
        "paper_reagent_ratio": {"sample_factor": {"type": "string", "minLength": 1}},
    }
    if "validation_issues" in properties:
        issue_name = properties["validation_issues"]["items"]["$ref"].rsplit("/", 1)[-1]
        stage_fields[issue_name] = {"covered_by_candidate_ids": {
            "type": "array", "items": {"type": "string"}, "uniqueItems": True,
        }}

    def include(value):
        """递归收集所选字段引用的定义。"""
        if isinstance(value, dict):
            if "$ref" in value:
                name = value["$ref"].rsplit("/", 1)[-1]
                if name not in definitions:
                    shape = schema["$defs"][name]
                    omitted = derived_fields.get(name, set())
                    if omitted or name in stage_fields:
                        optional = omitted | ({"source"} if name == "paper_series" else set())
                        shape = {
                            **shape,
                            "properties": {
                                **{key: field for key, field in shape["properties"].items() if key not in omitted},
                                **dict.fromkeys(omitted, False),
                                **stage_fields.get(name, {}),
                            },
                            "required": [key for key in shape.get("required", []) if key not in optional],
                        }
                    definitions[name] = shape
                    include(shape)
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
        if name == "series_sets":
            point_schema = schema["$defs"]["paper_series_member"]["properties"]["data_points"]["items"]
            point_validator = Draft202012Validator({**point_schema, "$defs": schema["$defs"]})
        child_field = _INDEPENDENT_MEMBERS.get(name)
        for index, item in enumerate(candidate, 1):
            location = f"{stage} 的 {name} 第 {index} 条"
            if name == "series_sets" and isinstance(item, dict) and isinstance(item.get("series_members"), list):
                for member in item["series_members"]:
                    if not isinstance(member, dict) or not isinstance(member.get("data_points"), list):
                        continue
                    points = member["data_points"]
                    kept = [point for point in points if point_validator.is_valid(point)
                            and not evidence_references(point) - source_ids]
                    if len(kept) != len(points):
                        notices.append(("invalid_curve_point", f"{location}的部分坐标格式或出处错误。"))
                    member["data_points"] = kept or None
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
                    if name == "observations":
                        member_refs = {
                            ref for member in item[child_field] if isinstance(member, dict)
                            and isinstance(member.get("evidence_ids"), list)
                            for ref in member["evidence_ids"] if isinstance(ref, str)
                        }
                        context = [ref for ref in item["evidence_ids"] if ref in source_ids and ref not in member_refs]
                        if set(item["evidence_ids"]) - source_ids - member_refs:
                            notices.append(("invalid_evidence_reference", f"{location}的部分观测上下文出处无法定位。"))
                        kept_refs = [ref for member in kept for ref in member["evidence_ids"]]
                        item = {**item, "evidence_ids": list(dict.fromkeys([*context, *kept_refs]))}
                    item = {**item, child_field: kept}
            if not validator.is_valid(item):
                notices.append(("invalid_record_shape", f"{location}缺少字段、含多余字段或类型错误。",
                                {"collection": name, "record": item}))
            elif evidence_references(item) - source_ids:
                notices.append(("invalid_evidence_reference", f"{location}引用了无法定位的原文块。",
                                {"collection": name, "record": item}))
            elif name == "evidence_attributions" and item["evidence_id"] not in source_ids:
                notices.append(("invalid_evidence_attribution", f"{location}不能定位到本次原文。"))
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
    series_runs = {run["run_id"] for run in record["experiment_runs"] if "series_set_ids" in run}
    series_by_id = {series["series_set_id"]: series for series in record["series_sets"]}
    runs = {run["run_id"]: run for run in record["experiment_runs"]}
    try:
        TopologicalSorter({key: run.get("input_run_ids", []) for key, run in runs.items()}).prepare()
    except CycleError as error:
        cycle = set(error.args[1])
        for index, run in enumerate(record["experiment_runs"]):
            if run["run_id"] in cycle:
                problems[("experiment_runs", index)] = "实验输入引用形成循环。"
    for collection in REGISTRIES:
        for index, item in enumerate(record[collection]):
            position = (collection, index)
            if position in problems:
                continue
            if collection == "series_sets" and item["run_id"] not in series_runs:
                problems[position] = "对应实验没有可登记该曲线的字段。"
                continue
            if collection == "series_sets":
                run = runs[item["run_id"]]
                if any(member["sample_state_id"] is not None and member["sample_state_id"] not in run["sample_state_ids"]
                       or member["entity_id"] is not None and member["entity_id"] not in run["species_entity_ids"]
                       for member in item["series_members"]):
                    problems[position] = "曲线的样品或物质未在对应实验中登记。"
                    continue
            if collection == "observations" and item["observation_type"] in {
                "quantitative_measurement", "reported_result", "result_group",
            }:
                run = runs.get(item["run_id"])
                if run is None:
                    problems[position] = "观测引用了不存在的实验。"
                    continue
                samples = item.get("sample_state_ids", [])
                if item.get("sample_state_id") is not None:
                    samples = [item["sample_state_id"]]
                if not set(samples) <= set(run.get("sample_state_ids", [])):
                    problems[position] = "观测样品未在所关联实验中登记。"
                    continue
                entities = [item.get("entity_id"), *item.get("ordered_species_pair", {}).values()]
                if notices is None:
                    entities.extend(result.get("entity_id") for result in item.get("results", []))
                if not (set(entities) - {None}) <= set(run.get("species_entity_ids", [])):
                    problems[position] = "观测物种未在所关联实验中登记。"
                    continue
                if series_id := item.get("series_set_id"):
                    series = series_by_id.get(series_id, {})
                    members = {member["sample_state_id"] for member in series.get("series_members", [])} - {None}
                    if series.get("run_id") != item["run_id"] or (members and not set(samples) <= members):
                        reason = "观测引用的曲线与实验或样品归属不一致。"
                        if notices is None:
                            problems[position] = reason
                            continue
                        notices.append(("invalid_observation_series", f'{item["observation_id"]}: {reason}'))
                        del item["series_set_id"]
            child_field = _INDEPENDENT_MEMBERS.get(collection)
            if notices is not None and child_field and isinstance(item.get(child_field), list):
                kept = []
                for member_index, member in enumerate(item[child_field], 1):
                    reason = (
                        "步骤编号重复，无法确定归属。" if member_index in duplicate_steps.get(position, ())
                        else _reference_error(member, ids, owners)
                    )
                    if not reason and collection == "observations" and (entity := member.get("entity_id")):
                        if entity not in run["species_entity_ids"]:
                            reason = "结果的物种未在所关联实验中登记。"
                    if reason:
                        notices.append((
                            "invalid_member_reference",
                            f"{collection} 第 {index + 1} 条的 {child_field} 第 {member_index} 项已剔除。{reason}",
                            {"collection": collection, "record": {**item, child_field: [member]}},
                        ))
                    else:
                        kept.append(member)
                item[child_field] = kept
            ignore_series = notices is not None and collection == "experiment_runs"
            if reason := _reference_error(item, ids, owners, ignore_series=ignore_series):
                problems[position] = reason
    return problems


def _reference_error(item, ids, owners, *, ignore_series=False) -> str | None:
    """找出记录中第一个无法解析的编号。"""
    for key, value in _fields(item):
        if ignore_series and key == "series_set_ids":
            continue
        name = key[:-1] if key.endswith("_ids") else key
        field = _REFERENCE_ALIASES.get(name, name)
        if key in _BATCH_MAPS:
            invalid = value.keys() - ids["sample_batch_id"]
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
            if isinstance(child, dict) and (
                child.keys() == {"raw_text", "value", "unit"} or child.get("kind") in ("scalar", "range")
            ):
                # 原文参数名属于论文内容，不按名称解释为编号引用。
                continue
            yield key, child
            if key not in _DYNAMIC_MAPS:
                yield from _fields(child)
    elif isinstance(value, list):
        for child in value:
            yield from _fields(child)
