# Updates Clip Factory from the newest Clip Factory zip. Keeps your keys, YouTube login and data.
# Usage: update.ps1                 (finds the zip in Downloads, Desktop or Documents)
#        update.ps1 -Zip "C:\path\to\clip-factory.zip"
param([string]$Zip)

if (-not $Zip) {
    $downloads = (Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders' -ErrorAction SilentlyContinue).'{374DE290-123F-4565-9164-39C4925E467B}'
    if ($downloads) { $downloads = [Environment]::ExpandEnvironmentVariables($downloads) }
    $places = @($downloads, "$env:USERPROFILE\Downloads", [Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('MyDocuments')) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Unique
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $found = foreach ($p in $places) {
        Get-ChildItem $p -Filter *.zip -Recurse -Depth 2 -ErrorAction SilentlyContinue | Where-Object {
            try { $a = [IO.Compression.ZipFile]::OpenRead($_.FullName); $ok = [bool]($a.Entries | Where-Object { $_.FullName -match 'windows/install\.ps1$' }); $a.Dispose(); $ok } catch { $false }
        }
    }
    $Zip = ($found | Sort-Object LastWriteTime | Select-Object -Last 1).FullName
}
if (-not $Zip -or -not (Test-Path $Zip)) {
    Write-Host "No Clip Factory zip found. Run again with: update.ps1 -Zip ""C:\path\to\the.zip""" -ForegroundColor Red; exit 1
}
Write-Host "Updating from $Zip"
$tmp = Join-Path $env:TEMP "clip-factory-update"
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
Expand-Archive -Path $Zip -DestinationPath $tmp -Force
$installer = Get-ChildItem $tmp -Recurse -Filter install.ps1 | Where-Object { $_.Directory.Name -eq "windows" } | Select-Object -First 1
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer.FullName -Update
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "Updated." -ForegroundColor Green
