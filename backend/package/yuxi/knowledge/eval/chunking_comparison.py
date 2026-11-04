"""以固定原文证据比较知识库切分方案，不改写知识库或金标准。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import unicodedata
from collections.abc import Awaitable, Callable
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path


async def compare_variants(
    cases: list[dict],
    variants: dict[str, str],
    *,
    query: Callable[..., Awaitable[list[dict]]],
    answer: Callable[[str], Awaitable[str]] | None = None,
    build_prompt: Callable[..., str],
    retrieval_config: dict,
) -> dict:
    """用同题同配置保存实际召回、回答上下文和待人工核验的答案。"""
    if len(variants) < 2 or len(set(variants.values())) != len(variants):
        raise ValueError("至少提供两个不同的方案知识库")
    retrieval_config = {**retrieval_config, "include_related_evidence": False}
    frozen_cases = json.loads(json.dumps(cases, ensure_ascii=False))
    _validate_cases(frozen_cases)
    report = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "cases_sha256": hashlib.sha256(
            json.dumps(frozen_cases, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
        "retrieval_config": dict(retrieval_config),
        "answer_context_limit": 5,
        "answer_quality": "pending_human_review" if answer else "not_run",
        "metric_note": "证据范围覆盖和条件词面覆盖不证明科学正确性；答案依据与条件归属需人工核验。",
        "variants": {},
    }
    for name, kb_id in variants.items():
        rows = []
        for case in frozen_cases:
            chunks = await query(case["query"], kb_id=kb_id, **retrieval_config)
            if not isinstance(chunks, list):
                raise ValueError(f"方案 {name} 未返回文档检索块列表")
            for chunk in chunks:
                source = (chunk.get("metadata") or {}).get("source_metadata")
                if not source or not source.get("source_sha256") or not source.get("source_spans"):
                    raise ValueError(f"方案 {name} 的检索块缺少可靠来源，请重新索引后比较")
            context_chunks = [chunk for chunk in chunks[:5] if chunk.get("content")]
            prompt = build_prompt(case["query"], chunks, max_docs=5)
            generated_answer = await answer(prompt) if answer and context_chunks else ""
            rows.append(
                {
                    "case_id": case["id"],
                    "query": case["query"],
                    "gold_evidence": case["gold_evidence"],
                    "required_conditions": case.get("required_conditions", []),
                    "answer_points": case["answer_points"],
                    "retrieved_chunks": chunks,
                    "answer_context_chunks": context_chunks,
                    "answer_prompt": prompt,
                    "retrieval_metrics": score_context(chunks, case),
                    "answer_context_metrics": score_context(context_chunks, case),
                    "generated_answer": generated_answer,
                    "answer_status": ("generated" if generated_answer else "no_context" if answer else "not_run"),
                    "human_review": {
                        "status": "pending",
                        "conditions_complete": None,
                        "answer_supported": None,
                        "unsupported_assertions": [],
                        "note": "",
                    },
                }
            )
        report["variants"][name] = {"kb_id": kb_id, "items": rows}
    return report


def score_context(chunks: list[dict], case: dict) -> dict:
    """按原文范围计算覆盖，条件只统计显式词面出现。"""
    covered: dict[str, list[tuple[int, int]]] = {}
    for chunk in chunks:
        source = (chunk.get("metadata") or {}).get("source_metadata") or {}
        fingerprint = source.get("source_sha256")
        for span in source.get("source_spans", []):
            start, end = span.get("start_char"), span.get("end_char")
            if fingerprint and type(start) is int and type(end) is int and 0 <= start < end:
                covered.setdefault(fingerprint, []).append((start, end))
    gold = case["gold_evidence"]
    evidence_results = []
    for item in gold:
        left, right = item["start_char"], item["end_char"]
        matched = 0
        cursor = left
        for start, end in sorted(covered.get(item["source_sha256"], [])):
            start, end = max(start, cursor), min(end, right)
            if start < end:
                matched += end - start
                cursor = end
        evidence_results.append(
            {**item, "covered": matched == right - left, "range_coverage": matched / (right - left)}
        )
    text = _normalize("\n".join(chunk.get("content", "") for chunk in chunks))
    condition_results = [
        {"alternatives": alternatives, "present": any(_contains_term(text, term) for term in alternatives)}
        for alternatives in case.get("required_conditions", [])
    ]
    return {
        "evidence_coverage": sum(item["covered"] for item in evidence_results) / len(gold) if gold else None,
        "evidence_details": evidence_results,
        "condition_text_coverage": (
            sum(item["present"] for item in condition_results) / len(condition_results) if condition_results else None
        ),
        "condition_details": condition_results,
    }


def load_cases(path: Path) -> list[dict]:
    """读取显式维护的 JSONL 问题，拒绝无原文依据的验收条目。"""
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    _validate_cases(cases)
    return cases


def validate_comparison_sources(cases: list[dict], corpora: list[Counter], embeddings: list[str]) -> None:
    """拒绝不同语料、不同嵌入或原文版本不匹配的对照。"""
    if len(corpora) < 2 or any(corpus != corpora[0] for corpus in corpora) or len(set(embeddings)) != 1:
        raise ValueError("对照方案必须使用同一批 Markdown 和同一 Embedding 模型")
    for case in cases:
        if any(item["source_sha256"] not in corpora[0] for item in case["gold_evidence"]):
            raise ValueError(f"题目 {case['id']} 的原文指纹不属于对照语料，请核对解析版本")


def _validate_cases(cases: list[dict]) -> None:
    """在问题输入边界校验固定证据和条件格式。"""
    ids = set()
    if not cases:
        raise ValueError("问题集不能为空")
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not case["id"].strip():
            raise ValueError("每题需要非空 id")
        if case["id"] in ids:
            raise ValueError("问题 id 不得重复")
        ids.add(case["id"])
        if not isinstance(case.get("query"), str) or not case["query"].strip():
            raise ValueError("每题需要非空 query")
        if (
            not isinstance(case.get("answer_points"), list)
            or not case["answer_points"]
            or any(not isinstance(point, str) or not point.strip() for point in case["answer_points"])
        ):
            raise ValueError("每题需要独立维护的 answer_points")
        evidence = case.get("gold_evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ValueError("每题需要非空 gold_evidence 原文范围")
        for item in evidence:
            if not isinstance(item, dict) or not re.fullmatch(r"[a-f0-9]{64}", str(item.get("source_sha256", ""))):
                raise ValueError("证据必须绑定 Markdown 的 source_sha256")
            start, end = item.get("line_start"), item.get("line_end")
            if type(start) is not int or type(end) is not int or not 0 < start <= end:
                raise ValueError("证据行号使用一基闭区间")
            start, end = item.get("start_char"), item.get("end_char")
            if type(start) is not int or type(end) is not int or not 0 <= start < end:
                raise ValueError("证据字符范围使用 Unicode 字符零基左闭右开区间")
        conditions = case.get("required_conditions", [])
        if not isinstance(conditions, list) or any(
            not isinstance(group, list)
            or not group
            or any(not isinstance(term, str) or not term.strip() for term in group)
            for group in conditions
        ):
            raise ValueError("required_conditions 每项必须是非空可接受表述列表")


def _normalize(text: str) -> str:
    """统一全半角与空白，仅用于显式词面覆盖。"""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _contains_term(text: str, term: str) -> bool:
    """允许等价空白，避免把 11 bar 当作 1 bar。"""
    normalized = _normalize(term)
    pattern = re.escape(normalized).replace(r"\ ", r"\s*")
    if re.match(r"[+-]?(?:\d|\.\d)", normalized):
        pattern = r"(?<![\w.+\-−–—])" + pattern
    elif normalized[0].isascii() and normalized[0].isalnum():
        pattern = r"(?<!\w)" + pattern
    if normalized[-1].isascii() and normalized[-1].isdigit():
        pattern += r"(?![\w.])"
    elif normalized[-1].isascii() and normalized[-1].isalnum():
        pattern += r"(?!\w)"
    return re.search(pattern, text) is not None


async def _run(args: argparse.Namespace) -> dict:
    """在已配置的 API 环境复用真实知识库与回答模型。"""
    from yuxi.knowledge.eval.evaluator import build_answer_prompt
    from yuxi.config import get_runtime_dir
    from yuxi.knowledge.implementations.milvus import MilvusKB
    from yuxi.knowledge.read_models import KnowledgeBaseConfig
    from yuxi.models import select_model
    from yuxi.repositories.knowledge_base_repository import KnowledgeBaseRepository
    from yuxi.repositories.knowledge_chunk_repository import KnowledgeChunkRepository
    from yuxi.repositories.knowledge_file_repository import KnowledgeFileRepository
    from yuxi.storage.postgres.manager import pg_manager

    variants = {}
    for value in args.variant:
        name, separator, kb_id = value.partition("=")
        if not separator or not name or not kb_id or name in variants:
            raise ValueError("方案格式为唯一名称=知识库ID")
        variants[name] = kb_id
    cases = load_cases(Path(args.cases))
    pg_manager.initialize()
    try:
        await pg_manager.require_current_schema()
        corpora = []
        configs = []
        kb_configs = {}
        file_params = {}
        for name, kb_id in variants.items():
            row = await KnowledgeBaseRepository().get_by_kb_id(kb_id)
            if row is None or row.kb_type != "milvus":
                raise ValueError("分块对照只支持文档型 Milvus 知识库")
            config = KnowledgeBaseConfig(
                kb_id=kb_id,
                kb_type=row.kb_type,
                embedding_model_spec=row.embedding_model_spec,
                query_params=row.query_params or {},
                additional_params=row.additional_params or {},
            )
            kb_configs[kb_id] = config
            chunks = await KnowledgeChunkRepository().list_by_kb_id(kb_id)
            if not chunks or any(not chunk.source_metadata for chunk in chunks):
                raise ValueError(
                    f"方案 {name} 的块缺少精确来源；请使用 Paper 或重叠为0的 Separator 重新入库，"
                    "General 等改写正文的策略暂不支持此对照"
                )
            file_sources = {chunk.file_id: chunk.source_metadata["source_sha256"] for chunk in chunks}
            files = await KnowledgeFileRepository().list_by_kb_id(kb_id)
            file_params[name] = [
                {
                    "file_id": file.file_id,
                    "filename": file.filename,
                    "source_sha256": file_sources[file.file_id],
                    "processing_params": {
                        key: value
                        for key, value in (file.processing_params or {}).items()
                        if key in {"chunk_preset_id", "chunk_parser_config", "chunk_engine_version"}
                    },
                }
                for file in files
                if file.file_id in file_sources
            ]
            corpus = Counter(file_sources.values())
            corpora.append(corpus)
            configs.append(config.embedding_model_spec)
        validate_comparison_sources(cases, corpora, configs)

        executor = await asyncio.to_thread(
            MilvusKB,
            str(get_runtime_dir() / "chunking_comparison"),
            require_existing_index=True,
            milvus_db=args.milvus_db,
        )

        async def query(text: str, *, kb_id: str, **options) -> list[dict]:
            """只在已有兼容集合检索，避免普通查询的创建或重建副作用。"""
            return await executor.aquery(text, kb_id, config=kb_configs[kb_id], **options)

        answer_model = select_model(model_spec=args.answer_model) if args.answer_model else None

        async def answer(prompt: str) -> str:
            """调用同一个真实模型；失败直接退出，不冒充未运行或空答案成功。"""
            response = await answer_model.call(prompt, stream=False)
            if not response or not isinstance(response.content, str) or not response.content.strip():
                raise ValueError("回答模型未返回正文")
            return response.content

        result = await compare_variants(
            cases,
            variants,
            query=query,
            answer=answer if answer_model else None,
            build_prompt=build_answer_prompt,
            retrieval_config={
                "search_mode": args.search_mode,
                "final_top_k": args.top_k,
                "similarity_threshold": 0.0,
                "use_graph_retrieval": False,
                "use_reranker": False,
                "vector_weight": 0.7,
                "bm25_weight": 0.3,
                "bm25_top_k": args.top_k,
                "bm25_drop_ratio_search": 0.0,
                "recall_top_k": args.top_k,
                "file_name": None,
            },
        )
        result.update(
            answer_model=args.answer_model,
            embedding_model=configs[0],
            milvus_database=args.milvus_db,
            cases=cases,
        )
        for name, files in file_params.items():
            result["variants"][name]["files"] = files
        return result
    finally:
        await pg_manager.close()


def main() -> None:
    """执行只读对照，结果写入新文件，保留人工答案审查空位。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", required=True, help="固定问题 JSONL，禁止用不同切分结果重新生成")
    parser.add_argument("--variant", action="append", required=True, help="方案名=已入库知识库ID，至少两组")
    parser.add_argument("--answer-model", help="与系统配置一致的回答模型 spec；省略时不生成或评价答案")
    parser.add_argument("--search-mode", choices=["vector", "keyword", "hybrid"], default="hybrid")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--milvus-db", default="default", help="包含现有索引的 Milvus 数据库，缺失时直接失败")
    parser.add_argument("--output", required=True, help="新建 JSON 报告文件，不覆盖已有核验记录")
    args = parser.parse_args()
    if not 1 <= args.top_k <= 100:
        parser.error("top-k 必须在 1 到 100 之间")
    output = Path(args.output)
    if output.exists():
        parser.error("输出文件已存在，请使用新文件名")
    if not output.parent.is_dir():
        parser.error("输出目录不存在")
    result = asyncio.run(_run(args))
    with output.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(f"已保存对照报告：{output}；答案质量仍需人工核验。")


if __name__ == "__main__":
    main()
