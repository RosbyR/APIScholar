# Uploads this machine's data\ and digests\ to R2 as the state the daily cloud run uses.
# Use it once to seed the cloud from your local history, and after local work that changes
# the database (paperbot label, regate, triage --rescore). The cloud copy it replaces is
# kept as state/backups/before-push-<time>.tar.gz first.
# Don't run it while the daily GitHub Action is running (weekdays ~13:00 UTC) or that run's
# results will be overwritten. Needs `npx wrangler login` once.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$wrangler = @("--yes", "wrangler@4.145.0")
$bucket = "paperbot-state"
$stamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHHmm")
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) "paperbot-state-$stamp"
New-Item -ItemType Directory -Force $tmp | Out-Null

if (-not (Test-Path (Join-Path $root "data\paperbot.sqlite"))) { throw "no data\paperbot.sqlite in $root" }
if (Test-Path (Join-Path $root "data\paperbot.lock")) { throw "a local paperbot run is in progress (data\paperbot.lock); wait for it" }

# Fold the WAL into the main file so the archive is self-contained.
& (Join-Path $root ".venv\Scripts\python.exe") -c "import sqlite3; c = sqlite3.connect(r'$root\data\paperbot.sqlite'); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()"

$old = Join-Path $tmp "cloud.tar.gz"
& npx @wrangler r2 object get "$bucket/state/latest.tar.gz" --file $old --remote 2>$null
if ($LASTEXITCODE -eq 0) {
    & npx @wrangler r2 object put "$bucket/state/backups/before-push-$stamp.tar.gz" --file $old --remote
    if ($LASTEXITCODE -ne 0) { throw "could not back up the current cloud state; nothing was replaced" }
} else {
    Write-Host "No cloud state yet; seeding it."
}

$new = Join-Path $tmp "state.tar.gz"
tar -czf $new -C $root --exclude=paperbot.lock data digests
if ($LASTEXITCODE -ne 0) { throw "tar failed" }
& npx @wrangler r2 object put "$bucket/state/latest.tar.gz" --file $new --remote
if ($LASTEXITCODE -ne 0) { throw "upload failed; the cloud state is unchanged" }
Write-Host "Uploaded $new as $bucket/state/latest.tar.gz"
