from collections.abc import Callable
import json
from typing import Any

from yuxi.knowledge.eval.metrics import EvaluationMetricsCalculator
from yuxi.knowledge.runtime import knowledge_base as kb_manager
from yuxi.utils import logger


def normalize_query_result(query_result: Any) -> tuple[str, list[dict[str, Any]]]:
    if isinstance(query_result, dict):
        return query_result.get("answer", ""), query_result.get("retrieved_chunks", [])
    if isinstance(query_result, list):
        return "", query_result
    return "", []


def build_answer_prompt(query: str, retrieved_chunks: list[dict[str, Any]], max_docs: int = 5) -> str:
    """前五个主命中连同其独立补充证据、来源和缺失状态进入回答输入。"""
    from yuxi.knowledge.related_evidence import pack_retrieval_evidence

    primary, related = pack_retrieval_evidence("", retrieved_chunks, max_docs=max_docs)
    context_docs = []
    for idx, chunk in enumerate([*primary, *related]):
        content = chunk.get("content", "")
        if content:
            metadata = chunk["metadata"]
            identity = {
                "chunk_id": chunk["id"],
                "file_id": chunk["file_id"],
                "role": metadata.get("evidence_role", "primary"),
                "source": metadata.get("source"),
                "source_metadata": metadata.get("source_metadata"),
                "anchor_chunk_id": metadata.get("anchor_chunk_id"),
                "status": metadata.get("evidence_status"),
            }
            context_docs.append(f"文档 {idx + 1} 来源：{json.dumps(identity, ensure_ascii=False)}\n{content}")

    context_text = "\\n\\n".join(context_docs)
    return (
        f"基于以下上下文信息，请回答用户的问题。\n\n"
        f"上下文信息：{context_text}\n\n"
        f"用户问题：{query}\n\n"
        "请根据上下文信息准确回答问题。\n\n"
        "如果上下文中缺少相关信息，请回答“信息不足，无法回答”。\n\n"
    )


async def generate_answer_if_needed(
    *,
    query: str,
    generated_answer: str,
    retrieved_chunks: list[dict[str, Any]],
    retrieval_config: dict[str, Any],
    select_model_fn: Callable[..., Any],
) -> str:
    if generated_answer:
        return generated_answer
    if not retrieved_chunks or not retrieval_config.get("answer_llm"):
        return ""

    logger.debug(f"使用 LLM {retrieval_config.get('answer_llm')} 生成答案...")
    try:
        llm = select_model_fn(model_spec=retrieval_config["answer_llm"])
        response = await llm.call(build_answer_prompt(query, retrieved_chunks), stream=False)
        generated_answer = response.content if response else ""
        logger.debug(f"LLM 生成的答案长度: {len(generated_answer) if generated_answer else 0}")
        return generated_answer
    except Exception as e:
        logger.error(f"LLM 生成答案失败: {e}")
        return ""


async def evaluate_question(
    *,
    kb_id: str,
    question_data: dict[str, Any],
    retrieval_config: dict[str, Any],
    has_gold_chunks: bool,
    has_gold_answers: bool,
    judge_llm: Any | None,
    select_model_fn: Callable[..., Any],
) -> dict[str, Any]:
    query = question_data["query"]
    query_result = await kb_manager.aquery(query, kb_id, **retrieval_config)
    generated_answer, retrieved_chunks = normalize_query_result(query_result)
    generated_answer = await generate_answer_if_needed(
        query=query,
        generated_answer=generated_answer,
        retrieved_chunks=retrieved_chunks,
        retrieval_config=retrieval_config,
        select_model_fn=select_model_fn,
    )

    current_metrics = {}
    retrieval_scores = {}
    answer_scores = {}

    if has_gold_chunks and question_data.get("gold_chunk_ids"):
        retrieval_scores = EvaluationMetricsCalculator.calculate_retrieval_metrics(
            retrieved_chunks, question_data["gold_chunk_ids"]
        )
        current_metrics.update(retrieval_scores)

    if has_gold_answers and question_data.get("gold_answer"):
        if judge_llm:
            answer_scores = await EvaluationMetricsCalculator.calculate_answer_metrics(
                query=query,
                generated_answer=generated_answer,
                gold_answer=question_data["gold_answer"],
                judge_llm=judge_llm,
            )
            current_metrics.update(answer_scores)
        else:
            logger.warning("需要计算答案指标但未配置 Judge LLM")

    return {
        "detail": {
            "query_text": query,
            "gold_chunk_ids": question_data.get("gold_chunk_ids"),
            "gold_answer": question_data.get("gold_answer"),
            "generated_answer": generated_answer,
            "retrieved_chunks": retrieved_chunks,
            "metrics": current_metrics,
        },
        "retrieval_scores": retrieval_scores,
        "answer_scores": answer_scores,
    }


def aggregate_metrics(
    retrieval_metrics_list: list[dict[str, float]],
    answer_metrics_list: list[dict[str, Any]],
    *,
    include_overall_score: bool = False,
) -> tuple[dict[str, Any], float | None]:
    overall_metrics = {}

    if retrieval_metrics_list:
        keys = retrieval_metrics_list[0].keys()
        for key in keys:
            overall_metrics[key] = sum(m.get(key, 0) for m in retrieval_metrics_list) / len(retrieval_metrics_list)

    if answer_metrics_list:
        scores = [m.get("score", 0) for m in answer_metrics_list]
        overall_metrics["answer_correctness"] = sum(scores) / len(scores) if scores else 0.0

    overall_score = EvaluationMetricsCalculator.calculate_overall_score(retrieval_metrics_list, answer_metrics_list)
    if include_overall_score:
        overall_metrics["overall_score"] = overall_score

    return overall_metrics, overall_score
