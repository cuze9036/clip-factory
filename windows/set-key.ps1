# Saves one key in .env without showing it on screen or in chat.
# Example:  powershell -ExecutionPolicy Bypass -File windows\set-key.ps1 HEARTBEAT_URL
param([Parameter(Mandatory = $true)][string]$Name)
$Root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $Root ".env"
if ($Name -notmatch '^[A-Z0-9_]+$') { Write-Host "Use a name like HEARTBEAT_URL" -ForegroundColor Red; exit 1 }
$s = Read-Host -Prompt "Paste $Name (it stays hidden), then press Enter" -AsSecureString
$v = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)).Trim()
if (-not $v) { Write-Host "Nothing pasted, nothing changed." -ForegroundColor Yellow; exit 1 }
# Common paste slips: IDs are digits only; Meta tokens start with "EAA".
if ($Name -match '_ID$' -and $v -notmatch '^\d+$') {
    $v = ($v -replace '\D', '')
    Write-Host "Removed characters that aren't digits from $Name." -ForegroundColor Yellow
}
if ($Name -match 'TOKEN$' -and $v.Contains("EAA") -and -not $v.StartsWith("EAA")) {
    $v = $v.Substring($v.IndexOf("EAA"))
    Write-Host "Removed stray characters before the token." -ForegroundColor Yellow
}
$lines = @()
if (Test-Path $envFile) { $lines = @(Get-Content $envFile | Where-Object { $_ -notmatch "^$Name=" }) }
$lines += "$Name=$v"
[IO.File]::WriteAllLines($envFile, [string[]]$lines, (New-Object System.Text.UTF8Encoding($false)))
try { icacls "$envFile" /inheritance:r /grant:r "$($env:USERNAME):(F)" 2>$null | Out-Null } catch { }
Write-Host "$Name saved ($($v.Length) characters). Clip Factory restarts by itself in about 10 seconds to use it." -ForegroundColor Green
