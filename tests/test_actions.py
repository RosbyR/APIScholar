"""Website actions: Jev triage and partial debates from buttons, run lock, and the routes."""

import http.client
import threading
import time
from urllib.parse import urlencode

import pytest

from paperbot import actions as act
from paperbot import friends as fr
from paperbot.lock import RunLock
from paperbot.models import Paper
from paperbot.serve import make_server

KEY = "owner-key"


def _queue(store, scores):
    ids = []
    for i, s in enumerate(scores):
        pid, _ = store.upsert(Paper(source="arxiv_rss", title=f"Queued {i}", abstract="wafer fab x",
                                    arxiv_id=f"2610.3{i:04d}"))
        store.update(pid, status="passed", jev_score=s)
        ids.append(pid)
    return ids


def _deps(fake_jev, fake_llm):
    return act.Deps(jev=lambda: fake_jev, llm=lambda: fake_llm,
                    get_text=lambda: (lambda paper: (paper["abstract"], "abstract only")))


def _run(cfg, action, deps, **kw):
    msgs = []
    act.run_action(cfg, action, msgs.append, deps=deps, **kw)
    return msgs


def test_debate_top_n_leaves_rest_queued(cfg, store, fake_jev, fake_llm):
    ids = _queue(store, [2.6, 3.9, 3.1, 2.4, 2.9])
    msgs = _run(cfg, "debate", _deps(fake_jev, fake_llm), top=2)
    statuses = {pid: store.get(pid)["status"] for pid in ids}
    assert statuses[ids[1]] == statuses[ids[2]] == "accepted"  # the two highest scores
    assert [statuses[i] for i in (ids[0], ids[3], ids[4])] == ["passed"] * 3  # not skimmed
    assert msgs[0].startswith("Debating 1 of 2: Queued 1") and "Debated 2: 2 kept" in msgs[-1]


def test_debate_selected_only_waiting_papers(cfg, store, fake_jev, fake_llm):
    ids = _queue(store, [2.6, 3.9, 3.1])
    store.update(ids[2], status="rejected")
    msgs = _run(cfg, "debate", _deps(fake_jev, fake_llm), ids=[ids[0], ids[2], "arxiv:nope"])
    assert store.get(ids[0])["status"] == "accepted"
    assert store.get(ids[1])["status"] == "passed" and store.get(ids[2])["status"] == "rejected"
    assert "Debated 1:" in msgs[-1]
    assert "Nothing to debate" in _run(cfg, "debate", _deps(fake_jev, fake_llm), ids=["arxiv:nope"])[-1]


def test_triage_action(cfg, store, fake_jev, fake_llm):
    store.upsert(Paper(source="arxiv_rss", title="Wafer yield", abstract="fab", arxiv_id="2610.39999"))
    msgs = _run(cfg, "triage", _deps(fake_jev, fake_llm))
    assert "Jev scored 1 papers: 1 passed the gate" in msgs[-1]


def test_actions_respect_run_lock(cfg, store, fake_jev, fake_llm):
    with RunLock(cfg.db_path.parent / "paperbot.lock"):
        msgs = _run(cfg, "triage", _deps(fake_jev, fake_llm))
    assert "Another run is in progress" in msgs[-1]
    assert fake_jev.calls == 0
    with pytest.raises(ValueError):
        _run(cfg, "rm -rf", _deps(fake_jev, fake_llm))


# ---- routes ------------------------------------------------------------------------------------
@pytest.fixture
def site(cfg, store):
    calls, gate = [], threading.Event()

    def runner(_cfg, action, report, ids=None, top=None):
        calls.append((action, ids, top))
        report(f"running {action}")
        gate.wait(5)
        report(f"finished {action}")

    jobs = fr.Jobs()
    server = make_server(cfg, KEY, 0, action_runner=runner, owner_jobs=jobs)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], calls, gate, jobs
    gate.set()
    server.shutdown()
    server.server_close()


def req(port, method, path, form=None, cookie=f"pb_key={KEY}"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Cookie": cookie} if cookie else {}
    body = None
    if form is not None:
        body = urlencode(form, doseq=True)
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    c.request(method, path, body=body, headers=headers)
    r = c.getresponse()
    return r.status, dict(r.getheaders()), r.read().decode()


def _wait(pred):
    for _ in range(100):
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_buttons_and_routes(site, store):
    port, calls, gate, jobs = site
    ids = _queue(store, [3.0, 2.8])
    _, _, bank = req(port, "GET", "/bank")
    assert 'value="harvest"' in bank and 'value="triage"' in bank and "data-confirm" in bank
    _, _, queue = req(port, "GET", "/queue")
    assert 'id="debate-form"' in queue and f'value="{ids[0]}" form="debate-form"' in queue

    status, headers, _ = req(port, "POST", "/actions", {"action": "debate", "mode": "selected", "back": "/queue",
                                                      "ids": [ids[1]]})
    assert status == 303 and headers["Location"] == "/queue"
    assert _wait(lambda: calls) and calls[0] == ("debate", [ids[1]], None)
    _, _, queue = req(port, "GET", "/queue")
    assert "running debate" in queue and "data-poll" in queue and "disabled" in queue
    req(port, "POST", "/actions", {"action": "triage", "back": "/bank"})  # busy: refused, not queued
    assert len(calls) == 1 and "Still busy" in jobs.status[0]
    gate.set()
    assert _wait(lambda: not jobs.running(0))
    _, _, queue = req(port, "GET", "/queue")
    assert "finished debate" in queue and "data-poll" not in queue

    gate.clear()
    req(port, "POST", "/actions", {"action": "debate", "mode": "top", "top": "99", "back": "/queue"})
    assert _wait(lambda: len(calls) == 2) and calls[1] == ("debate", None, act.MAX_DEBATE)
    gate.set()
    assert _wait(lambda: not jobs.running(0))
    req(port, "POST", "/actions", {"action": "debate", "mode": "selected", "back": "/queue"})
    assert len(calls) == 2 and "Tick at least one paper" in jobs.status[0]
    assert req(port, "POST", "/actions", {"action": "shell", "back": "/bank"})[0] == 400


def test_actions_owner_only(site, store):
    port, calls, _, _ = site
    friend = fr.create_friend(store, "Pat")
    assert req(port, "POST", "/actions", {"action": "triage"}, cookie=f"pb_user={friend.token}")[0] == 404
    assert req(port, "POST", "/actions", {"action": "triage"}, cookie="")[0] == 403
    assert calls == []
