"""Bounded, read-only PDF page rasterization. No shell or output files.

PDFium is not thread-safe; serialize its lifetime, including handle closure.
API/memory rules: https://pypdfium2.readthedocs.io/en/stable/python_api.html
"""
import io
import math
import threading
from contextlib import ExitStack

from mewcode.tools.artifact_read_cache import CountedReader

_PDFIUM_LOCK = threading.Lock()


def render_page(path, page_number, *, read_meter=None):
    with _PDFIUM_LOCK:
        import pypdfium2 as pdfium

        with ExitStack() as resources:
            source = str(path) if read_meter is None else resources.enter_context(CountedReader(path, read_meter))
            document = resources.enter_context(pdfium.PdfDocument(source))
            if not 1 <= page_number <= len(document):
                raise ValueError("PDF 页码超出范围")
            page = document[page_number - 1]
            try:
                width, height = page.get_size()
                if not all(math.isfinite(x) and x > 0 for x in (width, height)):
                    raise ValueError("PDF 页面尺寸无效")
                scale = min(2.0, 2400 / max(width, height), math.sqrt(4_000_000 / (width * height)))
                bitmap = page.render(scale=scale)
                try:
                    picture = bitmap.to_pil()
                    try:
                        output = io.BytesIO()
                        picture.save(output, format="PNG")
                        return output.getvalue(), len(document)
                    finally:
                        picture.close()
                finally:
                    bitmap.close()
            finally:
                page.close()


def extract_page_text(path, page_number, *, read_meter=None):
    """Seek to a selected page without copying the whole PDF into Python RAM."""
    with _PDFIUM_LOCK:
        import pypdfium2 as pdfium

        with ExitStack() as resources:
            source = str(path) if read_meter is None else resources.enter_context(CountedReader(path, read_meter))
            document = resources.enter_context(pdfium.PdfDocument(source))
            if not 1 <= page_number <= len(document):
                raise ValueError("PDF 页码超出范围")
            page = document[page_number - 1]
            resources.callback(page.close)
            textpage = page.get_textpage()
            resources.callback(textpage.close)
            if textpage.count_chars() > 1_000_000:
                raise ValueError("PDF 单页文字超过 100 万字符，请提供更小的页级证据")
            return textpage.get_text_bounded(errors="surrogatepass"), len(document)
