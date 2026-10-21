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
from pathlib import Path

import markdown

ASSETS = Path(__file__).parent
FEEDBACK_RE = re.compile(r"`paperbot feedback (\S+) up`(?: / `down`)?")
TOKEN = "PBFEEDBACK{}X"
HREF_RE = re.compile(r'href="([^"]*)"')


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
    body = markdown.markdown(src, extensions=["sane_lists"])
    body = HREF_RE.sub(_safe_href, body)
    body = body.replace("<a href=", '<a rel="noopener" target="_blank" href=')
    for i, pid in enumerate(ids):
        body = body.replace(TOKEN.format(i), _buttons(pid))
    papers = sum(1 for line in lines if line.startswith("### "))
    return DigestPage(stem=stem, title=title, papers=papers, body=body)


def _page(title: str, body: str, back: bool) -> str:
    nav = '<nav><a href="./">← All digests</a></nav>' if back else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{html.escape(title)}</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
<main>
{nav}
{body}
</main>
<script src="app.js" defer></script>
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


def digest_html(page: DigestPage) -> str:
    return _page(page.title, page.body, back=True)


def index_html(pages: list[DigestPage]) -> str:
    if pages:
        items = "\n".join(
            f'<li><a href="{html.escape(p.stem)}">{html.escape(p.title)}</a>'
            f'<span class="count">{p.papers} debated</span></li>' for p in pages)
        body = f'<h1>Paper digests</h1>\n<ul class="digests">\n{items}\n</ul>'
    else:
        body = "<h1>Paper digests</h1>\n<p>No digests yet.</p>"
    return _page("Paper digests", body, back=False)
