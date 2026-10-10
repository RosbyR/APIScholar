import io

import httpx
import pytest
import respx

from paperbot.fulltext import get_text
from paperbot.http import RateLimiter

reportlab = pytest.importorskip("reportlab")


def make_pdf(paragraphs: list[str]) -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    y = 750
    for para in paragraphs:
        for line in [para[i:i + 90] for i in range(0, len(para), 90)]:
            c.drawString(40, y, line)
            y -= 14
            if y < 40:
                c.showPage()
                y = 750
    c.save()
    return buf.getvalue()


@respx.mock
def test_pdf_full_text_and_fallback():
    body = ["1 Introduction"] + ["Lot genealogy graphs link wafers to tools and steps. " * 4] * 20
    pdf = make_pdf(body + ["References", "[1] Someone. A cited paper."])
    respx.get("https://arxiv.org/pdf/2610.04211").mock(
        return_value=httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"}))
    respx.get("https://example.org/broken.pdf").mock(return_value=httpx.Response(404))
    with httpx.Client() as client:
        text, kind = get_text({"id": "a", "pdf_url": "https://arxiv.org/pdf/2610.04211", "abstract": "abs"},
                              client, RateLimiter(0.0))
        assert kind == "full text"
        assert "Lot genealogy graphs" in text and "A cited paper" not in text
        text, kind = get_text({"id": "b", "pdf_url": "https://example.org/broken.pdf", "abstract": "abs"},
                              client, None)
        assert (text, kind) == ("abs", "abstract only")
        assert get_text({"id": "c", "pdf_url": None, "abstract": ""}, client, None)[1] == "abstract only"
