# Stops Clip Factory until the next login or network connection.
# To turn auto-start off for good, run uninstall.ps1.

Stop-ScheduledTask -TaskName "Clip Factory" -ErrorAction SilentlyContinue
$procs = Get-CimInstance Win32_Process | Where-Object {
    ($_.Name -in @("clipfactory.exe", "python.exe", "pythonw.exe") -and $_.CommandLine -match "clipfactory") -or
    ($_.Name -eq "powershell.exe" -and $_.CommandLine -match "windows\\start\.ps1")
}
foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
Write-Host "Clip Factory stopped. It starts again at your next login or network connection." -ForegroundColor Yellow
Start-Sleep -Seconds 3
