# Serves the digest website from this laptop through a Cloudflare quick tunnel.
# Prints a public https://<random>.trycloudflare.com link with an access key; open it on your
# phone/iPad. The link changes every time this starts. Ctrl+C stops both server and tunnel.
#   powershell -ExecutionPolicy Bypass -File scripts\serve.ps1 [-Port 8765]

param([int]$Port = 8765)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $root ".venv\Scripts\paperbot.exe"
if (-not (Test-Path $exe)) { throw "paperbot.exe not found at $exe - run: python -m venv .venv; .venv\Scripts\pip install -e ." }

$cf = (Get-Command cloudflared -ErrorAction SilentlyContinue).Source
if (-not $cf) { $cf = "${env:ProgramFiles(x86)}\cloudflared\cloudflared.exe" }
if (-not (Test-Path $cf)) { throw "cloudflared not found; install it with: winget install Cloudflare.cloudflared" }

$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$serveLog = Join-Path $logDir "serve.log"
$tunnelLog = Join-Path $logDir "tunnel.log"
Remove-Item $tunnelLog -ErrorAction SilentlyContinue

# One key per start; the tunnel name is new each start too, so old links stop working.
$bytes = New-Object byte[] 18
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$env:PAPERBOT_SERVE_KEY = [Convert]::ToBase64String($bytes).Replace("+", "-").Replace("/", "_")
$env:PYTHONIOENCODING = "utf-8"

$server = Start-Process -FilePath $exe -ArgumentList "--root", "`"$root`"", "serve", "--port", $Port `
    -RedirectStandardOutput $serveLog -RedirectStandardError "$serveLog.err" -NoNewWindow -PassThru
$tunnel = Start-Process -FilePath $cf -ArgumentList "tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:$Port" `
    -RedirectStandardError $tunnelLog -NoNewWindow -PassThru

try {
    $url = $null
    for ($i = 0; $i -lt 60 -and -not $url; $i++) {
        Start-Sleep -Seconds 1
        if ($server.HasExited) { throw "paperbot serve exited; see $serveLog.err" }
        if ($tunnel.HasExited) { throw "cloudflared exited; see $tunnelLog" }
        $m = Select-String -Path $tunnelLog -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($m) { $url = $m.Matches[0].Value }
    }
    if (-not $url) { throw "no tunnel URL after 60 s; see $tunnelLog" }
    # `paperbot invite` reads this to print full links for the current address.
    Set-Content -Path (Join-Path $root "data\public_url.txt") -Value $url -Encoding utf8
    Write-Host ""
    Write-Host "Digests:  $url/?key=$($env:PAPERBOT_SERVE_KEY)" -ForegroundColor Green
    Write-Host "Local:    http://127.0.0.1:$Port/?key=$($env:PAPERBOT_SERVE_KEY)"
    Write-Host "Keep this window open. Ctrl+C stops the site. (A new tunnel can take ~30 s to resolve.)"
    while (-not $server.HasExited -and -not $tunnel.HasExited) { Start-Sleep -Seconds 2 }
    if ($server.HasExited) { Write-Warning "paperbot serve stopped; see $serveLog.err" }
    if ($tunnel.HasExited) { Write-Warning "cloudflared stopped; see $tunnelLog" }
} finally {
    foreach ($p in $server, $tunnel) { if ($p -and -not $p.HasExited) { Stop-Process -Id $p.Id -Force } }
}
