"""`paperbot serve`: the website from this machine, for a Cloudflare quick tunnel.

Two kinds of visitor:
- Roz (owner): digests, votes, Jev bank, debate queue, paper pages, and the Friends admin page.
  Authenticated by the per-start access key: open `/?key=...` once and it is kept in a cookie.
- Friends: invite links `/join?t=<token>` (created on the Friends page or `paperbot invite`) set
  a cookie for that friend. They only reach /me (their Jev-only feed), /me/train, /me/topics
  and their own votes API. They never see Roz's papers, votes or settings.

Pages are rendered from the database / digests/*.md on every request. The server listens on
127.0.0.1 only; `cloudflared tunnel --url` makes it public under a random trycloudflare.com
name, which is why everything needs a key or invite token. Cookies are HttpOnly and
SameSite=Lax; state-changing requests are POSTs that must be same-origin (Origin checked) and,
for the JSON APIs, application/json.
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
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlsplit

from . import actions as act
from . import friends as fr
from .config import Config, load_config
from .site import ASSETS, _page, digest_html, index_html, load_digests, render_digest
from .site import friend_views, views
from .store import Store

log = logging.getLogger(__name__)

COOKIE = "pb_key"          # owner
FRIEND_COOKIE = "pb_user"  # friend invite token
COOKIE_ATTRS = "Path=/; HttpOnly; SameSite=Lax"
STEM_RE = re.compile(r"^/([\w-]+?)(?:\.html)?$")
ASSET_TYPES = {"/style.css": "text/css; charset=utf-8", "/app.js": "text/javascript; charset=utf-8"}
OWNER_VIEWS = ("/votes", "/bank", "/queue", "/paper")
# Inline style attributes are allowed for the score bars; all database text is HTML-escaped.
CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; base-uri 'none'; "
       "form-action 'self'; frame-ancestors 'none'")


def new_key() -> str:
    return secrets.token_urlsafe(16)


def latest_votes(store: Store) -> dict[str, int]:
    """Current vote per paper as +1/-1, from labels: thumbs write labels too, and hand labels
    from `paperbot label` count as votes."""
    return {r[0]: 1 if r[1] else -1 for r in store.conn.execute("SELECT paper_id, relevant FROM labels")}


def friend_votes(store: Store, user_id: int) -> dict[str, int]:
    return {r[0]: 1 if r[1] else -1 for r in store.conn.execute(
        "SELECT paper_id, relevant FROM user_labels WHERE user_id = ?", (user_id,))}


def owner_nav(active: str) -> str:
    return views.nav(active).replace("</nav>", f'<a href="friends"{" aria-current=page" if active == "friends" else ""}>'
                                               "Friends</a></nav>")


OWNER_JOB = 0  # key in owner_jobs; friends' jobs are keyed by their user id in a separate Jobs


def actions_html(page: str, store: Store, cfg: Config, owner_jobs: fr.Jobs) -> str:
    """Buttons for the bank/queue pages plus the status of the current or last owner job."""
    running = owner_jobs.running(OWNER_JOB)
    off = " disabled" if running else ""
    status = owner_jobs.status.get(OWNER_JOB)
    box = ""
    if status:
        poll = ' data-poll="1"' if running else ""
        box = f'<p class="notice"{poll}>{"⏳ " if running else ""}{views.e(status)}</p>'

    def button(action: str, label: str, confirm: str = "") -> str:
        c = f' data-confirm="{views.e(confirm)}"' if confirm else ""
        return (f'<form method="post" action="/actions"{c}><input type="hidden" name="action" value="{action}">'
                f'<input type="hidden" name="back" value="/{page}"><button{off}>{label}</button></form>')

    if page == "bank":
        n_new = store.conn.execute("SELECT COUNT(*) FROM papers WHERE status IN ('new', 'triage_error')").fetchone()[0]
        n_friends = sum(1 for f in fr.list_friends(store) if f.ready and not f.disabled)
        parts = [button("harvest", "Fetch new papers"),
                 button("triage", f"Score {n_new:,} new with Jev" if n_new else "Score new with Jev (none waiting)",
                        f"Score {n_new:,} papers with Jev? About ${n_new * 1000 * 0.042 / 1e6:.3f}.")]
        if n_friends:
            parts.append(button("friends", f"Score friends' papers ({n_friends})",
                                "Score new papers for all friends with Jev? Usually well under $0.10."))
        return f'<div class="actions">{"".join(parts)}</div>{box}'
    if page == "queue":
        cost = store.conn.execute("SELECT COALESCE(SUM(cost_usd), 0), COUNT(DISTINCT paper_id) FROM llm_calls "
                                  "WHERE cost_usd IS NOT NULL").fetchone()
        per = cost[0] / cost[1] if cost[1] else 0.3
        tops = "".join(f'<option value="{n}"{" selected" if n == 3 else ""}>{n}</option>' for n in (1, 2, 3, 5, 8, 10))
        confirm = f"Run Claude debates? About ${per:.2f} per paper."
        return (f'<form id="debate-form" method="post" action="/actions" class="actions" data-confirm="{confirm}">'
                f'<input type="hidden" name="action" value="debate"><input type="hidden" name="back" value="/queue">'
                f'<button name="mode" value="selected"{off}>Debate ticked papers</button>'
                f'<span class="hint">or</span><button name="mode" value="top"{off}>Debate the top</button>'
                f'<select name="top" aria-label="How many">{tops}</select>'
                f'<span class="hint">~${per:.2f}/paper, max {act.MAX_DEBATE} per press. The rest of the queue is '
                f'left as is.</span></form>'
                f'<div class="actions">{button("digest", "Write digest")}'
                f'<span class="hint">Puts kept papers into a new digest.</span></div>{box}')
    return box


def make_handler(cfg: Config, key: str, onboard: Callable = fr.onboard,
                 jobs: fr.Jobs | None = None, action_runner: Callable = act.run_action,
                 owner_jobs: fr.Jobs | None = None) -> type[BaseHTTPRequestHandler]:
    db_path, digest_dir = cfg.db_path, cfg.digest_dir
    jobs = jobs or fr.Jobs()
    owner_jobs = owner_jobs or fr.Jobs()

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
            self.send_header("Referrer-Policy", "no-referrer")  # invite tokens must not leak via Referer
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, location: str, cookie: str | None = None) -> None:
            headers = {"Location": location}
            if cookie:
                headers["Set-Cookie"] = cookie
            self._send(HTTPStatus.SEE_OTHER, "", headers=headers)

        def _html(self, title: str, body: str, status: int = HTTPStatus.OK, friend: bool = False) -> None:
            self._send(status, _page(title, body, back=False, api="/api/me/votes" if friend else "/api/feedback",
                                     notes=not friend))

        def _cookie(self, name: str) -> str:
            jar = SimpleCookie(self.headers.get("Cookie", ""))
            return jar[name].value if name in jar else ""

        def _is_owner(self) -> bool:
            return hmac.compare_digest(self._cookie(COOKIE).encode(), key.encode())

        def _friend(self, store: Store) -> fr.Friend | None:
            f = fr.get_by_token(store, self._cookie(FRIEND_COOKIE))
            return f if f and not f.disabled else None

        def _same_origin(self) -> bool:
            origin = self.headers.get("Origin")
            if not origin:
                return True  # older browsers omit it on same-origin POSTs; SameSite=Lax still applies
            return urlsplit(origin).netloc == self.headers.get("Host", "")

        def _base_url(self) -> str:
            host = self.headers.get("Host", "127.0.0.1")
            proto = self.headers.get("X-Forwarded-Proto") or ("http" if host.startswith(("127.", "localhost")) else "https")
            return f"{proto}://{host}"

        def _form(self) -> dict[str, list[str]]:
            length = min(int(self.headers.get("Content-Length") or 0), 50_000)
            return parse_qs(self.rfile.read(length).decode("utf-8", "replace"))

        def _json(self) -> dict | None:
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                self._send(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "expected application/json", "text/plain")
                return None
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 10_000)) or b"{}")
            except (ValueError, json.JSONDecodeError):
                body = None
            if not isinstance(body, dict):
                self._send(HTTPStatus.BAD_REQUEST, "invalid JSON", "text/plain")
                return None
            return body

        def _forbidden(self) -> None:
            self._send(HTTPStatus.FORBIDDEN, _page("Not signed in", "<h1>Not signed in</h1><p>Open your personal "
                                                   "invite link again. If the site's address changed, ask for a new "
                                                   "one.</p>", back=False))

        # ---- GET ---------------------------------------------------------------------------
        def do_GET(self):
            url = urlsplit(self.path)
            params = {k: v[0] for k, v in parse_qs(url.query).items()}
            given = params.get("key", "")
            if given and hmac.compare_digest(given.encode(), key.encode()):
                # Swap the key in the URL for a cookie, so it doesn't sit in history or get shared.
                return self._redirect(url.path or "/", f"{COOKIE}={key}; {COOKIE_ATTRS}; Max-Age=2592000")
            if url.path in ASSET_TYPES:
                return self._send(HTTPStatus.OK, (ASSETS / url.path[1:]).read_bytes(), ASSET_TYPES[url.path])
            if url.path == "/join":
                return self._join(params.get("t", ""))
            if self._is_owner():
                return self._owner_get(url.path, params)
            store = Store(db_path)
            try:
                friend = self._friend(store)
                if friend is None:
                    return self._forbidden()
                fr.touch(store, friend.id)
                return self._friend_get(store, friend, url.path, params)
            finally:
                store.close()

        def _join(self, token: str) -> None:
            store = Store(db_path)
            try:
                f = fr.get_by_token(store, token)
            finally:
                store.close()
            if f is None or f.disabled:
                return self._forbidden()
            self._redirect("/me" if f.ready else "/me/topics",
                           f"{FRIEND_COOKIE}={token}; {COOKIE_ATTRS}; Max-Age=31536000")

        def _owner_get(self, path: str, params: dict[str, str]) -> None:
            if path == "/api/feedback":
                store = Store(db_path)
                try:
                    return self._send(HTTPStatus.OK, json.dumps(latest_votes(store)), "application/json")
                finally:
                    store.close()
            if path in ("/", "/index.html"):
                return self._send(HTTPStatus.OK, index_html(load_digests(digest_dir), owner_nav("./")))
            if path in OWNER_VIEWS:
                return self._owner_view(path, params)
            if path == "/friends":
                store = Store(db_path)
                try:
                    title, body = friend_views.admin_page(store, self._base_url(), owner_nav("friends"),
                                                          new_link=None)
                finally:
                    store.close()
                return self._html(title, body)
            if path.startswith("/me"):
                return self._html("Friends only", owner_nav("") + "<p>These pages are for friends. "
                                  "Use an invite link in a private window to see what they see.</p>")
            m = STEM_RE.match(path)
            md = digest_dir / f"{m.group(1)}.md" if m else None
            if md and md.is_file():
                page = render_digest(md.read_text(encoding="utf-8"), md.stem)
                return self._send(HTTPStatus.OK, digest_html(page, owner_nav("")))
            self._send(HTTPStatus.NOT_FOUND, "<p>Not found. <a href=\"/\">All digests</a></p>")

        def _owner_view(self, path: str, params: dict[str, str]) -> None:
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
                if result is not None and "<!--actions-->" in result[1]:
                    result = (result[0], result[1].replace(
                        "<!--actions-->", actions_html(path.strip("/"), store, ctx.cfg, owner_jobs), 1))
            finally:
                store.close()
            if result is None:
                return self._html("Not found", owner_nav("") + "<p>No such paper.</p>", HTTPStatus.NOT_FOUND)
            title, body = result
            self._html(title, body.replace(views.nav(_active(path)), owner_nav(_active(path)), 1))

        def _friend_get(self, store: Store, friend: fr.Friend, path: str, params: dict[str, str]) -> None:
            if path == "/api/me/votes":
                return self._send(HTTPStatus.OK, json.dumps(friend_votes(store, friend.id)), "application/json")
            if not friend.ready and path != "/me/topics":
                return self._redirect("/me/topics")
            status, running = jobs.status.get(friend.id), jobs.running(friend.id)
            if path == "/me/topics":
                title, body = friend_views.topics_page(friend, saved=params.get("saved") == "1")
            elif path == "/me/train":
                skip = [s for s in params.get("skip", "").split(",") if s][:20]
                title, body = friend_views.train_page(store, friend, skip, status, running)
            elif path in ("/me", "/"):
                try:
                    page = max(1, int(params.get("page") or 1))
                except ValueError:
                    page = 1
                title, body = friend_views.feed_page(store, friend, page, status, running)
            else:
                return self._redirect("/me")
            self._html(title, body, friend=True)

        # ---- POST --------------------------------------------------------------------------
        def do_POST(self):
            url = urlsplit(self.path)
            if not self._same_origin():
                return self._send(HTTPStatus.FORBIDDEN, "cross-origin request refused", "text/plain")
            if self._is_owner():
                if url.path == "/api/feedback":
                    return self._owner_vote()
                if url.path == "/friends":
                    return self._owner_friends_post()
                if url.path == "/actions":
                    return self._owner_action()
                return self._send(HTTPStatus.NOT_FOUND, "not found", "text/plain")
            store = Store(db_path)
            try:
                friend = self._friend(store)
                if friend is None:
                    return self._send(HTTPStatus.FORBIDDEN, "not signed in", "text/plain")
                if url.path == "/api/me/votes":
                    return self._friend_vote(store, friend)
                if url.path == "/me/topics":
                    return self._friend_topics(store, friend)
                if url.path == "/me/train":
                    return self._friend_train(store, friend)
                return self._send(HTTPStatus.NOT_FOUND, "not found", "text/plain")
            finally:
                store.close()

        def _owner_vote(self) -> None:
            body = self._json()
            if body is None:
                return
            pid, rating, note = body.get("paper_id"), body.get("rating"), body.get("note")
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

        def _owner_friends_post(self) -> None:
            form = self._form()
            action = (form.get("action") or [""])[0]
            store = Store(db_path)
            try:
                new_link = None
                if action == "invite":
                    try:
                        f = fr.create_friend(store, (form.get("name") or [""])[0])
                    except fr.FriendError as e:
                        return self._html("Friends", owner_nav("friends") + f'<p class="error">{views.e(e)}</p>',
                                          HTTPStatus.BAD_REQUEST)
                    new_link = f"{self._base_url()}/join?{urlencode({'t': f.token})}"
                    log.info("invited %s (user %s)", f.name, f.id)
                elif action in ("disable", "enable"):
                    try:
                        fr.set_disabled(store, int((form.get("id") or ["0"])[0]), action == "disable")
                    except ValueError:
                        pass
                    return self._redirect("/friends")
                title, body = friend_views.admin_page(store, self._base_url(), owner_nav("friends"), new_link)
            finally:
                store.close()
            self._html(title, body)

        def _owner_action(self) -> None:
            form = self._form()
            action = (form.get("action") or [""])[0]
            back = (form.get("back") or ["/bank"])[0]
            back = back if back in ("/bank", "/queue") else "/bank"
            if action not in act.ACTIONS:
                return self._send(HTTPStatus.BAD_REQUEST, "unknown action", "text/plain")
            ids, top = None, None
            if action == "debate":
                if (form.get("mode") or [""])[0] == "selected":
                    ids = [i for i in form.get("ids") or [] if i][: act.MAX_DEBATE]
                    if not ids:
                        owner_jobs.status[OWNER_JOB] = "Tick at least one paper first, or use Debate the top."
                        return self._redirect(back)
                else:
                    try:
                        top = max(1, min(act.MAX_DEBATE, int((form.get("top") or ["3"])[0])))
                    except ValueError:
                        top = 3
            live_cfg = load_config(cfg.root)
            started = owner_jobs.start(OWNER_JOB, lambda report: action_runner(live_cfg, action, report,
                                                                               ids=ids, top=top))
            if started:
                log.info("action %s started%s", action, f" ids={ids}" if ids else f" top={top}" if top else "")
            else:
                owner_jobs.status[OWNER_JOB] = "Still busy with the previous job; wait for it to finish."
            self._redirect(back)

        def _friend_vote(self, store: Store, friend: fr.Friend) -> None:
            body = self._json()
            if body is None:
                return
            pid, rating = body.get("paper_id"), body.get("rating")
            if not isinstance(pid, str) or rating not in (1, -1):
                return self._send(HTTPStatus.BAD_REQUEST, "need paper_id and rating of 1 or -1", "text/plain")
            scored = store.conn.execute("SELECT 1 FROM user_scores WHERE user_id = ? AND paper_id = ?",
                                        (friend.id, pid)).fetchone()
            if not scored:  # friends can only vote on papers in their own feed
                return self._send(HTTPStatus.NOT_FOUND, "not in your feed", "text/plain")
            fr.label(store, friend.id, pid, rating > 0, "feed")
            self._send(HTTPStatus.OK, json.dumps({"ok": True}), "application/json")

        def _friend_topics(self, store: Store, friend: fr.Friend) -> None:
            form = self._form()
            values = {"interests": (form.get("interests") or [""])[0], "topics": (form.get("topics") or [""])[0],
                      "phrases": (form.get("phrases") or [""])[0], "categories": form.get("categories") or []}
            try:
                changed = fr.save_profile(store, friend, values["interests"], values["topics"],
                                          values["categories"], values["phrases"])
            except fr.FriendError as e:
                title, body = friend_views.topics_page(friend, error=str(e), form=values)
                return self._html(title, body, HTTPStatus.BAD_REQUEST, friend=True)
            if changed:
                live_cfg = load_config(cfg.root)
                jobs.start(friend.id, lambda report: onboard(live_cfg, friend.id, report))
            self._redirect("/me/train" if changed else "/me")

        def _friend_train(self, store: Store, friend: fr.Friend) -> None:
            form = self._form()
            pid = (form.get("paper_id") or [""])[0]
            answer = (form.get("answer") or [""])[0]
            skip = [s for s in (form.get("skip") or [""])[0].split(",") if s][-20:]
            scored = store.conn.execute("SELECT 1 FROM user_scores WHERE user_id = ? AND paper_id = ?",
                                        (friend.id, pid)).fetchone()
            if scored and answer in ("yes", "no"):
                fr.label(store, friend.id, pid, answer == "yes", "train")
            self._redirect("/me/train" + (f"?{urlencode({'skip': ','.join(skip)})}" if answer == "skip" else ""))

    return Handler


def _active(path: str) -> str:
    return {"/votes": "votes", "/bank": "bank", "/queue": "queue"}.get(path, "")


def make_server(cfg: Config, key: str, port: int, onboard: Callable = fr.onboard,
                jobs: fr.Jobs | None = None, action_runner: Callable = act.run_action,
                owner_jobs: fr.Jobs | None = None) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port),
                               make_handler(cfg, key, onboard, jobs, action_runner, owner_jobs))
