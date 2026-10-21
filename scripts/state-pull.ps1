# Downloads the cloud state (database + digests) over this machine's data\ and digests\,
# so local commands (paperbot label, calibrate, show, costs) see what the daily run did.
# The local copies are moved to data.bak-<time>\ and digests.bak-<time>\ first.
# Needs `npx wrangler login` once.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$stamp = (Get-Date).ToString("yyyyMMdd-HHmm")
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) "paperbot-state-$stamp"
New-Item -ItemType Directory -Force $tmp | Out-Null
$file = Join-Path $tmp "state.tar.gz"

& npx --yes wrangler@4.145.0 r2 object get "paperbot-state/state/latest.tar.gz" --file $file --remote
if ($LASTEXITCODE -ne 0) { throw "download failed; local files untouched" }

foreach ($d in "data", "digests") {
    $p = Join-Path $root $d
    if (Test-Path $p) { Move-Item $p (Join-Path $root "$d.bak-$stamp") }
}
tar -xzf $file -C $root
if ($LASTEXITCODE -ne 0) { throw "extract failed; your previous files are in data.bak-$stamp and digests.bak-$stamp" }
Write-Host "Pulled cloud state. Previous local copies: data.bak-$stamp, digests.bak-$stamp"
