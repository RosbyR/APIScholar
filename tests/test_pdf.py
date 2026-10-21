"""Digest PDF export: the browser call, the route, the button and failure handling.
No real browser is launched; `run` and the renderer are faked."""

import http.client
import threading
from pathlib import Path

import pytest

from paperbot.serve import make_server
from paperbot.site import render_digest
from paperbot.site import pdf

DIGEST = """# Paper digest — 2026-10-14

### [A paper](https://arxiv.org/abs/2610.00001)
*A. Author · arXiv · 2026-10-07*
**Read now** · relevance 5/5

**Evidence.**
- first
- second

`paperbot feedback arxiv:2610.00001 up` / `down`
"""


def test_render_pdf_calls_browser_with_isolated_profile(tmp_path):
    seen = {}

    def fake_run(args, **kw):
        seen["args"] = args
        out = Path(next(a for a in args if a.startswith("--print-to-pdf=")).split("=", 1)[1])
        src = Path(args[-1].removeprefix("file:///").replace("%20", " "))
        seen["html"] = src.read_text(encoding="utf-8") if src.exists() else ""
        out.write_bytes(b"%PDF-1.7 fake")

    data = pdf.render_pdf(render_digest(DIGEST, "2026-10-14"), browser="chrome", run=fake_run)
    assert data.startswith(b"%PDF")
    args = seen["args"]
    assert "--headless=new" in args and any(a.startswith("--user-data-dir=") for a in args)
    assert "--blink-settings=preferredColorScheme=1" in args and args[-1].startswith("file:")
    assert "<style>" in seen["html"] and "@media print" in seen["html"] and "Exported from paperbot" in seen["html"]


def test_render_pdf_errors(monkeypatch):
    page = render_digest(DIGEST, "2026-10-14")
    with pytest.raises(pdf.PdfError, match="didn't produce"):
        pdf.render_pdf(page, browser="chrome", run=lambda *a, **k: None)
    monkeypatch.setattr(pdf, "find_browser", lambda: None)
    with pytest.raises(pdf.PdfError, match="No Chrome or Edge"):
        pdf.render_pdf(page, run=lambda *a, **k: None)


@pytest.fixture
def site(cfg):
    cfg.digest_dir.mkdir(parents=True, exist_ok=True)
    (cfg.digest_dir / "2026-10-14.md").write_text(DIGEST, encoding="utf-8")
    calls = []

    def renderer(page):
        calls.append(page.stem)
        if page.stem == "2026-10-14":
            return b"%PDF-1.7 test"
        raise pdf.PdfError("boom")

    server = make_server(cfg, "k", 0, pdf_renderer=renderer)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], calls
    server.shutdown()
    server.server_close()


def get(port, path, cookie="pb_key=k"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path, headers={"Cookie": cookie} if cookie else {})
    r = c.getresponse()
    return r.status, dict(r.getheaders()), r.read()


def test_export_button_and_download(site, cfg):
    port, calls = site
    status, _, body = get(port, "/2026-10-14")
    assert status == 200 and b'href="/2026-10-14.pdf" download' in body and b"Export PDF" in body
    status, headers, body = get(port, "/2026-10-14.pdf")
    assert status == 200 and headers["Content-Type"] == "application/pdf" and body.startswith(b"%PDF")
    assert headers["Content-Disposition"] == 'attachment; filename="paper-digest-2026-10-14.pdf"'
    assert get(port, "/2026-01-01.pdf")[0] == 404
    assert get(port, "/..%2Fsecret.pdf")[0] == 404
    assert get(port, "/2026-10-14.pdf", cookie="")[0] == 403
    (cfg.digest_dir / "2026-10-15.md").write_text(DIGEST, encoding="utf-8")
    status, _, body = get(port, "/2026-10-15.pdf")
    assert status == 503 and b"PDF export failed: boom" in body and b"Save as PDF" in body
    assert calls == ["2026-10-14", "2026-10-15"]
