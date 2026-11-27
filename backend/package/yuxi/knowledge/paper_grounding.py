"""按论文原文核对观测，并由已核对的数表观测生成曲线点。"""

from __future__ import annotations

import html
import re
import unicodedata
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from yuxi.knowledge.adsorption_contract import Notice
from yuxi.knowledge.adsorption_review import Measurement
from yuxi.knowledge.structure_tables import CellPosition, table_cells, table_grid

_NUMBER = re.compile(
    r"(?<![A-Za-z0-9_.])[+-]?(?:(?:\d{1,3}(?: \d{3})+|\d+)(?:\.\d+(?: \d{1,3})*)?|\.\d+)"
    r"(?:[eE][+-]?\d+|\s*(?:\\times|×|⋅|·|x)\s*10\s*\^?\s*\{?\s*[+-]?\d+\s*\}?)?"
    r"(?!\d|\.\d)"
)
_METRIC_COLUMNS = {"项目", "指标", "检测项目", "测定项目", "metric", "parameter", "property"}
_VALUE_COLUMNS = {"值", "数值", "结果", "检测值", "测定值", "检测结果", "value", "result", "measurement"}
_SAMPLE_COLUMNS = {"样品", "样本", "样品编号", "样品名称", "材料", "sample", "samplename", "sampleid", "material"}
_ESTIMATE = re.compile(r"推算|估算|估计|\bestimat(?:e|ed|ion)\b|\bproject(?:ed|ion)\b", re.I)
_DIRECT_MEASUREMENT = re.compile(r"实测|测得|实地测量|\bmeasured\b", re.I)
_TABLE_CAPTION = re.compile(r"^(?:表\s*|Table\s+)\d+", re.I)
_CALLOUT = re.compile(r"[=:：]|为|达到|\b(?:is|was|reached)\b", re.I)
_RESPECTIVE = re.compile(r"分别|\brespectively\b", re.I)
_PROSE_QUANTITY_LINK = re.compile(
    r"\s*(?:(?:高达|达到|约为|为|降至|增至|升至|达|等于|=|:|(?:is|was|were|reached|of)\b)\s*)?"
    r"(?P<operator><=|>=|[<>≤≥≈~≃]|约|approximately\b)?\s*",
    re.I,
)

type TableMeasurement = tuple[dict, dict, str, CellPosition]


def paper_table_rows(quote: str) -> list[list[str]] | None:
    """按原文保留逻辑表格的行列，待核表格返回空列表。"""
    spans = table_cells(quote, 0, len(quote))
    if spans is None:
        return None
    return [[_visible(quote[start:end]) for start, end in row] for row in spans]


def ground_paper_results(
    record: dict, sources: dict, subjects_by_run: dict[str, set[str]] | None = None,
) -> tuple[dict[str, list[Measurement]], list[Notice]]:
    """整理已验证的观测和曲线，并保留需重新核对的候选。"""
    tables = {}
    for ref, source in sources.items():
        quote = source["quote"]
        grid = table_grid(quote, 0, len(quote))
        if grid is None:
            continue
        # 多层表头和合并单元格由整表映射核对。
        tables[ref] = []
        if grid.header_rows == 1 and all(
            origin == (row, column) for row, origins in enumerate(grid.origins) for column, origin in enumerate(origins)
        ):
            tables[ref] = [[_visible(quote[start:end]) for start, end in row] for row in grid.rows]
    runs = {item["run_id"]: item for item in record["experiment_runs"]}
    series_by_id = {item["series_set_id"]: item for item in record["series_sets"]}
    states = {item["sample_state_id"]: item for item in record["sample_states"]}
    batches = {item["sample_batch_id"]: item for item in record["sample_batches"]}
    samples = {}
    for ref, item in states.items():
        batch = batches[item["sample_batch_id"]]
        state_factors = item.get("state_factors", {})
        samples[ref] = (item["reported_name"], {
            **batch, "state_factors": state_factors,
            "factor_assignments": {**batch["factor_assignments"], **state_factors},
        })
    notices: list[Notice] = []
    measurements: dict[str, list[Measurement]] = {}
    rows_by_run: dict[str, list[TableMeasurement]] = defaultdict(list)
    kept = []
    for observation in record["observations"]:
        run = runs[observation["run_id"]]
        subjects = (subjects_by_run or {}).get(run["run_id"], set())
        grouped = "results" in observation
        results = observation["results"] if grouped else [observation]
        member_refs = {ref for result in results for ref in result["evidence_ids"]}
        accepted, proved = [], []
        for result in results:
            try:
                checked, reason = _ground_result(result, observation, run, samples, sources, tables, subjects)
            except InvalidOperation:
                checked, reason = [], "所引数值格式超出可核对范围。"
            if checked and "fit_result" in result and _fit_series_conflicts(result, observation, run, series_by_id, subjects):
                checked, reason = [], "正文拟合主体与所关联曲线的目标不一致。"
            if not checked:
                rejected = {key: value for key, value in result.items() if key not in {"source_text", "fit_result"}}
                candidate = {**observation, "results": [rejected]} if grouped else rejected
                notices.append((
                    "unverified_observation",
                    f'{observation["observation_id"]} 的 {result["metric_id"]} 无法核对。{reason}',
                    {"collection": "observations", "record": candidate},
                ))
                continue
            member = {key: value for key, value in result.items() if key != "source_cell" or "fit_result" in result}
            member["evidence_ids"] = list(dict.fromkeys(ref for _, _, _, refs in checked for ref in refs))
            _, raw, cells, _ = checked[0]
            if cells and "value" in member:
                member["value"] = {**member["value"], "raw_text": raw}
                rows_by_run[run["run_id"]].extend((observation, member, ref, cell) for ref, cell in cells.items())
            accepted.append(member)
            proved.extend(checked)

        if not accepted:
            if not results:
                notices.append((
                    "empty_observation_value", f'{observation["observation_id"]} 没有可用结果。',
                    {"collection": "observations", "record": observation},
                ))
            continue
        if grouped:
            context = [ref for ref in observation["evidence_ids"] if ref not in member_refs]
            refs = [ref for member in accepted for ref in member["evidence_ids"]]
            kept.append({**observation, "results": accepted, "evidence_ids": list(dict.fromkeys([*context, *refs]))})
        else:
            kept.extend(accepted)
        measurements[observation["observation_id"]] = proved

    record["observations"] = kept
    for observation in kept:
        if series := series_by_id.get(observation.get("series_set_id")):
            refs = series["observation_ids"]
            if observation["observation_id"] not in refs:
                refs.append(observation["observation_id"])
    _ground_curve_points(record["series_sets"], rows_by_run, sources, tables, notices)
    return measurements, notices


def _ground_result(result, observation, run, samples, sources, tables, subjects) -> tuple[list[Measurement], str]:
    """按坐标核对数表结果，或在所引原文中定位报告原值。"""
    refs = result["evidence_ids"]
    metric = result["metric_id"]
    value = result.get("value")
    anchor = result.get("source_text")
    if (anchor and (not value or "source_cell" in result)) or (
        "fit_result" in result and (not value or not (anchor or "source_cell" in result))
    ):
        return [], "正文核对需要一个独立的数值陈述出处。"
    basis = value.get("basis") if value else None
    display_metric = f"{metric} ({basis})" if basis else metric
    if value:
        expected = _quantity_numbers(value)
        raw_text = value["raw_text"]
        unit = value["unit_raw"]
        if expected is None or not set(expected) <= set(_numbers(raw_text)):
            return [], "数值与报告原值不一致。"

    checked: list[Measurement] = []
    if "source_cell" in result:
        anchor = result["source_cell"]
        ref, row, column = anchor["evidence_id"], anchor["row"], anchor["column"]
        rows = tables.get(ref)
        if ref not in refs or not rows or not 0 < row < len(rows) or not 0 <= column < len(rows[row]):
            return [], "所引表格、行或列无法定位。"
        if not _table_result_matches(result, observation, run, samples, rows, row, column):
            return [], "该单元格的原值、单位、指标、样品或条件与观测不一致。"
        raw = rows[row][column]
        label = (
            f"{display_metric} ({unit})"
            if value and _unit_key(unit) and not _number_unit_present(expected, unit, raw) else display_metric
        )
        checked.append((label, raw, {ref: (row, column)}, [ref]))
    elif value:
        if anchor:
            ref = anchor["evidence_id"]
            if ref not in refs or ref in tables or ref not in sources or not _anchored_prose_matches(
                anchor, result, observation, run, samples, sources[ref], expected, subjects,
            ):
                return [], "所引分句未直接支持指标、主体、原值或单位。"
            proved = [ref]
        else:
            visual_refs = [ref for ref in refs if sources[ref]["origin"] in {"figure", "formula"}]
            proved = _prose_references(raw_text, metric, expected, unit, observation, run, samples, visual_refs, sources, tables)
        if not proved:
            return [], "正文需要原文分句定位，数表需要单元格位置，图和公式需要直接标注报告值。"
        label = (f"{display_metric} ({unit})"
                 if _unit_key(unit) and not _number_unit_present(expected, unit, raw_text) else display_metric)
        checked.append((label, raw_text, {}, proved))

    if (phrase := result.get("result")) and (value or "source_cell" not in result):
        proved = _prose_references(phrase, None, None, None, observation, run, samples, refs, sources, tables)
        if not proved:
            return [], "所引原文未直接包含报告结果。"
        checked.append((metric, phrase, {}, proved))
    return checked, ""


def _anchored_prose_matches(anchor, result, observation, run, samples, source, expected, subjects) -> bool:
    """核对同一原文分句中的指标、直接报告值和上下文主体。"""
    if source["origin"] != "text":
        return False
    metric, subject = anchor["metric"], anchor.get("subject")
    literal_metric = _name(metric, case_sensitive=True).replace("的", "")
    literal_subject = _name(subject, case_sensitive=True) if subject is not None else None
    display = _name(result["metric_id"], case_sensitive=True).replace("的", "")
    if not literal_metric or literal_metric not in display or (
        literal_subject is not None and (not literal_subject or literal_subject not in display)
    ):
        return False
    if "fit_result" in result and subjects and not subject:
        return False
    value = result["value"]
    phrase = _number_text(anchor["quote"])
    raw = _literal_pattern(value["raw_text"])
    if not raw or not raw.search(_compact(phrase)):
        return False
    metric_pattern = _prose_label(metric)
    metric_match = metric_pattern.search(phrase)
    if not metric_match:
        return False
    tail = phrase[metric_match.end():]
    # 报告量必须紧跟指标和连接词，不能取分句中另一个指标或条件的数值。
    link = _PROSE_QUANTITY_LINK.match(tail)
    parsed, unit = _numeric_cell(tail[link.end():])
    if parsed != expected or _unit_key(unit) != _unit_key(value["unit_raw"]):
        return False
    operator = link["operator"] or ("约" if "约为" in link[0] else "")
    if _operator_key(operator) != _operator_key(value.get("operator", "")):
        return False

    data_origin = observation.get("data_origin", "")
    measured = (_DIRECT_MEASUREMENT.search(data_origin) or "测量" in data_origin) and not _ESTIMATE.search(data_origin)
    if measured and "fit_result" in result:
        return False
    own_names, other_names = _prose_sample_names(observation, run, samples)
    state_scope = _distinct_prose_state(observation.get("sample_state_id"), run, samples)
    quote = re.compile(r"\s*".join(re.escape(part) for part in phrase.split()))
    subject_pattern = _prose_label(subject) if subject else None
    competitor_patterns = [
        _prose_label(candidate) for candidate in subjects if _name(candidate) and _name(candidate) != _name(subject or "")
    ]
    sentence = ""
    for source_sentence, _ in _literal_sources(source):
        preceding, sentence = sentence, _number_text(source_sentence)
        competing = own_names and any(_mentions_sample(sentence, name) for name in other_names)
        for clause in re.finditer(r"[^,;]+", sentence):
            statement = clause[0]
            found = quote.search(statement)
            if not found or _RESPECTIVE.search(statement) or (
                measured and (_ESTIMATE.search(statement) or re.search(r"拟合|计算|可求出|\b(?:fit|calculated)\b", statement, re.I))
            ):
                continue
            if competing and (
                _RESPECTIVE.search(sentence) or not any(_mentions_sample(statement, name) for name in own_names)
                or any(_mentions_sample(statement, name) for name in other_names)
            ):
                continue
            actual_metric = metric_pattern.search(found[0])
            if not actual_metric:
                continue
            metric_start = found.start() + actual_metric.start()
            if state_scope:
                name, factors = state_scope
                context = _compact(statement[:metric_start])
                if not _mentions_sample(statement, name) and not any(factor.search(context) for factor in factors):
                    continue
            actual_metric_end = found.start() + actual_metric.end()
            source_tail = statement[actual_metric_end:]
            source_amount = source_tail[_PROSE_QUANTITY_LINK.match(source_tail).end():]
            observed, _ = _numeric_cell(source_amount)
            following = statement[found.end():]
            if observed != expected or re.match(r"[A-Za-z0-9_]|\s*[%‰°℃℉/·⋅^×]|\.\d", following):
                continue
            if re.match(r"\s+(?:[A-Za-zµμÅ]+\s+)*[A-Za-zµμÅ]+\s*\^?[+-]\d", following):
                continue
            if not unit and re.match(r"\s+[A-Za-zµμÅ][A-Za-z0-9µμÅ]*\s*(?:[/^·]|[.)\s]*$)", following):
                continue
            if subject_pattern is None or _prose_subject_matches(
                sentence, preceding, subject_pattern, competitor_patterns,
                clause.start() + metric_start, clause.start() + actual_metric_end,
            ):
                return True
    return False


def _prose_subject_matches(sentence, preceding, subject, competitors, metric_start, metric_end) -> bool:
    """核对最近的明确主体，无新主体时允许紧邻前句延续。"""
    matches = list(subject.finditer(sentence, 0, metric_end))
    if not matches and preceding and not subject.search(sentence) and not any(
        pattern.search(sentence) for pattern in competitors
    ):
        sentence, metric_start = preceding, len(preceding)
        matches = list(subject.finditer(sentence))
    if not matches:
        return False
    subject_end = matches[-1].end()
    if subject_end >= metric_start:
        return True
    return not any(pattern.search(sentence, subject_end, metric_start) for pattern in competitors)


def _distinct_prose_state(sample_id, run, samples) -> tuple[str, list[re.Pattern]] | None:
    """提取同批次状态需要在数值分句直接出现的区别信息。"""
    if not sample_id:
        return None
    name, sample = samples[sample_id]
    competitors = [samples[ref] for ref in run["sample_state_ids"]
                   if ref != sample_id and samples[ref][1]["sample_batch_id"] == sample["sample_batch_id"]]
    if not competitors:
        return None
    if _name(name) in {_name(sample["reported_name"]), *(_name(label) for label, _ in competitors)}:
        name = ""
    factors = []
    for key, factor in sample.get("state_factors", {}).items():
        raw = _compact(factor["raw_text"])
        pattern = _literal_pattern(factor["raw_text"])
        if pattern and all(
            raw != _compact(other.get("factor_assignments", {}).get(key, {}).get("raw_text", ""))
            for _, other in competitors
        ):
            factors.append(pattern)
    return name, factors


def _fit_series_conflicts(result, observation, run, series_by_id, subjects) -> bool:
    """对比同一实验同类曲线的明确目标，避免拟合结果挂到另一目标。"""
    subject = result.get("source_text", {}).get("subject")
    series = series_by_id.get(observation.get("series_set_id"))
    if not subject or not series:
        return False
    label = _prose_label(subject)
    selected = _fit_series_labels(series, observation)
    if any(label.search(text) for text in selected):
        return False
    for candidate in subjects:
        if _name(candidate) != _name(subject) and any(_prose_label(candidate).search(text) for text in selected):
            return True
    for series_id in run["series_set_ids"]:
        if series_id not in series_by_id or series_id == series["series_set_id"]:
            continue
        alternatives = _fit_series_labels(series_by_id[series_id], observation)
        if selected[2] and label.fullmatch(alternatives[2]):
            return True
        for actual, alternate in zip(selected[:2], alternatives[:2], strict=True):
            if match := label.search(alternate):
                prefix, suffix = alternate[:match.start()], alternate[match.end():]
                if prefix and suffix and actual.startswith(prefix) and actual.endswith(suffix):
                    return True
    return False


def _fit_series_labels(series: dict, observation: dict) -> tuple[str, str, str]:
    """读取同时描述拟合曲线及拟合目标的标签。"""
    members = [
        member["label"] for member in series["series_members"] if member.get("label") and all(
            observation.get(field) is None or member[field] == observation[field]
            for field in ("sample_state_id", "entity_id")
        )
    ]
    member = members[0] if len(members) == 1 else ""
    return _number_text(series["series_kind"]), _number_text(series["y"]["quantity"] or ""), _number_text(member)


def _table_result_matches(result, observation, run, samples, rows, row_index, column) -> bool:
    """仅在锚定行列验证指标、值、单位及显式归属。"""
    header, row = rows[0], rows[row_index]
    value, metric = result.get("value", {}), result["metric_id"]
    unit, cell = value.get("unit_raw"), row[column]
    headings = [_name(title) for title in header]
    metric_fields = [index for index, title in enumerate(headings) if title in _METRIC_COLUMNS]
    row_metric = next((row[index] for index in (metric_fields or [0]) if index != column
                       and _heading_matches(row[index], metric, unit)), None)
    basis = value.get("basis")
    by_column = _heading_matches(header[column], metric, unit)
    by_row = row_metric is not None and (
        headings[column] in _VALUE_COLUMNS or (basis and _heading_matches(header[column], basis, unit))
    )
    sample_id = observation.get("sample_state_id")
    names, batch = samples.get(sample_id, ("", {}))
    own_labels = {_name(name) for name in (names, batch.get("reported_name", "")) if name}
    by_sample_column = row_metric is not None and _name(header[column]) in own_labels
    if not (by_column or by_row or by_sample_column):
        return False
    if basis and row_metric is not None and not by_column and not by_sample_column:
        if not _heading_matches(header[column], basis, unit):
            return False

    unit_labels = [row[index] for index, title in enumerate(headings) if title in {"单位", "unit", "units"}]
    unit_labels.extend(_heading_units(header[column], metric if by_column else basis or header[column], unit))
    if row_metric is not None:
        unit_labels.extend(_heading_units(row_metric, metric, unit))
    if value and not _table_value_matches(value, cell, unit_labels, exact_raw=True):
        return False
    if not value and _compact(result["result"]) != _compact(cell):
        return False

    conditions = {**run["conditions"], **observation["conditions"]}
    factors = batch.get("factor_assignments", {})
    condition_matches = [_table_condition(name, item, header, row, exclude=column) for name, item in conditions.items()]
    factor_matches = [_table_condition(name, item, header, row, exclude=column) for name, item in factors.items()]
    if False in condition_matches or False in factor_matches:
        return False
    sample_fields = [index for index, title in enumerate(headings) if title in _SAMPLE_COLUMNS]
    labels = {_name(row[index]) for index in sample_fields or [0]}
    if by_sample_column:
        labels.add(_name(header[column]))
    matched_sample = bool(labels & own_labels)
    conflicting_samples = {
        ref for ref, (name, other_batch) in samples.items() if ref != sample_id
        and labels & {_name(label) for label in (name, other_batch["reported_name"]) if label.strip()}
    }
    if matched_sample and not conflicting_samples.intersection(run["sample_state_ids"]):
        return True
    if not matched_sample and (sample_fields or conflicting_samples):
        return False

    if len(run["sample_state_ids"]) > 1:
        # 多样品表必须由当前样品特有且在同行出现的因素定位。
        matched_factors = {name for name, matched in zip(factors, factor_matches, strict=True) if matched is True}
        if not matched_factors:
            return False
        for ref in run["sample_state_ids"]:
            if ref == sample_id:
                continue
            other_factors = samples[ref][1].get("factor_assignments", {})
            comparable = matched_factors & other_factors.keys()
            if comparable and all(_table_condition(name, other_factors[name], header, row, exclude=column) is True
                                  for name in comparable):
                return False
    # 随条件变化的横表需要至少一个可核对的同行条件或样品因素。
    if by_column and column != 0 and len(rows) > 2 and all(_numeric_cell(item[0])[0] for item in rows[1:]):
        return True in condition_matches or True in factor_matches
    return True


def _prose_references(raw, metric, expected, unit, observation, run, samples, refs, sources, tables) -> list[str]:
    """定位正文、图表标题或公式中的字面报告。"""
    own_names, other_names = _prose_sample_names(observation, run, samples)
    literal = _literal_pattern(raw)
    if literal is None:
        return []
    proved = []
    data_origin = observation.get("data_origin", "")
    claimed_measurement = _DIRECT_MEASUREMENT.search(data_origin) and not _ESTIMATE.search(data_origin)
    if claimed_measurement and any(_ESTIMATE.search(run.get(field, "")) for field in ("run_type", "method")):
        return []
    for ref in refs:
        source = sources.get(ref)
        if source is None or ref in tables:
            continue
        for sentence, origin in _literal_sources(source):
            if metric and _name(metric).replace("的", "") not in _name(sentence).replace("的", ""):
                continue
            if origin == "callout" and (not metric or not _unit_key(unit)):
                continue
            if origin == "formula" and expected:
                expression = _number_text(sentence).removeprefix(r"\[").removesuffix(r"\]").strip("$ ")
                name, equals, right = expression.partition("=")
                parsed, suffix = _numeric_cell(right.strip("$ "))
                if not equals or _name(name) != _name(metric) or parsed != expected or _unit_key(suffix) != _unit_key(unit):
                    continue
            competing_samples = own_names and any(_mentions_sample(sentence, name) for name in other_names)
            if competing_samples and _RESPECTIVE.search(sentence):
                continue
            sentence = _number_text(sentence)
            characters = [(index, char) for index, original in enumerate(sentence)
                          if not original.isspace() for char in original.casefold()]
            for match in literal.finditer("".join(char for _, char in characters)):
                start, end = characters[match.start()][0], characters[match.end() - 1][0] + 1
                clauses = r"[,，;；]"
                prefix = re.split(clauses, sentence[:start])[-1]
                suffix = re.split(clauses, sentence[end:], maxsplit=1)[0]
                statement = prefix + sentence[start:end] + suffix
                if competing_samples and (
                    not any(_mentions_sample(statement, name) for name in own_names)
                    or any(_mentions_sample(statement, name) for name in other_names)
                ):
                    continue
                if expected and _unit_key(unit) and not _number_unit_present(
                    expected, unit, sentence[start:], before=end - start,
                ):
                    continue
                if origin == "callout" and not _printed_callout(metric, expected, prefix + sentence[start:end]):
                    continue
                if claimed_measurement and _ESTIMATE.search(prefix + sentence[start:end]):
                    continue
                proved.append(ref)
                break
            if proved and proved[-1] == ref:
                break
    return proved


def _prose_sample_names(observation, run, samples) -> tuple[set[str], set[str]]:
    """按当前观测归属列出同一实验的样品名及可能混淆的样品名。"""
    sample_ids = observation.get("sample_state_ids", [])
    if sample_id := observation.get("sample_state_id"):
        sample_ids = [sample_id]
    own_names = {name for ref in sample_ids for name in (samples[ref][0], samples[ref][1]["reported_name"])}
    other_names = {name for ref in run["sample_state_ids"] if ref not in sample_ids
                   for name in (samples[ref][0], samples[ref][1]["reported_name"])} - own_names
    return own_names, other_names


def _literal_sources(source: dict):
    """从同一来源保留正文或明确印在图中的标注。"""
    origin = source["origin"]
    quote = source["quote"].lstrip()
    if (origin in {"text", "formula"} or (origin == "figure" and not quote.startswith(("![", "<img")))
            or (origin == "table" and _TABLE_CAPTION.match(quote))):
        for sentence in re.split(r"[。！？\n]+|(?<=[.!?])\s+(?=[A-Z])", _visible(quote)):
            yield sentence, origin
    if origin == "figure" and (figure_text := source.get("figure_text")):
        for line in _visible(figure_text).splitlines():
            yield line, "callout"


def _printed_callout(metric, expected, statement) -> bool:
    """只使用图内直接把量名和报告值连在一起的标注。"""
    markers = list(_CALLOUT.finditer(statement))
    if not markers:
        return False
    last = markers[-1]
    start = markers[-2].end() if len(markers) > 1 else 0
    label = statement[start:last.start()]
    values, _ = _numeric_cell(statement[last.end():])
    return _name(metric) in _name(label) and values == expected


def _ground_curve_points(series_sets, rows_by_run, sources, tables, notices) -> None:
    """同一实验、样品及数表行同时支持两轴时才发布坐标。"""
    for series in series_sets:
        series_id = series["series_set_id"]
        source = series["source"]
        refs = set(series["evidence_ids"])
        refs.update(series["x"].get("evidence_ids", []))
        refs.update(series["y"].get("evidence_ids", []))
        members = series["series_members"]
        points = [defaultdict(list) for _ in members]
        linked_observations = dict.fromkeys(series["observation_ids"])
        if series.get("axis_consistency_status") != "needs_review":
            for observation, result, ref, (row, column) in rows_by_run[series["run_id"]]:
                if ref not in refs or (observation.get("series_set_id") or series_id) != series_id:
                    continue
                value = result["value"]
                if (value["kind"] != "scalar" or _operator_key(value.get("operator", ""))
                        or not _axis_matches(series["y"], result["metric_id"], value["unit_raw"])):
                    continue
                header, cells = tables[ref][0], tables[ref][row]
                x_values = [
                    condition["value"] for name, condition in observation["conditions"].items()
                    if type(condition.get("value")) in (int, float)
                    and not _operator_key(condition.get("operator", ""))
                    and _axis_matches(series["x"], name, condition.get("unit", condition.get("unit_raw")))
                    and _table_condition(name, condition, header, cells, exclude=column) is True
                ]
                if len(set(x_values)) != 1:
                    continue
                matches = [index for index, member in enumerate(members)
                           if _curve_member_matches(member, observation, result, header, cells)]
                if len(matches) == 1:
                    points[matches[0]][(x_values[0], value["value"])].append(ref)
                    linked_observations[observation["observation_id"]] = None

        series["observation_ids"] = list(linked_observations)

        rejected = []
        for member, by_point in zip(members, points, strict=True):
            unsupported = [
                point
                for point in member["data_points"] or []
                if not set(point["evidence_ids"]) <= set(by_point.get((point["x"], point["y"]), []))
            ]
            if unsupported:
                rejected.append({**member, "data_points": unsupported})
            member["data_points"] = [
                {"x": x, "y": y, "evidence_ids": list(dict.fromkeys(point_refs))}
                for (x, y), point_refs in sorted(by_point.items())
            ] or None
        if rejected:
            notices.append((
                "unverified_curve_point", f"{series_id} 有坐标缺少同一数表行与曲线归属的支持。",
                {"collection": "series_sets", "record": {**series, "series_members": rejected}},
            ))
        if any(points):
            source["data_availability"] = "reported_points"
        elif (refs.intersection(tables) or any(sources[ref]["origin"] == "table" for ref in refs)
              or series.get("axis_consistency_status") == "needs_review"):
            source["data_availability"] = "needs_review"
        elif source.get("figure") or any(sources[ref]["origin"] == "figure" for ref in refs):
            source["data_availability"] = "figure_only_not_digitized"
        else:
            source["data_availability"] = "not_reported"


def _curve_member_matches(member, observation, result, header, row) -> bool:
    """对照曲线成员声明的样品、物质和分支。"""
    if member["sample_state_id"] != observation.get("sample_state_id"):
        return False
    if member["entity_id"] and member["entity_id"] != (result.get("entity_id") or observation.get("entity_id")):
        return False
    branches = member.get("branches", [])
    if branches and (len(branches) != 1 or _name(branches[0]) not in {_name(text) for text in [*header, *row]}):
        return False
    return all(_table_condition(name, condition, header, row) is not False
               for name, condition in member.get("conditions", {}).items())


def _axis_matches(axis: dict, name: str, unit: str | None) -> bool:
    """坐标轴与原报告量同名且单位等同。"""
    return _unit_key(axis.get("unit_raw", axis["unit"])) == _unit_key(unit) and any(
        _heading_matches(name, axis[key], unit) for key in ("quantity", "symbol") if axis.get(key)
    )


def _table_condition(name, parameter, header, row, *, exclude=None) -> bool | None:
    """核对原表中同名列的条件，缺列与不一致分别返回。"""
    unit = parameter.get("unit", parameter.get("unit_raw"))
    columns = [index for index, title in enumerate(header) if index != exclude and _heading_matches(title, name, unit)]
    if not columns:
        return None
    if len(columns) != 1:
        return False
    column = columns[0]
    value = parameter.get("value")
    if type(value) in (int, float) or parameter.get("kind") == "range":
        return _table_value_matches(parameter, row[column], _heading_units(header[column], name, unit))
    return _name(row[column]) in {_name(str(value)), _name(parameter["raw_text"])}


def _table_value_matches(value: dict, cell: str, unit_labels: list[str], *, exact_raw: bool = False) -> bool:
    """原数值必须完整出现在同一单元格，单位可来自同行列标签。"""
    expected = _quantity_numbers(value)
    if expected is None:
        return False
    unit = _unit_key(value.get("unit_raw", value.get("unit")))
    observed, suffix = _numeric_cell(cell)
    raw_text = value["raw_text"]
    if observed != expected or not set(expected) <= set(_numbers(raw_text)):
        return False
    if exact_raw:
        raw_numbers, raw_unit = _numeric_cell(raw_text)
        if raw_numbers != observed or raw_unit and _unit_key(raw_unit) != unit:
            return False
    prefix = re.match(r"(?:<=|>=|[<>≤≥≈~≃=])", _number_text(cell).strip("$ "))
    reported_operator = _operator_key(prefix[0] if prefix else "")
    if _operator_key(value.get("operator", "")) != reported_operator:
        return False
    units = {_unit_key(label) for label in unit_labels if label.strip()}
    if suffix:
        units.add(_unit_key(suffix))
    return units == {unit} or not unit and not units


def _quantity_numbers(value: dict) -> tuple[Decimal, ...] | None:
    """取得标量或范围的有序数量值。"""
    if value.get("kind") == "range":
        minimum, maximum = Decimal(str(value["minimum"])), Decimal(str(value["maximum"]))
        return (minimum, maximum) if minimum <= maximum else None
    values = (Decimal(str(value["value"])),)
    return (*values, Decimal(str(value["uncertainty"]))) if "uncertainty" in value else values


def _numeric_cell(cell: str) -> tuple[tuple[Decimal, ...], str]:
    """读取数值单元格中的量及其独立单位后缀。"""
    text = _number_text(cell).strip("$ ")
    first = _NUMBER.search(text)
    if first is None or not re.fullmatch(r"[\s<>=≤≥≈~≃]*", text[:first.start()]):
        return (), ""
    rest = text[first.end():]
    separator = re.match(r"\s*(?:[~～–—至到±]|-(?=\s*[+−-]?\d)|to\b)\s*", rest, re.I)
    if separator and (second := _NUMBER.match(rest, separator.end())):
        return (_decimal(first.group()), _decimal(second.group())), rest[second.end():].strip()
    return (_decimal(first.group()),), rest.strip()


def _number_unit_present(expected, unit: str, text: str, *, before: int | None = None) -> bool:
    """将报告单位限定在该数值之后，允许其后还有正文。"""
    normalized = _unit_key(unit)
    text = _number_text(text)
    for match in _NUMBER.finditer(text):
        if before is not None and match.end() > before:
            break
        if _decimal(match.group()) not in expected:
            continue
        suffix = text[match.end():].lstrip()
        # 先检查原文单位边界，再忽略单位排版空白。
        for end in range(1, min(len(suffix), max(32, len(unit) * 3)) + 1):
            if end < len(suffix) and re.match(r"[A-Za-z0-9/^+−-]", suffix[end]):
                continue
            if _unit_key(suffix[:end]) == normalized:
                return True
    return False


def _numbers(text: str) -> tuple[Decimal, ...]:
    """提取原文中的十进制数字，包括排版分组的数字。"""
    return tuple(_decimal(match.group()) for match in _NUMBER.finditer(_number_text(text)))


def _decimal(text: str) -> Decimal:
    """读取十进制或乘十幂写法。"""
    text = re.sub(r"\s*(?:\\times|×|⋅|·|x)\s*10\s*\^?\s*\{?\s*([+-]?\d+)\s*\}?", r"e\1", text)
    return Decimal(text.replace(" ", ""))


def _number_text(text: str) -> str:
    """统一数字中的兼容字符、空格与数学负号。"""
    return unicodedata.normalize("NFKC", _visible(text)).replace("−", "-")


def _heading_matches(label: str, name: str, unit: str | None) -> bool:
    """识别同名标签及附有原单位的表头。"""
    if _name(label) == _name(name):
        return True
    return bool(_heading_units(label, name, unit))


def _heading_units(label: str, name: str, unit: str | None) -> list[str]:
    """读取同名表头用斜线或尾括号引出的单位。"""
    label = _number_text(label).strip("$ ").replace(r"\%", "%")
    wanted = _name(name)
    if label.endswith("%") and _name(label[:-1]) == wanted:
        return ["%"]
    for slash in re.finditer("/", label):
        prefix, suffix = label[:slash.start()], label[slash.end():].strip()
        if suffix and (_name(prefix) == wanted or _name(label) == wanted and _unit_key(suffix) == _unit_key(unit)):
            return [suffix]
    if bracket := re.search(r"\(([^()]*)\)\s*$", label):
        suffix = bracket[1].strip()
        if suffix and _name(label[:bracket.start()]) == wanted and (unit or re.search(r"\s\(", label)):
            return [suffix]
    return []


def _operator_key(operator: str) -> str:
    """等同数表中表达同一数量限定的符号和常见文字。"""
    key = _number_text(operator).casefold()
    return {
        "=": "", "exact": "", "<=": "≤", ">=": "≥", "~": "≈", "≃": "≈",
        "约": "≈", "approximately": "≈", "approximate": "≈", "approx": "≈",
        "小于": "<", "less than": "<", "less_than": "<", "大于": ">", "greater than": ">", "greater_than": ">",
        "不大于": "≤", "不小于": "≥", "at most": "≤", "at least": "≥",
    }.get(key, key)


def _unit_key(text: str | None) -> str:
    """统一单位的排版差异，保留大小写与数量级。"""
    if text is None:
        return ""
    text = _number_text(text).strip().replace(r"\%", "%").replace(r"\cdot", "·")
    text = re.sub(r"\s*(?:[·⋅*]\s*|\s+)([A-Za-zµμÅ]+)\s*\^?\s*\{?\s*-1\s*\}?\s*(?=\)?$)", r"/\1", text)
    text = re.sub(r"[\s$^{}·⋅]", "", text)
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    return "" if text == "1" else text


def _visible(text: str) -> str:
    """还原可见文字并保留原表符号。"""
    return html.unescape(re.sub(r"</?[A-Za-z][^>]*>", "", text)).strip()


def _name(text: str, *, case_sensitive: bool = False) -> str:
    """忽略标签排版空白、数学下标与括号样式。"""
    normal = _number_text(text)
    return re.sub(r"[\s_$(){}]", "", normal if case_sensitive else normal.casefold())


def _compact(text: str) -> str:
    """忽略 PDF 排版空白以定位引用字面量。"""
    return re.sub(r"\s", "", _number_text(text).casefold())


def _literal_pattern(raw: str) -> re.Pattern | None:
    """防止报告数值仅命中正文中另一数值的子串。"""
    literal = _compact(raw)
    if not literal:
        return None
    left = r"(?<![\d.+-])" if literal[0].isdigit() or literal[0] in ".+-" else ""
    right = r"(?!\d|\.\d)" if literal[-1].isdigit() else ""
    return re.compile(left + re.escape(literal) + right)


def _mentions_sample(text: str, name: str) -> bool:
    """按样品名边界匹配，避免编号前缀混淆。"""
    name = r"\s*".join(re.escape(part) for part in _number_text(name).casefold().split())
    return bool(name and re.search(rf"(?<![a-z0-9_-]){name}(?![a-z0-9_-])", _number_text(text).casefold()))


def _prose_label(text: str) -> re.Pattern:
    """定位保持大小写的原文标签，避免匹配到另一符号内部。"""
    normal = _number_text(text)
    literal = r"\s*".join(re.escape(char) for char in normal if not char.isspace())
    left = r"(?<![A-Za-z0-9_])" if re.match(r"[A-Za-z0-9_]", normal) else ""
    right = r"(?![A-Za-z0-9_])" if re.search(r"[A-Za-z0-9_]$", normal) else ""
    return re.compile(left + literal + right)
