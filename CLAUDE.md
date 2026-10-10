# paperbot — handoff spec for Claude Code

A passive research agent for Roz (applied mathematician; starting as a Process Quality
Engineer at Micron, Boise, March 2027). Every day it harvests new paper abstracts, scores
them with **Jev** (TypeSafe's typed classification model), sends the ones above a relevance
threshold through a **multi-agent debate** (advocate vs skeptic, then a judge with veto),
and writes a **digest** explaining each surviving paper and why Roz should care.

This file is the source of truth for continuing the build. Read it fully before changing code.

---

## 1. Architecture

```
            config/profile.yaml  (interest profile: sources, Jev rubric, reader priorities)
                 │                         │
                 ▼                         ▼
 scheduler ─► HARVEST ─► SQLite ─► JEV TRIAGE ─► GATE ──pass──► FULL TEXT ─► DEBATE ─► JUDGE ─► DIGEST ─► feedback
  (daily)    arXiv RSS   dedup     Score +       score ≥        arXiv PDF    advocate   keep/     digests/   thumbs →
             OpenAlex    by ID/    topic Nouls   threshold      or abstract  vs        veto      YYYY-MM-DD labels →
                         title                    (+ rescue)                 skeptic                .md       recalibrate
                                                   │fail                               │veto
                                                   ▼                                   ▼
                                                 rejected                            vetoed
```

Colour-coding used in the original design diagram: Jev = classification only (cheap, no
text generation); Claude = all writing (debate, judge); everything else = plain Python.

### Paper status lifecycle (`papers.status`)

`new → (triage) → passed | rejected | triage_error → (debate) → accepted | vetoed | debate_error → (digest) → digested`

- `triage_error` and `debate_error` are **retried automatically** on the next run (fail open:
  nothing is silently dropped; counts appear in the digest's health section).
- A paper only moves forward. Never re-triage `rejected` papers automatically; after a
  threshold change, re-gating from stored `jev_answers` is a TODO (see §6).

---

## 2. Status: what is done and how sure we are

Built in a sandbox that **could not reach arXiv, OpenAlex, TypeSafe or Anthropic**. Everything
below is implemented and covered by offline tests (`pytest`: 41 passing, mocked HTTP via
`respx`, fake Jev and fake LLM in `tests/conftest.py`). An offline end-to-end run through the
CLI (seed → triage → debate → digest → feedback → calibrate → label) also worked.

| Component | File | Offline-tested | Verified against live API |
|---|---|---|---|
| Config loading | `config.py`, `config/*.yaml` | yes | n/a |
| Paper model, ID normalization | `models.py` | yes | n/a |
| SQLite store, dedup, watermarks, run log, labels, feedback | `store.py` | yes | n/a |
| Rate limiter, GET with backoff | `http.py` | yes | **no** |
| arXiv RSS adapter | `harvest/arxiv.py` | yes (fixture) | **no** — fixture written from the documented 2024+ RSS format, not a captured live item |
| arXiv query API (backfill) | `harvest/arxiv.py` | yes (fixture) | **no** |
| OpenAlex adapter | `harvest/openalex.py` | yes (fixture) | **no** — filter/select/cursor syntax from docs |
| Harvest orchestration | `harvest/__init__.py` | yes | n/a |
| Jev client | `triage/jev.py` | yes | **no** — request/response shape matches docs.typesafe.ai/api (fetched Oct 2026) |
| Question builder, gate | `triage/questions.py`, `triage/gate.py` | yes | **no** — rubric wording untested on real abstracts |
| Triage orchestration (threaded) | `triage/__init__.py` | yes | n/a |
| Full-text fetch + PDF extraction | `fulltext.py` | yes (generated PDF) | **no** |
| Debate orchestrator + prompts | `debate/__init__.py`, `debate/prompts.py` | yes (fake LLM) | **no** — prompts never run on a real model |
| Anthropic adapter | `debate/llm.py` | yes (fake client) | **no** |
| Digest | `digest.py` | yes | n/a |
| Calibration (sweep, stratified sampling) | `calibrate.py` | yes | n/a |
| CLI | `cli.py` | smoke-tested offline | n/a |
| Re-gate from stored answers | `triage/__init__.py` (`run_regate`) | yes | n/a |
| Claude cost log (`llm_calls` table, `paperbot costs`, digest line) | `debate/`, `store.py` | yes | **no** — `usage` field names from the Anthropic SDK 1.13 docs |
| Refusal fallback + refusal/max_tokens handling | `debate/llm.py` | yes (fake client) | **no** |
| Run lock, `.env` loading | `lock.py`, `config.py` | yes | n/a |
| Windows Task Scheduler scripts | `scripts/*.ps1` | parse-checked only | **not registered yet** |

**Not built yet:** digest delivery (email/Slack/etc.), Semantic Scholar adapter, journal RSS adapter,
prompt caching for the debate (see §6 step 5).

---

## 3. Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # fill in keys; paperbot loads <root>/.env itself (real env vars win)
pytest                        # must stay green and must never touch the network
```

Environment variables (never put secrets in YAML):

| Var | Needed for | Notes |
|---|---|---|
| `TYPESAFE_API_KEY` | triage | Jev key from TypeSafe |
| `OPENALEX_API_KEY` | harvest | Free key at openalex.org/settings/api; required since 13 Feb 2026 |
| `ANTHROPIC_API_KEY` | debate | Claude API |
| `PAPERBOT_ROOT` | optional | Project root containing `config/` (default: cwd) |

Also set `contact_email` in `config/settings.yaml` (goes into the User-Agent for arXiv).

### CLI

```
paperbot harvest            # all enabled sources, since watermark − overlap
paperbot backfill --days 7  # arXiv query API, one-off history pull
paperbot triage [--limit N] # Jev-score new + previously failed papers, then gate
paperbot regate [--dry-run] # re-apply the gate to rejected/passed papers from stored answers (no Jev calls)
paperbot debate             # debate up to debate.max_papers_per_run passed papers (highest score first)
paperbot digest             # write digests/YYYY-MM-DD.md, mark papers digested
paperbot run                # harvest → triage → debate → digest (each stage runs even if an earlier one failed);
                            #   holds data/paperbot.lock, exits 2 if another run holds it (stale after lock_stale_hours, default 6)
paperbot status | show <id>
paperbot costs [--days 30]  # Claude tokens and USD per day/model, $/paper
paperbot label [--n 50]     # interactive hand-labeling, stratified across score buckets, score hidden
paperbot calibrate          # precision/recall/F2 per threshold + suggestion
paperbot feedback <id> up|down [--note ...]   # also becomes a calibration label
```

---

## 4. Key design decisions (keep these unless there is evidence to change them)

**Jev usage**
- Relevance is a **Score** (ordered 5-level ladder in `profile.yaml`), not a Choice. `score` is
  the probability-weighted mean of level indices; use it to rank/threshold, never as a magnitude.
- Topic facets are **Nouls**, one per facet, because they overlap. Putting overlapping labels in
  one Choice splits probability and collapses confidence on exactly the hard cases.
- **Nouls have no `confidence` field.** Only Choice/Score return it. Reading `.confidence` on a
  Noul is a known footgun.
- All questions go in **one request per paper** (Jev evaluates them in parallel; cheaper/faster
  than one request per question). Results are written back **by paper id**, never by position.
- State is minimal: `{title, abstract, venue}`. TypeSafe documents that irrelevant context hurts.
- Pin `jev_model` (currently `jev-1.13.0`). Changing model = recalibrate the threshold.
- 401/422 = configuration bug → `JevFatalError`, stop the stage loudly. 429/529 = backoff.
- Jev guarantees schema-valid answers, **not correct ones**. That is why the threshold must be
  calibrated on hand labels and why the judge has veto power.

**Gate**
- `relevance_threshold` (default 2.5 on 0–4) is an **uncalibrated placeholder**.
- Rescue rule: a paper within `rescue_margin` below the threshold passes if any `must_have`
  facet Noul ≥ `rescue_noul`. Keeps core-topic papers with conservative scores.
- Calibration objective is **F2** (recall weighted over precision): missing a relevant paper
  costs more than one extra debate.

**Harvest**
- Lean toward recall; Jev is cheap ($0.042 per 1M input tokens, output free) and handles precision.
- Dedup order: arXiv ID → DOI → normalized title hash. arXiv DOIs (`10.48550/arxiv.*`) are mapped
  to arXiv IDs. Version suffixes (`v2`) are stripped. Merges keep the longer abstract and record
  every source that saw the paper.
- Watermark per source, re-query `lookback_overlap_days` before it; dedup absorbs repeats.
  A failing source does **not** advance its watermark, so the gap is re-covered next run.
- arXiv RSS has no date parameter (it is "today's announcements") and is **empty on Sat/Sun**
  (feed declares `skipDays`; confirmed 10 Oct 2026). The digest warns only on weekday zeros.
- Keep RSS announce types `new` and `cross`; `replace` would re-surface old papers.

**Debate**
- Advocate and skeptic each give an opening and a rebuttal (rebuttal sees the other's
  opening); the judge sees paper + all four turns and returns strict JSON (schema in
  `prompts.JUDGE_SYSTEM`; parsed/validated by `parse_judge`, one retry on bad JSON).
- Paper text is **untrusted** (prompt injection via PDFs is realistic): it is fenced in
  `<paper>` tags and every system prompt says to treat it as data.
- Budget cap `debate.max_papers_per_run` (default 8), highest Jev score first; overflow waits
  in `passed` and is reported in the digest.
- Models: advocate/skeptic `claude-sonnet-5-5`, judge `claude-opus-5-5` (configurable).
- `max_tokens` is 16000: thinking is always on for Opus 5.5 and counts against it (1500 risked truncated judge JSON).
- Requests use the server-side refusal fallback (`fallbacks="default"`, beta `server-side-fallback-2026-07-01`).
  A refusal that survives the fallback raises `LLMRefusal` → `debate_error`. Usage is attributed to the model
  that actually served the call; models missing from `debate.pricing` are logged with NULL cost and flagged.
- Every Claude call (including ones in debates that later fail) is a row in `llm_calls`.
- Full text: arXiv PDF via `pdf_url`, references section trimmed, capped at 60k chars;
  falls back to abstract and says so in the prompt and digest ("based on abstract only").

---

## 5. External API facts (checked October 2026 — re-verify if something breaks)

- **arXiv ToU:** legacy APIs (RSS, OAI-PMH, query API) ≤ 1 request / 3 s, single connection.
  Query API host is `export.arxiv.org` (`arxiv.org/api` redirects). RSS: `rss.arxiv.org/rss/<cat>`.
  All arXiv traffic (RSS, API, PDFs) should share one `RateLimiter`; currently RSS and the
  full-text fetcher each create their own — fine for a daily batch, but unify if you add volume.
- **OpenAlex:** API key required since 13 Feb 2026; free key = $1/day of usage; keyless = $0.10/day.
  Pass `api_key` as a query param. Use `per_page=100`. Abstracts come as `abstract_inverted_index`.
  Basic paging capped at 10k results (we use cursor paging). Usage headers / `/rate-limit` endpoint
  show remaining budget. Some publishers' abstracts are absent.
- **Jev:** `POST https://api.typesafe.ai/v1/systemone`, `Authorization: Bearer <key>`,
  body `{state, model, questions}`. Score: 2–10 levels. Choice: ≤255 options. Errors 401/422/429/529.
  Published limits (third-party summary of TypeSafe docs): 64k tokens/request, 1,200 req/min.

---

## 6. Next steps for Claude Code, in order

1. **Live smoke test each source** with real keys, one at a time, `-v` logging:
   - `paperbot harvest` on a weekday. Confirm the RSS item format matches
     `tests/fixtures/arxiv_rss.xml` (abstract after `Abstract:`, `arxiv:announce_type`,
     `dc:creator` comma-separated). If it differs, **capture a real response as the new fixture**
     and fix the parser — fixtures should be real data from here on.
   - Confirm the OpenAlex `filter=title_and_abstract.search:...,from_publication_date:...` +
     `select` + `cursor` combination works and check the usage cost per run in the response headers.
   - `paperbot backfill --days 7` to seed a week of history.
2. **Live Jev check:** `paperbot triage --limit 20`, then `paperbot show <id>` on a few. Verify the
   real answer shapes match `triage/jev.py`'s docstring. Look at score distribution.
3. **Calibrate:** `paperbot triage` on the backfill, `paperbot label --n 80`, `paperbot calibrate`,
   update `relevance_threshold`. Expect to iterate on the rubric wording in `profile.yaml`
   (boundary cases belong in level wording, not instructions).
4. ~~Re-gate command~~ — done: `paperbot regate [--dry-run]` after every threshold change.
5. **Debate on real papers:** run `paperbot debate` with the cap at 2–3, read the transcripts
   (`paperbot show`), tune prompts. Check the judge actually vetoes weak papers and that
   `key_evidence` locations are real. Check `paperbot costs` for real $/paper (cost log is built).
   Cheap win to try then: the reader+paper block is a shared prefix across the 4 debate turns —
   split it into its own content block with `cache_control` so rebuttals read it from cache.
   Also consider setting `output_config.effort` explicitly (Opus 5.5 defaults to `medium`).
6. **Scheduling:** the run lock is built. Roz's machine is Windows: `scripts\install-schedule.ps1`
   registers a weekday 06:00 (local) Task Scheduler job that runs `scripts\run-daily.ps1`
   (logs in `logs/`). Register it only after the live smoke tests pass and `.env` has keys.
   (On macOS use launchd; alternative: GitHub Actions cron with the DB persisted as an artifact.)
7. **Delivery:** decide with Roz (email, Slack, Notion, or just the Markdown file). Keep it a
   separate stage after `digest`.
8. Optional sources: Semantic Scholar (citation watches), journal RSS (IEEE TSM, J. Manuf. Systems).

## 7. Open decisions for Roz

- Delivery channel and cadence (daily vs weekly digest).
- Whether to add specific venues/authors/labs to follow (would go in `profile.yaml`).
- Debate budget (papers per run) once real costs are known.
- Whether `cs.LG` stays in the firehose (high volume; Jev cost is negligible but noise is not).

## 8. Conventions

- Tests never touch the network: mock HTTP with `respx`; use `FakeJev`/`FakeLLM`.
  When adding real-data fixtures, strip anything personal.
- Never commit `.env`, `data/`, `digests/`, or `logs/` (see `.gitignore`).
- On Windows, always pass `encoding="utf-8"` to `read_text`/`write_text`/`open` (the default is cp1252).
- `profile.yaml` is user-owned content; keep code free of hard-coded interests.
- Keep the stage boundaries (harvest / triage / debate / digest) so each can be rerun alone.
