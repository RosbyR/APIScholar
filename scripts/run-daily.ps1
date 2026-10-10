# Runs the full pipeline once and appends output to logs\YYYY-MM-DD.log.
# Called by the scheduled task from install-schedule.ps1; safe to run by hand too.
# Keys come from .env in the project root (loaded by paperbot itself).

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $root ".venv\Scripts\paperbot.exe"
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$log = Join-Path $logDir ((Get-Date -Format "yyyy-MM-dd") + ".log")

if (-not (Test-Path $exe)) {
    "$(Get-Date -Format s) paperbot.exe not found at $exe - run: python -m venv .venv; .venv\Scripts\pip install -e ." |
        Out-File -Append -Encoding utf8 $log
    exit 1
}

"===== $(Get-Date -Format s) paperbot run =====" | Out-File -Append -Encoding utf8 $log
$env:PYTHONIOENCODING = "utf-8"
# Run through cmd so stderr (Python logging) lands in the log without PowerShell error wrapping.
& cmd.exe /c "`"$exe`" --root `"$root`" run >> `"$log`" 2>&1"
$rc = $LASTEXITCODE
"===== exit $rc =====" | Out-File -Append -Encoding utf8 $log

# Keep the last 60 days of logs.
Get-ChildItem $logDir -Filter *.log | Sort-Object Name -Descending | Select-Object -Skip 60 | Remove-Item -Force
exit $rc
