"""Website build, D1 vote import, and the OneNote skip off Windows."""

import json
import sys

import httpx
import pytest
import respx

from paperbot.deliver import run_deliver
from paperbot.feedback_sync import D1_QUERY_URL, D1Error, WATERMARK, pull_feedback
from paperbot.models import Paper
from paperbot.site import build_site, render_digest

DIGEST = """# Paper digest — 2026-10-14

1 paper(s) survived triage and debate.

### [Wafer maps & more](https://arxiv.org/abs/2610.09993)
*A. Author · arXiv · 2026-10-07*
**Skim** · relevance 3/5

**Summary.** Uses <script>alert(1)</script> and x < y.

`paperbot feedback arxiv:2610.09993 up` / `down`

---
## Also worth a look

- [Evil](javascript:alert(1)) · Jev 2.10 · arXiv
  First sentence. `paperbot feedback doi:10.1/x&y up`
"""


def test_render_digest_buttons_and_escaping():
    page = render_digest(DIGEST, "2026-10-14")
    assert page.title == "Paper digest — 2026-10-14"
    assert page.papers == 1
    assert '<span class="fb" data-id="arxiv:2610.09993">' in page.body
    assert 'data-id="doi:10.1/x&amp;y"' in page.body
    assert "paperbot feedback" not in page.body
    assert "<script>" not in page.body and "&lt;script&gt;" in page.body
    assert 'href="https://arxiv.org/abs/2610.09993"' in page.body
    assert "javascript:" not in page.body
    assert "Wafer maps &amp; more" in page.body  # escaped once, not twice


def test_build_site_index_order(tmp_path):
    d = tmp_path / "digests"
    d.mkdir()
    for stem in ("2026-10-12", "2026-10-14", "2026-10-14-2"):
        (d / f"{stem}.md").write_text(DIGEST.replace("2026-10-14", stem, 1), encoding="utf-8")
    out = tmp_path / "public"
    (out / "stale.html").parent.mkdir()
    (out / "stale.html").write_text("old", encoding="utf-8")
    pages = build_site(d, out)
    assert [p.stem for p in pages] == ["2026-10-14-2", "2026-10-14", "2026-10-12"]
    assert not (out / "stale.html").exists()
    assert {"index.html", "style.css", "app.js", "2026-10-12.html"} <= {p.name for p in out.iterdir()}
    index = (out / "index.html").read_text(encoding="utf-8")
    assert index.index('href="2026-10-14-2"') < index.index('href="2026-10-12"')


def _d1_url():
    return D1_QUERY_URL.format(account="acct", database="db")


def _d1_body(rows):
    return {"success": True, "errors": [], "messages": [],
            "result": [{"results": rows, "success": True, "meta": {}}]}


@respx.mock
def test_pull_feedback_imports_and_advances_watermark(store):
    pid, _ = store.upsert(Paper(source="arxiv_rss", title="A paper", abstract="x", arxiv_id="2610.00001"))
    route = respx.post(_d1_url()).mock(return_value=httpx.Response(200, json=_d1_body([
        {"id": 4, "paper_id": pid, "rating": 1, "note": None},
        {"id": 5, "paper_id": "arxiv:9999.99999", "rating": -1, "note": "nope"},
        {"id": 7, "paper_id": pid, "rating": -1, "note": "changed my mind"},
    ])))
    with httpx.Client() as c:
        stats = pull_feedback(store, c, "acct", "db", "tok")
    assert stats.imported == 2 and stats.unknown == ["arxiv:9999.99999"]
    assert json.loads(route.calls[0].request.content)["params"] == [0]
    assert route.calls[0].request.headers["authorization"] == "Bearer tok"
    assert store.get_watermark(WATERMARK) == "7"
    assert store.conn.execute("SELECT relevant FROM labels WHERE paper_id=?", (pid,)).fetchone()[0] == 0

    route.mock(return_value=httpx.Response(200, json=_d1_body([])))
    with httpx.Client() as c:
        assert pull_feedback(store, c, "acct", "db", "tok").imported == 0
    assert json.loads(route.calls[1].request.content)["params"] == [7]
    assert store.get_watermark(WATERMARK) == "7"


@respx.mock
def test_pull_feedback_bad_token(store):
    respx.post(_d1_url()).mock(return_value=httpx.Response(403, json={"success": False}))
    with httpx.Client() as c, pytest.raises(D1Error):
        pull_feedback(store, c, "acct", "db", "tok")
    assert store.get_watermark(WATERMARK) is None


def test_onenote_skipped_off_windows(cfg, store, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    path = tmp_path / "2026-10-14.md"
    path.write_text(DIGEST, encoding="utf-8")
    stats = run_deliver(cfg, store, path)  # default push: must not try COM on Linux
    assert stats.errors == [] and stats.delivered == []
    assert stats.skipped == ["onenote: needs Windows with OneNote desktop"]
