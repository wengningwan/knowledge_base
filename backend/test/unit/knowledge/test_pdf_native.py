"""原生字符和旋转裁切的临时文件契约，无应用或外部服务。"""

import io

import pytest
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject, RectangleObject

from yuxi.knowledge.parser.pdf_native import read_native_blocks, read_native_image_regions, render_pdf_region


def _pdf(tmp_path, *, rotated=False):
    """生成含原生字符、CropBox 与纯色矩形的最小 PDF oracle。"""
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=100)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b"1 0 0 rg 20 10 40 20 re f 0 0 0 rg BT /F1 8 Tf 30 50 Td (Native 12 mg) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    if rotated:
        page.cropbox = RectangleObject([20, 10, 180, 90])
        page.rotate(90)
    path = tmp_path / ("rotated.pdf" if rotated else "native.pdf")
    writer.write(path)
    writer.close()
    return path


def test_native_characters_have_visible_normalized_positions(tmp_path):
    """原生文字保持数字、单位和逐字符位置。"""
    blocks = read_native_blocks(str(_pdf(tmp_path)))
    assert "Native 12 mg" in "\n".join(block["content"] for block in blocks)
    assert all(block["page"] == 1 for block in blocks)
    assert all(0 <= value <= 1 for block in blocks for value in block["bbox"])
    assert all(block["chars"] and "index" in block["chars"][0] for block in blocks)


def test_pdf_region_uses_visible_rotation_and_cropbox(tmp_path):
    """已知红矩形旋转后位于可见页左上，区域渲染不能选到其它象限。"""
    path = _pdf(tmp_path, rotated=True)
    data = render_pdf_region(str(path), 1, [0, 0, 0.25, 0.25])
    with Image.open(io.BytesIO(data)) as image:
        assert 59 <= image.width <= 64
        assert 119 <= image.height <= 124
        red, green, blue = image.convert("RGB").getpixel((image.width // 2, image.height // 2))
        assert red > 240 and green < 20 and blue < 20
    native = read_native_blocks(str(path))
    assert "Native 12 mg" in "\n".join(block["content"] for block in native)
    # CropBox 下 x=30、y=50 的文本旋转后应落在中上部，而非原始纸张坐标。
    assert native[0]["bbox"][0] > 0.4
    assert native[0]["bbox"][1] < 0.2


def test_image_region_preserves_complete_requested_area(tmp_path):
    """图片区域采用左上归一坐标，PNG 输出包含整个请求区域。"""
    path = tmp_path / "region.png"
    with Image.new("RGB", (80, 40), "white") as image:
        for x in range(40, 80):
            for y in range(20, 40):
                image.putpixel((x, y), (0, 0, 255))
        image.save(path)
    data = render_pdf_region(str(path), 1, [0.5, 0.5, 1, 1], scale=2)
    with Image.open(io.BytesIO(data)) as region:
        assert region.size == (80, 40)
        assert region.convert("RGB").getpixel((40, 20)) == (0, 0, 255)


def test_repeated_pdf_image_keeps_each_visible_location(tmp_path):
    """同一图像资源两次绘制必须保留两个位置，不能退化为附录去重图。"""
    writer = PdfWriter()
    page = writer.add_blank_page(width=200, height=100)
    image = DecodedStreamObject()
    image.set_data(b"\xff\x00\x00")
    image.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"), NameObject("/Width"): NumberObject(1), NameObject("/Height"): NumberObject(1), NameObject("/ColorSpace"): NameObject("/DeviceRGB"), NameObject("/BitsPerComponent"): NumberObject(8)})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"): DictionaryObject({NameObject("/Im0"): writer._add_object(image)})})
    stream = DecodedStreamObject()
    stream.set_data(b"q 40 0 0 20 20 10 cm /Im0 Do Q q 40 0 0 20 100 50 cm /Im0 Do Q")
    page[NameObject("/Contents")] = writer._add_object(stream)
    page.cropbox = RectangleObject([20, 10, 180, 90])
    page.rotate(90)
    path = tmp_path / "repeated-image.pdf"
    writer.write(path)
    writer.close()

    regions = read_native_image_regions(str(path))
    assert len(regions) == 2
    assert regions[0]["id"] != regions[1]["id"]
    assert [item["order"] for item in regions] == [0, 1]
    assert regions[0]["bbox"] == pytest.approx([0, 0, 0.25, 0.25], abs=0.001)
    assert regions[1]["bbox"] == pytest.approx([0.5, 0.5, 0.75, 0.75], abs=0.001)


@pytest.mark.parametrize("page,bbox,scale", [(0, [0, 0, 1, 1], 3), (1, [0.5, 0, 0.5, 1], 3), (1, [-0.1, 0, 1, 1], 3), (1, [0, 0, 1, 1], 0)])
def test_invalid_region_fails_before_opening_source(page, bbox, scale):
    """无效区域不能触发文件打开或原生渲染。"""
    with pytest.raises(ValueError):
        render_pdf_region("does-not-exist.pdf", page, bbox, scale)
