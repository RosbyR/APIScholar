"""Spam filter: rules, harvest-time flagging, rescan/release, and staying out of triage,
debate queue and friends' feeds."""

import http.client
import json
import threading

import pytest

from paperbot import friends as fr
from paperbot import spam
from paperbot.harvest import run_harvest
from paperbot.models import Paper
from paperbot.serve import make_server
from paperbot.triage import run_triage

RULES = spam.SpamRules(patterns=[spam.re.compile(r"\bSTARLI\b", spam.re.I)])
ZENODO = "Zenodo (CERN European Organization for Nuclear Research)"
STARLI = "STARLI RESEARCH WORKS — INSTITUTE XXIII / FLAGSHIP 09 REVERSIBLE PROCESS PERTURBATION FOR WAFERS"


@pytest.mark.parametrize("title,authors,venue,hit", [
    (STARLI, ["33"], ZENODO, True),                                   # all three rules
    ("A normal title about wafers", ["33"], "arXiv", True),           # numeric "author"
    ("A normal title about wafers", [], ZENODO, False),               # missing authors isn't spam
    ("A Multi-Agent AI System for Tokamak Simulation", ["Sarthak Sharma"], ZENODO, False),
    ("SELF-CALIBRATING QUANTUM NANOFABRICATION AT THE LIMIT", ["Real Name"], ZENODO, True),  # shouting repo
    ("SELF-CALIBRATING QUANTUM NANOFABRICATION AT THE LIMIT", ["Real Name"], "IEEE TSM", False),
    ("Starlight imaging of exoplanets", ["A. B."], "arXiv", False),   # word boundary
    ("DNA", ["Real Name"], ZENODO, False),                            # short all-caps titles are fine
])
def test_rules(title, authors, venue, hit):
    assert bool(spam.spam_reasons(title, authors, venue, RULES)) is hit


def test_rules_from_settings_and_disable():
    r = spam.SpamRules.from_settings({"title_patterns": ["foo"], "repositories": ["Zenodo"]})
    assert r.repositories == ["zenodo"] and r.patterns[0].search("FOO bar")
    assert spam.spam_reasons(STARLI, ["33"], ZENODO, spam.SpamRules(enabled=False)) == []


class _Src:
    name = "openalex"

    def __init__(self, papers):
        self.papers = papers

    def fetch(self, since=None):
        yield from self.papers


def test_harvest_flags_spam_and_triage_skips_it(cfg, store, fake_jev):
    cfg.settings["spam"] = {"title_patterns": [r"\bSTARLI\b"]}
    papers = [Paper(source="openalex", title=STARLI, abstract="wafer fab", authors=["33"], venue=ZENODO,
                    doi="10.5281/zenodo.1"),
              Paper(source="openalex", title="Wafer yield with real data", abstract="fab", authors=["Ann Lee"],
                    venue="IEEE TSM", doi="10.1/real")]
    stats = run_harvest(cfg, store, [_Src(papers)])
    assert stats.per_source["openalex"]["spam"] == 1
    junk, real = store.get("doi:10.5281/zenodo.1"), store.get("doi:10.1/real")
    assert junk["status"] == "spam" and "STARLI" in junk["error"]
    run_triage(cfg, store, fake_jev)
    assert fake_jev.calls == 1 and store.get(junk["id"])["jev_score"] is None
    assert store.get(real["id"])["status"] == "passed"


def test_rescan_and_release(store):
    junk, _ = store.upsert(Paper(source="openalex", title=STARLI, abstract="x", authors=["33"], venue=ZENODO,
                                 doi="10.5281/zenodo.2"))
    ok, _ = store.upsert(Paper(source="openalex", title="Fine", abstract="x", authors=["Ann Lee"], doi="10.1/ok"))
    store.update(junk, status="passed", jev_score=3.7)
    debated, _ = store.upsert(Paper(source="openalex", title="STARLI already debated", abstract="x",
                                    authors=["Bo Li"], doi="10.1/deb"))
    store.update(debated, status="accepted")
    assert [f[0] for f in spam.rescan(store, RULES, dry_run=True)] == [junk]
    assert store.get(junk)["status"] == "passed"  # dry run changes nothing
    spam.rescan(store, RULES)
    assert store.get(junk)["status"] == "spam" and store.get(ok)["status"] == "new"
    assert store.get(debated)["status"] == "accepted"  # debated papers are left alone
    assert spam.release(store, junk) and store.get(junk)["status"] == "new"
    assert not spam.release(store, ok)


def test_spam_hidden_from_friends(store, fake_jev):
    junk, _ = store.upsert(Paper(source="arxiv_rss", title="Wafer STARLI thing", abstract="wafer", authors=["Al Bo"],
                                 arxiv_id="2610.40001", categories=["cs.LG"]))
    real, _ = store.upsert(Paper(source="arxiv_rss", title="Wafer real thing", abstract="wafer", authors=["Al Bo"],
                                 arxiv_id="2610.40002", categories=["cs.LG"]))
    f = fr.create_friend(store, "Kim")
    fr.save_profile(store, f, "Wafer-scale machine learning", "wafer defects", ["cs.LG"], "")
    f = fr.get_friend(store, f.id)
    spam.mark(store, junk, ["test"])
    assert {r["id"] for r in fr.candidates(store, f, 10)} == {real}


# ---- website button ------------------------------------------------------------------------------
@pytest.fixture
def site(cfg):
    server = make_server(cfg, "k", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def _post(port, body, cookie="pb_key=k"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", "/api/spam", body=json.dumps(body),
              headers={"Cookie": cookie, "Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"})
    r = c.getresponse()
    return r.status, r.read().decode()


def test_spam_button_routes(site, store):
    pid, _ = store.upsert(Paper(source="openalex", title="Looks fine", abstract="x", authors=["Ann Lee"], doi="10.1/b"))
    store.update(pid, status="passed", jev_score=3.0)
    assert _post(site, {"paper_id": pid, "spam": True})[0] == 200
    assert store.get(pid)["status"] == "spam" and "marked by hand" in store.get(pid)["error"]
    c = http.client.HTTPConnection("127.0.0.1", site, timeout=5)
    c.request("GET", "/bank?status=spam", headers={"Cookie": "pb_key=k"})
    body = c.getresponse().read().decode()
    assert "Looks fine" in body and 'data-spam="0"' in body
    assert _post(site, {"paper_id": pid, "spam": False})[0] == 200 and store.get(pid)["status"] == "new"
    store.update(pid, status="digested")
    assert _post(site, {"paper_id": pid, "spam": True})[0] == 409
    friend = fr.create_friend(store, "Nope")
    assert _post(site, {"paper_id": pid, "spam": True}, cookie=f"pb_user={friend.token}")[0] == 404


def test_cli_spam_command(cfg, store, capsys):
    from paperbot.cli import main
    pid, _ = store.upsert(Paper(source="openalex", title=STARLI, abstract="x", authors=["33"], venue=ZENODO,
                                doi="10.5281/zenodo.9"))
    store.update(pid, status="passed")
    assert main(["--root", str(cfg.root), "spam", "--dry-run"]) == 0
    assert "would mark 1 paper(s) as spam" in capsys.readouterr().out
    assert main(["--root", str(cfg.root), "spam"]) == 0 and store.get(pid)["status"] == "spam"
    assert main(["--root", str(cfg.root), "spam", "--release", pid]) == 0 and store.get(pid)["status"] == "new"
