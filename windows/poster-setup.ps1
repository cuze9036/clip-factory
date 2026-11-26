# Sends TikTok / X clips to your helper's Discord channel: asks the channel ID (and the helper's user ID) and saves them.
# Run:  powershell -ExecutionPolicy Bypass -File windows\poster-setup.ps1
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
& (Join-Path $Root ".venv\Scripts\clipfactory.exe") poster-setup
Write-Host ""
Read-Host "Press Enter to close"
