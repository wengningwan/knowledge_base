"""在三轮论文抽取中汇总覆盖缺口和复核结论。"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict

from yuxi.knowledge.adsorption_contract import REGISTRIES


def paper_review_checks(plans: list[dict], record: dict, notices: list, sources: dict) -> list[dict]:
    """把程序发现的缺口按原文归并，交给模型核对。"""
    checks = {}
    runs = {item["run_id"] for item in record["experiment_runs"]}
    results = defaultdict(set)
    for observation in record["observations"]:
        scope = (observation.get("sample_state_id"), tuple(observation.get("sample_state_ids", [])),
                 str(sorted(observation["conditions"].items())))
        for item in observation.get("results", [observation]):
            metric = item["metric_id"]
            value = item.get("value", {})
            fact = (scope, metric, value.get("basis") or "", str(value or item.get("result")))
            for ref in item["evidence_ids"]:
                results[observation["run_id"], ref].add(fact)
    for plan in plans:
        run_id = plan["run_id"]
        if run_id not in runs:
            _add_check(checks, plan["evidence_ids"], f'未登记计划中的实验 {plan["run_type"]}。', source=sources)
        for expected in plan["results"]:
            ref = expected["evidence_ids"][0]
            filled = results[run_id, ref]
            actual_fields = {_metric_key(label) for _, metric, basis, _ in filled for label in (metric, basis) if label}
            expected_fields = [field for field in expected["fields"] if _metric_key(field) not in actual_fields]
            shortfall = len(filled) < expected.get("expected_count", 1)
            if expected_fields or shortfall:
                _add_check(
                    checks, [ref],
                    f'{plan["run_type"]}的原文结果需核对。计划项目 {"、".join(expected["fields"])}。'
                    f'计划数量 {expected.get("expected_count", "未指定")}，现有 {len(filled)} 项。',
                    source=sources, record_id=run_id if run_id in runs else None, blocking=shortfall,
                )
    for series in record["series_sets"]:
        if series["source"]["data_availability"] == "needs_review":
            refs = [ref for ref in series["evidence_ids"] if sources[ref]["origin"] == "table"]
            if not refs and series.get("axis_consistency_status") == "needs_review":
                refs = series["evidence_ids"]
            _add_check(checks, refs, "曲线坐标或轴定义需对照原文核对。", source=sources,
                       record_id=series["series_set_id"])
    for kind, message, *details in notices:
        if kind in {"unverified_printed_pages", "printed_page_conflict"} or not details:
            continue
        detail = details[0]
        candidate = detail.get("record")
        if not isinstance(candidate, dict):
            continue
        ref_field = REGISTRIES.get(detail.get("collection"))
        record_id = candidate.get(ref_field) if ref_field else None
        refs = list(candidate.get("evidence_ids", [])) if isinstance(candidate.get("evidence_ids"), list) else []
        if refs:
            _add_check(checks, refs, message, source=sources, record_id=record_id, candidate=candidate)
    return list(checks.values())


def add_model_checks(checks: list[dict], targets: list[dict], sources: dict) -> list[dict]:
    """把总检发现的同一原文问题合并到同一个核对组。"""
    by_refs = {tuple(item["evidence_ids"]): item for item in checks}
    for target in targets:
        _add_check(by_refs, target["evidence_ids"], target["message"], source=sources)
    return [{"check_id": f"check:{index}", **check} for index, check in enumerate(by_refs.values(), 1)]


def review_notes(checks: list[dict], decisions: dict, remaining: list[dict], record: dict,
                 reviewed_refs: dict[str, str]) -> tuple[list[dict], list[dict]]:
    """依据第三轮结论和最终证据核验生成缺失说明与人工疑点。"""
    ids = {record["record_id"]} | {
        item[key] for name, key in REGISTRIES.items() for item in record[name]
    }
    outstanding_by_check = defaultdict(list)
    outstanding_refs = defaultdict(list)
    for item in remaining:
        if not item["blocking"]:
            continue
        scope = set(item["evidence_ids"])
        matching = [check for check in checks if scope.intersection(check["evidence_ids"])]
        if matching:
            target = max(matching, key=lambda check: (scope == set(check["evidence_ids"]),
                                                       len(scope.intersection(check["evidence_ids"]))))["check_id"]
        else:
            target = next((reviewed_refs[ref] for ref in item["evidence_ids"] if ref in reviewed_refs), None)
        if target is not None:
            outstanding_by_check[target].extend(item["messages"])
            outstanding_refs[target].extend(item["evidence_ids"])
        else:
            raise ValueError("总检查后的证据核验发现未复核的原文范围，停止发布")
    missing, issues = [], []
    for index, check in enumerate(checks, 1):
        decision = decisions[check["check_id"]]
        refs = list(dict.fromkeys([*decision["evidence_ids"], *check["evidence_ids"],
                                  *outstanding_refs[check["check_id"]]]))
        affected = list(dict.fromkeys(
            item for item in [*decision.get("record_ids", []), *check["record_ids"]] if item in ids
        )) or [record["record_id"]]
        status = decision["status"]
        outstanding = list(dict.fromkeys(outstanding_by_check[check["check_id"]]))
        if status in {"not_reported", "unavailable"} and decision.get("fields") and not outstanding:
            missing.append({
                "missing_id": f"missing:review:{index}", "record_id": affected[0], "status": status,
                "fields": decision["fields"], "reason": decision["message"], "evidence_ids": refs,
            })
        elif status == "uncertain" or outstanding:
            message = decision["message"]
            if status != "uncertain":
                message = "复核后仍缺少可核对的结果或归属。" + " ".join(outstanding)
            issues.append({
                "issue_id": f"issue:review:{index}", "issue_type": "unresolved_after_review", "severity": "warning",
                "message": message, "record_ids": affected, "evidence_ids": refs,
            })
    return missing, issues


def _add_check(checks, refs, message, *, source, record_id=None, candidate=None, blocking=True):
    """同一组出处只登记一个可复核问题。"""
    key = tuple(sorted({ref for ref in refs if isinstance(ref, str) and ref in source}))
    if not key:
        return
    item = checks.setdefault(key, {
        "evidence_ids": list(key), "messages": [], "record_ids": [], "candidates": [], "blocking": False,
    })
    item["blocking"] |= blocking
    if message not in item["messages"]:
        item["messages"].append(message)
    if isinstance(record_id, str) and record_id not in item["record_ids"]:
        item["record_ids"].append(record_id)
    if candidate is not None:
        item["candidates"].append(candidate)


def _metric_key(metric):
    """统一原文指标名的排版空白。"""
    return re.sub(r"[\s_$(){}]", "", unicodedata.normalize("NFKC", metric).casefold())
