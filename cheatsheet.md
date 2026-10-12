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

## Scheduling (Windows Task Scheduler)

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
