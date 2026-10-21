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
    server = make_server(cfg.db_path, cfg.digest_dir, KEY, 0)
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
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
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
