"""实验记录审核的请求契约、机器草稿投影和确认规则。"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Text = Annotated[str, Field(max_length=1000)]
EvidenceId = Annotated[str, Field(pattern=r"^e[1-9][0-9]*$", max_length=32)]


class ExperimentReviewFields(BaseModel):
    """一个可保存为待核对或确认的实验记录。"""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    kind: Literal["preparation", "measurement"]
    sample: Text = ""
    test: Text = ""
    metric: Text = ""
    conditions: Annotated[str, Field(max_length=16000)] = ""
    result: Text = ""
    evidence_ids: list[EvidenceId] = Field(max_length=32, default_factory=list)


class ExperimentReviewRequest(BaseModel):
    """绑定正文、审核版本和目标记录的单次操作。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    revision_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    version: int = Field(ge=0)
    record_id: str | None = Field(default=None, max_length=64)
    fragment_id: str | None = Field(default=None, pattern=r"^f[1-9][0-9]*$", max_length=32)
    action: Literal["save", "confirm", "exclude", "resolve"]
    task_id: str | None = Field(default=None, min_length=1, max_length=256)
    resolution: Literal["unreported", "ignored", "checked"] | None = None
    fields: ExperimentReviewFields | None = None
    verified: bool = False  # 接受已打开旧页面的请求，确认只依赖操作及字段校验。


def experiment_review_records(cards: list[dict], coverage: dict) -> dict[str, dict]:
    """以不可变机器记录和已保存人工覆盖构建当前工作台。"""
    records = {}
    for card_index, card in enumerate(cards):
        for kind, key in (("preparation", "preparation_steps"), ("measurement", "measurements")):
            for index, entry in enumerate(card.get(key, [])):
                record_id = f"{kind}-{card_index}-{index}"
                records[record_id] = {
                    **experiment_record_fields(kind, card.get("sample"), entry),
                    "record_id": record_id, "status": "machine",
                }
    for index, candidate in enumerate(coverage.get("review_candidates", [])):
        record_id = f"candidate-{index}"
        records[record_id] = {**candidate, "record_id": record_id, "status": "pending"}
    for record_id, entry in coverage.get("manual_review", {}).get("items", {}).items():
        records[record_id] = entry["record"]
    return records


def experiment_record_fields(kind: str, sample: dict | None, entry: dict) -> dict:
    """将制备或测量字段和证据投影为同一条记录。"""
    metric = entry.get("operation") if kind == "preparation" else entry.get("property")
    conditions = [item for item in entry.get("conditions", []) if item.get("value")]
    fields = [sample, metric, entry.get("method"), entry.get("result"), *(item["value"] for item in conditions)]
    ids = [evidence_id for field in fields if field for evidence_id in field.get("evidence_ids", [])]
    return {
        **({"canonical_id": entry["canonical_id"]} if entry.get("canonical_id") else {}),
        "kind": kind,
        "sample": (sample or {}).get("raw_text", ""),
        "test": (entry.get("method") or {}).get("raw_text", ""),
        "metric": (metric or {}).get("raw_text", ""),
        "result": (entry.get("result") or {}).get("raw_text", ""),
        "conditions": " · ".join(f'{item["name"]} {item["value"]["raw_text"]}' for item in conditions),
        "evidence_ids": list(dict.fromkeys([*entry.get("assignment_evidence_ids", []), *ids])),
    }


def validate_experiment_review(
    fields: ExperimentReviewFields, evidence: list[dict], *, confirm: bool, edit: bool = False,
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
    if not _locates_literal(fields.sample, selected):
        raise ValueError("所选出处中找不到填写的样品名")
    if not _locates_literal(core, selected, result=fields.kind == "measurement"):
        raise ValueError("所选出处中找不到填写的操作或结果，请保留原文字面写法")


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
