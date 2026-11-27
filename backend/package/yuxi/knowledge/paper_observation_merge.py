"""合并同一原文所支持的重复论文观测。"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict

from yuxi.knowledge.adsorption_review import Measurement
from yuxi.knowledge.structure_tables import CellPosition

_ESTIMATE = re.compile(r"推算|估算|估计|可求出|\bestimat(?:e|ed|ion)\b|\bderived\b", re.I)

type MemberPosition = tuple[str, int]
type GroundedMember = tuple[dict, list[Measurement]]


def discard_wrong_table_scopes(
    record: dict, measurements: dict[str, list[Measurement]], resolved_cells: dict[tuple[str, str, CellPosition], str | None],
) -> None:
    """按程序已确认的单元格归属移除冲突值，保留同组中有独立出处的结果。"""
    kept = []
    for item in record["observations"]:
        observation_id = item["observation_id"]
        members = grounded_observation_members(item, measurements[observation_id])
        remaining = []
        for member, proof in members:
            sample_id = item.get("sample_state_id")
            if not any(
                resolved_cells.get((item["run_id"], ref, position), sample_id) != sample_id
                for ref, position in proof[0][2].items()
            ):
                remaining.append((member, proof))
            elif len(proof) == 2:
                text = {key: value for key, value in member.items()
                        if key not in {"value", "sign_convention", "source_cell", "fit_result"}}
                text["evidence_ids"] = proof[1][3]
                remaining.append((text, proof[1:]))
        if not remaining:
            del measurements[observation_id]
            continue
        if remaining != members:
            prior_refs = {ref for member, _ in members for ref in member["evidence_ids"]}
            context = [ref for ref in item["evidence_ids"] if ref not in prior_refs]
            item["results"] = [member for member, _ in remaining]
            refs = [ref for member, _ in remaining for ref in member["evidence_ids"]]
            item["evidence_ids"] = list(dict.fromkeys([*context, *refs]))
            measurements[observation_id] = [check for _, proof in remaining for check in proof]
        kept.append(item)
    record["observations"] = kept


def deduplicate_paper_observations(record: dict, measurements: dict[str, list[Measurement]], sources: dict) -> None:
    """合并完全重复、直接引用同一陈述及同一原文推算的观测。"""
    seen, exact_aliases, observations = {}, {}, {}
    for item in record["observations"]:
        observation_id = item["observation_id"]
        signature = json.dumps(
            ({key: value for key, value in item.items() if key != "observation_id"}, measurements[observation_id]),
            ensure_ascii=False, sort_keys=True,
        )
        if signature in seen:
            exact_aliases[observation_id] = seen[signature]
            del measurements[observation_id]
        else:
            seen[signature] = observation_id
            observations[observation_id] = item

    prose_aliases = _merge_anchored_prose(observations, measurements, sources)
    estimate_aliases = _merge_prose_estimates(record, observations, measurements, sources)
    record["observations"] = list(observations.values())

    def aliases(refs):
        """沿去重顺序保留有效观测及其剩余成员。"""
        refs = (exact_aliases.get(ref, ref) for ref in refs)
        proved = (alias for ref in refs for alias in prose_aliases.get(ref, [ref]))
        return list(dict.fromkeys(alias for ref in proved for alias in estimate_aliases.get(ref, [ref])))

    for series in record["series_sets"]:
        series["observation_ids"] = aliases(series["observation_ids"])
    for claim in record["mechanism_claims"]:
        claim["supported_by_observation_ids"] = aliases(claim["supported_by_observation_ids"])


def _merge_anchored_prose(observations, measurements, sources):
    """合并相同归属和条件下被重复引用的已核对原文陈述。"""
    groups = defaultdict(list)
    grounded = {}
    for ref, observation in observations.items():
        members = grounded[ref] = grounded_observation_members(observation, measurements[ref])
        conditions = [
            (_compact(name).casefold(), {
                field: value for field, value in condition.items()
                if field != "evidence_ids" and (field != "raw_text" or not {"value", "minimum"} & condition.keys())
            })
            for name, condition in sorted(observation["conditions"].items())
        ]
        scope = [observation.get(name) for name in (
            "run_id", "sample_state_id", "sample_state_ids", "ordered_species_pair", "composition_basis", "series_set_id",
        )]
        for index, (member, proof) in enumerate(members):
            anchor = member.get("source_text")
            if not anchor or proof[0][2] or any(
                name in member for name in ("result", "reported_order", "reported_assignment", "author_interpretation")
            ):
                continue
            citations = re.split(r"[。！？\n]+|(?<=[.!?])\s+(?=[A-Z])", sources[anchor["evidence_id"]]["quote"])
            quote = _compact(anchor["quote"])
            sentences = [source for item in citations if quote in (source := _compact(item))]
            if len(sentences) != 1 or sentences[0].count(quote) != 1:
                continue
            metric = _compact(anchor["metric"])
            within_quote = quote.find(metric)
            if within_quote < 0:
                continue
            metric_offset = sentences[0].index(quote) + within_quote
            value = {field: item for field, item in member["value"].items() if field != "raw_text"}
            key = json.dumps((
                scope, conditions, member.get("entity_id") or observation.get("entity_id"),
                member.get("sign_convention"), bool(_ESTIMATE.search(observation.get("data_origin", ""))),
                _compact(anchor.get("subject", "")), metric, sentences[0], metric_offset, value,
            ), ensure_ascii=False, sort_keys=True)
            groups[key].append((ref, index))

    removed = defaultdict(dict)
    for group in groups.values():
        if len(group) < 2:
            continue
        members = {position: grounded[position[0]][position[1]] for position in group}
        roles = [member["fit_result"] for member, _ in members.values() if "fit_result" in member]
        if roles and any(role != roles[0] for role in roles[1:]):
            continue
        keeper = max(group, key=lambda position: "fit_result" in members[position][0])
        primary, proof = members[keeper]
        refs = list(dict.fromkeys(source for _, checks in members.values() for source in checks[0][3]))
        primary["evidence_ids"] = refs
        proof[0] = (proof[0][0], proof[0][1], {}, refs)
        measurements[keeper[0]] = [check for _, proofs in grounded[keeper[0]] for check in proofs]
        parent = observations[keeper[0]]
        if primary is not parent:
            parent["evidence_ids"] = list(dict.fromkeys([*parent["evidence_ids"], *refs]))
        for ref, index in group:
            if (ref, index) != keeper:
                removed[ref][index] = keeper[0]

    return _remove_merged_members(observations, measurements, grounded, removed)


def _merge_prose_estimates(record, observations, measurements, sources):
    """在同一推算原句中找重复项，并逐成员保留同组的其他结果。"""
    runs = {item["run_id"] for item in record["experiment_runs"] if _ESTIMATE.search(item["run_type"])}
    sample_names = {item["sample_state_id"]: item["reported_name"] for item in record["sample_states"]}
    groups = defaultdict(list)
    grounded = {}
    citations = {}
    author_estimates = set()
    for ref, observation in observations.items():
        if not (observation["run_id"] in runs or _ESTIMATE.search(observation.get("data_origin", ""))) or any(
            name in observation for name in ("series_set_id", "ordered_species_pair", "composition_basis")
        ):
            continue
        members = grounded[ref] = grounded_observation_members(observation, measurements[ref])
        for index, (member, proof) in enumerate(members):
            value = member.get("value", {})
            if value.get("kind") != "scalar" or proof[0][2] or any(
                name in member for name in ("result", "reported_order", "reported_assignment", "author_interpretation", "fit_result")
            ):
                continue
            position = (ref, index)
            anchor = member.get("source_text")
            authored = anchor and observation.get("sample_state_id") is None
            raw = _compact(anchor["quote"] if authored else value["raw_text"])
            sample = sample_names.get(observation.get("sample_state_id"), "")
            refs = {
                evidence for evidence in proof[0][3]
                if _estimate_citation(sources[evidence], raw, sample)
            }
            if not refs:
                continue
            if authored:
                author_estimates.add(position)
                label = (_compact(anchor["metric"]), _compact(anchor.get("subject", "")))
            else:
                label = (member["metric_id"], raw)
            key = (
                observation["run_id"], bool(authored), label, member.get("entity_id") or observation.get("entity_id"),
                member.get("sign_convention"), value["value"], value["unit_raw"],
            )
            groups[key].append(position)
            citations[position] = refs

    removed = defaultdict(dict)
    for group in groups.values():
        unscoped = [position for position in group if observations[position[0]].get("sample_state_id") is None]
        authored = author_estimates.issuperset(group)
        if len(group) < 2 or (not authored and not set.intersection(*(citations[position] for position in group))):
            continue
        contexts = [observations[ref] for ref, _ in group]
        if len({item["sample_state_id"] for item in contexts} - {None}) > 1 or _condition_conflicts(contexts):
            continue
        members = {position: grounded[position[0]][position[1]] for position in group}
        qualifiers = {"operator", "basis", "uncertainty"}
        specified = {
            field: {member["value"][field] for member, _ in members.values() if field in member["value"]}
            for field in qualifiers
        }
        if any(len(values) > 1 for values in specified.values()):
            continue
        keeper = (unscoped or group)[0]
        if authored:
            keeper = max(group, key=lambda position: "operator" in members[position][0]["value"])
        primary, proof = members[keeper]
        if authored and any(field not in primary["value"] for field, values in specified.items() if values):
            continue
        if not authored:
            primary["value"].update({field: next(iter(values)) for field, values in specified.items() if values})
        refs = list(dict.fromkeys(ref for _, checks in members.values() for ref in checks[0][3]))
        primary["evidence_ids"] = refs
        label = next(
            (checks[0][0] for member, checks in members.values()
             if member is not primary and specified["basis"] and "basis" in member["value"]),
            proof[0][0],
        )
        proof[0] = (label, proof[0][1], {}, refs)
        measurements[keeper[0]] = [check for _, proofs in grounded[keeper[0]] for check in proofs]
        parent = observations[keeper[0]]
        if primary is not parent:
            parent["evidence_ids"] = list(dict.fromkeys([*parent["evidence_ids"], *refs]))
        for ref, index in group:
            if (ref, index) != keeper:
                removed[ref][index] = keeper[0]

    return _remove_merged_members(observations, measurements, grounded, removed)


def _remove_merged_members(observations, measurements, grounded, removed):
    """移除重复成员、整理余下的出处并保留机制主张的已核对支持。"""
    aliases = {}
    for ref, replaced in removed.items():
        item = observations[ref]
        members = grounded[ref]
        remaining = [entry for index, entry in enumerate(members) if index not in replaced]
        destinations = list(dict.fromkeys(replaced.values()))
        if not remaining:
            aliases[ref] = destinations
            del observations[ref]
            del measurements[ref]
            continue
        all_refs = {source for member, _ in members for source in member["evidence_ids"]}
        context = [source for source in item["evidence_ids"] if source not in all_refs]
        item["results"] = [member for member, _ in remaining]
        refs = [source for member, _ in remaining for source in member["evidence_ids"]]
        item["evidence_ids"] = list(dict.fromkeys([*context, *refs]))
        measurements[ref] = [check for _, proof in remaining for check in proof]
        aliases[ref] = [ref, *destinations]
    return aliases


def grounded_observation_members(observation: dict, proof: list[Measurement]) -> list[GroundedMember]:
    """保留逐成员的核对记录，数值和描述可各占一项。"""
    members = []
    offset = 0
    for member in observation.get("results", [observation]):
        count = 2 if "value" in member and "result" in member else 1
        members.append((member, proof[offset:offset + count]))
        offset += count
    return members


def _estimate_citation(source: dict, raw: str, sample: str) -> bool:
    """仅使用正文中唯一出现且未绑定指定样品的明确推算。"""
    if source["origin"] != "text":
        return False
    quote = _compact(source["quote"])
    matches = list(re.finditer(rf"(?<![0-9.+-]){re.escape(raw)}(?![0-9A-Za-z.])", quote))
    if len(matches) != 1:
        return False
    clause = re.split(r"[,;。！？!?]", quote[:matches[0].end()])[-1]
    return bool(_ESTIMATE.search(clause) and (not sample or _compact(sample) not in clause))


def _condition_conflicts(observations: list[dict]) -> bool:
    """保留同名条件的不同取值。"""
    seen = {}
    for observation in observations:
        for name, condition in observation["conditions"].items():
            key = _compact(name).casefold()
            reported = condition.get("value", (condition.get("minimum"), condition.get("maximum")))
            if isinstance(reported, str):
                reported = _compact(reported).replace("超过", ">")
            value = (
                reported,
                condition.get("unit", condition.get("unit_raw")), condition.get("operator"),
            )
            if seen.setdefault(key, value) != value:
                return True
    return False


def _compact(text: str) -> str:
    """收拢排版空白，保留量值单位的大小写。"""
    return "".join(unicodedata.normalize("NFKC", text).split())
