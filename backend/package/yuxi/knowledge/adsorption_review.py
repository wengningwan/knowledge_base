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
type Measurement = tuple[str, str, dict[str, CellPosition], list[str]]


class _UnverifiedObservation(ValueError):
    """模型给出的观测无法与所引原文核对。"""


def filter_observation_values(record: dict, sources: dict, notices: list[Notice]) -> dict:
    """保留有依据的观测及独立结果，并返回对应的审核显示值。"""
    states = {item["sample_state_id"]: item["reported_name"] for item in record["sample_states"]}
    runs = {item["run_id"]: item for item in record["experiment_runs"]}
    batches = {item["sample_batch_id"]: item for item in record["sample_batches"]}
    sample_batches = {item["sample_state_id"]: batches[item["sample_batch_id"]] for item in record["sample_states"]}
    kept = []
    values = {}
    for index, observation in enumerate(record["observations"], 1):
        run = runs[observation["run_id"]]
        sample_id = observation.get("sample_state_id")
        names = [states[sample_id], sample_batches[sample_id]["reported_name"]] if sample_id else []
        other_names = [name for ref, name in states.items() if sample_id and ref != sample_id and name not in names]
        conditions = {key: value for key, value in observation["conditions"].items()
                      if value != run["conditions"].get(key)}
        parameters = _table_parameters(observation, run, sample_batches)
        grouped = "results" in observation
        accepted, measurements = [], []
        results = observation["results"] if grouped else [observation]
        for result in results:
            sources_by_ref = {ref: sources[ref] for ref in result["evidence_ids"]}
            proved = []
            try:
                if "value" in result:
                    proved.append(_verified_quantitative_measurement(
                        result, sources_by_ref, names, other_names, len(run["sample_state_ids"]) > 1,
                        parameters, conditions,
                    ))
                if "result" in result:
                    phrase = _phrase(result["result"])
                    refs = [ref for ref, source in sources_by_ref.items()
                            if phrase and phrase in _phrase(source["quote"])]
                    if not refs:
                        raise _UnverifiedObservation("定性结果无法在所引原文中定位。")
                    proved.append((result["metric_id"], result["result"], {}, refs))
            except _UnverifiedObservation as error:
                candidate = (
                    {**observation, "results": [result], "evidence_ids": result["evidence_ids"]}
                    if grouped else observation
                )
                notices.append(("unverified_observation", f'第 {index} 条观测的 {result["metric_id"]} 已剔除。{error}',
                                {"collection": "observations", "record": candidate}))
            else:
                if grouped:
                    proved_refs = list(dict.fromkeys(ref for _, _, _, refs in proved for ref in refs))
                    accepted.append({**result, "evidence_ids": proved_refs})
                measurements.extend(proved)
        if not measurements:
            if not results:
                notices.append(("empty_observation_value", f"第 {index} 条观测没有可用结果。"))
            continue
        if grouped:
            member_refs = {ref for result in results for ref in result["evidence_ids"]}
            context_refs = [ref for ref in observation["evidence_ids"] if ref not in member_refs]
            proved_refs = [ref for result in accepted for ref in result["evidence_ids"]]
            observation = {**observation, "results": accepted,
                           "evidence_ids": list(dict.fromkeys([*context_refs, *proved_refs]))}
        kept.append(observation)
        values[observation["observation_id"]] = measurements
    record["observations"] = kept
    return values


def filter_curve_points(record: dict, sources: dict, markdown: str, notices: list[Notice]) -> None:
    """只发布原文表格中能按坐标轴和曲线归属核对的点。"""
    samples = {item["sample_state_id"]: item["reported_name"] for item in record["sample_states"]}
    entities = {item["entity_id"]: item["reported_name"] for item in record["chemical_entities"]}
    for series in record["series_sets"]:
        axes = series["x"], series["y"]
        members = series["series_members"]
        for member in members:
            names = samples.get(member["sample_state_id"]), entities.get(member["entity_id"])
            labels = [name for name in names if name]
            branches = member.get("branches", [])
            if len(branches) == 1:
                labels.append(branches[0])
            conditions = member.get("conditions", {})
            if member.get("label") and (len(members) > 1 or not labels and not conditions):
                labels.append(member["label"])
            points = member["data_points"] or []
            kept = []
            if len(branches) <= 1 and (len(members) == 1 or labels or conditions):
                for point in points:
                    refs = [ref for ref in point["evidence_ids"]
                            if _reported_curve_point(point, axes, sources[ref], markdown, labels, conditions)]
                    if refs:
                        kept.append({**point, "evidence_ids": refs})
            if len(kept) != len(points):
                notices.append(("unverified_curve_point", f'{series["series_set_id"]} 的部分坐标缺少可核对的数表或曲线归属。'))
            member["data_points"] = kept or None
        source = series.setdefault("source", {})
        if any(member["data_points"] for member in members):
            source["data_availability"] = "reported_points"
        elif any(table_cells(sources[ref]["quote"], 0, len(sources[ref]["quote"])) is not None
                 for ref in series["evidence_ids"]):
            source["data_availability"] = "needs_review"
        elif source.get("figure") or any(sources[ref]["origin"] == "figure" for ref in series["evidence_ids"]):
            source["data_availability"] = "figure_only_not_digitized"
        else:
            source["data_availability"] = "not_reported"


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


def _reported_curve_point(point, axes, source, markdown, labels, conditions) -> bool:
    """在有明确轴列、条件和样品标签的同一行核对坐标。"""
    grid = table_cells(markdown, source["start_char"], source["end_char"])
    if not grid:
        return False
    rows = [[_plain_cell(markdown[start:end]) for start, end in row] for row in grid]
    header = rows[0]
    columns = []
    for axis in axes:
        unit = axis.get("unit_raw", axis["unit"])
        if unit == "1":
            unit = None
        titles = {_table_name(axis[key]) for key in ("quantity", "symbol") if axis.get(key)}
        matches = [(index, has_unit) for index, cell in enumerate(header)
                   for name, has_unit in [_table_heading(cell, unit)] if name in titles]
        if len(matches) != 1:
            return False
        columns.append((*matches[0], unit))
    if columns[0][0] == columns[1][0]:
        return False
    for row in rows[1:]:
        cells = {_table_name(cell) for cell in row}
        if not {_table_name(label) for label in labels} <= cells:
            continue
        if not all(
            _table_condition_matches(name, value, header, row, required=True) for name, value in conditions.items()
        ):
            continue
        if all(_numbers(row[index]) == {Decimal(str(point[axis]))}
               and (not unit or has_unit or _unit_matches(unit, row[index], case_sensitive=True))
               for axis, (index, has_unit, unit) in zip(("x", "y"), columns, strict=True)):
            return True
    return False


def _table_condition_matches(name, parameter, header, row, *, required) -> bool:
    """先识别条件列，再核对同行的值和原单位。"""
    unit = parameter.get("unit", parameter.get("unit_raw"))
    value = parameter.get("value")
    expected = {Decimal(str(value))} if type(value) in (int, float) else _numbers(parameter["raw_text"])
    wanted, _ = _table_heading(name, unit)
    base_name = _table_name(re.sub(r"\s*\([^()]*\)\s*$", "", name).split("/", 1)[0])
    for title, cell in zip(header, row, strict=True):
        heading, has_unit = _table_heading(title, unit)
        title_without_unit = re.sub(r"\s*\([^()]*\)\s*$", "", title).split("/", 1)[0]
        base_heading = _table_name(title_without_unit)
        if {wanted, base_name}.isdisjoint({heading, base_heading}):
            continue
        if unit and not has_unit and (
            heading != base_heading or not _unit_matches(unit, cell, case_sensitive=True)
        ):
            return False
        return _table_name(cell) == _table_name(parameter["raw_text"]) or bool(expected and expected == _numbers(cell))
    return not required


def _sample_ids(observation: dict) -> list[str]:
    """读取观测显式关联的样品状态编号。"""
    if "sample_state_id" in observation:
        return [observation["sample_state_id"]] if observation["sample_state_id"] is not None else []
    return observation.get("sample_state_ids", [])


def _verified_quantitative_measurement(
    observation: dict, evidence: dict, names: list[str], other_names: list[str], multiple_samples: bool,
    parameters: dict, conditions: dict,
) -> Measurement:
    """规则表格核对交点，正文核对包含归属和原值的同一句。"""
    value = observation["value"]
    numbers = (
        [Decimal(str(value["value"]))] if value["kind"] == "scalar"
        else [Decimal(str(value["minimum"])), Decimal(str(value["maximum"]))]
    )
    if len(numbers) == 2 and numbers[0] > numbers[1]:
        raise _UnverifiedObservation("数值范围的下界大于上界。")
    expected = set(numbers)
    if "uncertainty" in value:
        expected.add(Decimal(str(value["uncertainty"])))
    if not expected <= _numbers(value["raw_text"]):
        raise _UnverifiedObservation("数值与原文写法不一致。")
    unit = value["unit_raw"]
    metric = observation["metric_id"]
    metric_name = _table_heading(metric, unit)[0]
    literal = re.compile(rf"(?<![\d.]){re.escape(_phrase(value['raw_text']))}(?!\d|\.\d)")
    matches = {}
    table_raw = None
    prose_ref = None
    relevant_table = False
    for ref, source in evidence.items():
        quote = source["quote"]
        grid = table_cells(quote, 0, len(quote))
        if grid is not None:
            relevant_table |= _table_name(metric) in _table_name(quote) and expected <= _numbers(_plain_cell(quote))
            cell = _locate_table_value(quote, grid, metric, unit, expected, names, parameters, conditions) if grid else None
            if cell:
                matches[ref] = cell
                left, right = grid[cell[0]][cell[1]]
                table_raw = table_raw or _plain_cell(quote[left:right])
            continue
        if prose_ref is not None:
            continue
        for sentence in re.split(r"[。！？\n]+|(?<=[.!?])\s+", quote):
            clauses = re.split(r"(?<!\d),(?!\d)|[，；;]|\band\b", sentence) if value["kind"] == "scalar" else [sentence]
            if not any(metric_name in _table_name(clause) and expected <= _numbers(clause)
                       and literal.search(_phrase(clause)) for clause in clauses):
                continue
            if unit and not _unit_matches(unit, sentence, case_sensitive=True):
                continue
            if (names and multiple_samples and not any(_mentions_sample(sentence, name) for name in names)) or any(
                _mentions_sample(sentence, name) for name in other_names
            ):
                continue
            if not _sentence_conditions_match(sentence, expected, unit, conditions):
                continue
            prose_ref = ref
            break
    if raw := table_raw or (value["raw_text"] if prose_ref and not relevant_table else None):
        label = f"{metric} ({unit})" if unit and not _unit_matches(unit, raw, case_sensitive=True) else metric
        return label, raw, matches, list(matches) if matches else [prose_ref]
    if relevant_table:
        raise _UnverifiedObservation("无法在所引表格中唯一核对样品或条件、指标和数值。")
    raise _UnverifiedObservation("数值或已报告单位无法在所引原文中定位。")


def _mentions_sample(sentence: str, name: str) -> bool:
    """按样品名边界定位正文，避免 S1 匹配 S10。"""
    label = r"\s+".join(re.escape(part) for part in unicodedata.normalize("NFKC", name).split())
    return bool(label and re.search(rf"(?<![A-Za-z0-9_-]){label}(?![A-Za-z0-9_-])",
                                    unicodedata.normalize("NFKC", sentence), re.I))


def _sentence_conditions_match(
    sentence: str, result_numbers: set[Decimal], result_unit: str | None, conditions: dict,
) -> bool:
    """差异条件需要原文支持，无法区分同单位多组数值时留给校对。"""
    text = _phrase(sentence)
    known = result_numbers | {number for value in conditions.values() for number in _numbers(value["raw_text"])}
    units = {value.get("unit", value.get("unit_raw")) for value in conditions.values()} | {result_unit}
    for value in conditions.values():
        if _phrase(value["raw_text"]) not in text:
            return False
    for match in _NUMBER.finditer(sentence):
        if _number(match.group()) not in known and any(
            _unit_matches(unit, sentence[match.end():], prefix=True, case_sensitive=True) for unit in units if unit
        ):
            return False
    return True


def _table_parameters(observation: dict, run: dict, sample_batches: dict) -> dict:
    """用样品因素定位多样品表，单样品实验也可按本条实际条件定位。"""
    sample_id = observation.get("sample_state_id")
    conditions = {
        key: {**item, "unit": item.get("unit", item.get("unit_raw"))}
        for key, item in {**run["conditions"], **observation["conditions"]}.items() if "value" in item
    }
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


def _locate_table_value(
    quote, grid: list[list[Span]], metric, unit, expected, names, parameters, conditions,
) -> CellPosition | None:
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
        if not all(
            _table_condition_matches(name, value, header, row, required=False) for name, value in conditions.items()
        ):
            continue
        for column, cell in enumerate(row[1:], 1):
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
    if len(matches) == 1:
        row, column = matches[0]
        if _numbers(rows[row][column]) == expected:
            return row, column
    return None


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
