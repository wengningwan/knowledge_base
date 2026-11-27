"""将数表列的用途映射为带原文单元格的实验观测。"""

from __future__ import annotations

import html
import math
import re
import unicodedata
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from yuxi.knowledge.adsorption_review import Measurement
from yuxi.knowledge.paper_observation_merge import discard_wrong_table_scopes, grounded_observation_members
from yuxi.knowledge.structure_tables import table_cells

_NUMBER = (
    r"[+−-]?(?:(?:\d{1,3}(?:[ \u00a0\u2009\u202f]\d{3})+|\d+)(?:\.\d+(?:[ \u00a0\u2009\u202f]\d+)*)?|\.\d+)"
    r"(?:[eE][+−-]?\d+)?"
)
_UNIT_TOKEN = r"[a-zA-ZµμΩÅÅ℃℉°%％‰]+(?:\^?[+−-]\d+|[⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+)?"
_UNIT = rf"{_UNIT_TOKEN}(?:(?:\s*[·⋅/×]\s*|\s+){_UNIT_TOKEN})*"
_HEADING = re.compile(rf"(.+?)\s*/\s*(?:[（(]\s*({_UNIT})\s*[）)]|({_UNIT}))")
_QUANTITY = re.compile(rf"([<≤>≥≈~]?)[ ]*({_NUMBER})(?:\s*[~～–—至-]\s*({_NUMBER}))?\s*({_UNIT})?")
_EMPTY = {"", "-", "−", "–", "—"}


def table_columns(text: str) -> list[str]:
    """向抽取模型提供从同次原文可定位的单行表头。"""
    grid = table_cells(text, 0, len(text))
    return [_cell(text[left:right]) for left, right in grid[0]] if grid else []


def project_table_observations(
    record: dict, sources: dict, mappings: list[dict], measurements: dict[str, list[Measurement]],
) -> tuple[list[dict], set[tuple[str, str]]]:
    """按已核验的列用途取数，返回疑点以及由程序核对的实验与数表。"""
    runs = {item["run_id"]: item for item in record["experiment_runs"]}
    states = {item["sample_state_id"]: item for item in record["sample_states"]}
    batches = {item["sample_batch_id"]: item for item in record["sample_batches"]}
    by_source = defaultdict(list)
    for mapping in mappings:
        proposals = by_source[mapping["evidence_ids"][0]]
        if mapping not in proposals:
            proposals.append(mapping)
    used_cells = defaultdict(set)
    for observation in record["observations"]:
        for member, proof in grounded_observation_members(observation, measurements[observation["observation_id"]]):
            if "value" not in member:
                continue
            for ref, position in proof[0][2].items():
                used_cells[(observation["run_id"], ref, position)].add(observation.get("sample_state_id"))
    resolved_cells = {}
    issues = []
    covered_tables = set()
    for ref, proposals in by_source.items():
        mapping = proposals[0]
        run = runs.get(mapping["run_id"])
        if run is None or ref not in run["evidence_ids"]:
            issues.append(_issue(ref, "原表未归入所声明的实验。"))
            continue

        text = sources[ref]["quote"]
        spans = table_cells(text, 0, len(text))
        columns = [_cell(text[left:right]) for left, right in spans[0]] if spans else []
        selected = [*mapping["condition_columns"], *mapping["result_columns"], *mapping.get("ignored_columns", [])]
        selected.extend(mapping[key] for key in ("metric_column", "sample_column") if key in mapping)
        keys = [_key(column) for column in columns]
        roles = [_key(column) for column in selected]
        entity_id = mapping.get("entity_id")
        if (
            len(proposals) != 1 or not spans
            or (entity_id is not None and entity_id not in run["species_entity_ids"])
            or "" in roles or len(set(roles)) != len(roles) or any(keys.count(role) != 1 for role in roles)
        ):
            issues.append(_issue(ref, "数表的实验、物质或列用途无法唯一确认。", run["run_id"]))
            continue

        positions = {column: keys.index(_key(column)) for column in selected}
        headers = [_heading(column) for column in columns]
        sample_ids = run["sample_state_ids"]
        conditions = [positions[column] for column in mapping["condition_columns"]]
        results = [positions[column] for column in mapping["result_columns"]]
        metric = positions.get(mapping.get("metric_column"))
        sample = positions.get(mapping.get("sample_column"))
        scope = mapping.get("sample_scope")
        experiment_level = scope == "experiment"
        samples = _sample_factors(sample_ids, states, batches, {_key(headers[column][0]) for column in conditions}, ref)
        if experiment_level and (sample is not None or _has_state_axis(samples)):
            issues.append(_issue(ref, "数表声明为实验级数据，但列用途包含区分样品状态的变量。", run["run_id"]))
            continue
        if scope == "state" and not samples:
            issues.append(_issue(ref, "数表声明按样品状态取数，但实验没有登记样品状态。", run["run_id"]))
            continue
        covered_tables.add((run["run_id"], ref))
        curves = (
            _table_curves(record["series_sets"], run["run_id"], ref, headers, results, experiment_level, entity_id)
            if metric is None else []
        )
        unmapped = set(range(len(headers))) - set(positions.values())
        omitted = set()
        skipped_rows = 0
        for row_index, row_spans in enumerate(spans[1:], 1):
            row = [_cell(text[left:right]) for left, right in row_spans]
            omitted.update(column for column in unmapped if _quantity(row[column], headers[column][1]) is not None)
            if not any(row[column] not in _EMPTY for column in results):
                continue
            row_conditions = {}
            for column in conditions:
                name, unit = headers[column]
                raw = row[column]
                if quantity := _quantity(raw, unit):
                    row_conditions[name] = quantity
                elif unit is None and raw not in _EMPTY:
                    row_conditions[name] = {"raw_text": raw, "value": raw, "unit": None}
            if len(row_conditions) != len(conditions):
                skipped_rows += 1
                continue
            sample_id, resolved = (None, True) if experiment_level else _sample_for_row(
                samples, row_conditions, row[sample] if sample is not None else None,
            )
            metric_name, metric_unit = _metric_heading(row[metric]) if metric is not None else (None, None)
            if not resolved or metric is not None and (metric_name in _EMPTY or _quantity(metric_name) is not None):
                skipped_rows += 1
                continue

            members, quantities = [], {}
            values: list[Measurement] = []
            unresolved = False
            for column in results:
                if row[column] in _EMPTY:
                    continue
                heading, unit = headers[column]
                quantity = _quantity(row[column], unit, metric_unit)
                if quantity is None:
                    unresolved = True
                    continue
                quantities[column] = quantity
                cell = (run["run_id"], ref, (row_index, column))
                if scope:
                    resolved_cells[cell] = sample_id
                if cell in used_cells and (scope is None or sample_id in used_cells[cell]):
                    continue
                name = metric_name or heading
                if metric_name and len(results) > 1:
                    quantity["basis"] = heading
                label = f"{name} ({heading})" if "basis" in quantity else name
                if unit := quantity["unit_raw"]:
                    label = f"{label} ({unit})"
                members.append({"metric_id": name, "value": quantity, "evidence_ids": [ref]})
                values.append((label, row[column], {ref: (row_index, column)}, [ref]))
            row_series = []
            for series, member, x_column, y_column, points in curves:
                member_sample = member["sample_state_id"]
                sample_differs = member_sample != sample_id and (member_sample is not None or len(samples) > 1)
                if not experiment_level and sample_differs:
                    continue
                if any(
                    not _same_parameter(value, row_conditions[name])
                    for name, value in member.get("conditions", {}).items() if name in row_conditions
                ):
                    continue
                x = _quantity(row[x_column], headers[x_column][1])
                y = quantities.get(y_column)
                if x and y and all(
                    item["kind"] == "scalar" and "operator" not in item
                    and _key(item["unit_raw"] or "") == _key(headers[column][1] or "")
                    for item, column in ((x, x_column), (y, y_column))
                ):
                    points.append({"x": x["value"], "y": y["value"], "evidence_ids": [ref]})
                    row_series.append(series["series_set_id"])
                elif row[y_column] not in _EMPTY:
                    unresolved = True
            if unresolved:
                skipped_rows += 1
            if members:
                observation_id = f"obs:table:{ref}:{row_index}"
                observation = {
                    "observation_id": observation_id, "observation_type": "result_group", "run_id": run["run_id"],
                    "sample_state_id": sample_id, "entity_id": entity_id, "conditions": row_conditions,
                    "results": members, "evidence_ids": [ref],
                }
                if len(row_series) == 1 and len(results) == 1:
                    observation["series_set_id"] = row_series[0]
                record["observations"].append(observation)
                measurements[observation_id] = values
        for series, member, _, _, points in curves:
            if points:
                if experiment_level:
                    member["sample_state_id"] = None
                member["data_points"] = points + [
                    point for point in member["data_points"] or [] if ref not in point["evidence_ids"]
                ]
                series["source"]["data_availability"] = "reported_points"
        messages = []
        if skipped_rows:
            messages.append(f"有 {skipped_rows} 行的结果、单位或样品归属需核对。")
        if omitted:
            messages.append("含数值的列尚未分配用途：" + "、".join(columns[index] for index in sorted(omitted)) + "。")
        if messages:
            issues.append(_issue(ref, "".join(messages), run["run_id"]))
    if resolved_cells:
        discard_wrong_table_scopes(record, measurements, resolved_cells)
    return issues, covered_tables


def _table_curves(series_sets, run_id, ref, headers, results, experiment_level, entity_id):
    """接入原表已关联且两个轴能在表头唯一定位的单条曲线。"""
    curves = []
    for series in series_sets:
        refs = set(series["evidence_ids"]) | set(series["x"].get("evidence_ids", [])) | set(
            series["y"].get("evidence_ids", [])
        )
        if (series["run_id"] != run_id or ref not in refs or len(series["series_members"]) != 1
                or series.get("axis_consistency_status") == "needs_review"):
            continue
        member = series["series_members"][0]
        if entity_id is not None and member["entity_id"] not in (None, entity_id):
            continue
        if experiment_level and member["sample_state_id"] is not None and any(
            ref not in point["evidence_ids"] for point in member["data_points"] or []
        ):
            continue
        branches = member.get("branches", [])
        if len(branches) > 1:
            continue
        columns = [
            [index for index, (name, unit) in enumerate(headers)
             if _key(name) == _key(series[axis]["quantity"] or "")
             and _key(unit or "") == _key(series[axis].get("unit_raw") or series[axis]["unit"] or "")]
            for axis in ("x", "y")
        ]
        if all(len(items) == 1 for items in columns):
            x, y = (items[0] for items in columns)
            if x != y and y in results and (not branches or _key(_heading(branches[0])[0]) == _key(headers[y][0])):
                curves.append((series, member, x, y, []))
    return curves


def _sample_factors(sample_ids, states, batches, columns, ref):
    """为同一批次的各状态保留实际区分行的变量。"""
    samples = {}
    for sample_id in sample_ids:
        state = states[sample_id]
        batch = batches[state["sample_batch_id"]]
        factors = {
            _key(key): value for values in (batch["factor_assignments"], state.get("state_factors", {}))
            for key, value in values.items() if _key(key) in columns
        }
        state_labels = [label for label in (state["reported_name"], state.get("state")) if label]
        labels = {_key(label) for label in [*state_labels, batch["reported_name"]] if label}
        if ref in state["evidence_ids"]:
            for column in columns - factors.keys():
                if not column.endswith("次数"):
                    continue
                phase = column.removesuffix("次数")
                names = [_key(label) for label in state_labels]
                ordinals = {match for label in names if phase in label for match in re.findall(r"第([1-9]\d*)次", label)}
                if phase and len(ordinals) == 1:
                    ordinal = ordinals.pop()
                    factors[column] = {"value": int(ordinal), "unit": None, "raw_text": f"第{ordinal}次"}
        samples[sample_id] = labels, factors
    return samples


def _has_state_axis(samples):
    """识别同一列确实记录了多个状态的不同取值。"""
    by_column = defaultdict(list)
    for _, factors in samples.values():
        for name, value in factors.items():
            prior = by_column[name]
            if prior and not _same_parameter(prior[0], value):
                return True
            prior.append(value)
    return False


def _sample_for_row(samples, conditions, name):
    """依据原表样品列或登记因素找到唯一状态。"""
    if not samples:
        return None, name is None
    conditions = {_key(key): value for key, value in conditions.items()}
    matches = []
    for sample_id, (labels, factors) in samples.items():
        if name is not None and _key(name) not in labels:
            continue
        if (name is not None or factors or len(samples) == 1) and all(
            _same_parameter(value, conditions[key]) for key, value in factors.items()
        ):
            matches.append(sample_id)
    if len(matches) != 1:
        return None, False
    selected = matches[0]
    if name is None and any(not factors for ref, (_, factors) in samples.items() if ref != selected):
        return None, False
    return selected, True


def _same_parameter(left, right):
    """比较登记因素和同行取值，保留单位及原文限制。"""
    left_unit = left.get("unit", left.get("unit_raw"))
    right_unit = right.get("unit", right.get("unit_raw"))
    if _key(left_unit or "") != _key(right_unit or ""):
        return False
    if type(left.get("value")) in (int, float) and type(right.get("value")) in (int, float):
        return left["value"] == right["value"] and left.get("operator") == right.get("operator")
    return _key(left["raw_text"]) == _key(right["raw_text"])


def _quantity(raw, *declared_units):
    """从原格读取完整标量或范围及其可核对单位。"""
    if len(raw) > 128:
        return None
    match = _QUANTITY.fullmatch(raw)
    if match is None:
        return None
    operator, low, high, cell_unit = match.groups()
    units = [unit for unit in (cell_unit, *declared_units) if unit]
    if len({_key(unit) for unit in units}) > 1:
        return None
    try:
        decimals = [Decimal("".join(value.replace("−", "-").split())) for value in (low, high) if value is not None]
    except (InvalidOperation, ValueError):
        return None
    approximations = [float(value) for value in decimals]
    if any(not math.isfinite(number) or number == 0 and original != 0
           for number, original in zip(approximations, decimals, strict=True)):
        return None
    if high is not None and (operator or decimals[0] > decimals[1]):
        return None
    numbers = [int(value) if value == value.to_integral_value() else number
               for value, number in zip(decimals, approximations, strict=True)]
    common = {"raw_text": raw, "unit_raw": units[0] if units else None}
    if high is not None:
        return {"kind": "range", "minimum": numbers[0], "maximum": numbers[1], **common}
    if operator:
        common["operator"] = operator
    return {"kind": "scalar", "value": numbers[0], **common}


def _heading(text):
    """识别表头或指标名中以斜杠分隔的单位。"""
    if match := _HEADING.fullmatch(text):
        name, bracket, plain = match.groups()
        return name.strip(), bracket or plain
    return text, None


def _metric_heading(text):
    """保留与指标单位一起写在同一格的分组限定词。"""
    if match := re.fullmatch(r"(.+?)\s*/\s*([%％‰])\s+(\S.*)", text):
        name, unit, qualifier = match.groups()
        return f"{name.strip()} {qualifier}", unit
    return _heading(text)


def _cell(text):
    """从原文单元格去掉布局标签，保留可见单位和数字。"""
    text = re.sub(r"<sup[^>]*>([^<]+)</sup>", r"^\1", text, flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def _key(text):
    """忽略排版空白和公式标记，保留原文大小写与符号。"""
    return re.sub(r"[\s$_{}^]+", "", unicodedata.normalize("NFKC", text)).replace("−", "-")


def _issue(ref, message, run_id=None):
    """登记可直接回看整表的校对项。"""
    return {
        "issue_id": f"issue:table:{uuid4().hex}", "issue_type": "table_mapping_review", "severity": "warning",
        "message": message, "record_ids": [run_id or ref], "evidence_ids": [ref],
    }
