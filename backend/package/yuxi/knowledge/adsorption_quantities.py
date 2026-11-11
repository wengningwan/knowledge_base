"""核对实验数量的完整原文表达，不推测单位或归一化值。"""

from __future__ import annotations

import html
import math
import re
import unicodedata
from decimal import Decimal, InvalidOperation

from yuxi.knowledge.structure_tables import table_cell_has_note, table_cells

_NUMBER = re.compile(
    r"[+−-]?(?:\d+(?:\.\d+)?|\.\d+)"
    r"(?:[eE][+−-]?\d+|\s*(?:\\times|×|⋅|·|x)\s*10\s*"
    r"(?:\^\s*\{?\s*[+−-]?\d+\s*\}?|[+−-]\d+|[⁻⁺⁰¹²³⁴⁵⁶⁷⁸⁹]+))?"
)
_QUALIFIER = (
    r">=|<=|>|<|≥|≤|≈|~|∼|=|about\b|approximately\b|approx\.?|ca\.?|at\s+least\b|"
    r"at\s+most\b|no\s+more\s+than\b|no\s+less\s+than\b|less\s+than\b|greater\s+than\b|"
    r"more\s+than\b|higher\s+than\b|lower\s+than\b|over\b|above\b|below\b|under\b|up\s+to\b|"
    r"约|大于|小于|不少于|不超过|超过|高于|低于|至少|至多"
)
_METRIC_LABELS = {
    "textural.surface_area.specific": r"\bS?\s*BET\b|surface\s+area|比表面积",
    "textural.pore_volume.total": r"pore\s+volume|\bV\s*total\b|孔容",
    "equilibrium_uptake": r"uptake|loading|capacity|吸附量",
    "breakthrough_working_capacity": r"working\s+capacity|capacity|uptake|容量",
    "isosteric_heat": r"isosteric|adsorption\s+heat|Q\s*st|吸附热",
    "diffusion_time_constant": r"diffusion|D\s*/\s*r|time\s+constant|扩散",
    "kinetic_selectivity": r"kinetic|selectivity|选择性",
    "selectivity": r"selectivity|选择性",
    "dynamic_selectivity": r"selectivity|选择性",
    "binding_energy": r"binding|energy|结合能",
    "signed_value_from_figure": r"binding|energy|结合能",
    "magnitude_reported_in_prose": r"binding|energy|结合能",
    "interaction_distance_from_figure": r"distance|距离",
    "quantitative_particle_size": r"particle\s+size|粒径",
    "numeric_mass_loss": r"mass\s+loss|weight\s+loss|失重",
    "C_percent": r"\bC\b(?!\s*/)|carbon",
    "O_percent": r"\bO\b(?!\s*/)|oxygen",
    "N_percent": r"\bN\b(?!\s*/)|nitrogen",
    "O_over_C_percent": r"\bO\s*/\s*C\b",
    "N_over_C_percent": r"\bN\s*/\s*C\b",
    "temperature": r"temperature|温度|\bT\b",
    "pressure": r"pressure|压力|\bP\b",
    "relative_pressure_interval": r"relative\s+pressure|相对压力|P\s*/\s*P\s*_?\s*0",
    "duration": r"duration|time|for|时长|时间",
    "hold_duration": r"duration|time|for|保持|保温",
    "flow_rate": r"flow|流量",
    "total_flow_rate": r"flow|流量",
    "yield": r"yield|产率|收率",
    "purity": r"purity|纯度",
    "replicate_count": r"replicate|repeat|重复",
    "repeat_count": r"repeat|重复",
    "heating_rate": r"heating\s+rate|升温速率",
}
_METRIC_UNITS = {
    "textural.surface_area.specific": {"m2/g"},
    "textural.pore_volume.total": {"cm3/g", "mL/g"},
    "equilibrium_uptake": {"mmol/g", "mol/g", "mg/g", "cm3(STP)/g", "cm3/g"},
    "breakthrough_working_capacity": {"mmol/g", "mol/g", "mg/g", "cm3(STP)/g", "cm3/g"},
    "isosteric_heat": {"kJ/mol", "J/mol"},
    "binding_energy": {"eV", "kJ/mol", "J/mol"},
    "signed_value_from_figure": {"eV", "kJ/mol", "J/mol"},
    "magnitude_reported_in_prose": {"eV", "kJ/mol", "J/mol"},
    "diffusion_time_constant": {"s^-1", "min^-1", "h^-1"},
    "selectivity": {None, "1"}, "kinetic_selectivity": {None, "1"}, "dynamic_selectivity": {None, "1"},
    "temperature": {"K", "°C"}, "pressure": {"Pa", "kPa", "MPa", "mPa", "bar", "mbar", "atm", "Torr"},
    "relative_pressure_interval": {None, "1"},
}


class UnverifiedQuantity(ValueError):
    """数量、限定或出处不能唯一核对。"""


def quantity_text(value: object) -> str | None:
    """读取数量原文；兼容旧稿的简单值只供显示。"""
    if isinstance(value, dict):
        if isinstance(value.get("raw_text"), str):
            raw = value["raw_text"]
            if value.get("unit_raw") and value.get("kind") != "text" and (expression := _expression(raw)) and not expression[3].strip():
                return f'{raw} {value["unit_raw"]}'
            return raw
        if value.get("value") is not None and not isinstance(value["value"], (dict, list, bool)):
            return f'{value["value"]} {value.get("unit") or ""}'.strip()
        return None
    return str(value) if isinstance(value, (str, int, float)) and not isinstance(value, bool) else None


def quantity_from_text(text: str) -> dict:
    """仅解析完整且单位已知的数量，其他写法保留文字。"""
    raw = text.strip()
    try:
        expression = _expression(raw)
    except UnverifiedQuantity:
        expression = None
    if expression is None:
        return {"kind": "text", "raw_text": raw}
    kind, operator, values, suffix = expression
    unit = suffix.strip() or None
    numbers = [float(number) for number in values]
    if (unit is not None and canonical_unit(unit) is None) or any(
        not math.isfinite(number) or Decimal(str(number)) != original
        for number, original in zip(numbers, values, strict=True)
    ):
        return {"kind": "text", "raw_text": raw}
    if (kind == "range" and values[0] > values[1]) or (kind == "uncertainty" and values[1] < 0):
        return {"kind": "text", "raw_text": raw}
    base = {"kind": kind, "raw_text": raw, "unit_raw": unit, "unit_canonical": canonical_unit(unit)}
    if kind == "range":
        return {
            **base, "minimum": numbers[0], "maximum": numbers[1],
            "minimum_inclusive": operator in {"=", "[]", "[)"},
            "maximum_inclusive": operator in {"=", "[]", "(]"}, "range_semantics": "reported_range",
        }
    if kind == "uncertainty":
        return {
            **base, "operator": operator, "value": numbers[0],
            "uncertainty_minus": numbers[1], "uncertainty_plus": numbers[1],
            "uncertainty_type": "reported", "basis": None,
        }
    return {**base, "operator": operator, "value": numbers[0], "basis": None}


def ground_quantity(
    metric: str, value: dict, sources: list[dict], samples: list[str], markdown: str,
    *, species: tuple[str, ...] = (), other_samples: tuple[str, ...] = (), other_species: tuple[str, ...] = (),
    parameter: bool = False,
) -> str:
    """按同句或同表交点核对数量、单位、限定和误差。"""
    check_quantity(value)
    if metric in _METRIC_UNITS and value.get("kind") != "text" and value.get("unit_canonical") not in _METRIC_UNITS[metric]:
        raise UnverifiedQuantity("指标的数量维度与登记单位不一致。")
    if metric == "pressure" and value.get("kind") != "text" and re.search(r"relative|相对|p\s*/\s*p\s*0", value.get("basis") or "", re.I):
        raise UnverifiedQuantity("相对压力不能使用绝对压力单位作为规范数量。")
    raw = value["raw_text"]
    if value["kind"] == "text":
        if any(not match.start() or not raw[match.start() - 1].isalnum() for match in _NUMBER.finditer(raw)):
            raise UnverifiedQuantity("含数值的未知表达保留为人工候选，不能按文字绕过数量核对。")
        if any(
            _phrase(raw) in _phrase(source["quote"])
            and all(_sample_present(sample, source["quote"]) for sample in samples)
            and _species_matches(species, source["quote"])
            for source in sources
        ):
            return raw
        raise UnverifiedQuantity("文字结果不能在所引原文中定位。")
    for source in sources:
        bounds = source.get("table_range") or [source["start_char"], source["end_char"]]
        grid = table_cells(markdown, *bounds)
        if grid is not None:
            if not grid or len(samples) != 1:
                continue
            cells = [[markdown[a:b] for a, b in row] for row in grid]
            intersections = []
            for row_index, row in enumerate(grid[1:], 1):
                for column_index, (start, end) in enumerate(row[1:], 1):
                    if end <= source["start_char"] or start >= source["end_char"]:
                        continue
                    row_label, column_label = cells[row_index][0], cells[0][column_index]
                    if _phrase(row_label) == _phrase(samples[0]):
                        header = column_label
                    elif _phrase(column_label) == _phrase(samples[0]):
                        header = row_label
                    else:
                        continue
                    if _metric_matches(metric, header) and _species_matches(species, f"{row_label} {column_label}"):
                        intersections.append((row_label, column_label, header, cells[row_index][column_index]))
            if len(intersections) == 1:
                row_label, column_label, header, cell = intersections[0]
                basis_matches = not value.get("basis") or _phrase(value["basis"]) in _phrase(f"{row_label} {header} {cell}")
                if basis_matches and not any(table_cell_has_note(item) for item in (row_label, column_label, cell)) and _same_quantity(value, cell, header):
                    return raw
            continue
        for sentence in re.split(r"[\n。;；]|(?<=[.!?])\s+", source["quote"]):
            if value.get("basis") and _phrase(value["basis"]) not in _phrase(sentence):
                continue
            if samples and not all(_sample_present(sample, sentence) for sample in samples):
                continue
            if any(_sample_present(sample, sentence) for sample in other_samples):
                continue
            for clause in re.split(r"[,，;；]|\band\b|以及|且", sentence, flags=re.I):
                if not _species_matches(species, clause):
                    continue
                if any(_sample_present(item, clause) for item in other_species):
                    continue
                if _bound_quantity(metric, value, clause, parameter=parameter):
                    return raw
    raise UnverifiedQuantity("完整数量、指标、样品或单位不能在同一原文位置核对。")


def check_quantity(value: dict) -> None:
    """拒绝丢失限定、范围、误差或未知单位的规范数值。"""
    if value.get("kind") == "text":
        if not isinstance(value.get("raw_text"), str) or not value["raw_text"].strip():
            raise UnverifiedQuantity("文字结果为空。")
        return
    expression = _expression(value.get("raw_text", ""))
    if expression is None:
        raise UnverifiedQuantity("数量原文不是可完整解释的数量表达。")
    kind, operator, values, suffix = expression
    if kind != value.get("kind"):
        raise UnverifiedQuantity("数量种类与原文的范围或误差表达不一致。")
    try:
        if kind == "range":
            expected = [Decimal(str(value["minimum"])), Decimal(str(value["maximum"]))]
            if expected[0] > expected[1] or (
                value.get("minimum_inclusive") != (operator in {"=", "[]", "[)"})
                or value.get("maximum_inclusive") != (operator in {"=", "[]", "(]"})
            ):
                raise UnverifiedQuantity("范围边界与原文不一致。")
        elif kind == "uncertainty":
            expected = [Decimal(str(value["value"])), Decimal(str(value["uncertainty_minus"]))]
            if expected[1] < 0 or Decimal(str(value["uncertainty_plus"])) != expected[1]:
                raise UnverifiedQuantity("误差与原文不一致，非对称误差应保留原文候选。")
        else:
            expected = [Decimal(str(value["value"]))]
    except (KeyError, InvalidOperation) as error:
        raise UnverifiedQuantity("数量缺少可核对的数值。") from error
    if expected != values or any(not number.is_finite() for number in expected):
        raise UnverifiedQuantity("数值、顺序或指数与原文不一致。")
    if kind != "range" and value.get("operator") != operator:
        raise UnverifiedQuantity("数量限定词与原文不一致。")
    unit = value.get("unit_raw")
    canonical = canonical_unit(unit)
    if unit is not None and canonical is None:
        raise UnverifiedQuantity("未知单位必须保留原文候选，不能生成规范值。")
    if value.get("unit_canonical") != canonical:
        raise UnverifiedQuantity("原始单位与规范单位不一致；此处不自动换算。")
    if suffix.strip() and (canonical_unit(suffix) is None or canonical_unit(suffix) != canonical):
        raise UnverifiedQuantity("数量原文所带单位与登记单位不一致。")


def canonical_unit(value: str | None) -> str | None:
    """识别已有数量单位，仅统一写法，不换算数值。"""
    if value is None:
        return None
    aliases = {
        "1": "1", "dimensionless": "1", "%": "%", "wt%": "wt%", "wt.%": "wt%", "at%": "at%", "at.%": "at%",
        "K": "K", "°C": "°C", "℃": "°C", "Pa": "Pa", "kPa": "kPa", "MPa": "MPa", "mPa": "mPa",
        "bar": "bar", "mbar": "mbar", "atm": "atm", "Torr": "Torr",
        "s": "s", "min": "min", "h": "h", "ms": "ms", "s-1": "s^-1", "1/s": "s^-1",
        "min-1": "min^-1", "1/min": "min^-1", "h-1": "h^-1", "1/h": "h^-1",
        "m": "m", "cm": "cm", "mm": "mm", "µm": "µm", "μm": "µm", "nm": "nm", "Å": "Å",
        "g": "g", "mg": "mg", "kg": "kg", "mol": "mol", "mmol": "mmol", "M": "M", "mM": "mM",
        "mL": "mL", "L": "L", "eV": "eV", "kJ/mol": "kJ/mol", "kJmol-1": "kJ/mol",
        "J/mol": "J/mol", "Jmol-1": "J/mol", "mmol/g": "mmol/g", "mmolg-1": "mmol/g",
        "mol/g": "mol/g", "molg-1": "mol/g", "mg/g": "mg/g", "mgg-1": "mg/g",
        "m2/g": "m2/g", "m2g-1": "m2/g", "cm3/g": "cm3/g", "cm3g-1": "cm3/g",
        "cm-1": "cm^-1", "1/cm": "cm^-1", "mL/min": "mL/min", "mLmin-1": "mL/min",
        "cm3(STP)/g": "cm3(STP)/g", "cm3STP/g": "cm3(STP)/g", "cm3(STP)g-1": "cm3(STP)/g",
        "cm3STPg-1": "cm3(STP)/g", "sccm": "sccm", "mL/g": "mL/g", "mLg-1": "mL/g",
        "K/min": "K/min", "Kmin-1": "K/min", "°C/min": "°C/min", "°Cmin-1": "°C/min",
    }
    return aliases.get(_unit(value))


def _expression(text: str) -> tuple[str, str, list[Decimal], str] | None:
    """拆出一个完整数量的限定、顺序数值与单位后缀。"""
    value = _plain(text).strip().strip("$").strip()
    interval = re.match(rf"^([\[(])\s*({_NUMBER.pattern})\s*[,，]\s*({_NUMBER.pattern})\s*([\])])", value)
    if interval:
        return "range", interval[1] + interval[4], [_number(interval[2]), _number(interval[3])], value[interval.end():]
    qualifier = re.match(rf"^({_QUALIFIER})\s*", value, re.I)
    operator = "="
    if qualifier:
        token = qualifier.group(1).casefold()
        operator = {
            "≥": ">=", "≤": "<=", "大于": ">", "小于": "<", "不少于": ">=", "不超过": "<=",
            "at least": ">=", "no less than": ">=", "at most": "<=", "no more than": "<=",
            "greater than": ">", "less than": "<",
            "more than": ">", "higher than": ">", "lower than": "<", "over": ">", "above": ">",
            "below": "<", "under": "<", "up to": "<=", "超过": ">", "高于": ">", "低于": "<",
            "至少": ">=", "至多": "<=",
        }.get(" ".join(token.split()), token)
        if operator not in {"=", ">", "<", ">=", "<="}:
            operator = "approximately"
        value = value[qualifier.end():]
    match = _NUMBER.match(value)
    if not match:
        return None
    first = _number(match.group())
    tail = value[match.end():]
    separator = re.match(r"\s*(±|\+/-|–|—|-|to\b|至)\s*", tail, re.I)
    if separator:
        following = tail[separator.end():]
        second = _NUMBER.match(following)
        if second is None:
            return None
        kind = "uncertainty" if separator.group(1) in {"±", "+/-"} else "range"
        if operator not in ({"=", "approximately"} if kind == "uncertainty" else {"="}):
            return None
        return kind, operator, [first, _number(second.group())], following[second.end():]
    return "scalar", operator, [first], tail


def _same_quantity(value: dict, text: str, header: str = "") -> bool:
    """仅接受同一数量表达，不能从邻项借单位、限定或误差。"""
    raw = value["raw_text"]
    unit = value.get("unit_raw")
    expected = _expression(raw)
    if expected is None:
        return False
    plain = _plain(text)
    consumed = 0
    for match in _NUMBER.finditer(plain):
        start = match.start()
        if start < consumed or (start and (plain[start - 1].isalnum() or plain[start - 1] in "._")):
            continue
        prefix = plain[max(0, start - 24):start]
        qualifier = re.search(rf"(?:{_QUALIFIER})\s*$", prefix, re.I)
        if qualifier:
            start = max(0, match.start() - 24) + qualifier.start()
        elif interval_start := re.search(r"[\[(]\s*$", prefix):
            start = max(0, match.start() - 24) + interval_start.start()
        candidate = _expression(plain[start:])
        if candidate is None:
            continue
        consumed = len(plain.rstrip()) - len(candidate[3])
        if candidate[:3] != expected[:3]:
            continue
        suffix = candidate[3].lstrip()
        source_unit = next((
            recognized for index in reversed(range(1, min(36, len(suffix)) + 1))
            if (index == len(suffix) or not (suffix[index].isalnum() or suffix[index] in "/^+-"))
            and (recognized := canonical_unit(suffix[:index])) is not None
        ), None)
        if unit is None:
            if source_unit or (header and (_header_unit(header) or suffix)) or (
                suffix and not re.match(r"^[,.;:。；）)]|^(?:at|in|for|on|was|were)\b", suffix, re.I)
            ):
                continue
            return True
        if source_unit == canonical_unit(unit) and (not header or canonical_unit(suffix) == source_unit):
            return True
        if header and canonical_unit(unit) == canonical_unit(_header_unit(header)) and not suffix:
            return True
    return False


def _bound_quantity(metric: str, value: dict, clause: str, *, parameter: bool) -> bool:
    """指标与其后首个数量绑定，多指标或多同量纲值无法确定时拒绝。"""
    plain = _plain(clause)
    pattern = _METRIC_LABELS.get(metric, re.escape(metric.replace("_", " ")))
    anchors = list(re.finditer(pattern, plain, re.I))
    if len(anchors) > 1:
        return False
    if anchors:
        tail = plain[anchors[0].end():]
        first = _NUMBER.search(tail)
        if first is None:
            return False
        if any(re.search(pattern, tail, re.I) for key, pattern in _METRIC_LABELS.items() if key != metric):
            return False
        prefix_start = max(0, first.start() - 24)
        qualifier = re.search(rf"(?:{_QUALIFIER})\s*$", tail[prefix_start:first.start()], re.I)
        start = prefix_start + qualifier.start() if qualifier else first.start()
        if not qualifier and (interval_start := re.search(r"[\[(]\s*$", tail[:first.start()])):
            start = interval_start.start()
        neutral = tail[:start].strip()
        if neutral and not re.fullmatch(r"(?:(?:was|were|is|are|of|for|at|reported|measured|found|value|equal|to|为|达到|达|是)\b|[\s:=（）()\[\]])*", neutral, re.I):
            return False
        expression = _expression(tail[start:])
        if expression is None or expression[:3] != _expression(value["raw_text"])[:3]:
            return False
        consumed = len(tail[start:]) - len(expression[3])
        suffix = expression[3]
        next_number = next((
            match.start() for match in _NUMBER.finditer(suffix)
            if not match.start() or not (suffix[match.start() - 1].isalnum() or suffix[match.start() - 1] in "^._")
        ), len(suffix))
        return _same_quantity(value, tail[start:start + consumed + next_number])
    if not parameter or not value.get("unit_canonical") or metric not in _METRIC_UNITS:
        return False
    first = next((
        match for match in _NUMBER.finditer(plain)
        if not match.start() or not plain[match.start() - 1].isalnum()
    ), None)
    if first is None:
        return False
    prefix = plain[:first.start()].strip()
    qualifier = re.search(rf"(?:{_QUALIFIER})\s*$", prefix, re.I)
    if qualifier:
        prefix = prefix[:qualifier.start()].strip()
    if prefix and not re.search(r"(?:\b(?:at|for|of|to|was|were|is|are)|[=:]|为|是|达到)\s*$", prefix, re.I):
        return False
    return _same_quantity(value, plain) and sum(
        1 for match in _NUMBER.finditer(plain)
        if not match.start() or not plain[match.start() - 1].isalnum()
    ) == (2 if value["kind"] in {"range", "uncertainty"} else 1)


def _header_unit(header: str) -> str | None:
    """读取列自身的单位标记；未知标记也不能按无单位通过。"""
    plain = _plain(header)
    bracket = re.search(r"[（(\[]([^）)\]]+)[）)\]]\s*$", plain)
    if bracket:
        return bracket[1]
    return next((
        plain[start:] for start in range(len(plain)) if (start == 0 or plain[start - 1].isspace())
        and canonical_unit(plain[start:]) is not None
    ), None)


def _metric_matches(metric: str, text: str) -> bool:
    """使用固定指标别名或原文指标名定位相应列。"""
    pattern = _METRIC_LABELS.get(metric)
    if pattern:
        return bool(re.search(pattern, _plain(text), re.I))
    label = metric.replace("_", " ").strip()
    return bool(label and _phrase(label) in _phrase(text))


def _sample_present(sample: str, text: str) -> bool:
    """用完整样品名称核对同句归属。"""
    name = re.escape(unicodedata.normalize("NFKC", sample))
    return bool(re.search(rf"(?<![\w-]){name}(?![\w-])", unicodedata.normalize("NFKC", _plain(text)), re.I))


def _species_matches(species: tuple[str, ...], text: str) -> bool:
    """有序选择性必须在原文中以同一气体对出现。"""
    if not species:
        return True
    if len(species) == 1:
        return _sample_present(species[0], text)
    first, second = (re.escape(unicodedata.normalize("NFKC", item)) for item in species)
    plain = unicodedata.normalize("NFKC", _plain(text))
    return bool(re.search(rf"(?<![\w-]){first}\s*(?:/|over|to|对)\s*{second}(?![\w-])", plain, re.I))


def _plain(text: str) -> str:
    """保留上标指数语义并去掉展示标签和数学围栏。"""
    text = re.sub(r"<sup\b[^>]*>(.*?)</sup\s*>", r"^\1", html.unescape(text), flags=re.I | re.S)
    return re.sub(r"</?[A-Za-z][^>]*>|\$|\\(?:mathrm|text)\s*\{([^}]+)\}", lambda match: match[1] or "", text)


def _unit(text: str) -> str:
    """统一单位字面写法，保留十进制前缀与幂次。"""
    value = unicodedata.normalize("NFKC", _plain(text)).replace("−", "-")
    value = re.sub(r"[\s^{}_·⋅]", "", value)
    return value


def _phrase(text: str) -> str:
    """忽略排版空白，保留限定符号。"""
    return "".join(unicodedata.normalize("NFKC", _plain(text)).casefold().split())


def _number(value: str) -> Decimal:
    """读取科学计数和上标指数中的完整数值。"""
    normalized = unicodedata.normalize("NFKC", value).replace("−", "-")
    normalized = re.sub(r"\s*(?:\\times|×|⋅|·|x)\s*10\s*\^?\s*\{?\s*([+-]?\d+)\s*\}?", r"e\1", normalized)
    try:
        return Decimal(normalized)
    except InvalidOperation as error:
        raise UnverifiedQuantity("无法读取原文数量。") from error
