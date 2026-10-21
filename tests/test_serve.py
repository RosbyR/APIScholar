"""`paperbot serve`: key gate, pages, and votes written to the local store."""

import http.client
import json
import threading

import pytest

from paperbot.models import Paper
from paperbot.serve import make_server

KEY = "test-key"
DIGEST = """# Paper digest — 2026-10-14

### [A paper](https://arxiv.org/abs/2610.00001)

`paperbot feedback {pid} up` / `down`
"""


@pytest.fixture
def served(cfg, store):
    pid, _ = store.upsert(Paper(source="arxiv_rss", title="A paper", abstract="x", arxiv_id="2610.00001"))
    cfg.digest_dir.mkdir(parents=True, exist_ok=True)
    (cfg.digest_dir / "2026-10-14.md").write_text(DIGEST.format(pid=pid), encoding="utf-8")
    server = make_server(cfg, KEY, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], pid
    server.shutdown()
    server.server_close()


def req(port, method, path, body=None, cookie=True, ctype="application/json"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Cookie": f"pb_key={KEY}"} if cookie else {}
    if body is not None:
        headers["Content-Type"] = ctype
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    r = c.getresponse()
    return r.status, dict(r.getheaders()), r.read().decode()


def test_key_gate(served):
    port, _ = served
    assert req(port, "GET", "/", cookie=False)[0] == 403
    assert req(port, "GET", "/?key=wrong", cookie=False)[0] == 403
    status, headers, _ = req(port, "GET", "/2026-10-14?key=" + KEY, cookie=False)
    assert status == 303 and headers["Location"] == "/2026-10-14"
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Lax" in headers["Set-Cookie"]
    assert req(port, "POST", "/api/feedback", {"paper_id": "x", "rating": 1}, cookie=False)[0] == 403


def test_pages_and_assets(served):
    port, pid = served
    status, _, body = req(port, "GET", "/")
    assert status == 200 and 'href="2026-10-14"' in body
    status, headers, body = req(port, "GET", "/2026-10-14")
    assert status == 200 and f'data-id="{pid}"' in body and "default-src 'self'" in headers["Content-Security-Policy"]
    assert req(port, "GET", "/app.js")[0] == 200
    assert req(port, "GET", "/nope")[0] == 404
    assert req(port, "GET", "/..%2Fconfig%2Fsettings")[0] == 404


def test_votes_go_to_store(served, store):
    port, pid = served
    assert req(port, "POST", "/api/feedback", {"paper_id": pid, "rating": 1})[0] == 200
    assert req(port, "POST", "/api/feedback", {"paper_id": pid, "rating": -1, "note": " meh "})[0] == 200
    assert json.loads(req(port, "GET", "/api/feedback")[2]) == {pid: -1}
    rows = store.conn.execute("SELECT rating, note FROM feedback WHERE paper_id=? ORDER BY id", (pid,)).fetchall()
    assert [tuple(r) for r in rows] == [(1, None), (-1, "meh")]
    assert store.conn.execute("SELECT relevant FROM labels WHERE paper_id=?", (pid,)).fetchone()[0] == 0
    assert req(port, "POST", "/api/feedback", {"paper_id": "arxiv:0000.00000", "rating": 1})[0] == 404
    assert req(port, "POST", "/api/feedback", {"paper_id": pid, "rating": 3})[0] == 400
    assert req(port, "POST", "/api/feedback", {"paper_id": pid, "rating": 1}, ctype="text/plain")[0] == 415


def _seed(cfg, store):
    """Three scored papers: one labeled relevant, one in the debate queue, one with hostile text."""
    from paperbot.store import Store  # noqa: F401  (store fixture already open)
    ids = []
    for i, (title, score, status) in enumerate([("Wafer yield model", 3.9, "passed"),
                                                 ("Second queued", 2.6, "passed"),
                                                 ("<script>alert(1)</script> paper", 0.4, "rejected")]):
        pid, _ = store.upsert(Paper(source="arxiv_rss", title=title, abstract=f"abstract {i}", arxiv_id=f"2610.1000{i}"))
        answers = {"relevance": {"type": "score", "score": score, "confidence": 0.8,
                                 "probabilities": {"0": 0.1, "1": 0.1, "2": 0.2, "3": 0.3, "4": 0.3}},
                   "facet_semiconductor": {"type": "noul", "noul": 0.95 if i == 0 else 0.05}}
        store.update(pid, status=status, jev_score=score, jev_answers=answers, jev_model="jev-1.13.0")
        ids.append(pid)
    store.add_label(ids[0], True)
    return ids


def test_tracking_pages(served, cfg, store):
    port, _ = served
    ids = _seed(cfg, store)
    status, _, body = req(port, "GET", "/votes")
    assert status == 200 and "Wafer yield model" in body and "1 relevant: 1 score at or above" in body
    status, _, body = req(port, "GET", "/bank?facet=semiconductor")
    assert status == 200 and "Wafer yield model" in body and "Second queued" not in body
    _, _, body = req(port, "GET", "/bank?status=passed&sort=score")
    assert body.index("Wafer yield model") < body.index("Second queued")
    assert "<script>alert" not in body  # filtered out here, and escaped everywhere
    _, _, body = req(port, "GET", "/bank?q=script")
    assert "&lt;script&gt;" in body and "<script>alert" not in body
    status, _, body = req(port, "GET", "/queue")
    assert status == 200 and "2 paper(s) passed" in body and "#1" in body
    status, _, body = req(port, "GET", "/paper?id=" + ids[0])
    assert status == 200 and "Relevance level probabilities" in body and "relevant (" in body
    assert req(port, "GET", "/paper?id=nope")[0] == 404
    assert json.loads(req(port, "GET", "/api/feedback")[2])[ids[0]] == 1  # hand labels count as votes


def test_queue_cutoff(served, cfg, store):
    port, _ = served
    _seed(cfg, store)
    import yaml
    s = yaml.safe_load((cfg.root / "config" / "settings.yaml").read_text(encoding="utf-8"))
    s["debate"]["max_papers_per_run"] = 1
    (cfg.root / "config" / "settings.yaml").write_text(yaml.safe_dump(s), encoding="utf-8")
    _, _, body = req(port, "GET", "/queue")  # config is reloaded per request
    assert 'class="cutoff"' in body and body.index("Wafer yield model") < body.index("cutoff") < body.index("Second queued")
