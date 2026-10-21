"""Friends: profiles, candidates, Jev-only scoring, training/calibration, harvest/triage
separation, and the invite/sign-in/permission rules of the website."""

import http.client
import json
import threading
from urllib.parse import urlencode

import pytest

from paperbot import friends as fr
from paperbot.models import Paper
from paperbot.serve import make_server
from paperbot.store import now_iso
from paperbot.triage import run_triage

KEY = "owner-key"


def _paper(store, title, cats=("cs.LG",), source="arxiv_rss", abstract="x", n=[0]):
    n[0] += 1
    pid, _ = store.upsert(Paper(source=source, title=title, abstract=abstract, arxiv_id=f"2610.2{n[0]:04d}",
                                categories=list(cats)))
    return pid


def _setup(store, name="Alex", cats=("cs.LG",), phrases=""):
    f = fr.create_friend(store, name)
    fr.save_profile(store, f, "Wafer-scale ML and fab data", "wafer defects\nfab scheduling", list(cats), phrases)
    return fr.get_friend(store, f.id)


# ---- profile and questions ----------------------------------------------------------------------
def test_save_profile_validation(store):
    f = fr.create_friend(store, "  Alex   P ")
    assert f.name == "Alex P" and not f.ready and len(f.token) >= 30
    with pytest.raises(fr.FriendError):
        fr.save_profile(store, f, "short", "a topic", ["cs.LG"], "")
    with pytest.raises(fr.FriendError):
        fr.save_profile(store, f, "Long enough interests", "", ["cs.LG"], "")
    with pytest.raises(fr.FriendError):  # no sources
        fr.save_profile(store, f, "Long enough interests", "a topic", ["not.a.cat"], "")
    with pytest.raises(fr.FriendError):
        fr.save_profile(store, f, "Long enough interests", "\n".join(f"t{i}" for i in range(9)), ["cs.LG"], "")
    assert fr.save_profile(store, f, "Long enough interests", "Topic A\ntopic a\nTopic B", ["cs.LG", "bogus"],
                           "graph neural nets, molecules: x|y")
    f = fr.get_friend(store, f.id)
    assert f.ready and f.topics == ["Topic A", "Topic B"] and f.categories == ["cs.LG"]
    assert f.phrases == ["graph neural nets molecules x y"]  # OpenAlex separators stripped


def test_questions_from_topics(store):
    f = _setup(store)
    prof = fr.friend_profile(f)
    from paperbot.triage.questions import build_questions
    qs = build_questions(prof)
    assert set(qs) == {"relevance", "facet_wafer_defects", "facet_fab_scheduling"}
    assert len(qs["relevance"]["criteria"]) == 5 and "wafer defects" in qs["relevance"]["criteria"][4]
    assert fr.facet_labels(f) == {"facet_wafer_defects": "wafer defects", "facet_fab_scheduling": "fab scheduling"}


# ---- candidates and scoring ------------------------------------------------------------------------
def test_candidates_and_scoring(store, fake_jev):
    lg = _paper(store, "Wafer map classifier", cats=["cs.LG"])
    bio = _paper(store, "Protein folding", cats=["q-bio.BM"])
    phrase = _paper(store, "Scheduling in a fab", cats=["math.OC"], abstract="semiconductor fab scheduling heuristics")
    f = _setup(store, cats=["cs.LG"], phrases="fab scheduling")
    ids = {r["id"] for r in fr.candidates(store, f, 100)}
    assert ids == {lg, phrase} and bio not in ids
    stats = fr.score_friend(store, fake_jev, f, 100, concurrency=2)
    assert stats.scored == 2 and stats.input_tokens == 500
    assert fr.candidates(store, f, 100) == []  # scored papers aren't candidates again
    assert fr.usage(store, f.id) == (2, 500)
    row = store.conn.execute("SELECT score, answers FROM user_scores WHERE user_id=? AND paper_id=?", (f.id, lg)).fetchone()
    assert row["score"] == 3.6 and json.loads(row["answers"])["facet_wafer_defects"]["noul"] == 0.95
    # Roz's own Jev columns are untouched
    assert store.get(lg)["jev_score"] is None


def test_profile_change_drops_scores_keeps_labels(store, fake_jev):
    pid = _paper(store, "Wafer thing")
    f = _setup(store)
    fr.score_friend(store, fake_jev, f, 10)
    fr.label(store, f.id, pid, True, "train")
    fr.save_profile(store, f, "Something else entirely", "new topic", ["cs.LG"], "")
    assert store.conn.execute("SELECT COUNT(*) FROM user_scores WHERE user_id=?", (f.id,)).fetchone()[0] == 0
    assert fr.label_counts(store, f.id) == (1, 1)


def test_old_papers_outside_backfill_window(store, fake_jev):
    pid = _paper(store, "Wafer ancient")
    store.conn.execute("UPDATE papers SET first_seen='2020-01-01T00:00:00+00:00' WHERE id=?", (pid,))
    store.conn.commit()
    assert fr.candidates(store, _setup(store), 10) == []


# ---- training and calibration ---------------------------------------------------------------------
def _scored_friend(store, scores):
    f = _setup(store)
    for i, s in enumerate(scores):
        pid = _paper(store, f"Paper {i}")
        store.conn.execute("INSERT INTO user_scores VALUES (?,?,?,?,?,?,?)", (f.id, pid, s, 0.8, "{}", "m", now_iso()))
    store.conn.commit()
    return f


def test_training_covers_buckets(store):
    f = _scored_friend(store, [0.1] * 30 + [3.8] * 3)
    picked = []
    for _ in range(4):
        row = fr.next_training_paper(store, f.id)
        picked.append(row["score"])
        fr.label(store, f.id, row["id"], row["score"] > 3, "train")
    assert 3.8 in picked and 0.1 in picked  # the rare high bucket is sampled early


def test_recalibrate_needs_enough_labels(store):
    f = _scored_friend(store, [0.2] * 40 + [3.5] * 10)
    rows = list(store.conn.execute("SELECT paper_id, score FROM user_scores WHERE user_id=?", (f.id,)))
    for r in rows[:5]:
        fr.label(store, f.id, r["paper_id"], r["score"] > 3, "train")
    assert fr.get_friend(store, f.id).threshold is None
    for r in rows:
        fr.label(store, f.id, r["paper_id"], r["score"] > 3, "train")
    t = fr.get_friend(store, f.id).threshold
    assert t is not None and 0.2 < t <= 3.5


# ---- harvest / triage separation ----------------------------------------------------------------
def test_friend_only_papers_skip_roz_triage(cfg, store, fake_jev):
    mine = _paper(store, "Wafer yield", source="arxiv_rss")
    theirs = _paper(store, "Wafer-like protein", source="arxiv_rss_friends")
    both = _paper(store, "Shared wafer paper", source="arxiv_rss_friends")
    store.upsert(Paper(source="arxiv_rss", title="Shared wafer paper", abstract="x", arxiv_id=store.get(both)["arxiv_id"]))
    run_triage(cfg, store, fake_jev)
    assert store.get(mine)["status"] != "new" and store.get(both)["status"] != "new"
    assert store.get(theirs)["status"] == "friends_only" and store.get(theirs)["jev_score"] is None
    # later seen by one of Roz's own sources: now it gets triaged
    store.upsert(Paper(source="openalex", title="Wafer-like protein", abstract="x", arxiv_id=store.get(theirs)["arxiv_id"]))
    run_triage(cfg, store, fake_jev)
    assert store.get(theirs)["status"] in ("passed", "rejected")


def test_harvest_extras(store):
    _setup(store, "A", cats=["cs.LG", "q-bio.GN"], phrases="single cell")
    _setup(store, "B", cats=["physics.comp-ph"], phrases="Wafer yield")
    off = fr.create_friend(store, "Not set up")
    cats, phrases = fr.harvest_extras(store, ["cs.LG"], ["wafer yield"])
    assert cats == ["physics.comp-ph", "q-bio.GN"] and phrases == ["single cell"]
    assert off.id  # friends without a profile contribute nothing


# ---- website -------------------------------------------------------------------------------------
@pytest.fixture
def site(cfg, store):
    onboarded = []

    def fake_onboard(_cfg, user_id, report):
        onboarded.append(user_id)
        report("done")

    server = make_server(cfg, KEY, 0, onboard=fake_onboard)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1], onboarded
    server.shutdown()
    server.server_close()


def req(port, method, path, body=None, cookie="", ctype=None, origin=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Host": f"127.0.0.1:{port}"}
    if cookie:
        headers["Cookie"] = cookie
    if origin:
        headers["Origin"] = origin
    data = None
    if isinstance(body, dict) and ctype == "json":
        data, headers["Content-Type"] = json.dumps(body), "application/json"
    elif isinstance(body, dict):
        data, headers["Content-Type"] = urlencode(body, doseq=True), "application/x-www-form-urlencoded"
    c.request(method, path, body=data, headers=headers)
    r = c.getresponse()
    return r.status, dict(r.getheaders()), r.read().decode()


OWNER = f"pb_key={KEY}"


def test_invite_join_and_setup_flow(site, store):
    port, onboarded = site
    status, _, body = req(port, "POST", "/friends", {"action": "invite", "name": "Sam"}, cookie=OWNER)
    assert status == 200 and "/join?t=" in body
    f = fr.list_friends(store)[0]
    status, headers, _ = req(port, "GET", f"/join?t={f.token}")
    assert status == 303 and headers["Location"] == "/me/topics" and "pb_user=" in headers["Set-Cookie"]
    me = f"pb_user={f.token}"
    assert req(port, "GET", "/me", cookie=me)[1].get("Location") == "/me/topics"  # must set up first
    status, _, body = req(port, "GET", "/me/topics", cookie=me)
    assert status == 200 and "Welcome, Sam" in body and 'value="q-bio.GN"' in body
    status, _, body = req(port, "POST", "/me/topics", {"interests": "x"}, cookie=me)
    assert status == 400 and "10–600 characters" in body
    form = {"interests": "Genomics and single-cell methods", "topics": "single cell\nCRISPR screens",
            "categories": ["q-bio.GN", "cs.LG"], "phrases": "single cell RNA"}
    status, headers, _ = req(port, "POST", "/me/topics", form, cookie=me)
    assert status == 303 and headers["Location"] == "/me/train"
    for _ in range(50):
        if onboarded:
            break
        threading.Event().wait(0.05)
    assert onboarded == [f.id]
    assert req(port, "GET", "/me", cookie=me)[0] == 200


def test_friend_permissions(site, store, fake_jev):
    port, _ = site
    pid = _paper(store, "Wafer paper")
    other = _paper(store, "Unscored paper", cats=["q-bio.BM"])
    f = _setup(store)
    fr.score_friend(store, fake_jev, f, 10)
    me = f"pb_user={f.token}"
    for path in ("/", "/bank", "/votes", "/queue", "/friends", "/api/feedback", f"/paper?id={pid}"):
        status, headers, body = req(port, "GET", path, cookie=me)
        assert "Roz" not in body and "Jev bank" not in body, path
    assert req(port, "GET", "/bank", cookie=me)[1]["Location"] == "/me"
    assert req(port, "POST", "/api/feedback", {"paper_id": pid, "rating": 1}, cookie=me, ctype="json")[0] == 404
    assert req(port, "POST", "/friends", {"action": "invite", "name": "x"}, cookie=me)[0] == 404
    # votes only on their own scored papers, and only same-origin
    assert req(port, "POST", "/api/me/votes", {"paper_id": other, "rating": 1}, cookie=me, ctype="json")[0] == 404
    assert req(port, "POST", "/api/me/votes", {"paper_id": pid, "rating": 1}, cookie=me, ctype="json",
               origin="https://evil.example")[0] == 403
    assert req(port, "POST", "/api/me/votes", {"paper_id": pid, "rating": 1}, cookie=me, ctype="json")[0] == 200
    assert json.loads(req(port, "GET", "/api/me/votes", cookie=me)[2]) == {pid: 1}
    assert store.conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0] == 0  # Roz's labels untouched
    # no cookie, bad token, disabled friend
    assert req(port, "GET", "/me")[0] == 403
    assert req(port, "GET", "/join?t=nope")[0] == 403
    fr.set_disabled(store, f.id, True)
    assert req(port, "GET", "/me", cookie=me)[0] == 403


def test_training_page_and_feed(site, store, fake_jev):
    port, _ = site
    a = _paper(store, "Wafer defects on 300mm")
    _paper(store, "Generic unrelated thing")
    f = _setup(store)
    fr.score_friend(store, fake_jev, f, 10)
    me = f"pb_user={f.token}"
    status, _, body = req(port, "GET", "/me/train", cookie=me)
    assert status == 200 and "0 of 40 labeled" in body and "3.60" not in body  # score hidden
    status, headers, _ = req(port, "POST", "/me/train", {"paper_id": a, "answer": "yes", "skip": ""}, cookie=me)
    assert status == 303 and fr.label_counts(store, f.id) == (1, 1)
    _, _, body = req(port, "GET", "/me", cookie=me)
    assert "Wafer defects on 300mm" in body and "Generic unrelated thing" not in body  # 0.3 < default 2.5
    assert 'data-api="/api/me/votes"' in body and 'class="chip"' in body
    _, _, admin = req(port, "GET", "/friends", cookie=OWNER)
    assert "Alex" in admin and "1 labels (1 relevant)" in admin and "2 scored" in admin
