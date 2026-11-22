# Runs when Windows shuts down, restarts or goes to sleep, and when you stop Clip Factory.
# It tells the offline-alert service right away, so Discord shows "Bot is offline" within seconds.
# (If the laptop loses power or internet instead, the service notices the missing heartbeat in about 2 minutes.)
$Root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $Root ".env"
if (-not (Test-Path $envFile)) { exit 0 }
$line = Get-Content $envFile | Where-Object { $_ -match '^HEARTBEAT_URL=.+' } | Select-Object -First 1
if (-not $line) { exit 0 }
$url = ($line -replace '^HEARTBEAT_URL=', '').Trim().TrimEnd('/')
try { Invoke-WebRequest -Uri "$url/fail" -Method Post -Body "laptop going offline" -UseBasicParsing -TimeoutSec 4 | Out-Null } catch { }
