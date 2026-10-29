"""PDF 原生预检共同锁的回归用例，不启动解析服务。"""

import threading
from concurrent.futures import ThreadPoolExecutor


def test_preflight_waits_for_the_shared_pdfium_lock(monkeypatch):
    """预检不能绕过正在使用 PDFium 的原生调用锁。"""
    import yuxi.knowledge.utils.pdf_utils as pdf_utils

    native_entered = threading.Event()
    requested = threading.Event()

    class FakePage:
        """提供预检需要的页面接口。"""

        def get_size(self):
            """返回固定尺寸。"""
            return 600, 800

        def close(self):
            """模拟释放页面。"""

    class FakeDocument:
        """记录原生调用开始的时点。"""

        def __init__(self, path):
            """标记进入原生库。"""
            native_entered.set()

        def __len__(self):
            """返回一页。"""
            return 1

        def __getitem__(self, index):
            """返回待检页面。"""
            return FakePage()

        def close(self):
            """模拟释放文档。"""

    monkeypatch.setattr(pdf_utils.pdfium, "PdfDocument", FakeDocument)

    def invoke():
        """由另一个线程调用真实预检入口。"""
        requested.set()
        return pdf_utils.validate_pdf_page_tree_loadable("fake.pdf")

    with ThreadPoolExecutor(max_workers=1) as executor:
        with pdf_utils.PDFIUM_LOCK:
            future = executor.submit(invoke)
            assert requested.wait(timeout=2)
            assert not native_entered.wait(timeout=0.1)
        future.result(timeout=2)
    assert native_entered.is_set()
