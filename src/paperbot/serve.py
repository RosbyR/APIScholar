"""`paperbot serve`: the digest website from this machine, for a Cloudflare quick tunnel.

Pages are rendered from digests/*.md on every request, so a new digest shows up without a
rebuild. Thumb clicks (site/app.js) go straight into the local store as feedback, which also
makes them calibration labels.

The server listens on 127.0.0.1 only; `cloudflared tunnel --url` makes it public under a random
trycloudflare.com name. Because that is reachable by anyone who learns the name, every request
needs the access key: open the printed link once (`/?key=...`) and the key is kept in a
SameSite=Strict cookie, which also stops cross-site form posts.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import secrets
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .config import Config, load_config
from .site import ASSETS, _page, digest_html, index_html, load_digests, render_digest
from .site import views
from .store import Store

log = logging.getLogger(__name__)

COOKIE = "pb_key"
STEM_RE = re.compile(r"^/([\w-]+?)(?:\.html)?$")
ASSET_TYPES = {"/style.css": "text/css; charset=utf-8", "/app.js": "text/javascript; charset=utf-8"}
# Inline style attributes are allowed for the score bars; all database text is HTML-escaped.
CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; base-uri 'none'; "
       "form-action 'self'; frame-ancestors 'none'")


def new_key() -> str:
    return secrets.token_urlsafe(16)


def latest_votes(store: Store) -> dict[str, int]:
    """Current vote per paper as +1/-1, from labels: thumbs write labels too, and hand labels
    from `paperbot label` count as votes."""
    return {r[0]: 1 if r[1] else -1 for r in store.conn.execute("SELECT paper_id, relevant FROM labels")}


def make_handler(cfg: Config, key: str) -> type[BaseHTTPRequestHandler]:
    db_path, digest_dir = cfg.db_path, cfg.digest_dir

    class Handler(BaseHTTPRequestHandler):
        server_version = "paperbot"
        sys_version = ""

        def log_message(self, fmt, *args):  # route through logging instead of stderr
            log.debug("%s %s", self.address_string(), fmt % args)

        # ---- helpers ---------------------------------------------------------------
        def _send(self, status: int, body: str | bytes, ctype: str = "text/html; charset=utf-8",
                  headers: dict[str, str] | None = None) -> None:
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Robots-Tag", "noindex")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            jar = SimpleCookie(self.headers.get("Cookie", ""))
            got = jar[COOKIE].value if COOKIE in jar else ""
            return hmac.compare_digest(got.encode(), key.encode())

        def _gate(self, path: str, query: str) -> bool:
            """True if the request may proceed; otherwise a response was already sent."""
            given = (parse_qs(query).get("key") or [""])[0]
            if given and hmac.compare_digest(given.encode(), key.encode()):
                # Swap the key in the URL for a cookie, so it doesn't sit in history or get shared.
                self._send(HTTPStatus.SEE_OTHER, "", headers={
                    "Location": path or "/",
                    "Set-Cookie": f"{COOKIE}={key}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000",
                })
                return False
            if self._authorized():
                return True
            self._send(HTTPStatus.FORBIDDEN, "<p>Open the link printed by <code>paperbot serve</code> "
                                             "(it ends in <code>?key=…</code>).</p>")
            return False

        # ---- routes -------------------------------------------------------------------
        def do_GET(self):
            url = urlsplit(self.path)
            if not self._gate(url.path, url.query):
                return
            if url.path in ASSET_TYPES:
                return self._send(HTTPStatus.OK, (ASSETS / url.path[1:]).read_bytes(), ASSET_TYPES[url.path])
            if url.path == "/api/feedback":
                store = Store(db_path)
                try:
                    return self._send(HTTPStatus.OK, json.dumps(latest_votes(store)), "application/json")
                finally:
                    store.close()
            if url.path in ("/", "/index.html"):
                return self._send(HTTPStatus.OK, index_html(load_digests(digest_dir), views.nav("./")))
            if url.path in ("/votes", "/bank", "/queue", "/paper"):
                return self._view(url.path, {k: v[0] for k, v in parse_qs(url.query).items()})
            m = STEM_RE.match(url.path)
            md = digest_dir / f"{m.group(1)}.md" if m else None
            if md and md.is_file():
                page = render_digest(md.read_text(encoding="utf-8"), md.stem)
                return self._send(HTTPStatus.OK, digest_html(page, views.nav("")))
            self._send(HTTPStatus.NOT_FOUND, "<p>Not found. <a href=\"/\">All digests</a></p>")

        def _view(self, path: str, params: dict[str, str]):
            # Reload config each time so threshold/profile edits show without a restart.
            store = Store(db_path)
            try:
                ctx = views.Ctx(load_config(cfg.root), store)
                if path == "/votes":
                    result = views.votes_page(ctx, params.get("show", "relevant"))
                elif path == "/bank":
                    result = views.bank_page(ctx, params)
                elif path == "/queue":
                    result = views.queue_page(ctx)
                else:
                    result = views.paper_page(ctx, params.get("id", ""))
            finally:
                store.close()
            if result is None:
                return self._send(HTTPStatus.NOT_FOUND, _page("Not found", views.nav("") + "<p>No such paper.</p>", False))
            title, body = result
            self._send(HTTPStatus.OK, _page(title, body, back=False))

        def do_POST(self):
            url = urlsplit(self.path)
            if url.path != "/api/feedback":
                return self._send(HTTPStatus.NOT_FOUND, "not found", "text/plain")
            if not self._authorized():
                return self._send(HTTPStatus.FORBIDDEN, "missing or wrong access key", "text/plain")
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self._send(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "expected application/json", "text/plain")
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(length, 10_000)) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self._send(HTTPStatus.BAD_REQUEST, "invalid JSON", "text/plain")
            pid = body.get("paper_id") if isinstance(body, dict) else None
            rating = body.get("rating") if isinstance(body, dict) else None
            note = body.get("note") if isinstance(body, dict) else None
            if not isinstance(pid, str) or not pid.strip() or rating not in (1, -1):
                return self._send(HTTPStatus.BAD_REQUEST, "need paper_id and rating of 1 or -1", "text/plain")
            note = (note.strip()[:1000] or None) if isinstance(note, str) else None
            store = Store(db_path)
            try:
                store.add_feedback(pid.strip(), rating, note)
            except KeyError:
                return self._send(HTTPStatus.NOT_FOUND, f"no paper {pid!r} in the database", "text/plain")
            finally:
                store.close()
            log.info("vote %+d on %s%s", rating, pid, f" ({note})" if note else "")
            self._send(HTTPStatus.OK, json.dumps({"ok": True}), "application/json")

    return Handler


def make_server(cfg: Config, key: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(cfg, key))
