"""校对文案可读性与旧产物投影。"""

import pytest

from yuxi.knowledge.parser.pdf_review_messages import readable_review_reason


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (
            "原生区域未覆盖；缺少唯一正文插入锚点，未猜写新块",
            "疑似遗漏了一段正文，但系统无法确定补入位置，暂未自动补全，请对照原文检查。",
        ),
        (
            "数值、单位、引用或科学 token 有差异，本版 R1 不自动改写，需视觉裁决",
            "数字、单位、引用或科学符号存在差异，暂未自动修改，请对照原文检查。",
        ),
        ("missing_issue_id", "模型没有返回这条问题的核对结果，请重新核对。"),
        ("script_scope_loss_requires_native_geometry", "修改可能改变上下标的含义，缺少可靠原文依据，暂未应用。"),
    ],
)
def test_predefined_review_reasons_are_readable(reason, expected):
    """程序原因使用通俗文字，历史原句同样可转换。"""
    assert readable_review_reason(reason) == expected


def test_model_explanation_and_readable_message_are_preserved():
    """不替换模型自己的解释，也不重复改写已转换文案。"""
    reason = "原图中是 13 mg，请确认图片是否清晰。"
    assert readable_review_reason(reason) == reason
    readable = readable_review_reason("missing_issue_id")
    assert readable_review_reason(readable) == readable
