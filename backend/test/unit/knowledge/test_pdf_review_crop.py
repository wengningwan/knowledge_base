"""裁图区域以程序测量为准，不借模型自报位置证明覆盖。"""

from yuxi.knowledge.parser.pdf_native import review_crop_region


def test_crop_includes_native_line_prefix_missing_from_ocr_box():
    """原生行的Cite前缀不能因OCR块从This起始而被裁掉。"""
    block = {"id": "b", "page": 1, "bbox": [.15, .2, .5, .22], "content": "This: Journal"}
    native = [{"page": 1, "bbox": [.10, .2, .5, .22], "content": "Cite This: Journal"}]
    targets = [{"source_region": {"bbox": block["bbox"]}}]
    crop = review_crop_region(block, targets, native, [block])
    assert crop[0] < .10 and crop[2] > .5
    assert crop[1] < .2 and crop[3] > .22


def test_local_context_stops_before_neighbor_column_and_table():
    """外扩上下文仍需在真实邻栏和图表之间保持隔离。"""
    block = {"id": "b", "page": 1, "bbox": [.1, .2, .48, .22], "content": "The value is SA."}
    neighbors = [
        block,
        {"id": "right", "page": 1, "bbox": [.50, .18, .9, .25]},
        {"id": "table", "page": 1, "bbox": [.1, .235, .48, .4]},
    ]
    crop = review_crop_region(block, [{"source_region": {"bbox": block["bbox"]}}], [], neighbors)
    assert crop[0] <= .1 and crop[1] <= .2
    assert .48 <= crop[2] <= .49
    assert .22 <= crop[3] <= .2275


def test_character_target_only_expands_to_its_own_line():
    """有真实字符区域时只补同一行，不把整页作为局部目标。"""
    block = {"id": "b", "page": 2, "bbox": [.1, .2, .48, .6], "content": "A paragraph." * 40}
    native = [
        {"page": 2, "bbox": [.1, .3, .48, .32]},
        {"page": 2, "bbox": [.52, .3, .9, .32]},
        {"page": 2, "bbox": [.1, .4, .48, .42]},
    ]
    crop = review_crop_region(block, [{"source_region": {"bbox": [.25, .3, .28, .32]}}], native, [block])
    assert crop[0] < .1 and .48 < crop[2] < .52
    assert .2 < crop[1] < .3 and .32 < crop[3] < .4
