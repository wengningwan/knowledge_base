"""实验记录审核的请求契约、机器草稿投影和确认规则。"""

from __future__ import annotations

import re
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Text = Annotated[str, Field(max_length=1000)]
EvidenceId = Annotated[str, Field(pattern=r"^e[1-9][0-9]*$", max_length=32)]
ObjectId = Annotated[str, Field(min_length=1, max_length=128)]
ParameterName = Annotated[str, Field(min_length=1, max_length=128)]


class ExperimentParameter(BaseModel):
    """保留条件的原文及可确定的数量与单位。"""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    raw_text: Annotated[str, Field(min_length=1, max_length=1000)]
    value: int | float | Text | None = None
    unit: Annotated[str, Field(max_length=128)] | None = None


class ExperimentReviewFields(BaseModel):
    """一个可保存为待核对或确认的实验记录。"""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    kind: Literal["preparation", "measurement"]
    sample: Text = ""
    sample_state_id: ObjectId | None = None
    sample_state_ids: list[ObjectId] = Field(max_length=128, default_factory=list)
    scope: Literal["sample", "group"] = "sample"
    run_id: ObjectId | None = None
    run_missing_reason: Literal["not_reported", "unresolved"] | None = None
    test: Text = ""
    metric: Text = ""
    conditions: Annotated[str, Field(max_length=16000)] = ""
    result: Text = ""
    evidence_ids: list[EvidenceId] = Field(max_length=32, default_factory=list)
    parameters: dict[ParameterName, ExperimentParameter] = Field(max_length=64, default_factory=dict)


class ExperimentEntityChange(BaseModel):
    """编辑固定登记字段，或补录有明确样品归属的实验。"""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    kind: Literal["sample_state", "sample_batch", "synthesis_route", "experiment_run"]
    entity_id: ObjectId | None = None
    reported_name: Text | None = None
    state: Text | None = None
    physical_form: Text | None = None
    sample_batch_id: ObjectId | None = None
    preparation_route_id: ObjectId | None = None
    batch_number: Text | None = None
    yield_text: Text | None = None
    replicate_count: int | None = Field(default=None, ge=1)
    method: Text | None = None
    experiment_type: Literal["measurement", "fitting", "prediction", "derivation", "computation"] | None = None
    sample_state_ids: list[ObjectId] = Field(max_length=128, default_factory=list)
    species_entity_ids: list[ObjectId] = Field(max_length=128, default_factory=list)
    parameters: dict[ParameterName, ExperimentParameter] = Field(max_length=64, default_factory=dict)
    evidence_ids: list[EvidenceId] = Field(max_length=32, default_factory=list)


class ExperimentReviewRequest(BaseModel):
    """绑定正文、审核版本和目标记录的单次操作。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    revision_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    version: int = Field(ge=0)
    record_id: str | None = Field(default=None, max_length=64)
    target_record_id: str | None = Field(default=None, max_length=64)
    fragment_id: str | None = Field(default=None, pattern=r"^f[1-9][0-9]*$", max_length=32)
    action: Literal["save", "confirm", "exclude", "resolve"]
    task_id: str | None = Field(default=None, min_length=1, max_length=256)
    resolution: Literal["unreported", "ignored", "checked"] | None = None
    fields: ExperimentReviewFields | None = None
    entity_change: ExperimentEntityChange | None = None
    verified: bool = False  # 接受已打开旧页面的请求，确认只依赖操作及字段校验。


def experiment_review_records(cards: list[dict], coverage: dict) -> dict[str, dict]:
    """以不可变机器记录和已保存人工覆盖构建当前工作台。"""
    records = {}
    for card_index, card in enumerate(cards):
        for kind, key in (("preparation", "preparation_steps"), ("measurement", "measurements")):
            for index, entry in enumerate(card.get(key, [])):
                fields = experiment_record_fields(kind, card.get("sample"), entry)
                record_id = experiment_record_id(fields) or f"{kind}-{card_index}-{index}"
                records[record_id] = {**fields, "record_id": record_id, "status": "machine"}
    for index, candidate in enumerate(coverage.get("review_candidates", [])):
        record_id = f"candidate-{index}"
        records[record_id] = {**candidate, "record_id": record_id, "status": "pending"}
    for record_id, entry in coverage.get("manual_review", {}).get("items", {}).items():
        records[record_id] = entry["record"]
    names = {card.get("sample_state_id"): card.get("sample", {}).get("raw_text", "") for card in cards}
    for record in records.values():
        if record["kind"] == "preparation":
            record["scope"] = "sample"
        elif record.get("scope") == "group":
            record["sample_state_id"] = None
            record["sample"] = " / ".join(names.get(key, key) for key in record.get("sample_state_ids", []))
    return records


def experiment_record_id(fields: dict) -> str | None:
    """按固定结果或样品步骤身份定位审核记录，旧稿没有身份时交由兼容读取。"""
    kind = fields["kind"]
    if kind == "measurement" and fields.get("result_id"):
        identity = fields["result_id"]
    elif kind == "preparation" and fields.get("operation_id") and fields.get("sample_state_id"):
        identity = fields["operation_id"] + "\0" + fields["sample_state_id"]
    else:
        return None
    return f"{kind}-{sha256(identity.encode()).hexdigest()[:32]}"


def experiment_record_fields(kind: str, sample: dict | None, entry: dict) -> dict:
    """将制备或测量字段和证据投影为同一条记录。"""
    metric = entry.get("operation") if kind == "preparation" else entry.get("property")
    conditions = [item for item in entry.get("conditions", []) if item.get("value")]
    fields = [sample, metric, entry.get("method"), entry.get("result"), *(item["value"] for item in conditions)]
    ids = [evidence_id for field in fields if field for evidence_id in field.get("evidence_ids", [])]
    return {
        **{key: entry[key] for key in (
            "canonical_id", "operation_id", "result_id", "sample_state_id", "sample_state_ids", "scope",
            "run_id", "run_ids", "run_missing_reason", "metric_id", "applies_to_sample_state_ids",
        ) if key in entry},
        "parameters": review_parameters(entry.get("parameters", {})),
        "kind": kind,
        "sample": entry.get("sample_label") or (sample or {}).get("raw_text", ""),
        "test": (entry.get("method") or {}).get("raw_text", ""),
        "metric": (metric or {}).get("raw_text", ""),
        "result": (entry.get("result") or {}).get("raw_text", ""),
        "conditions": " · ".join(f'{item["name"]} {item["value"]["raw_text"]}' for item in conditions),
        "evidence_ids": list(dict.fromkeys([*entry.get("assignment_evidence_ids", []), *ids])),
    }


def review_parameters(values: dict) -> dict:
    """把数量条件投影为固定的人工表单原文，类型不明时保留完整文字。"""
    from yuxi.knowledge.adsorption_quantities import quantity_text

    result = {}
    for key, value in values.items():
        text = quantity_text(value)
        if text:
            result[key] = {
                "raw_text": str(text),
                "value": value.get("value") if isinstance(value, dict) and value.get("kind") in {None, "scalar"} else None,
                "unit": (value.get("unit_raw") or value.get("unit")) if isinstance(value, dict) else None,
            }
    return result


def validate_experiment_review(
    fields: ExperimentReviewFields, evidence: list[dict], *, confirm: bool, edit: bool = False,
    sample_names: list[str] | None = None,
) -> None:
    """校验出处身份、完整记录字段及补录确认的原文依据。"""
    quotes = {item["evidence_id"]: item["quote"] for item in evidence}
    if any(ref not in quotes for ref in fields.evidence_ids):
        raise ValueError("所选出处不属于当前正文草稿")
    if fields.kind == "preparation" and fields.result:
        raise ValueError("制备记录使用操作和条件字段")
    if not fields.metric and not fields.result:
        raise ValueError("请填写实验操作、指标或结果")
    if not (confirm or edit):
        return
    core = fields.result if fields.kind == "measurement" else fields.metric
    if not (fields.sample and fields.metric and core):
        raise ValueError("保存前须填写样品及核心实验字段")
    if not confirm:
        return
    if not fields.evidence_ids:
        raise ValueError("确认前须选择出处")
    selected = [quotes[ref] for ref in fields.evidence_ids]
    if not all(_locates_literal(name, selected) for name in (sample_names or [fields.sample])):
        raise ValueError("所选出处中找不到填写的样品名")
    if not _locates_literal(core, selected, result=fields.kind == "measurement"):
        raise ValueError("所选出处中找不到填写的操作或结果，请保留原文字面写法")


def bind_review_fields(fields: ExperimentReviewFields, original: dict | None, paper: dict) -> dict:
    """只按本次样品身份绑定人工字段；未确定实验关系仍保留待处理。"""
    states = {item["sample_state_id"]: item for item in paper["sample_states"]}
    value = fields.model_dump()
    if original and original.get("scope") == "group" and original["kind"] == "measurement":
        if (
            fields.kind != "measurement" or fields.scope != "group"
            or set(fields.sample_state_ids) != set(original.get("sample_state_ids", []))
        ):
            raise ValueError("这条组观察必须保留完整样品集合")
        ids = original["sample_state_ids"]
        value["sample_state_id"] = None
    else:
        if fields.scope == "group":
            raise ValueError("新记录请分别补录到明确样品，不能隐式创建组观察")
        ids = [fields.sample_state_id] if fields.sample_state_id else []
        value["sample_state_ids"] = []
    if not ids or any(key not in states for key in ids):
        raise ValueError("请选择本次已登记的样品")
    value["sample"] = " / ".join(states[key]["reported_name"] for key in ids)
    run = next((row for row in paper["experiment_runs"] if row["run_id"] == fields.run_id), None)
    if fields.run_id and run is None:
        raise ValueError("所选实验不属于本次结果")
    if fields.kind == "measurement":
        if original and not original.get("run_id") and original.get("run_ids"):
            if fields.run_id:
                raise ValueError("多实验观测必须保留已有的实验关联")
            value["run_missing_reason"] = None
            return value
        if not fields.run_id and not fields.run_missing_reason:
            raise ValueError("请选择实验或说明实验关联缺失原因")
        if run:
            value["run_missing_reason"] = None
            value["test"] = run.get("method") or run.get("measurement_method") or run.get("technique") or run["run_type"]
    else:
        value.update(run_id=None, run_missing_reason=None, scope="sample")
    return value


def _locates_literal(value: str, quotes: list[str], *, result: bool = False) -> bool:
    """忽略空白差异，并拦截局部数值和遗漏的常见比较限定词。"""
    pattern = r"\s+".join(re.escape(part) for part in value.split())
    if re.match(r"[A-Za-z0-9_]", value):
        pattern = r"(?<![A-Za-z0-9_+−-])(?<!\d[.,])" + pattern
    if re.search(r"[A-Za-z0-9_]$", value):
        pattern += r"(?![A-Za-z0-9_-]|[.,]\d|[%^]|\$\^)"
    if result and re.match(r"^[+−-]?(?:\d|\.\d)", value):
        qualifiers = (
            r"(?:\b(?:higher than|lower than|greater than|less than|more than|at least|at most|approximately|about)"
            r"|[<>≤≥≈~约]|高于|低于|超过)\s*$"
        )
        return any(
            not re.search(qualifiers, quote[:match.start()], re.IGNORECASE)
            for quote in quotes for match in re.finditer(pattern, quote)
        )
    return any(re.search(pattern, quote) for quote in quotes)
