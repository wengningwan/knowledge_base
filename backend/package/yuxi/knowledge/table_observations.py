"""将数表列的用途映射为带原文单元格的实验观测。"""

from __future__ import annotations

import html
import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, DecimalException, InvalidOperation
from itertools import takewhile
from uuid import uuid4

from yuxi.knowledge.adsorption_review import Measurement
from yuxi.knowledge.paper_observation_merge import discard_wrong_table_scopes, grounded_observation_members
from yuxi.knowledge.structure_tables import TableGrid, table_grid

_NUMBER = (
    r"[+−-]?(?:(?:\d{1,3}(?:[ \u00a0\u2009\u202f]\d{3})+|\d+)(?:\.\d+(?:[ \u00a0\u2009\u202f]\d+)*)?|\.\d+)"
    r"(?:[eE][+−-]?\d+)?"
)
_UNIT_TOKEN = r"[a-zA-ZµμΩÅÅ℃℉°%％‰]+(?:\^?[+−-]\d+|[⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+)?"
_UNIT = rf"{_UNIT_TOKEN}(?:(?:\s*[·⋅/×]\s*|\s+){_UNIT_TOKEN})*"
_HEADING = re.compile(rf"(.+?)\s*/\s*(?:[（(]\s*({_UNIT})\s*[）)]|({_UNIT}))")
_QUANTITY = re.compile(rf"([<≤>≥≈~]?)[ ]*({_NUMBER})(?:\s*[~～–—至-]\s*({_NUMBER}))?\s*({_UNIT})?")
_EMPTY = {"", "-", "−", "–", "—"}
_EXHIBITS = {
    "figure": re.compile(r"(?:图|\bfig(?:ure)?\.?)\s*([a-z]?\d+[a-z]?)(?!\w)", re.I | re.ASCII),
    "table": re.compile(r"(?:表|\btable)\s*([a-z]?\d+[a-z]?)(?!\w)", re.I | re.ASCII),
}


@dataclass(frozen=True)
class _Column:
    """保留表头原文路径、量名和可继承的报告单位。"""

    label: str
    names: tuple[str, ...]
    unit: str | None

    @property
    def name(self) -> str:
        """用完整表头路径区分不同组的同名子列。"""
        return " | ".join(self.names)


def table_columns(text: str) -> list[str]:
    """向抽取模型提供从原文可定位的完整列头。"""
    grid = table_grid(text, 0, len(text))
    return [item.label for item in _table_headers(text, grid)] if grid else []


def project_table_observations(
    record: dict, sources: dict, mappings: list[dict], measurements: dict[str, list[Measurement]],
) -> tuple[list[dict], set[tuple[str, str]]]:
    """按已核验的列用途取数，返回疑点以及由程序核对的实验与数表。"""
    runs = {item["run_id"]: item for item in record["experiment_runs"]}
    series_sets = {item["series_set_id"]: item for item in record["series_sets"]}
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
        grid = table_grid(text, 0, len(text))
        headers = _table_headers(text, grid) if grid else []
        columns = [item.label for item in headers]
        selected = [*mapping["condition_columns"], *mapping["result_columns"], *mapping.get("ignored_columns", [])]
        selected.extend(mapping[key] for key in ("metric_column", "sample_column") if key in mapping)
        keys = [_key(column) for column in columns]
        roles = [_key(column) for column in selected]
        entity_id = mapping.get("entity_id")
        if (
            len(proposals) != 1 or grid is None or not columns
            or (entity_id is not None and entity_id not in run["species_entity_ids"])
            or "" in roles or len(set(roles)) != len(roles) or any(keys.count(role) != 1 for role in roles)
        ):
            issues.append(_issue(ref, "数表的实验、物质或列用途无法唯一确认。", run["run_id"]))
            continue

        positions = {column: keys.index(_key(column)) for column in selected}
        sample_ids = run["sample_state_ids"]
        conditions = [positions[column] for column in mapping["condition_columns"]]
        results = [positions[column] for column in mapping["result_columns"]]
        metric = positions.get(mapping.get("metric_column"))
        sample = positions.get(mapping.get("sample_column"))
        scope = mapping.get("sample_scope")
        experiment_level = scope == "experiment"
        condition_names = {_key(headers[column].name) for column in conditions}
        samples = _sample_factors(sample_ids, states, batches, condition_names, ref)
        row_states = {item["row"]: item for item in mapping.get("row_states", [])}
        column_states = {item["column"]: item["sample_state_id"] for item in mapping.get("column_states", [])}
        state_labels = {sample_id: _state_labels(states[sample_id]) for sample_id in sample_ids}
        column_candidates = {
            column: [
                sample_id for sample_id, labels in state_labels.items() if _key(headers[column].names[-1]) in labels
            ]
            for column in results
        }
        if (
            len(row_states) != len(mapping.get("row_states", []))
            or len(column_states) != len(mapping.get("column_states", []))
            or row_states and (
                metric is None or any(not grid.header_rows <= row < len(grid.rows) for row in row_states)
            )
            or column_states and (
                set(column_states) != set(results)
                or any(column_candidates[column] != ([] if state_id is None else [state_id])
                       for column, state_id in column_states.items())
            )
            or not column_states and any(column_candidates.values())
            or sum((bool(row_states), bool(column_states), sample is not None)) > 1
        ):
            issues.append(_issue(ref, "数表中指定的行列样品归属无法唯一确认。", run["run_id"]))
            continue
        if experiment_level and (sample is not None or row_states or column_states or _has_state_axis(samples)):
            issues.append(_issue(ref, "数表声明为实验级数据，但列用途包含区分样品状态的变量。", run["run_id"]))
            continue
        if scope == "state" and not samples:
            issues.append(_issue(ref, "数表声明按样品状态取数，但实验没有登记样品状态。", run["run_id"]))
            continue
        covered_tables.add((run["run_id"], ref))
        bindings = mapping.get("curve_bindings", [])
        curve_results = results if metric is None and not column_states else []
        curves = _table_curves(
            bindings, series_sets, run, sources, ref, headers, conditions, curve_results,
            experiment_level, entity_id,
        )
        unmapped = set(range(len(headers))) - set(positions.values())
        omitted = set()
        skipped_rows = 0
        unverified_baselines = 0
        rows = [[_cell(text[left:right]) for left, right in row] for row in grid.rows]
        row_contexts = _row_contexts(row_states, rows, grid, metric, sample_ids, states, batches, ref, results, headers)
        scoped = scope is not None or bool(row_states or column_states)
        ignored = [positions[column] for column in mapping.get("ignored_columns", [])]
        carried_metric = None
        for row_index in range(grid.header_rows, len(rows)):
            row, origins = rows[row_index], grid.origins[row_index]
            omitted.update(column for column in unmapped if _quantity(row[column], headers[column].unit) is not None)
            active = [column for column in results if row[column] not in _EMPTY and origins[column][0] == row_index]
            if not active:
                carried_metric = None
                continue
            row_conditions = {}
            for column in conditions:
                name, unit, raw = headers[column].name, headers[column].unit, row[column]
                if origins[column][1] != column:
                    continue
                if quantity := _quantity(raw, unit):
                    row_conditions[name] = quantity
                elif unit is None and raw not in _EMPTY:
                    row_conditions[name] = {"raw_text": raw, "value": raw, "unit": None}
            if len(row_conditions) != len(conditions):
                carried_metric = None
                skipped_rows += 1
                continue
            raw_metric = row[metric] if metric is not None else ""
            baseline = None
            comparison_row = False
            if row_states:
                context = row_contexts.get(row_index)
                if context is None:
                    carried_metric = None
                    skipped_rows += 1
                    continue
                sample_id, raw_metric, factor, baseline = context
                comparison_row = "baseline_row" in row_states[row_index]
                unverified_baselines += comparison_row and baseline is None
                if factor and any(
                    _key(name) == _key(factor[0]) and not _same_parameter(parameter, factor[1])
                    for name, parameter in row_conditions.items()
                ):
                    carried_metric = None
                    skipped_rows += 1
                    continue
                if factor:
                    row_conditions[factor[0]] = factor[1]
                resolved = _row_matches_state(samples[sample_id][1], row_conditions)
            elif experiment_level or column_states:
                sample_id, resolved = None, True
            else:
                sample_name = row[sample] if sample is not None else None
                sample_id, resolved = _sample_for_row(samples, row_conditions, sample_name)

            if not resolved or metric is not None and (origins[metric][1] != metric or raw_metric in _EMPTY):
                carried_metric = None
                skipped_rows += 1
                continue
            metric_name, metric_unit, metric_basis = _metric_heading(raw_metric)
            if metric is not None and (interval := _quantity(metric_name)) is not None:
                previous = _quantity(carried_metric[2]) if carried_metric is not None else None
                if not (
                    previous and interval["unit_raw"] and _unit_key(interval["unit_raw"]) == _unit_key(previous["unit_raw"])
                    and ignored and all(row[column] in _EMPTY for column in ignored)
                ):
                    carried_metric = None
                    skipped_rows += 1
                    continue
                metric_name, metric_unit, metric_basis = carried_metric[0], carried_metric[1], raw_metric
            else:
                carried_metric = (metric_name, metric_unit, metric_basis) if metric_basis else None

            by_state: dict[str | None, list[tuple[int, dict, Measurement]]] = defaultdict(list)
            quantities = {}
            unresolved = False
            for column in active:
                header = headers[column]
                column_sample = column_states.get(column, sample_id)
                comparison_column = bool(column_states) and column_sample is None
                if (
                    not _unmerged_cell(grid, row_index, column)
                    or column_sample is not None and not _row_matches_state(samples[column_sample][1], row_conditions)
                ):
                    unresolved = True
                    continue
                quantity = _quantity(row[column], header.unit, None if comparison_column else metric_unit)
                if quantity is None:
                    unresolved = True
                    continue
                quantities[column] = quantity
                cell = (run["run_id"], ref, (row_index, column))
                if scoped:
                    resolved_cells[cell] = column_sample
                if cell in used_cells and (not scoped or column_sample in used_cells[cell]):
                    continue
                name, basis = _result_identity(
                    header, metric_name, metric_basis, len(results), bool(column_states),
                    comparison_column, bool(row_states), comparison_row,
                )
                if basis:
                    quantity["basis"] = basis
                label = " ".join([name, *(f"({part})" for part in (basis, quantity["unit_raw"]) if part)])
                member = {"metric_id": name, "value": quantity, "evidence_ids": [ref]}
                proof = (label, row[column], {ref: (row_index, column)}, [ref])
                by_state[column_sample].append((column, member, proof))
            row_series = []
            for series, member, x_column, y_column, points in curves:
                if member["sample_state_id"] != sample_id:
                    continue
                if any(
                    not _same_parameter(value, row_conditions[name])
                    for name, value in member.get("conditions", {}).items() if name in row_conditions
                ):
                    continue
                x = _quantity(row[x_column], headers[x_column].unit)
                y = quantities.get(y_column)
                if x and y and all(
                    item["kind"] == "scalar" and "operator" not in item
                    and _unit_key(item["unit_raw"]) == _unit_key(headers[column].unit)
                    for item, column in ((x, x_column), (y, y_column))
                ) and origins[x_column] == (row_index, x_column):
                    points.append({"x": x["value"], "y": y["value"], "evidence_ids": [ref]})
                    row_series.append(series["series_set_id"])
                elif row[y_column] not in _EMPTY:
                    unresolved = True
            if unresolved:
                skipped_rows += 1
            for state_id, members in by_state.items():
                suffix = f":{members[0][0]}" if column_states else ""
                observation_id = f"obs:table:{ref}:{row_index}{suffix}"
                observation = {
                    "observation_id": observation_id, "observation_type": "result_group", "run_id": run["run_id"],
                    "sample_state_id": state_id, "entity_id": entity_id, "conditions": row_conditions,
                    "results": [member for _, member, _ in members], "evidence_ids": [ref],
                }
                if baseline:
                    observation["condition_context"] = f"相对{baseline}"
                if len(row_series) == 1 and len(results) == 1:
                    observation["series_set_id"] = row_series[0]
                record["observations"].append(observation)
                measurements[observation_id] = [proof for _, _, proof in members]
        for series, member, _, _, points in curves:
            if points:
                member["data_points"] = points + [
                    point for point in member["data_points"] or [] if ref not in point["evidence_ids"]
                ]
                series["source"]["data_availability"] = "reported_points"
        messages = []
        if len(curves) != len(bindings) or any(not points for _, _, _, _, points in curves):
            messages.append("曲线与原表列的对应关系无法核对。")
        if skipped_rows:
            messages.append(f"有 {skipped_rows} 行的结果、单位或样品归属需核对。")
        if unverified_baselines:
            messages.append(f"有 {unverified_baselines} 行的变化率比较基准需核对。")
        if omitted:
            messages.append("含数值的列尚未分配用途：" + "、".join(columns[index] for index in sorted(omitted)) + "。")
        if messages:
            issues.append(_issue(ref, "".join(messages), run["run_id"]))
    if resolved_cells:
        discard_wrong_table_scopes(record, measurements, resolved_cells)
    return issues, covered_tables


def _table_curves(bindings, series_sets, run, sources, ref, headers, conditions, results, experiment_level, entity_id):
    """核对显式轴列绑定及曲线来源。"""
    curves = []
    target_ids = Counter(binding["series_set_id"] for binding in bindings)
    for binding in bindings:
        series_id = binding["series_set_id"]
        series = series_sets.get(series_id)
        x, y = binding["x_column"], binding["y_column"]
        if (
            target_ids[series_id] != 1 or series is None or series["run_id"] != run["run_id"]
            or x not in conditions or y not in results or len(series["series_members"]) != 1
            or series.get("axis_consistency_status") == "needs_review"
        ):
            continue
        refs = set(series["evidence_ids"]) | set(series["x"].get("evidence_ids", [])) | set(
            series["y"].get("evidence_ids", [])
        )
        if ref not in refs:
            continue
        member = series["series_members"][0]
        if (
            member["entity_id"] not in (None, entity_id) or experiment_level and member["sample_state_id"] is not None
            or len(member.get("branches", [])) > 1
        ):
            continue
        bridge, bridge_ref = _curve_prose_link(binding, series, sources, ref, refs | set(run["evidence_ids"]))
        if (
            _axis_matches_column(series["x"], x, conditions, headers, bridge)
            and _axis_matches_column(series["y"], y, results, headers, bridge)
        ):
            if bridge_ref and bridge_ref not in series["evidence_ids"]:
                series["evidence_ids"].append(bridge_ref)
            curves.append((series, member, x, y, []))
    return curves


def _curve_prose_link(binding, series, sources, table_ref, allowed):
    """找出实验原文中同时指向这一图和这一表的单一段落。"""
    figure = _EXHIBITS["figure"].findall(unicodedata.normalize("NFKC", series["source"].get("figure", "")))
    table = _EXHIBITS["table"].findall(unicodedata.normalize("NFKC", sources[table_ref]["location"]))
    if len(figure) != 1 or len(table) != 1:
        return "", None
    for ref in binding.get("evidence_ids", []):
        source = sources.get(ref)
        if ref not in allowed or source is None or source["origin"] != "text":
            continue
        for paragraph in re.split(r"\n\s*\n", unicodedata.normalize("NFKC", source["quote"]).casefold()):
            if (
                set(_EXHIBITS["figure"].findall(paragraph)) == {figure[0].casefold()}
                and set(_EXHIBITS["table"].findall(paragraph)) == {table[0].casefold()}
            ):
                return _key(paragraph), ref
    return "", None


def _axis_matches_column(axis, column, role_columns, headers, bridge):
    """先核对原名或括注，再核对相连图表中可唯一对应的量。"""
    name = _key(axis["quantity"] or "")
    unit = _unit_key(axis.get("unit_raw") or axis["unit"])
    if not name or _unit_key(headers[column].unit) != unit:
        return False
    exact = [index for index, header in enumerate(headers) if _key(header.name) == name]
    if exact:
        return exact == [column]
    aliases = re.findall(r"\(([^()]+)\)", name)
    parentheticals = [
        index for index, header in enumerate(headers)
        if _key(header.name) in aliases or name in re.findall(r"\(([^()]+)\)", _key(header.name))
    ]
    if parentheticals:
        return parentheticals == [column]
    compatible = [index for index in role_columns if _unit_key(headers[index].unit) == unit]
    heading = _key(headers[column].name)
    shared = "".join(left for left, _ in takewhile(lambda pair: pair[0] == pair[1], zip(name, heading)))
    return bool(
        len(shared) > 1 and compatible == [column] and shared.casefold() in bridge
        and not (name.casefold() in bridge and heading.casefold() in bridge)
    )


def _table_headers(text: str, grid: TableGrid) -> list[_Column]:
    """沿每列的表头路径读取量名和报告单位。"""
    if not grid.rows:
        return []
    headers = []
    for spans in zip(*grid.rows[:grid.header_rows], strict=True):
        pieces = [raw for left, right in dict.fromkeys(spans) if (raw := _cell(text[left:right]))]
        levels = [_heading(raw) for raw in pieces]
        units = {_unit_key(unit): unit for _, unit in levels if unit}
        if len(units) > 1:
            return []
        headers.append(_Column(" | ".join(pieces), tuple(name for name, _ in levels), next(iter(units.values()), None)))
    return headers


def _row_contexts(bindings, rows, grid, metric, sample_ids, states, batches, ref, results, headers):
    """核对原行标签及相对变化行所引用的基准行。"""
    if not bindings:
        return {}
    checked = {}
    for row, binding in bindings.items():
        sample_id = binding["sample_state_id"]
        label = binding["label"]
        if grid.origins[row][metric] != (row, metric) or sample_id not in sample_ids or not label.strip():
            continue
        prefix = r"\s*".join(re.escape(part) for part in label.split())
        match = re.fullmatch(rf"({prefix})\s+(\S.*)", rows[row][metric], flags=re.S)
        if match is None:
            continue
        literal, suffix = match.groups()
        quantity = _quantity(literal)
        matched = {}
        for candidate in sample_ids:
            state = states[candidate]
            batch = batches[state["sample_batch_id"]]
            factors = {**batch["factor_assignments"], **state.get("state_factors", {})}
            supporting = [name for name, factor in factors.items() if quantity and _same_parameter(factor, quantity)]
            if len(supporting) == 1:
                matched[candidate] = supporting[0], quantity
            elif not supporting and _key(literal) in _state_labels(state) and ref in state["evidence_ids"]:
                matched[candidate] = None
        if list(matched) == [sample_id]:
            checked[row] = sample_id, suffix, matched[sample_id]

    measured = {row: identity[0] for row, identity in checked.items() if "baseline_row" not in bindings[row]}
    contexts = {}
    for row, (sample_id, suffix, factor) in checked.items():
        baseline = bindings[row].get("baseline_row")
        baseline_label = None
        subject_rows = [index for index, owner in measured.items() if index < row and owner == sample_id]
        if baseline is not None and len(subject_rows) == 1:
            subject = subject_rows[0]
            possible = [
                index for index, owner in measured.items() if index < subject and owner != sample_id
                and _relative_change_matches(rows, grid, results, headers, row, subject, index)
            ]
            if possible == [baseline]:
                baseline_label = bindings[baseline]["label"]
        contexts[row] = sample_id, suffix, factor, baseline_label
    return contexts


def _relative_change_matches(rows, grid, results, headers, change, subject, baseline):
    """用三行同列的报告精度核对相对变化，保留原表数值。"""
    compared = False
    for column in results:
        if rows[change][column] in _EMPTY:
            continue
        if not all(_unmerged_cell(grid, index, column) for index in (change, subject, baseline)):
            return False
        reported, current, prior = (_printed_interval(rows[index][column]) for index in (change, subject, baseline))
        if reported is None or current is None or prior is None:
            return False
        lo, hi, raw_unit = reported
        unit = _unit_key(raw_unit)
        header_unit = _unit_key(headers[column].unit)
        measured_units = {_unit_key(item[2]) or header_unit for item in (current, prior)}
        if (
            (unit or header_unit) != "%" or unit and header_unit and unit != header_unit
            or len(measured_units) != 1 or header_unit and header_unit not in measured_units
        ):
            return False
        if prior[0] <= 0 <= prior[1]:
            return False
        try:
            possible = [(amount / base - 1) * 100 for amount in current[:2] for base in prior[:2]]
        except DecimalException:
            return False
        if hi < min(possible) or lo > max(possible):
            return False
        compared = True
    return compared


def _printed_interval(raw):
    """将明确标量的末位精度转成可核对的舍入范围。"""
    match = _QUANTITY.fullmatch(raw)
    if not match or match[1] or match[3]:
        return None
    try:
        value = Decimal("".join(match[2].replace("−", "-").split()))
        half = Decimal(5).scaleb(value.as_tuple().exponent - 1)
        return value - half, value + half, match[4]
    except DecimalException:
        return None


def _unmerged_cell(grid, row, column):
    """只有单独原格可以核对一个测量或比较值。"""
    origin = (row, column)
    return (
        grid.origins[row][column] == origin
        and (column + 1 == len(grid.origins[row]) or grid.origins[row][column + 1] != origin)
        and (row + 1 == len(grid.origins) or grid.origins[row + 1][column] != origin)
    )


def _result_identity(header, metric, basis, result_count, column_states, comparison_column, row_states, comparison_row):
    """保留原行指标、同组子列和比较结果各自的计量口径。"""
    if not metric:
        return header.name, None
    if comparison_column:
        return f"{metric} {header.name}", basis
    if row_states:
        if len(header.names) > 1:
            return metric, " | ".join(header.names[1:])
        return (f"{header.name} {metric}" if comparison_row else header.name), basis
    if column_states or result_count == 1:
        return metric, basis
    return metric, f"{basis}（{header.name}）" if basis else header.name


def _row_matches_state(factors, conditions):
    """拒绝同行条件与已登记样品变量冲突的结果。"""
    return all(
        _same_parameter(factors[key], value) for name, value in conditions.items() if (key := _key(name)) in factors
    )


def _state_labels(state):
    """保留登记名称及其原文括注中的可辨别名称。"""
    labels = [label for label in (state["reported_name"], state.get("state")) if label]
    return {
        _key(name) for label in labels
        for name in [label, *re.split(r"[()（）]", label)] if name.strip()
    }


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
    if _unit_key(left_unit) != _unit_key(right_unit):
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
    if len({_unit_key(unit) for unit in units}) > 1:
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
        return name.strip(), unit, qualifier
    return (*_heading(text), None)


def _cell(text):
    """从原文单元格去掉布局标签，保留可见单位和数字。"""
    text = re.sub(r"<sup[^>]*>([^<]+)</sup>", r"^\1", text, flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def _key(text):
    """忽略排版空白和公式标记，保留原文大小写与符号。"""
    return re.sub(r"[\s$_{}^]+", "", unicodedata.normalize("NFKC", text)).replace("−", "-")


def _unit_key(text):
    """统一可等价的单位写法，保留单位大小写和数量级。"""
    if text is None:
        return ""
    key = _key(text).replace(r"\%", "%")
    if key.startswith("(") and key.endswith(")"):
        key = key[1:-1]
    key = re.sub(r"[·⋅]([a-zA-ZµμÅ]+)-1$", r"/\1", key)
    return "" if key == "1" else key


def _issue(ref, message, run_id=None):
    """登记可直接回看整表的校对项。"""
    return {
        "issue_id": f"issue:table:{uuid4().hex}", "issue_type": "table_mapping_review", "severity": "warning",
        "message": message, "record_ids": [run_id or ref], "evidence_ids": [ref],
    }
