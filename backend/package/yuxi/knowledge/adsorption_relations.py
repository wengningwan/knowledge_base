"""实验观测与上游实验的归属约束。"""

from __future__ import annotations

import re
from decimal import Decimal


def observation_assignment_error(observation: dict, record: dict) -> str | None:
    """拒绝类型、样品、物种、条件或推导上游不一致的观测。"""
    runs = {item["run_id"]: item for item in record["experiment_runs"]}
    run_ids = observation.get("run_ids") or ([observation["run_id"]] if observation.get("run_id") else [])
    kind = observation["observation_type"]
    samples = set(observation.get("sample_state_ids", []))
    if observation.get("sample_state_id"):
        samples.add(observation["sample_state_id"])
    if not samples:
        return "观测没有明确的样品状态或样品组，不能从实验列表推测。"
    expected_types = {
        "textural_properties": {"characterization"},
        "qualitative_characterization": {"characterization"},
        "spectral_peak_assignments": {"characterization"},
        "spectral_ratio": {"characterization"},
        "surface_elemental_composition": {"characterization"},
        "equilibrium_uptake": {"equilibrium_isotherm"},
        "qualitative_ordering": {"equilibrium_isotherm", "mixture_prediction", "characterization"},
        "selectivity": {"mixture_prediction"},
        "isosteric_heat": {"isosteric_heat_derivation"},
        "breakthrough_working_capacity": {"breakthrough"},
        "dynamic_selectivity": {"breakthrough"},
        "qualitative_breakthrough_behavior": {"breakthrough"},
        "binding_energy": {"computational_adsorption"},
        "kinetic_selectivity": {"adsorption_kinetics"},
        "diffusion_time_constant": {"adsorption_kinetics"},
    }
    expected_techniques = {
        "textural_properties": {"gas_physisorption"},
        "spectral_peak_assignments": {"Raman_spectroscopy", "X-ray_photoelectron_spectroscopy"},
        "spectral_ratio": {"Raman_spectroscopy"},
        "surface_elemental_composition": {"X-ray_photoelectron_spectroscopy"},
    }
    scientific_kind = observation.get("reported_observation_type", kind)
    if scientific_kind != "reported_measurement" and scientific_kind not in expected_types:
        return "观测类型未登记。"
    manual_run_types = {
        "isotherm_model_fit": "fitting", "mixture_prediction": "prediction",
        "isosteric_heat_derivation": "derivation", "computational_adsorption": "computation",
    }
    pair = observation.get("ordered_species_pair")
    entities = {item["entity_id"] for item in record["chemical_entities"]}
    species = {observation["entity_id"]} if observation.get("entity_id") else set()
    species.update(item["metric_id"] for item in observation.get("results", []) if item.get("metric_id") in entities)
    if kind == "breakthrough_working_capacity" and any(
        item["metric_id"] not in entities for item in observation.get("results", [])
    ):
        return "穿透工作容量的每个结果须明确引用被测物种。"
    if scientific_kind == "selectivity" and not pair:
        return "选择性结果未明确声明有序气体对。"
    if pair:
        species.update(pair.values())
        if pair["numerator_entity_id"] == pair["denominator_entity_id"]:
            return "气体对必须包含两个不同物种。"
    if not species <= entities:
        return "观测引用了未登记的物种。"
    if not run_ids:
        if kind == "reported_measurement" and observation.get("run_missing_reason") == "not_reported":
            return None
        return "观测没有明确实验关联；未确定关联只能保留为待处理候选。"
    conditions = dict(observation.get("conditions", {}))
    conditions = {
        **{key: observation[key] for key in ("temperature", "pressure", "loading_domain") if key in observation},
        **conditions,
    }
    assigned_samples = set()
    for run_id in run_ids:
        run = runs.get(run_id)
        if run is None:
            return "观测引用了未登记的实验。"
        allowed_types = expected_types.get(scientific_kind)
        if kind == "reported_measurement" and run["run_type"] == "reported_experiment":
            if allowed_types and run["experiment_type"] not in {manual_run_types.get(key, "measurement") for key in allowed_types}:
                return "观测类型与所关联人工补录实验的类型不一致。"
        else:
            if allowed_types and run["run_type"] not in allowed_types:
                return "观测类型与所关联实验类型不一致。"
            if scientific_kind in expected_techniques and run.get("technique") not in expected_techniques[scientific_kind]:
                return "观测指标与所关联表征方法不一致。"
        ancestry, error = _run_ancestry(run, runs)
        if error:
            return error
        owner_samples = set.intersection(*(_run_samples(owner, runs) for owner in ancestry))
        if not samples & owner_samples:
            return "观测样品不属于对应实验及其明确推导上游的样品集合。"
        assigned_samples.update(owner_samples)
        available_species = set().union(*(_run_species(owner) for owner in ancestry))
        if species and not species <= available_species:
            return "观测物种不属于对应实验或明确推导上游的物种集合。"
        if pair:
            run_pair = run.get("ordered_species_pair", {})
            if (run_pair or run["run_type"] == "mixture_prediction") and any(
                run_pair.get(field) != entity_id for field, entity_id in pair.items()
            ):
                return "观测气体对的顺序与对应实验不一致。"
        available_conditions = _run_conditions(ancestry)
        for key, candidates in available_conditions.items():
            if (
                run["run_type"] in {"equilibrium_isotherm", "adsorption_kinetics", "breakthrough", "characterization"}
                and key not in conditions and len({repr(candidate) for candidate in candidates}) > 1
            ):
                return f"实验包含多个 {key} 条件，观测尚未明确选择适用条件。"
        for key, value in conditions.items():
            if value is None:
                continue
            if key not in available_conditions or not any(
                _condition_matches(value, candidate) for candidate in available_conditions[key]
            ):
                return f"观测条件 {key} 与对应实验及其上游条件不一致或尚未登记。"
    return None if samples <= assigned_samples else "观测样品组包含不属于任何对应实验的状态。"


def run_derivation_error(run: dict, record: dict) -> str | None:
    """核对推导实验的上游类型及闭合关系。"""
    runs = {item["run_id"]: item for item in record["experiment_runs"]}
    _, error = _run_ancestry(run, runs)
    return error


def _run_ancestry(run: dict, runs: dict) -> tuple[list[dict], str | None]:
    """遍历明确的输入边并拒绝环、缺失和错类上游。"""
    expected = {
        "isotherm_model_fit": ("input_run_ids", {"equilibrium_isotherm"}),
        "mixture_prediction": ("source_model_run_ids", {"isotherm_model_fit"}),
        "isosteric_heat_derivation": ("source_run_ids", {"equilibrium_isotherm", "isotherm_model_fit"}),
    }
    pending = [(run, set())]
    ancestry = {}
    while pending:
        current, path = pending.pop()
        run_id = current["run_id"]
        if run_id in path:
            return [], "推导实验存在上游循环引用。"
        ancestry[run_id] = current
        if current["run_type"] not in expected:
            continue
        field, allowed = expected[current["run_type"]]
        upstream_ids = current.get(field, [])
        if not upstream_ids:
            return [], "推导实验缺少明确的上游输入。"
        for upstream_id in upstream_ids:
            upstream = runs.get(upstream_id)
            if upstream is None or upstream["run_type"] not in allowed:
                return [], "推导实验的上游输入不存在或实验类型错误。"
            pending.append((upstream, path | {run_id}))
    return list(ancestry.values()), None


def _run_samples(run: dict, runs: dict) -> set[str]:
    """实验未直接列样品时，只沿明确上游取共同样品集合。"""
    if "sample_state_ids" in run:
        return set(run["sample_state_ids"])
    assignment = run.get("subject_scope", {}).get("sample_specific_assignment")
    if assignment:
        return set(assignment)
    upstream_ids = run.get("input_run_ids") or run.get("source_model_run_ids") or run.get("source_run_ids") or []
    groups = [_run_samples(runs[ref], runs) for ref in upstream_ids if ref in runs]
    return set.intersection(*groups) if groups else set()


def _run_species(run: dict) -> set[str]:
    """只读取测量物种，不把预处理气体当作被测物种。"""
    species = set(run.get("species_entity_ids", []))
    for field in ("adsorptives", "feed"):
        species.update(item["entity_id"] for item in run.get(field, []) if item.get("entity_id"))
    if probe := run.get("probe", {}).get("entity_id"):
        species.add(probe)
    pair = run.get("ordered_species_pair", {})
    species.update(pair[field] for field in ("numerator_entity_id", "denominator_entity_id") if field in pair)
    return species


def _run_conditions(ancestry: list[dict]) -> dict[str, list]:
    """读取当前实验的条件，上游只补当前未声明的条件。"""
    result = {}
    for owner in ancestry:
        conditions = {}
        for key in ("temperature", "pressure", "loading_domain", "pressure_domain"):
            if owner.get(key) is not None:
                conditions["pressure" if key == "pressure_domain" else key] = owner[key]
        if "pressure" not in conditions and (maximum := owner.get("pressure_max_explicit_in_results")) is not None:
            if maximum.get("kind") == "scalar" and maximum.get("operator", "=") == "=":
                conditions["pressure"] = {
                    **maximum, "maximum": maximum["value"], "_maximum_only": True,
                }
        if "loading_domain" in conditions:
            conditions["loading"] = conditions["loading_domain"]
        if "temperatures" in owner:
            conditions["temperature"] = owner["temperatures"]
        if temperature := owner.get("probe", {}).get("temperature"):
            conditions["temperature"] = temperature
        conditions.update(owner.get("conditions", {}))
        if isinstance(conditions.get("pressure"), dict):
            pressure = conditions["pressure"]
            conditions["pressure"] = {**pressure, "_pressure_basis": _pressure_basis(pressure, owner.get("pressure_basis"))}
        for key, value in conditions.items():
            if key not in result:
                result[key] = value if isinstance(value, list) else [value]
    return result


def _condition_matches(value: dict, candidate: dict) -> bool:
    """同单位下比较完整数量；区间条件只允许明确包含的标量。"""
    if not isinstance(value, dict) or not isinstance(candidate, dict):
        return value == candidate
    if value.get("kind") == "text" or candidate.get("kind") == "text":
        return value == candidate
    unit = value.get("unit_canonical", value.get("unit"))
    other_unit = candidate.get("unit_canonical", candidate.get("unit"))
    if unit != other_unit:
        return False
    if "_pressure_basis" in candidate and _pressure_basis(value) != candidate["_pressure_basis"]:
        return False
    if candidate.get("_maximum_only"):
        return value.get("kind") == "scalar" and value.get("operator", "=") == "=" and (
            Decimal(0) <= Decimal(str(value["value"])) <= Decimal(str(candidate["maximum"]))
        )
    if "minimum" in candidate and "maximum" in candidate and "value" in value:
        if value.get("operator", "=") != "=" or (
            "_pressure_basis" not in candidate and value.get("basis") != candidate.get("basis")
        ):
            return False
        number = Decimal(str(value["value"]))
        minimum, maximum = Decimal(str(candidate["minimum"])), Decimal(str(candidate["maximum"]))
        return minimum <= number <= maximum and (
            number != minimum or candidate.get("minimum_inclusive", True)
        ) and (number != maximum or candidate.get("maximum_inclusive", True))
    fields = (
        "kind", "operator", "value", "minimum", "maximum", "minimum_inclusive", "maximum_inclusive",
        "uncertainty_minus", "uncertainty_plus", "uncertainty_type", "basis",
    )
    return all(value.get(field) == candidate.get(field) for field in fields if field != "basis" or "_pressure_basis" not in candidate)


def _pressure_basis(quantity: dict, reported: str | None = None) -> str | None:
    """仅明确报告的基准可区分相对压力、表压和绝压。"""
    basis = quantity.get("basis") or reported or ""
    if re.search(r"relative|p\s*/\s*p\s*0|相对", basis, re.I):
        return "relative"
    if re.search(r"gauge|表压", basis, re.I):
        return "gauge"
    if re.search(r"absolute|绝对", basis, re.I):
        return "absolute"
    return None
