"""Site stage: render digests/*.md into a static website (deployed to Cloudflare Pages).

Each `paperbot feedback <id> up` line in a digest becomes a pair of thumb buttons; `app.js`
posts clicks to the Pages Function in web/functions/api/feedback.js, which stores them in
Cloudflare D1, and `paperbot pull-feedback` brings them back into the local store.

Digest text is untrusted (titles, abstracts and LLM output can carry markup), so the
Markdown source is HTML-escaped before conversion and only http(s) links survive.
"""

from __future__ import annotations

import html
import re
import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import markdown

ASSETS = Path(__file__).parent
FEEDBACK_RE = re.compile(r"`paperbot feedback (\S+) up`(?: / `down`)?")
TOKEN = "PBFEEDBACK{}X"
HREF_RE = re.compile(r'href="([^"]*)"')
# Python-Markdown only starts a list after a blank line; digests put "- item" right under
# "**Evidence.**", which otherwise renders as one run-on paragraph.
LIST_AFTER_TEXT_RE = re.compile(r"(?m)^([^\s\-].*)\n(- )")
PRIORITY_RE = re.compile(r"<strong>(Read now|Skim|File away)</strong>")
PRIORITY_CLASS = {"Read now": "prio-read", "Skim": "prio-skim", "File away": "prio-file"}
NAV_RE = re.compile(r'^\s*(<nav class="tabs">.*?</nav>)', re.S)


@dataclass
class DigestPage:
    stem: str
    title: str
    papers: int
    body: str


def _safe_href(m: re.Match) -> str:
    url = html.unescape(m.group(1))
    return m.group(0) if url.startswith(("https://", "http://")) else 'href="#"'


def _buttons(paper_id: str) -> str:
    pid = html.escape(paper_id, quote=True)
    return (f'<span class="fb" data-id="{pid}">'
            f'<button type="button" data-r="1" aria-pressed="false" title="More like this">👍</button>'
            f'<button type="button" data-r="-1" aria-pressed="false" title="Less like this">👎</button>'
            f'<span class="fb-msg" role="status"></span></span>')


def render_digest(md: str, stem: str) -> DigestPage:
    lines = md.splitlines()
    title = lines[0][2:].strip() if lines and lines[0].startswith("# ") else stem
    ids: list[str] = []

    def stash(m: re.Match) -> str:
        ids.append(html.unescape(m.group(1)))  # matched in escaped text; _buttons re-escapes
        return TOKEN.format(len(ids) - 1)

    src = FEEDBACK_RE.sub(stash, html.escape(md, quote=False))
    src = LIST_AFTER_TEXT_RE.sub(r"\1\n\n\2", src)
    body = markdown.markdown(src, extensions=["sane_lists"])
    body = HREF_RE.sub(_safe_href, body)
    body = body.replace("<a href=", '<a rel="noopener" target="_blank" href=')
    body = PRIORITY_RE.sub(lambda m: f'<span class="badge {PRIORITY_CLASS[m.group(1)]}">{m.group(1)}</span>', body)
    body = _structure(body, stem)
    for i, pid in enumerate(ids):
        body = body.replace(TOKEN.format(i), _buttons(pid))
    papers = sum(1 for line in lines if line.startswith("### "))
    return DigestPage(stem=stem, title=title, papers=papers, body=body)


def _structure(body: str, stem: str) -> str:
    """Wrap the flat Markdown output into a hero, one card per paper (h3 section) and one block
    per h2 section (skim list, pipeline health). Horizontal rules become the card gaps."""
    parts = re.split(r"(?=<h[23]>)|<hr />", body)
    out, entries = [], []

    def flush():
        if entries:
            out.append('<div class="entries">' + "".join(entries) + "</div>")
            entries.clear()

    for i, part in enumerate(parts):
        part = part.strip()
        if not part:
            continue
        if i == 0 and part.startswith("<h1>"):
            out.append(f'<header class="hero"><span class="eyebrow">{_day_label(stem)}</span>{part}</header>')
        elif part.startswith("<h3>"):
            entries.append(f'<article class="entry">{part}</article>')
        elif part.startswith("<h2>"):
            flush()
            out.append(f'<section class="block">{part}</section>')
        else:
            flush()
            out.append(part)
    flush()
    return "\n".join(out)


def _day_label(stem: str) -> str:
    try:
        d = date.fromisoformat(stem[:10])
    except ValueError:
        return "Digest"
    return f"{d:%A} · Digest" + (f" #{stem[11:]}" if len(stem) > 10 else "")


def _page(title: str, body: str, back: bool, api: str = "/api/feedback", notes: bool = True) -> str:
    nav = '<nav><a href="./">← All digests</a></nav>' if back else ""
    # Pages start with their tab bar; lift it into the sticky top bar next to the brand.
    tabs = ""
    m = NAV_RE.match(body)
    if m:
        tabs, body = m.group(1), body[m.end():]
    home = "/me" if api == "/api/me/votes" else "/"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{html.escape(title)}</title>
<meta name="color-scheme" content="light dark">
<link rel="stylesheet" href="/style.css">
</head>
<body data-api="{html.escape(api)}" data-notes="{'on' if notes else 'off'}">
<header class="topbar"><div class="topbar-inner">
<a class="brand" href="{home}"><span class="brand-mark">p</span><span class="brand-name">paperbot</span></a>
{tabs}
</div></header>
<main>
{nav}
{body}
</main>
<script src="/app.js" defer></script>
</body>
</html>
"""


def build_site(digest_dir: Path, out_dir: Path) -> list[DigestPage]:
    """Write out_dir/index.html, one <stem>.html per digest, and the static assets.
    out_dir is wiped first so deleted digests disappear from the site."""
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    for asset in ("style.css", "app.js"):
        shutil.copy(ASSETS / asset, out_dir / asset)
    pages = load_digests(digest_dir)
    for page in pages:
        (out_dir / f"{page.stem}.html").write_text(digest_html(page), encoding="utf-8")
    (out_dir / "index.html").write_text(index_html(pages), encoding="utf-8")
    return pages


def load_digests(digest_dir: Path) -> list[DigestPage]:
    # Newest first; "2026-10-14-2" sorts before "2026-10-14", which is also the right order.
    return [render_digest(path.read_text(encoding="utf-8"), path.stem)
            for path in sorted(digest_dir.glob("*.md"), key=lambda p: p.stem, reverse=True)]


def digest_html(page: DigestPage, nav: str = "") -> str:
    return _page(page.title, nav + page.body, back=not nav)


def index_html(pages: list[DigestPage], nav: str = "") -> str:
    def card(p: DigestPage) -> str:
        try:
            d = date.fromisoformat(p.stem[:10])
            day, when = f"{d:%A}", f"{d:%b} {d.day}, {d:%Y}"
        except ValueError:
            day, when = "Digest", p.stem
        extra = f" · #{p.stem[11:]}" if len(p.stem) > 10 else ""
        return (f'<li><a href="{html.escape(p.stem)}"><span class="d-day">{day}{extra}</span>'
                f'<span class="d-date">{html.escape(when)}</span>'
                f'<span class="count">{p.papers} paper{"" if p.papers == 1 else "s"} debated</span></a></li>')

    intro = ('<header class="hero"><span class="eyebrow">Your reading list</span><h1>Paper digests</h1>'
             "<p>Each run's papers that survived Jev triage and the advocate–skeptic debate.</p></header>")
    if pages:
        body = f'{intro}<ul class="digests">{"".join(card(p) for p in pages)}</ul>'
    else:
        body = f"{intro}<p>No digests yet.</p>"
    return _page("Paper digests", nav + body, back=False)
