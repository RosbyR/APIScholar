# paperbot cheatsheet

Run everything from the project folder (`paperbot\paperbot`) in PowerShell.

**Activate once per terminal**, then type plain `paperbot ...`:

```powershell
.venv\Scripts\Activate.ps1
```

If PowerShell blocks the script, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.
You can also skip activation and call `.venv\Scripts\paperbot ...` directly.

Global options go **before** the command: `paperbot -v harvest` (verbose logging),
`paperbot --root <folder> status` (use a different project folder).

---

## The daily pipeline

| Command | What it does | Costs |
|---|---|---|
| `paperbot run` | Everything below in order: harvest → triage → debate → digest → deliver. Each stage still runs if an earlier one fails. Exits with code 2 if another run is already going. | ~$1–2 |
| `paperbot harvest` | Pulls new papers from the arXiv feeds and OpenAlex since the last run. | ~$0.02 |
| `paperbot triage` | Scores new papers with Jev and applies the threshold. | ~$0.02/day |
| `paperbot debate` | Debates the top 8 passing papers (advocate, skeptic, judge). Passing papers over the cap go to the skim list. | ~$0.25/paper |
| `paperbot digest` | Writes `digests\YYYY-MM-DD.md` with the debated papers, the skim list and a health section. A second digest on the same day becomes `YYYY-MM-DD-2.md`. | free |
| `paperbot deliver` | Pushes the newest digest to OneNote (**AI Agents › Paper Digests**), which syncs it to your iPad. Never pushes the same digest twice. | free |

## Checking on things

| Command | What it shows |
|---|---|
| `paperbot status` | Paper counts by status, plus the last 10 runs. |
| `paperbot show <id>` | Everything about one paper: Jev answers, the full debate transcript, the judge's verdict, costs. |
| `paperbot costs` | Claude spend per day and model for the last 30 days, and $/paper. |
| `paperbot costs --days 7` | The same for the last 7 days. |

Paper ids look like `arxiv:2610.09993` or `doi:10.1145/3843282.3844428`. Each digest entry lists its id.

## Teaching it your taste

| Command | When to use it |
|---|---|
| `paperbot feedback <id> up` | You liked a digest paper. It also counts as a calibration label. |
| `paperbot feedback <id> down --note "too theoretical"` | You didn't. The note is optional. |
| `paperbot label` | Hand-label 50 abstracts (y / n / s = skip / q = quit). Scores are hidden. You can quit any time. |
| `paperbot label --n 40` | Label a different number of abstracts. Papers you've already labeled are skipped. |
| `paperbot calibrate` | Shows precision, recall and volume per threshold, and suggests one that fits the budget. |
| `paperbot calibrate --beta 1` | The same, but weighs precision equally with recall. The default 2 favors not missing papers. |

**After calibrating**, change the threshold and re-sort existing papers:

1. Edit `triage.relevance_threshold` in `config\settings.yaml`.
2. Run `paperbot regate --dry-run` to preview what would change.
3. Run `paperbot regate` to apply it. No Jev calls, so it's free.

**After editing the rubric** in `config\profile.yaml` (the relevance levels, facets or reader block):

1. Run `paperbot triage --rescore` to re-score every undebated paper (~$0.12 per week of papers).
2. Run `paperbot calibrate`.
3. Update the threshold, then run `paperbot regate`.

## One-off and advanced

| Command | What it does |
|---|---|
| `paperbot backfill --days 7` | Pulls the last N days of arXiv history, for seeding after a gap. |
| `paperbot triage --limit 20` | Scores only 20 papers. Good for a quick check after changing something. |
| `paperbot deliver --file digests6-10-12.md` | Delivers a specific digest instead of the newest. |
| `paperbot deliver --force` | Pushes again even if already delivered (e.g. you deleted the page). |
| `paperbot debate --id <id> --id <id>` | Debates exactly these papers and leaves the rest of the queue alone. Good for testing prompts. |
| `paperbot <command> --help` | Built-in help for any command. |

## Reading digests on your phone (Cloudflare quick tunnel)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\serve.ps1
```

Prints a link like `https://<random-words>.trycloudflare.com/?key=...`. Open it on your phone or iPad.
The 👍/👎 buttons save straight into your database, the same as `paperbot feedback`. New digests
show up without restarting. Keep the window open; Ctrl+C stops it. The link changes every start,
and anyone without the key gets "forbidden". `paperbot serve` alone runs just the local server.

Tabs on the site:
- **Digests**: each run's digest, with 👍/👎 on every paper.
- **My votes**: everything you've labeled (hand labels and thumbs), relevant vs not, and how many of
  your relevant papers score under the gate threshold, i.e. the ones the gate would miss.
- **Jev bank**: every Jev-scored paper, with search, filters (status, facet, min score, voted or not),
  a score histogram, and vote buttons, so you can label straight from here.
- **Debate queue**: passed papers in the order the next run debates them, the cap line, what happens
  to the overflow, and the estimated Claude cost.
- Click any title for its page: Jev's level probabilities and facets, your votes, the full debate.

Buttons (they run in the background, one at a time, and never overlap a `paperbot run`; paid ones ask first):
- **Jev bank:** *Fetch new papers* (harvest), *Score N new with Jev* (triage), *Score friends' papers*.
- **Debate queue:** tick papers and press *Debate ticked papers*, or *Debate the top* 1–10. Only those
  papers are debated (max 20 per press). The rest of the queue stays queued, unlike a plain
  `paperbot debate`, which moves overflow to the skim list. *Write digest* puts kept papers in a new digest.
- The status box updates itself while a job runs.

## Sharing with friends

Friends get their own feed, scored by Jev only (no Claude), so it costs fractions of a cent per
friend per day. The Friends tab shows each friend's Jev spend.

1. Start the site (`scripts\serve.ps1`), open your link, go to **Friends**, type a name, and click
   **Create invite link**. Send that link to that one person; it signs them in.
2. They describe what they want, list up to 8 topics, and pick arXiv categories and/or search phrases.
   Saving fetches the last week of papers for their sources and scores them, which takes a minute or
   a few.
3. **Train**: they answer 👍/👎 for about 40 papers spread across the score range (scores hidden).
   After 20 labels with at least 3 👍, their cutoff sets itself and keeps updating as they vote.
4. **My feed**: papers above their cutoff, newest first. 👍/👎 there keeps teaching it.

New papers reach friends when you run `paperbot run` (it has a `friends` stage), or
`paperbot harvest; paperbot friends` on its own.

| Command | What it does |
|---|---|
| `paperbot invite "Name"` | Creates an invite and prints the link (uses the current tunnel address). |
| `paperbot invite` | Lists everyone's links for the current address. Re-send these after a restart. |
| `paperbot friends` | Jev-scores new papers for all friends (cap: `friends.max_papers_per_run`). |

**The address changes every time the tunnel restarts.** Friends' invite tokens stay valid; send them
the fresh links from the Friends page or `paperbot invite`. Disable someone on the Friends page to
lock them out. Friends never see your digests, votes, bank or queue, and you can't see their pages
without their link.

## In the cloud (optional, not set up)

Start a run from GitHub (Actions → daily → Run workflow); there is no schedule. Digests appear on your Cloudflare Pages site. Setup
and day-to-day use are in `DEPLOY.md`.

| Command | What it does |
|---|---|
| `paperbot site` | Builds the website from `digests\` into `web\public\`. |
| `paperbot pull-feedback` | Imports 👍/👎 votes from the website. Each cloud run does this itself. |
| `scripts\state-pull.ps1` | Downloads the cloud database and digests over your local copies (keeps a `.bak`). |
| `scripts\state-push.ps1` | Uploads your local database and digests to the cloud. Not while a cloud run is going. |

## Scheduling on this PC (Windows Task Scheduler, replaced by the cloud run)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install-schedule.ps1            # weekdays 06:00
powershell -ExecutionPolicy Bypass -File scripts\install-schedule.ps1 -At 07:30  # different time
Start-ScheduledTask -TaskName paperbot-daily                                     # run it now as a test
powershell -ExecutionPolicy Bypass -File scripts\install-schedule.ps1 -Remove    # turn it off
powershell -ExecutionPolicy Bypass -File scripts\run-daily.ps1                   # one run by hand, with logging
```

Each scheduled run appends to `logs\YYYY-MM-DD.log`. Only the last 60 logs are kept.

## Settings you'll actually touch

**`config\settings.yaml`**

| Setting | Default | Meaning |
|---|---|---|
| `triage.relevance_threshold` | 2.25 | Score needed to pass the gate (0–4). |
| `debate.max_papers_per_run` | 8 | Debates per run. Cost scales directly with this. |
| `debate.overflow` | `skim` | Passing papers over the cap go to the skim list. `wait` keeps them queued instead. |
| `digest.max_skim` | 25 | How many skim papers the digest shows. |
| `deliver.onenote.enabled` / `notebook` / `section` | on / AI Agents / Paper Digests | Where `deliver` puts the page. The notebook must be open in OneNote desktop; the section is created if missing. |
| `debate.advocate_model` / `skeptic_model` / `judge_model` | Sonnet / Sonnet / Opus | Models for each role. Add any new model's price under `debate.pricing`. |

**`config\profile.yaml`** holds your interests:
- the reader summary and priorities,
- the relevance levels Jev scores against,
- the topic facets,
- the arXiv categories and OpenAlex search queries.

**`.env`** holds the API keys and `PAPERBOT_CONTACT_EMAIL`. Never commit it. It's already in `.gitignore`.

## Where things live

| Path | What |
|---|---|
| `digests\` | One Markdown digest per run. |
| `data\paperbot.sqlite` | The database: papers, scores, debates, labels, costs. |
| `logs\` | Logs from scheduled runs. |
| `CLAUDE.md` | Full design notes and status, written for working on the code. |

## Running the tests

```powershell
python -m pytest -q   # offline, never touches the network or spends money
```

## Paper statuses (what `status` counts)

| Status | Meaning |
|---|---|
| `new` | Harvested, not yet scored. |
| `rejected` | Scored below the threshold. |
| `passed` | Above the threshold, waiting for a debate. |
| `skim` | Passed, but over the debate cap. It goes on the digest's skim list. |
| `accepted` | Debated and kept. It goes in the next digest. |
| `vetoed` | Debated, and the judge said skip it. |
| `digested` | Already delivered in a digest. |
| `triage_error` / `debate_error` | Failed. Retried automatically next run. |

## Troubleshooting

- **"skipping: another run holds data\paperbot.lock"**: a run is already going. A lock left by a crash is ignored after 6 hours. If you're sure nothing is running, delete `data\paperbot.lock`.
- **"Environment variable … is not set"**: a key is missing from `.env`.
- **OneNote delivery failed** (shown in the digest run output and `paperbot status`): check that the OneNote desktop app is installed, that the notebook is open in it, and that you're logged in to Windows (scheduled runs need your session). Then run `paperbot deliver` again.
- **arXiv returned 0 items on a weekend**: normal. Most arXiv feeds are empty on Saturday and Sunday.
- **The digest health section warns about errors**: run `paperbot status` and `paperbot show <id>` on a failed paper. Its `error` field has the reason.
