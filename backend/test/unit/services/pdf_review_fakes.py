"""人工短例和显式回执，只替换 PDF/SDK 外部边界。"""

import json
from types import SimpleNamespace

from test.unit.services.pdf_model_fakes import configure_pdf_execution
from yuxi.knowledge.parser.result import ParsedDocument


def configure_review_case(monkeypatch, tmp_path, *, count=1, reply=None, before="The sample was 12 mg.", source="The sample was 13 mg.", native_geometry=True):
    """每页一个科学数字差异；可显式去掉字符几何以模拟弱原生证据。"""
    redis, _ = configure_pdf_execution(monkeypatch, limit=3)
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"explicit test PDF identity; extraction is a boundary fake")
    blocks = [{"id": f"b{page}", "page": page, "type": "text", "bbox": [.1, .2, .9, .4], "content": before} for page in range(1, count + 1)]
    natives = []
    for block in blocks:
        text = source
        advance = .8 / len(text)
        natives.append({"id": "native_" + block["id"], "page": block["page"], "bbox": list(block["bbox"]), "content": text, "chars": [
            {"text": char, "bbox": [.1 + index * advance, .2, .1 + (index + 1) * advance, .4], "baseline": (10., 10., 0.), "font_size": 10., "origin_normal": 10.}
            for index, char in enumerate(text)
        ]})

    if not native_geometry:
        for entry in natives:
            entry.pop("chars")

    def native(path, *, page_info):
        """提供具字符位置的人工原生候选，不冒充真实 PDF 测量。"""
        page_info["page_count"] = count
        return natives

    requests = []

    async def call(messages, stream=False):
        """回执只确认登记目标；业务 expected 独立写在测试内。"""
        payload = json.loads(messages[0].content[0]["text"].rsplit("\n", 1)[1])
        requests.append(payload)
        answer = reply(payload) if reply else acknowledge(payload)
        return SimpleNamespace(content=answer if isinstance(answer, str) else json.dumps(answer), usage_metadata={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14})

    def select(*args, **kwargs):
        """PDF 调用显式关闭单次时限和 SDK 自动重试。"""
        assert kwargs == {"timeout": None, "max_retries": 0, "safe_error_logging": True}
        return SimpleNamespace(call=call)

    monkeypatch.setattr("yuxi.knowledge.parser.pdf_native.read_native_blocks", native)
    monkeypatch.setattr("yuxi.knowledge.parser.pdf_native.render_pdf_region", lambda *args: b"manual image boundary fixture")
    monkeypatch.setattr("yuxi.models.chat.select_model", select)
    result = ParsedDocument(blocks=blocks, metadata={"parsed_pages": list(range(1, count + 1))})
    return result, str(path), requests, redis


def acknowledge(payload):
    """模拟无修改回执；有可靠原生冲突时此泛泛确认不能使问题完成。"""
    return {"batch_id": payload["batch_id"], "results": [
        {"issue_id": issue["issue_id"], "status": "unchanged", "patches": [], "evidence_ids": [entry["id"] for entry in issue["evidence"] if entry["source"] == "visual_region"], "reason": "人工短例原图读作 12 mg，保留原文"}
        for issue in payload["issues"]
    ]}


def correct_numeric_difference(payload):
    """人工原图短例明确为13，其他问题仍返回显式无修改回执。"""
    answer = acknowledge(payload)
    for row, issue in zip(answer["results"], payload["issues"], strict=True):
        if issue["kind"] != "critical_mismatch":
            continue
        assert [(target["old"], target.get("native_new")) for target in issue["targets"]] == [("12", "13")]
        row.update(status="corrected", patches=[{
            "target_id": issue["targets"][0]["id"], "old": "12", "new": "13",
        }], reason="人工原图短例明确读作13 mg")
    return answer
