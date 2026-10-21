# Running paperbot in the cloud

Once this is set up, nothing runs on your PC. There is no schedule: whenever you start it
(Actions → daily → Run workflow, on the web or in the GitHub mobile app), a GitHub Action
does the following:

1. Downloads the database and digests from Cloudflare R2.
2. Imports the 👍/👎 votes you clicked on the website.
3. Runs `paperbot run` (harvest → triage → debate → digest).
4. Uploads the database and digests back to R2.
5. Redeploys the website to Cloudflare Pages.

The website is behind Cloudflare Access, so only your email can open it.

```
GitHub Actions (manual) ► R2: state/latest.tar.gz   (data/ + digests/, plus dated backups)
        │
        ├──► paperbot run ──► Jev, Claude, arXiv, OpenAlex
        │
        └──► Cloudflare Pages: digests as web pages ──► /api/feedback ──► D1 (votes)
                                                                           │
                         next run: paperbot pull-feedback ◄────────────────┘
```

Cost: Cloudflare's free tiers cover this (Pages, D1, R2 at under 10 GB, Access for up to 50
users). GitHub Actions is free for public repos. API spend (Jev, Claude) is the same as now.

All commands below run in PowerShell from the project folder (`paperbot\paperbot`).

## 1. Cloudflare resources (once)

1. In the Cloudflare dashboard, open **R2** and enable it. R2 asks for a payment method even
   on the free tier.
2. Log in and create the resources:

```powershell
npx wrangler@4.145.0 login
npx wrangler@4.145.0 r2 bucket create paperbot-state
npx wrangler@4.145.0 d1 create paperbot                 # note the database_id it prints
npx wrangler@4.145.0 d1 execute paperbot --remote --file web/schema.sql
npx wrangler@4.145.0 pages project create paperbot --production-branch main
```

The last command prints the site address, normally `https://paperbot.pages.dev`. If that name
is taken, you get a variant such as `paperbot-abc.pages.dev`.

3. Optional: delete old backups automatically. Open **R2 → paperbot-state → Settings → Object
   lifecycle rules**, add a rule for prefix `state/backups/`, and set it to delete objects
   after 60 days.

## 2. Lock the site (Cloudflare Access)

Do this **before** the first deploy. Until Access is in front of the site, the feedback API
refuses every request (403), but the digests themselves would be readable by anyone.

1. Open **Zero Trust → Access → Applications → Add an application → Self-hosted**.
2. Add two public hostnames: `paperbot.pages.dev` (your address from step 1) and
   `*.paperbot.pages.dev`, which covers per-deploy preview URLs.
3. Add a policy: Action **Allow**, Include → **Emails** → your email address.
4. For the login method, the default **One-time PIN** works. Cloudflare emails you a code.

## 3. API token for the Action

1. Open **My Profile → API Tokens → Create Token → Custom token**.
2. Grant these three account permissions:
   - Cloudflare Pages: Edit
   - D1: Edit
   - Workers R2 Storage: Edit
3. Copy the token. Also copy your **Account ID** from the right sidebar of any zone or Workers
   page.

## 4. GitHub secrets

In github.com/RosbyR/APIScholar, open **Settings → Secrets and variables → Actions**.

**Secrets** (repository secrets):

| Name | Value |
|---|---|
| `TYPESAFE_API_KEY` | same as in `.env` |
| `OPENALEX_API_KEY` | same as in `.env` |
| `ANTHROPIC_API_KEY` | same as in `.env` |
| `PAPERBOT_CONTACT_EMAIL` | same as in `.env` |
| `CLOUDFLARE_API_TOKEN` | token from step 3 |
| `CLOUDFLARE_ACCOUNT_ID` | account ID from step 3 |

**Variables** tab:

| Name | Value |
|---|---|
| `PAPERBOT_D1_DATABASE_ID` | the `database_id` from `d1 create` |

## 5. Seed the cloud with your current database

This keeps your history, hand labels and calibration:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\state-push.ps1
```

## 6. First run

1. Commit and push.
2. On GitHub, open **Actions → daily → Run workflow**.
3. When it finishes, open your site. It asks for your email, then a PIN, then shows the
   digests.

If you ever registered the local Windows schedule, turn it off so the PC and the cloud
don't both write state:

```powershell
Unregister-ScheduledTask -TaskName paperbot-daily -Confirm:$false
```

## Day to day

- **Read** at your Pages address. Each paper has 👍 / 👎 buttons. 👎 asks for an optional note.
  Votes are saved right away and imported on the next run as feedback and calibration
  labels (same as `paperbot feedback`).
- **Run:** Actions → daily → Run workflow, whenever you want a new digest. This works from
  the GitHub mobile app too. Runs never overlap; a second click waits for the first.
- **Failures:** a failed run shows a red ❌ on the Actions tab, and GitHub emails you. The
  digest's "Pipeline health" section still lists per-source problems. State is saved and the
  site redeployed even when a stage fails.
- **Labeling, calibrating, rescoring** still happen on your PC, because `paperbot label` is
  interactive:

  ```powershell
  powershell -ExecutionPolicy Bypass -File scripts\state-pull.ps1   # get the cloud database
  paperbot label --n 40; paperbot calibrate                          # as before
  powershell -ExecutionPolicy Bypass -File scripts\state-push.ps1   # send it back
  ```

  Don't push while a workflow run is in progress, or that run's results are lost. `state-push` backs up the cloud copy first
  (`state/backups/before-push-*`).
- **Changing settings or the rubric:** edit `config/*.yaml`, commit and push. The next run
  uses them. Run `regate` or `triage --rescore` locally between `state-pull` and `state-push`.
- **Restoring a backup:** download `state/backups/<time>.tar.gz` from the R2 dashboard and
  upload it as `state/latest.tar.gz`.

## Things to know

- OneNote delivery is skipped in the cloud, because it needs Windows. `paperbot deliver`
  still works on your PC.
- Local preview: `paperbot site`, then `npx wrangler@4.145.0 pages dev` in `web/`, with a
  `web/.dev.vars` file containing `REQUIRE_ACCESS=false`. Run it from a short path with no
  spaces: under `...\Paper Bot\...` wrangler's local D1 fails with "internal error" (Windows
  path issue). Copy `web/` to something like `C:\pbw` first.
