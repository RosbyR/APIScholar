"""OneNote delivery: Markdown -> page XML, the push wrapper, and once-only delivery."""

import subprocess
import xml.etree.ElementTree as ET
from datetime import date

import pytest

from paperbot.deliver import run_deliver
from paperbot.deliver.onenote import NS, PAGE_ID_PLACEHOLDER, OneNoteError, markdown_to_page_xml, push_page
from paperbot.digest import build_digest

Q = f"{{{NS}}}"

SAMPLE = """# Paper digest — 2026-10-12

1 paper(s) survived triage and debate.

### [Wafer maps & more](https://arxiv.org/abs/2610.09993)
*A. Author · arXiv · 2026-10-07*
**Skim** · relevance 3/5 · Jev 3.99

**Caveats.**
- Single dataset; no cross-fab validation.
- Uses `x < y` ]]> edge

---
## Pipeline health
- ⚠ 143 papers waiting
"""


def _texts(xml):
    root = ET.fromstring(xml.encode("utf-8"))
    return root, [(oe.get("quickStyleIndex"), oe.find(f"{Q}List") is not None, oe.find(f"{Q}T").text)
                  for oe in root.iter(f"{Q}OE")]


def test_markdown_to_page_xml_structure():
    xml = markdown_to_page_xml(SAMPLE, "Paper digest — 2026-10-12")
    root, oes = _texts(xml)                                   # well-formed XML
    assert root.get("ID") == PAGE_ID_PLACEHOLDER
    styles = {s.get("name"): s.get("index") for s in root.iter(f"{Q}QuickStyleDef")}
    assert set(styles) == {"PageTitle", "h1", "h2", "h3", "p"}
    assert root.find(f"{Q}Title/{Q}OE/{Q}T").text == "Paper digest — 2026-10-12"
    by_text = {t: (style, bullet) for style, bullet, t in oes}
    heading = next(t for t in by_text if "Wafer maps" in t)
    assert by_text[heading] == (styles["h3"], False)
    assert heading == '<a href="https://arxiv.org/abs/2610.09993">Wafer maps &amp; more</a>'
    assert by_text["Pipeline health"] == (styles["h2"], False)
    assert by_text["<i>A. Author · arXiv · 2026-10-07</i>"][1] is False
    assert "<b>Skim</b> · relevance 3/5 · Jev 3.99" in by_text
    assert by_text["Single dataset; no cross-fab validation."] == (styles["p"], True)
    # code span, HTML escaping, and a literal CDATA terminator all survive
    code = next(t for t in by_text if "Consolas" in t)
    assert code == 'Uses <span style="font-family:Consolas">x &lt; y</span> ]]&gt; edge'
    assert not any(t.startswith("# ") or t == "---" for t in by_text)   # H1 is the title; rules dropped


def _proc(rc=0, out="PAGE_ID={abc}{1}{B0}\n", err=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr=err)


def test_push_page_passes_args_and_parses_id(tmp_path):
    seen = {}

    def run(cmd, **kw):
        seen["cmd"] = cmd
        xml_file = cmd[cmd.index("-XmlFile") + 1]
        seen["xml"] = open(xml_file, encoding="utf-8").read()
        return _proc()

    pid = push_page("<x/>", "AI Agents", "Paper Digests", tmp_path / "s.ps1", run=run)
    assert pid == "{abc}{1}{B0}"
    cmd = seen["cmd"]
    assert cmd[cmd.index("-Notebook") + 1] == "AI Agents" and cmd[cmd.index("-Section") + 1] == "Paper Digests"
    assert seen["xml"] == "<x/>"


def test_push_page_errors():
    with pytest.raises(OneNoteError, match="Notebook 'X' is not open"):
        push_page("<x/>", "X", "S", "s.ps1", run=lambda *a, **k: _proc(1, "", "Notebook 'X' is not open"))
    with pytest.raises(OneNoteError, match="no page id"):
        push_page("<x/>", "X", "S", "s.ps1", run=lambda *a, **k: _proc(0, "hello"))


def _enable(cfg):
    cfg.settings["deliver"] = {"onenote": {"enabled": True, "notebook": "AI Agents", "section": "Paper Digests"}}


def test_deliver_once_then_skip_then_force(cfg, store, tmp_path):
    _enable(cfg)
    path = tmp_path / "2026-10-12.md"
    path.write_text(SAMPLE, encoding="utf-8")
    calls = []

    def push(xml, notebook, section, script):
        calls.append((notebook, section, script.name))
        assert "Wafer maps" in xml
        return f"page-{len(calls)}"

    assert run_deliver(cfg, store, path, push=push).delivered
    assert calls == [("AI Agents", "Paper Digests", "onenote_push.ps1")]
    again = run_deliver(cfg, store, path, push=push)
    assert not again.delivered and "already delivered" in again.skipped[0] and len(calls) == 1
    assert run_deliver(cfg, store, path, force=True, push=push).delivered and len(calls) == 2
    assert store.delivered("2026-10-12.md", "onenote")


def test_deliver_failure_is_reported_not_recorded(cfg, store, tmp_path):
    _enable(cfg)
    path = tmp_path / "2026-10-12.md"
    path.write_text(SAMPLE, encoding="utf-8")

    def push(*a):
        raise OneNoteError("Notebook 'AI Agents' is not open in OneNote")

    stats = run_deliver(cfg, store, path, push=push)
    assert stats.errors and not store.delivered("2026-10-12.md", "onenote")
    assert store.conn.execute("SELECT errors FROM runs WHERE stage='deliver:onenote'").fetchone()[0] == 1


def test_deliver_disabled(cfg, store, tmp_path):
    cfg.settings["deliver"] = {"onenote": {"enabled": False}}
    path = tmp_path / "d.md"
    path.write_text(SAMPLE, encoding="utf-8")
    assert run_deliver(cfg, store, path, push=lambda *a: pytest.fail("should not push")).skipped


def test_second_digest_same_day_does_not_overwrite(store, tmp_path):
    d = tmp_path / "digests"
    first = build_digest(store, d, today=date(2026, 10, 12), since_iso="2000-01-01")
    second = build_digest(store, d, today=date(2026, 10, 12), since_iso="2000-01-01")
    assert first.name == "2026-10-12.md" and second.name == "2026-10-12-2.md"
    assert second.read_text(encoding="utf-8").startswith("# Paper digest — 2026-10-12 (#2)")
