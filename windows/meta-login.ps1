# Connects Instagram + Facebook in one step: paste one token from Meta's Graph API Explorer (it stays hidden);
# the Page, the Instagram account and a Page token that doesn't expire are found and saved in .env by themselves.
# Run:  powershell -ExecutionPolicy Bypass -File windows\meta-login.ps1
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
& (Join-Path $Root ".venv\Scripts\clipfactory.exe") meta-login
Write-Host ""
Read-Host "Press Enter to close"
