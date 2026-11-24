"""从论文规范记录投影待审核的样品卡和原文。"""

from __future__ import annotations

import html
import re
import unicodedata
from decimal import Decimal, InvalidOperation

from yuxi.knowledge.adsorption_contract import Notice
from yuxi.knowledge.structure_tables import CellPosition, Span, table_cells

_NUMBER = re.compile(
    r"(?<![A-Za-z0-9_.])[+−-]?(?:\d+(?:\.\d{3}(?:[ \u00a0\u2009]\d{1,3})+|\.\d+)?|\.\d+)"
    r"(?:[eE][+−-]?\d+|\s*(?:\\times|×|⋅|·|x)\s*10\s*"
    r"(?:\^\s*\{?\s*[+−-]?\d+\s*\}?|[+−-]\d+|[⁻⁺⁰¹²³⁴⁵⁶⁷⁸⁹]+))?(?!\d|\.\d)"
)
_TEXTURAL_HEADERS = {
    "textural.surface_area.specific": re.compile(r"\bS?\s*BET\b|surface\s+area", re.IGNORECASE),
    "textural.pore_volume.total": re.compile(r"pore\s+volume|\bV\s*total\b", re.IGNORECASE),
}
_TEXTURAL_LABELS = {
    "textural.surface_area.specific": "BET 比表面积",
    "textural.pore_volume.total": "总孔容",
}
type Measurement = tuple[str, str, dict[str, CellPosition]]


class _UnverifiedObservation(ValueError):
    """模型给出的观测无法与所引原文核对。"""


def filter_observation_values(record: dict, sources: dict, markdown: str, notices: list[Notice]) -> dict:
    """保留有依据的观测及独立结果，并返回对应的审核显示值。"""
    states = {item["sample_state_id"]: item["reported_name"] for item in record["sample_states"]}
    sample_names = {name.casefold() for name in states.values()}
    entities = {item["entity_id"]: item["reported_name"] for item in record["chemical_entities"]}
    runs = {item["run_id"]: item for item in record["experiment_runs"]}
    batches = {item["sample_batch_id"]: item for item in record["sample_batches"]}
    sample_batches = {item["sample_state_id"]: batches[item["sample_batch_id"]] for item in record["sample_states"]}
    kept = []
    values = {}
    for index, observation in enumerate(record["observations"], 1):
        samples = [states[sample_id] for sample_id in _sample_ids(observation)]
        evidence = [sources[ref] for ref in observation["evidence_ids"]]
        results = observation.get("results")
        if isinstance(results, list):
            accepted = []
            try:
                measurements = _verified_measurements(
                    {**observation, "results": []}, evidence, samples, sample_names, entities, markdown,
                )
            except _UnverifiedObservation as error:
                notices.append(("unverified_observation", f"第 {index} 条观测已剔除。{error}",
                                {"collection": "observations", "record": observation}))
                continue
            for result_index, result in enumerate(results, 1):
                try:
                    projected = _verified_measurements(
                        {"observation_type": observation["observation_type"], "results": [result]},
                        evidence, samples, sample_names, entities, markdown,
                    )
                except _UnverifiedObservation as error:
                    label = _TEXTURAL_LABELS.get(result.get("metric_id")) if isinstance(result, dict) else None
                    location = f'观测 {observation["observation_id"]} 的第 {result_index} 个结果'
                    if label:
                        location += f"（{label}）"
                    notices.append(("unverified_observation", f"{location}已剔除。{error}",
                                    {"collection": "observations", "record": {**observation, "results": [result]}}))
                else:
                    if not projected:
                        notices.append((
                            "empty_observation_value", f"第 {index} 条观测的第 {result_index} 个结果为空。",
                            {"collection": "observations", "record": {**observation, "results": [result]}},
                        ))
                        continue
                    accepted.append(result)
                    measurements.extend(projected)
            observation = {**observation, "results": accepted}
        else:
            try:
                if observation["observation_type"] == "quantitative_measurement":
                    run = runs[observation["run_id"]]
                    sample_id = observation["sample_state_id"]
                    names = [*samples, sample_batches[sample_id]["reported_name"]] if sample_id else []
                    parameters = _table_parameters(observation, run, sample_batches)
                    measurements = [_verified_quantitative_measurement(
                        observation, evidence, names, parameters,
                    )]
                else:
                    measurements = _verified_measurements(
                        observation, evidence, samples, sample_names, entities, markdown,
                    )
            except _UnverifiedObservation as error:
                notices.append(("unverified_observation", f"第 {index} 条观测已剔除。{error}",
                                {"collection": "observations", "record": observation}))
                continue
        if not measurements:
            notices.append(("empty_observation_value", f"第 {index} 条观测没有可用结果。",
                            {"collection": "observations", "record": observation}))
            continue
        kept.append(observation)
        values[observation["observation_id"]] = measurements
    record["observations"] = kept
    return values


def project_experiment_cards(record: dict, sources: dict, measurement_values: dict) -> tuple[list[dict], list[dict]]:
    """把已校验的观测和原文位置映射到审核工作台。"""
    review_ids = {ref: f"e{index}" for index, ref in enumerate(sources, 1)}
    evidence = [
        {
            "evidence_id": review_ids[ref],
            **{key: value for key, value in item.items() if key not in {"section", "location", "origin"}},
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
        for label, text, cells in measurement_values[observation["observation_id"]]:
            measurement = {
                "canonical_id": observation["observation_id"],
                "property": field(label, refs), "result": field(text, refs),
                "method": field(method, run["evidence_ids"]) if method else None, "conditions": conditions,
            }
            if cells:
                measurement["table_cells"] = {review_ids[ref]: list(cell) for ref, cell in cells.items()}
            for sample_id in sample_ids:
                add(sample_id, "measurements", measurement, refs)
    return list(cards.values()), evidence


def _sample_ids(observation: dict) -> list[str]:
    """读取观测显式关联的样品状态编号。"""
    if "sample_state_id" in observation:
        return [observation["sample_state_id"]] if observation["sample_state_id"] is not None else []
    return observation.get("sample_state_ids", [])


def _verified_measurements(observation, evidence, samples, sample_names, entities, markdown):
    """校验可展示的数值，并保留可在引文中定位的写法。"""
    measurements = []
    for metric, result in _measurement_values(observation):
        text = _display_value(result)
        if text is None or (isinstance(text, str) and not text.strip()):
            continue
        if isinstance(result, str) and not any(_phrase(result) in _phrase(source["quote"]) for source in evidence):
            raise _UnverifiedObservation("定性结果无法在所引原文中定位。")
        label = _TEXTURAL_LABELS.get(metric, entities.get(metric, metric))
        if isinstance(result, dict) and result.get("kind") in {"scalar", "range"}:
            text = _ground_quantity(metric, result, evidence, samples, sample_names, markdown)
            if text != result["raw_text"] and result.get("unit_raw"):
                label = f'{label} ({result["unit_raw"]})'
        elif type(numeric := result.get("value") if isinstance(result, dict) else result) in {int, float}:
            text = _ground_quantity(
                metric, {"kind": "scalar", "value": numeric, "raw_text": str(text)},
                evidence, samples, sample_names, markdown,
            )
        measurements.append((label, text, {}))
    return measurements


def _verified_quantitative_measurement(
    observation: dict, evidence: list[dict], names: list[str], parameters: dict,
) -> Measurement:
    """规则表格核对交点，独立正文按原有数量与单位规则保留。"""
    value = observation["value"]
    numbers = (
        [Decimal(str(value["value"]))] if value["kind"] == "scalar"
        else [Decimal(str(value["minimum"])), Decimal(str(value["maximum"]))]
    )
    if len(numbers) == 2 and numbers[0] > numbers[1]:
        raise _UnverifiedObservation("数值范围的下界大于上界。")
    expected = set(numbers)
    if not expected <= _numbers(value["raw_text"]):
        raise _UnverifiedObservation("数值与原文写法不一致。")
    unit = value["unit_raw"]
    metric = observation["metric_id"]
    matches = {}
    table_raw = None
    prose_raw = None
    relevant_table = False
    for ref, source in zip(observation["evidence_ids"], evidence, strict=True):
        quote = source["quote"]
        grid = table_cells(quote, 0, len(quote))
        if grid is not None:
            relevant_table |= _table_name(metric) in _table_name(quote) and expected <= _numbers(_plain_cell(quote))
            if grid and (cell := _locate_table_value(quote, grid, metric, unit, expected, names, parameters)):
                matches[ref] = cell
                left, right = grid[cell[0]][cell[1]]
                table_raw = table_raw or _plain_cell(quote[left:right])
            continue
        if prose_raw is not None or not expected <= _numbers(quote) or (
            unit and not _unit_matches(unit, quote, case_sensitive=True)
        ):
            continue
        raw = value["raw_text"]
        if raw not in quote and len(numbers) == 1:
            raw = next(match.group() for match in _NUMBER.finditer(quote) if _number(match.group()) == numbers[0])
        prose_raw = raw
    if raw := table_raw or (prose_raw if not relevant_table else None):
        return (f"{metric} ({unit})" if unit and unit not in raw else metric), raw, matches
    if relevant_table:
        raise _UnverifiedObservation("无法在所引表格中唯一核对样品或条件、指标和数值。")
    raise _UnverifiedObservation("数值或已报告单位无法在所引原文中定位。")


def _table_parameters(observation: dict, run: dict, sample_batches: dict) -> dict:
    """用样品因素定位多样品表，单样品实验也可按本条实际条件定位。"""
    sample_id = observation["sample_state_id"]
    conditions = {**run["conditions"], **observation["conditions"]}
    if sample_id is None:
        return conditions
    factors = sample_batches[sample_id].get("factor_assignments", {})
    if len(run["sample_state_ids"]) == 1:
        return {**conditions, **factors}

    def parameter_value(item):
        """原文名写法不同的参数必须有相同的登记值与单位。"""
        return item["value"], item["unit"]

    other = [
        parameter_value(item) for other_id in run["sample_state_ids"] if other_id != sample_id
        for item in sample_batches[other_id].get("factor_assignments", {}).values()
    ]
    unique_factors = {key: item for key, item in factors.items() if parameter_value(item) not in other}
    own = [parameter_value(item) for item in unique_factors.values()]
    aliases = {
        key: item for key, item in conditions.items() if parameter_value(item) in own
    }
    return {**aliases, **unique_factors}


def _locate_table_value(quote, grid: list[list[Span]], metric, unit, expected, names, parameters) -> CellPosition | None:
    """在横表、纵表或明确的指标列与数值列中寻找唯一有依据的单元格。"""
    rows = [[_plain_cell(quote[left:right]) for left, right in row] for row in grid]
    header = rows[0]
    sample_names = {_table_name(name) for name in names}
    metric_name = _table_heading(metric, unit)[0]
    matches = []

    def axis_matches(label, heading):
        """有样品归属时要求名称或有依据的变量与轴对应。"""
        if _table_name(label) in sample_names:
            return True
        for name, parameter in parameters.items():
            key, unit_in_header = _table_heading(heading, parameter["unit"])
            if key != _table_heading(name, parameter["unit"])[0]:
                continue
            value = parameter["value"]
            if type(value) in {int, float}:
                if (number := _NUMBER.match(label)) and _number(number.group()) == Decimal(str(value)):
                    suffix = label[number.end():].strip()
                    return (
                        not suffix and (unit_in_header or not parameter["unit"])
                        or bool(parameter["unit"] and _unit_matches(parameter["unit"], suffix, exact=True, case_sensitive=True))
                    )
                return False
            return _table_name(label) in {_table_name(str(value).split("(", 1)[0]), _table_name(parameter["raw_text"])}
        return not sample_names

    columns = [_table_name(label) for label in header]
    metric_columns = [index for index, label in enumerate(columns) if label in {"指标", "metric"}]
    value_columns = [index for index, label in enumerate(columns) if label in {"数值", "结果", "value", "result"}]
    unit_columns = [index for index, label in enumerate(columns) if label in {"单位", "單位", "unit", "units"}]
    for row_index, row in enumerate(rows[1:], 1):
        if row[0] and _table_name(row[0]) == columns[0]:
            header = row
            continue
        for column, cell in enumerate(row[1:], 1):
            if expected != _numbers(cell):
                continue
            row_metric, row_unit = _table_heading(row[0], unit)
            col_metric, col_unit = _table_heading(header[column], unit)
            if row_metric == metric_name and axis_matches(header[column], header[0]):
                declared_unit = row_unit
            elif col_metric == metric_name and axis_matches(row[0], header[0]):
                declared_unit = col_unit
            elif value_columns == [column] and len(metric_columns) == 1 and axis_matches(row[0], header[0]):
                long_metric, declared_unit = _table_heading(row[metric_columns[0]], unit)
                if long_metric != metric_name:
                    continue
                if unit and len(unit_columns) == 1:
                    declared_unit |= _unit_matches(unit, row[unit_columns[0]], exact=True, case_sensitive=True)
            else:
                continue
            bare_number = re.fullmatch(r"[\s<>≤≥≈~+\-−–—±.,至到]*", re.sub(r"\bto\b", "", _NUMBER.sub("", cell)))
            if not unit or (declared_unit and bare_number) or _unit_matches(unit, cell, case_sensitive=True):
                matches.append((row_index, column))
    return matches[0] if len(matches) == 1 else None


def _plain_cell(text: str) -> str:
    """还原表格单元格的可见文字。"""
    return unicodedata.normalize("NFKC", html.unescape(re.sub(r"<[^>]+>", "", text))).strip()


def _table_name(text: str) -> str:
    """忽略表头的数学与排版标记。"""
    return _phrase(_table_label(_plain_cell(text)))


def _table_heading(text: str, unit: str | None) -> tuple[str, bool]:
    """分离指标名和明确写在末尾的单位，复合单位保留整体。"""
    text = _plain_cell(text).replace(r"\%", "%")
    if unit:
        bracket = re.fullmatch(r"(.+)\(([^()]*)\)\s*", text)
        if bracket and _unit_matches(unit, bracket[2], exact=True, case_sensitive=True):
            return _table_name(bracket[1]), True
        for slash in re.finditer("/", text):
            title, suffix = text[:slash.start()].strip(), text[slash.end():]
            if title and _unit_matches(unit, suffix, exact=True, case_sensitive=True):
                return _table_name(title), True
    return _table_name(text), False


def _measurement_values(observation: dict):
    """读取观测的结果字段，保留指标名或物种编号。"""
    kind = observation["observation_type"]
    results = observation.get("results")
    if isinstance(results, list):
        for result in results:
            if isinstance(result, str):
                yield kind, result
            elif "value" in result or "position" in result:
                label = result.get("metric_id") or result.get("entity_id") or result.get("peak_name")
                yield label or kind, result.get("value", result.get("position"))
            elif "binding_energy_eV" in result:
                yield result["assignment"], {"value": result["binding_energy_eV"], "unit": "eV"}
    elif isinstance(results, dict):
        yield from results.items()
    for key in (
        "value", "selectivity", "result", "reported_observation", "reported_order", "qualitative_result",
        "signed_value_from_figure", "magnitude_reported_in_prose", "interaction_distance_from_figure",
    ):
        if key in observation:
            yield observation.get("metric_id") or (kind if key in {"value", "result"} else key), observation[key]
    for text in observation.get("qualitative_results", []):
        yield "qualitative_results", text
    if kind == "equilibrium_uptake":
        yield kind, observation["loading"]


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


def _ground_quantity(
    metric: str, value: dict, sources: list[dict], samples: list[str], sample_names: set[str], markdown: str,
) -> str:
    """核对科学数值和所引出处，规则表格需有样品与数值交点。"""
    numbers = (
        [Decimal(str(value["value"]))] if value["kind"] == "scalar"
        else [Decimal(str(value["minimum"])), Decimal(str(value["maximum"]))]
    )
    raw = value["raw_text"]
    numeric_raw = raw.strip()
    if fence := re.fullmatch(r"(\${1,2})(.*?)\1", numeric_raw, re.S):
        numeric_raw = fence[2].strip()
    number_only = _NUMBER.fullmatch(numeric_raw) is not None
    expected = set(numbers)
    if not expected <= _numbers(raw):
        raise _UnverifiedObservation("数值与原文写法不一致。")
    metric_header = _TEXTURAL_HEADERS.get(metric)
    unit = value.get("unit_raw", "")
    if metric_header is not None and (
        not _unit_matches(unit, value.get("unit_canonical", ""))
        or (not number_only and not _unit_matches(unit, raw))
    ):
        raise _UnverifiedObservation("原始单位、规范单位与结果写法不一致。")
    if metric == "diffusion_time_constant" and (
        not unit or not _unit_matches(unit, value.get("unit_canonical", ""), exact=True)
        or (not number_only and not _unit_matches(unit, raw))
    ):
        raise _UnverifiedObservation("扩散时间常数的原文写法、原始单位与规范单位不一致。")
    for source in sources:
        quote = source["quote"]
        if not expected <= _numbers(quote):
            continue
        grid = table_cells(markdown, source["start_char"], source["end_char"])
        if grid and len(samples) == 1:
            sample = samples[0].strip().casefold()
            cells = [[html.unescape(re.sub(r"<[^>]+>", "", markdown[a:b])).strip() for a, b in row] for row in grid]
            for row_index, row in enumerate(grid[1:], 1):
                for column_index, (start, end) in enumerate(row[1:], 1):
                    row_label, column_label = cells[row_index][0], cells[0][column_index]
                    if row_label.casefold() == sample:
                        header = column_label
                    elif column_label.casefold() == sample:
                        header = row_label
                    else:
                        continue
                    cell = cells[row_index][column_index]
                    if metric_header is not None and (
                        not metric_header.search(_table_label(header)) or not _unit_matches(unit, f"{header} {cell}")
                    ):
                        continue
                    if metric == "diffusion_time_constant" and not _unit_matches(unit, f"{header} {cell}"):
                        continue
                    if expected <= _numbers(cell):
                        return markdown[start:end]
            continue
        if metric_header is not None and (
            grid is not None or not _matches_plain_textural(quote, metric_header, unit, expected, samples, sample_names)
        ):
            continue
        if metric == "diffusion_time_constant" and not any(
            _number(match.group()) == numbers[0] and _unit_matches(unit, quote[match.end():], prefix=True)
            for match in _NUMBER.finditer(quote)
        ):
            continue
        if raw in quote:
            return raw
        if len(numbers) == 1:
            return next(match.group() for match in _NUMBER.finditer(quote) if _number(match.group()) == numbers[0])
        return raw
    raise _UnverifiedObservation("数值或单位无法在所引原文中定位。")


def _matches_plain_textural(
    quote: str, metric: re.Pattern, unit: str, expected: set[Decimal], samples: list[str], sample_names: set[str],
) -> bool:
    """文字层表格按同一表头的样品顺序核对指标行。"""
    if len(samples) != 1:
        return False
    sample = samples[0].casefold()
    columns = []
    for line in quote.splitlines():
        names = sorted(
            (match.start(), name) for name in sample_names
            if (match := re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", line.casefold()))
        )
        if len(names) > 1:
            columns = [name for _, name in names]
        label = _table_label(line)
        if not (header := metric.search(label)) or not _unit_matches(unit, line):
            continue
        if columns:
            row = re.sub(r"^\s*\([^)]*\)", "", label[header.end():])
            values = [_number(match.group()) for match in _NUMBER.finditer(row)]
            if len(columns) == len(values) and sample in columns and expected == {values[columns.index(sample)]}:
                return True
        elif len(names) == 1 and names[0][1] == sample:
            for match in _NUMBER.finditer(line):
                if expected == {_number(match.group())} and _unit_matches(unit, line[match.end():], prefix=True):
                    return True
    return False


def _table_label(text: str) -> str:
    """去掉表头的 Markdown 数学标记以核对指标。"""
    return re.sub(r"[$_{}]", "", text)


def _unit_matches(
    unit: str, text: str, *, prefix: bool = False, exact: bool = False, case_sensitive: bool = False,
) -> bool:
    """在表头或单元格中核对原始单位，接受上标和倒数写法。"""
    def normalize(value):
        """统一单位中常见的数学标记。"""
        value = unicodedata.normalize("NFKC", html.unescape(value)).replace("−", "-")
        if not case_sensitive:
            value = value.casefold()
        value = re.sub(r"[\^${}]", "", value).replace("·", " ").replace("⋅", " ")
        value = re.sub(r"(?<=[A-Za-z])\s+(?=[+-]?\d)|\s*(/)\s*", r"\1", value)
        value = re.sub(r"\b(s|min|h)\s*-\s*1\b", r"1/\1", value)
        return re.sub(r"(\d)\s*(kg|g)\s*-\s*1\b", r"\1/\2", value).strip()

    normalized = normalize(unit)
    if exact:
        return bool(normalized and normalized == normalize(text))
    start = "^" if prefix else r"(?<![A-Za-z/])"
    return bool(normalized and re.search(rf"{start}{re.escape(normalized)}(?![A-Za-z0-9/+-])", normalize(text)))


def _phrase(text: str) -> str:
    """保留字面内容并忽略 PDF 排版空白。"""
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def _numbers(text: str) -> set[Decimal]:
    """按完整数字边界匹配，保留正负号并忽略末尾零差异。"""
    return {_number(match.group()) for match in _NUMBER.finditer(text)}


def _number(value: str) -> Decimal:
    """把原文乘十幂、上标指数和数学负号转换为十进制数。"""
    value = unicodedata.normalize("NFKC", value).replace("−", "-")
    value = re.sub(r"\s*(?:\\times|×|⋅|·|x)\s*10\s*\^?\s*\{?\s*([+-]?\d+)\s*\}?", r"e\1", value)
    value = re.sub(r"[ \u00a0\u2009]", "", value)
    try:
        return Decimal(value)
    except InvalidOperation as error:
        raise _UnverifiedObservation("无法读取所引数值。") from error
