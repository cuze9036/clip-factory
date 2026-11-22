# Runs Clip Factory in the background: waits for internet, starts the dashboard and the pipeline,
# restarts the pipeline if it stops, and installs updates by itself within about 30 seconds:
# new commits on GitHub (update.github_repo in config.yaml), or a new Clip Factory zip saved into
# Desktop\CLIPING TOOL (or Downloads).
# Task Scheduler runs this at login and when the network connects.

$Root = Split-Path -Parent $PSScriptRoot
$Cf = Join-Path $Root ".venv\Scripts\clipfactory.exe"
$Logs = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $Logs | Out-Null
Set-Location $Root
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONIOENCODING = "utf-8"
Add-Type -AssemblyName System.IO.Compression.FileSystem

$mutex = New-Object System.Threading.Mutex($false, "Local\ClipFactoryRunner")
if (-not $mutex.WaitOne(0)) { exit 0 }   # already running

function Log($text) { Add-Content -Path (Join-Path $Logs "runner.log") -Value "$(Get-Date -Format s)  $text" }

function Online {
    try {
        $r = Invoke-WebRequest -Uri "https://www.gstatic.com/generate_204" -UseBasicParsing -TimeoutSec 8
        return $r.StatusCode -eq 204
    } catch { return $false }
}

function Wait-Online { while (-not (Online)) { Start-Sleep -Seconds 20 } }

function Start-Dashboard {
    return Start-Process -FilePath $Cf -ArgumentList "dashboard" -WorkingDirectory $Root -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $Logs "dashboard.log") `
        -RedirectStandardError (Join-Path $Logs "dashboard-errors.log") -PassThru
}

# ---------- auto-update ----------
$Desktop = [Environment]::GetFolderPath("Desktop")
$Downloads = (Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders' -ErrorAction SilentlyContinue).'{374DE290-123F-4565-9164-39C4925E467B}'
if ($Downloads) { $Downloads = [Environment]::ExpandEnvironmentVariables($Downloads) } else { $Downloads = Join-Path $env:USERPROFILE "Downloads" }
$StampFile = Join-Path $Logs "last-update.txt"

function Is-ClipFactoryZip($file) {
    try {
        $z = [IO.Compression.ZipFile]::OpenRead($file.FullName)
        $ok = [bool]($z.Entries | Where-Object { $_.FullName -match 'windows/install\.ps1$' })
        $z.Dispose()
        return $ok
    } catch { return $false }
}

function Newest-UpdateZip {
    $candidates = @()
    $folder = Join-Path $Desktop "CLIPING TOOL"
    if (Test-Path $folder) { $candidates += Get-ChildItem $folder -Filter *.zip -ErrorAction SilentlyContinue }
    foreach ($d in @($Downloads, $Desktop)) {
        if (Test-Path $d) { $candidates += Get-ChildItem $d -Filter "clip*factory*.zip" -ErrorAction SilentlyContinue }
    }
    # Only finished downloads: untouched for 10 seconds.
    $ready = $candidates | Where-Object { $_.LastWriteTime -lt (Get-Date).AddSeconds(-10) } | Sort-Object LastWriteTimeUtc -Descending
    foreach ($f in $ready) { if (Is-ClipFactoryZip $f) { return $f } }
    return $null
}

function Installed-Stamp {
    if (Test-Path $StampFile) { return [long](Get-Content $StampFile -TotalCount 1) }
    return 0
}

function Check-Update {
    $zip = Newest-UpdateZip
    if (-not $zip -or $zip.LastWriteTimeUtc.Ticks -le (Installed-Stamp)) { return }
    Set-Content -Path $StampFile -Value $zip.LastWriteTimeUtc.Ticks
    Log "Update found: $($zip.FullName). Installing."
    Install-FromZip $zip.FullName
}

# ---------- updates from GitHub ----------
$Repo = (Select-String -Path (Join-Path $Root "config.yaml") -Pattern '^\s*github_repo:\s*([\w.-]+/[\w.-]+)' |
         Select-Object -First 1).Matches.Groups[1].Value
$ShaFile = Join-Path $Logs "last-sha.txt"
$script:Etag = $null
$script:GhPauseUntil = [DateTime]::MinValue

function Latest-Sha {
    # A conditional request: "nothing new" (304) doesn't count against GitHub's hourly limit.
    $headers = @{ "Accept" = "application/vnd.github.sha"; "User-Agent" = "clip-factory-updater" }
    if ($script:Etag) { $headers["If-None-Match"] = $script:Etag }
    if ((Get-Date) -lt $script:GhPauseUntil) { return $null }
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri "https://api.github.com/repos/$Repo/commits/main" -Headers $headers -TimeoutSec 8
    } catch {
        # 304 = nothing new (Windows PowerShell reports it as an error). 403/429 = GitHub's hourly limit: wait 10 min.
        $code = 0
        try { $code = [int]$_.Exception.Response.StatusCode } catch { }
        if ($code -eq 403 -or $code -eq 429) {
            $script:GhPauseUntil = (Get-Date).AddMinutes(10)
            Log "GitHub asked to slow down; checking again in 10 minutes"
        }
        return $null
    }
    $script:Etag = $r.Headers["ETag"]
    $body = $r.Content
    if ($body -is [byte[]]) { $body = [Text.Encoding]::ASCII.GetString($body) }
    return "$body".Trim()
}

function Check-GitHub {
    if (-not $Repo) { return }
    $sha = Latest-Sha
    if (-not $sha -or $sha -notmatch '^[0-9a-f]{40}$') { return }
    $last = if (Test-Path $ShaFile) { "$(Get-Content $ShaFile -TotalCount 1)".Trim() } else { "" }
    if (-not $last) { Set-Content -Path $ShaFile -Value $sha; return }   # first run: what's installed counts as current
    if ($sha -eq $last) { return }
    Set-Content -Path $ShaFile -Value $sha
    Log "GitHub update $($sha.Substring(0, 7)) found. Installing."
    $zip = Join-Path $env:TEMP "clip-factory-github.zip"
    Invoke-WebRequest -UseBasicParsing -Uri "https://codeload.github.com/$Repo/zip/$sha" -OutFile $zip -TimeoutSec 180
    Install-FromZip $zip
}

function Install-FromZip($path) {
    $tmp = Join-Path $env:TEMP "clip-factory-autoupdate"
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    Expand-Archive -Path $path -DestinationPath $tmp -Force
    $installer = Get-ChildItem $tmp -Recurse -Filter install.ps1 | Where-Object { $_.Directory.Name -eq "windows" } | Select-Object -First 1
    # The installer stops this runner, replaces the files and starts Clip Factory again.
    Start-Process powershell.exe -WindowStyle Hidden `
        -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$($installer.FullName)`" -Update" `
        -RedirectStandardOutput (Join-Path $Logs "update.log") -RedirectStandardError (Join-Path $Logs "update-errors.log")
    Start-Sleep -Seconds 600
}

# On the very first run, whatever zip is already there counts as installed.
if (-not (Test-Path $StampFile)) {
    $first = Newest-UpdateZip
    Set-Content -Path $StampFile -Value $(if ($first) { $first.LastWriteTimeUtc.Ticks } else { 0 })
}

if (-not (Test-Path $Cf)) { Log "Not installed: run windows\install.ps1 first"; exit 1 }

Log "Starting"
$dash = Start-Dashboard
try {
    while ($true) {
        Wait-Online
        if ($dash.HasExited) { $dash = Start-Dashboard }
        $stamp = Get-Date -Format yyyy-MM-dd_HHmm
        Log "Pipeline starting"
        $p = Start-Process -FilePath $Cf -ArgumentList "run" -WorkingDirectory $Root -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $Logs "clipfactory-$stamp.log") `
            -RedirectStandardError (Join-Path $Logs "clipfactory-$stamp-errors.log") -PassThru
        while (-not $p.HasExited) {
            Start-Sleep -Seconds 10
            if ($dash.HasExited) { $dash = Start-Dashboard }
            try { Check-Update } catch { Log "Update check failed: $_" }
            try { Check-GitHub } catch { Log "GitHub check failed: $_" }   # every 10 s; "nothing new" is free
        }
        Log "Pipeline stopped (exit code $($p.ExitCode)); restarting in 30 seconds"
        Get-ChildItem $Logs -Filter "clipfactory-*.log" | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-14) } |
            Remove-Item -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 30
    }
} finally {
    if ($dash -and -not $dash.HasExited) { Stop-Process -Id $dash.Id -Force -ErrorAction SilentlyContinue }
    $mutex.ReleaseMutex()
}
