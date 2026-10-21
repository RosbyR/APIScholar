"""Pages for friends (feed, training, topics) and Roz's Friends admin page.

Same rule as views.py: every value from the database or a form goes through `e()`.
"""

from __future__ import annotations

import json
from urllib.parse import urlencode

from .. import friends as fr
from ..store import Store
from .views import e, q

FEED_PAGE = 30


def friend_nav(active: str) -> str:
    links = [("/me", "My feed"), ("/me/train", "Train"), ("/me/topics", "My topics")]
    return '<nav class="tabs">' + "".join(
        f'<a href="{h}"{" aria-current=page" if h == active else ""}>{t}</a>' for h, t in links) + "</nav>"


def _buttons(pid: str) -> str:
    return (f'<span class="fb" data-id="{e(pid)}">'
            '<button type="button" data-r="1" aria-pressed="false" title="Relevant">👍</button>'
            '<button type="button" data-r="-1" aria-pressed="false" title="Not relevant">👎</button>'
            '<span class="fb-msg" role="status"></span></span>')


def _link(row) -> str:
    url = row["url"] or (f"https://doi.org/{row['doi']}" if row["doi"] else "")
    if url.startswith(("https://", "http://")):
        return f'<a class="title" href="{e(url)}" rel="noopener" target="_blank">{e(row["title"])}</a>'
    return f'<span class="title">{e(row["title"])}</span>'


def _chips(friend: fr.Friend, answers_json: str) -> str:
    ans = json.loads(answers_json or "{}")
    names = fr.facet_labels(friend)
    hits = sorted(((names[k], v.get("noul") or 0) for k, v in ans.items()
                   if k in names and isinstance(v, dict) and (v.get("noul") or 0) >= 0.5), key=lambda x: -x[1])
    return "".join(f'<span class="chip" title="Jev {v:.2f}">{e(t)}</span>' for t, v in hits)


def _status_box(job_status: str | None, running: bool) -> str:
    if not job_status:
        return ""
    note = " <a href=\"\">Refresh</a> to update." if running else ""
    return f'<p class="notice">{e(job_status)}{note}</p>'


# ---- feed --------------------------------------------------------------------------------------
def feed_page(store: Store, friend: fr.Friend, page: int, job_status: str | None, running: bool) -> tuple[str, str]:
    c = store.conn
    t = friend.effective_threshold
    scored = c.execute("SELECT COUNT(*) FROM user_scores WHERE user_id = ?", (friend.id,)).fetchone()[0]
    n_labels, n_pos = fr.label_counts(store, friend.id)
    total = c.execute("SELECT COUNT(*) FROM user_scores WHERE user_id = ? AND score >= ?", (friend.id, t)).fetchone()[0]
    rows = list(c.execute(
        """SELECT p.*, s.score, s.answers FROM user_scores s JOIN papers p ON p.id = s.paper_id
           WHERE s.user_id = ? AND s.score >= ? ORDER BY COALESCE(p.published, p.first_seen) DESC, s.score DESC
           LIMIT ? OFFSET ?""", (friend.id, t, FEED_PAGE, (page - 1) * FEED_PAGE)))
    if friend.threshold is None:
        calib = (f'Showing papers Jev scores at least {t} out of 4 (the default). '
                 f'<a href="/me/train">Train it</a> on {fr.TRAINING_TARGET} papers to set your own cutoff '
                 f'({n_labels} labeled so far).')
    else:
        calib = (f"Your cutoff is {t} out of 4, set from your {n_labels} labels ({n_pos} relevant). "
                 'It updates as you vote and <a href="/me/train">train</a>.')
    items = []
    for r in rows:
        meta = " · ".join(e(x) for x in (r["venue"], (r["published"] or "")[:10]) if x)
        items.append(f"""<li class="paper">
<div class="row-top"><span class="score pass" title="Jev relevance for you">{r["score"]:.2f}</span><span class="meta">{meta}</span></div>
{_link(r)}
<div class="chips">{_chips(friend, r["answers"])}</div>
<details><summary>Abstract</summary><p>{e(r["abstract"]) or "<em>(no abstract)</em>"}</p></details>
{_buttons(r["id"])}
</li>""")
    pages = max(1, -(-total // FEED_PAGE))
    pager = '<nav class="pager">' + (
        f'<a href="/me{q(page=page - 1)}">← Newer</a>' if page > 1 else "<span></span>") + (
        f"<span>Page {page} of {pages} · {total:,} papers</span>") + (
        f'<a href="/me{q(page=page + 1)}">Older →</a>' if page < pages else "<span></span>") + "</nav>"
    empty = ("<p>No papers above your cutoff yet. They arrive each time Roz runs the pipeline; "
             "meanwhile, training helps.</p>" if scored else "<p>Nothing scored yet.</p>")
    body = f"""<h1>Papers for {e(friend.name)}</h1>
{_status_box(job_status, running)}
<p class="lead">{scored:,} papers scored for your topics. {calib} 👍/👎 here also teaches it.</p>
{pager if rows else ""}<ul class="papers">{"".join(items)}</ul>{pager if rows else empty}"""
    return "My papers", friend_nav("/me") + body


# ---- training --------------------------------------------------------------------------------
def train_page(store: Store, friend: fr.Friend, skip: list[str], job_status: str | None,
               running: bool) -> tuple[str, str]:
    n_labels, n_pos = fr.label_counts(store, friend.id)
    row = None
    for _ in range(25):  # try to avoid papers skipped in this session
        row = fr.next_training_paper(store, friend.id)
        if row is None or row["id"] not in skip:
            break
    done = n_labels >= fr.TRAINING_TARGET
    progress = (f'<p class="lead">{n_labels} of {fr.TRAINING_TARGET} labeled ({n_pos} relevant). '
                + ("Training set complete: your cutoff is set. Keep going any time to sharpen it." if done else
                   "Papers are picked across the whole score range, and the score is hidden so it doesn't "
                   "sway you. Answer for the abstract alone: would you want this in your feed?") + "</p>")
    if row is None:
        msg = ("Nothing to label right now." if not running else
               "Your papers are still being fetched and scored.")
        return "Train", friend_nav("/me/train") + f"<h1>Train your feed</h1>{_status_box(job_status, running)}{progress}<p>{msg}</p>"
    meta = " · ".join(e(x) for x in (row["venue"], (row["published"] or "")[:10]) if x)
    skip_val = ",".join([*skip[-20:], row["id"]])
    card = f"""<article class="card">
<p class="meta">{meta}</p>
<h2 class="plain">{e(row["title"])}</h2>
<p>{e(row["abstract"]) or "<em>(no abstract)</em>"}</p>
<form method="post" action="/me/train" class="answer">
<input type="hidden" name="paper_id" value="{e(row["id"])}">
<input type="hidden" name="skip" value="{e(skip_val)}">
<button name="answer" value="yes" class="yes">👍 Want it</button>
<button name="answer" value="no" class="no">👎 Not for me</button>
<button name="answer" value="skip" class="skip">Skip</button>
</form>
</article>"""
    return "Train", friend_nav("/me/train") + f"<h1>Train your feed</h1>{_status_box(job_status, running)}{progress}{card}"


# ---- topics form --------------------------------------------------------------------------------
def topics_page(friend: fr.Friend, error: str | None = None, form: dict | None = None,
                saved: bool = False) -> tuple[str, str]:
    form = form or {}
    interests = form.get("interests", friend.interests)
    topics = form.get("topics", "\n".join(friend.topics))
    phrases = form.get("phrases", "\n".join(friend.phrases))
    cats = set(form.get("categories", friend.categories))
    groups = []
    for group, items in fr.ARXIV_CATEGORIES.items():
        boxes = "".join(
            f'<label class="cat"><input type="checkbox" name="categories" value="{e(code)}"'
            f'{" checked" if code in cats else ""}> {e(label)} <code>{e(code)}</code></label>' for code, label in items)
        groups.append(f"<fieldset><legend>{e(group)}</legend>{boxes}</fieldset>")
    intro = ("" if friend.setup_at else
             f"<p class=\"lead\">Welcome, {e(friend.name)}! Tell paperbot what you want to read. It fetches new "
             "papers from the sources you pick and scores each one against your topics with a classifier. "
             "Then you train it on a few dozen papers so it learns your cutoff.</p>")
    body = f"""<h1>My topics</h1>{intro}
{f'<p class="error">{e(error)}</p>' if error else ""}{'<p class="notice">Saved. Fetching and scoring your papers in the background…</p>' if saved else ""}
<form method="post" action="/me/topics" class="topics">
<label for="interests"><b>What do you want papers about?</b> One or two sentences, in your own words.</label>
<textarea id="interests" name="interests" rows="3" maxlength="600" required>{e(interests)}</textarea>
<label for="topics"><b>Topics</b>: one per line, up to {fr.MAX_TOPICS}. Each one becomes a tag on matching papers.</label>
<textarea id="topics" name="topics" rows="5" required placeholder="e.g. protein structure prediction">{e(topics)}</textarea>
<p><b>Where to look</b>: pick arXiv categories (up to {fr.MAX_CATEGORIES}), add search phrases, or both.
Only papers from these are scored for you.</p>
{"".join(groups)}
<label for="phrases"><b>Search phrases</b>: one per line, up to {fr.MAX_PHRASES}. Searches journals and preprints via OpenAlex.</label>
<textarea id="phrases" name="phrases" rows="3" placeholder="e.g. single-cell RNA sequencing">{e(phrases)}</textarea>
<p class="meta">Changing these re-scores your papers. Your training labels are kept.</p>
<button type="submit">Save</button>
</form>"""
    return "My topics", friend_nav("/me/topics") + body


# ---- Roz's admin page --------------------------------------------------------------------------
def admin_page(store: Store, base_url: str, nav_html: str, new_link: str | None = None) -> tuple[str, str]:
    rows = []
    total_tokens = 0
    for f in fr.list_friends(store):
        papers, tokens = fr.usage(store, f.id)
        total_tokens += tokens
        n_labels, n_pos = fr.label_counts(store, f.id)
        scored = store.conn.execute("SELECT COUNT(*) FROM user_scores WHERE user_id = ?", (f.id,)).fetchone()[0]
        seen = store.conn.execute("SELECT last_seen FROM users WHERE id = ?", (f.id,)).fetchone()[0]
        link = f"{base_url}/join?{urlencode({'t': f.token})}"
        state = "disabled" if f.disabled else ("set up" if f.ready else "invited")
        rows.append(f"""<li class="paper">
<div class="row-top"><span class="status">{state}</span><b>{e(f.name)}</b>
<span class="meta">last seen {e((seen or "never")[:16].replace("T", " "))}</span></div>
<p class="meta">Topics: {e("; ".join(f.topics) or "—")}<br>Sources: {e(", ".join(f.categories + [f'"{p}"' for p in f.phrases]) or "—")}</p>
<p class="meta">{scored:,} scored · {n_labels} labels ({n_pos} relevant) · cutoff {f.effective_threshold}{"" if f.threshold is not None else " (default)"}
· Jev: {papers:,} papers, ${tokens * fr.JEV_USD_PER_M_INPUT / 1e6:.4f}</p>
<p class="invite"><input readonly value="{e(link)}" aria-label="Invite link for {e(f.name)}"></p>
<form method="post" action="/friends" class="inline">
<input type="hidden" name="id" value="{f.id}">
<button name="action" value="{"enable" if f.disabled else "disable"}">{"Re-enable" if f.disabled else "Disable"}</button>
</form>
</li>""")
    created = (f'<p class="notice">Invite created. Send this link (it works until the tunnel restarts; '
               f'then copy the new one from this page):<br><input readonly value="{e(new_link)}"></p>' if new_link else "")
    body = f"""<h1>Friends</h1>
<p class="lead">Each friend gets a Jev-only feed (no Claude) for their own topics. Links contain a secret,
so send each one only to that person. When the tunnel restarts the address changes: copy fresh links
from here. Total Jev spend on friends: ${total_tokens * fr.JEV_USD_PER_M_INPUT / 1e6:.4f}.</p>
{created}
<form method="post" action="/friends" class="filters">
<input type="hidden" name="action" value="invite">
<input name="name" placeholder="Friend's name" maxlength="60" required>
<button type="submit">Create invite link</button>
</form>
<ul class="papers">{"".join(rows) or "<li>No friends invited yet.</li>"}</ul>"""
    return "Friends", nav_html + body

