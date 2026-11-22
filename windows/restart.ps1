# Restarts Clip Factory's pipeline (Discord bot + clip maker) in about 10 seconds, using the runner that is
# already running: it touches .env, and the runner restarts the pipeline when .env changes.
# It never starts a second runner. If Clip Factory isn't running at all, it starts it.
$Root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $Root ".env"
$runner = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq "powershell.exe" -and $_.CommandLine -match "windows\\start\.ps1" }
if ($runner) {
    if (Test-Path $envFile) { (Get-Item $envFile).LastWriteTime = Get-Date } else { New-Item -ItemType File -Path $envFile | Out-Null }
    Write-Host "Restarting Clip Factory. Discord shows 'Clip Factory is running' in about 20 seconds." -ForegroundColor Green
} else {
    Start-ScheduledTask -TaskName "Clip Factory"
    Write-Host "Clip Factory wasn't running, so it was started." -ForegroundColor Green
}
