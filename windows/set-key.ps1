# Saves one key in .env without showing it on screen or in chat.
# Example:  powershell -ExecutionPolicy Bypass -File windows\set-key.ps1 HEARTBEAT_URL
param([Parameter(Mandatory = $true)][string]$Name)
$Root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $Root ".env"
if ($Name -notmatch '^[A-Z0-9_]+$') { Write-Host "Use a name like HEARTBEAT_URL" -ForegroundColor Red; exit 1 }
$s = Read-Host -Prompt "Paste $Name (it stays hidden), then press Enter" -AsSecureString
$v = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)).Trim()
if (-not $v) { Write-Host "Nothing pasted, nothing changed." -ForegroundColor Yellow; exit 1 }
$lines = @()
if (Test-Path $envFile) { $lines = @(Get-Content $envFile | Where-Object { $_ -notmatch "^$Name=" }) }
$lines += "$Name=$v"
[IO.File]::WriteAllLines($envFile, [string[]]$lines, (New-Object System.Text.UTF8Encoding($false)))
try { icacls "$envFile" /inheritance:r /grant:r "$($env:USERNAME):(F)" 2>$null | Out-Null } catch { }
Write-Host "$Name saved. Clip Factory restarts by itself in about 10 seconds to use it." -ForegroundColor Green
