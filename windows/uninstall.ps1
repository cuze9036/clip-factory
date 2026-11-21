# Turns off auto-start and stops Clip Factory. Your folder, keys and data stay where they are.

& (Join-Path $PSScriptRoot "stop.ps1")
Unregister-ScheduledTask -TaskName "Clip Factory" -Confirm:$false -ErrorAction SilentlyContinue
Write-Host "Auto-start removed. Delete the 'Clipping Tools' folder on your Desktop if you want everything gone." -ForegroundColor Yellow
Read-Host -Prompt "Press Enter to close"
