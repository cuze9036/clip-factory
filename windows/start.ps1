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
$script:GhPauseUntil = [DateTime]::MinValue

function Latest-Sha {
    # The same address "git ls-remote" uses: always fresh, and not under the GitHub API's 60-requests-an-hour
    # limit (which every 10-second check used up, so updates stalled).
    if ((Get-Date) -lt $script:GhPauseUntil) { return $null }
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/$Repo.git/info/refs?service=git-upload-pack" `
            -UserAgent "git/2.45.0" -TimeoutSec 8
    } catch {
        $code = 0
        try { $code = [int]$_.Exception.Response.StatusCode } catch { }
        if ($code -eq 429 -or $code -eq 403) {
            $script:GhPauseUntil = (Get-Date).AddMinutes(2)
            Log "GitHub asked to slow down; checking again in 2 minutes"
        }
        return $null
    }
    $body = $r.Content
    if ($body -is [byte[]]) { $body = [Text.Encoding]::ASCII.GetString($body) }
    if ("$body" -match '([0-9a-f]{40}) refs/heads/main') { return $Matches[1] }
    return $null
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

# ---------- downloads in progress ----------
# The tool keeps data\busy.lock fresh while it downloads campaign footage. Updates wait until it's done,
# so an update never cuts a download (a lock older than 10 minutes is treated as left over).
$BusyFile = Join-Path $Root "data\busy.lock"
$script:BusyNoted = $false
function Busy {
    if (-not (Test-Path $BusyFile)) { $script:BusyNoted = $false; return $false }
    $fresh = ((Get-Date) - (Get-Item $BusyFile).LastWriteTime).TotalMinutes -lt 10
    if ($fresh -and -not $script:BusyNoted) { Log "A download is running; updates wait until it's done"; $script:BusyNoted = $true }
    return $fresh
}

# ---------- settings changes ----------
# A new key (set-key.ps1, restart.ps1) or a changed config.local.yaml restarts the pipeline within about 10 seconds.
$WatchFiles = @((Join-Path $Root ".env"), (Join-Path $Root "config.local.yaml"))

function Settings-Stamp {
    return ($WatchFiles | ForEach-Object { if (Test-Path $_) { (Get-Item $_).LastWriteTimeUtc.Ticks } else { 0 } }) -join ","
}

function Stop-Tree($proc) {
    # clipfactory.exe starts python.exe as a child, so stop the whole tree.
    if ($proc -and -not $proc.HasExited) { taskkill /PID $proc.Id /T /F 2>&1 | Out-Null }
}

Log "Starting"
$dash = Start-Dashboard
try {
    while ($true) {
        Wait-Online
        if ($dash.HasExited) { $dash = Start-Dashboard }
        $stamp = Get-Date -Format yyyy-MM-dd_HHmm
        Log "Pipeline starting"
        $settings = Settings-Stamp
        $p = Start-Process -FilePath $Cf -ArgumentList "run" -WorkingDirectory $Root -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $Logs "clipfactory-$stamp.log") `
            -RedirectStandardError (Join-Path $Logs "clipfactory-$stamp-errors.log") -PassThru
        $changed = $false
        while (-not $p.HasExited) {
            Start-Sleep -Seconds 10
            if ($dash.HasExited) { $dash = Start-Dashboard }
            if ((Settings-Stamp) -ne $settings) {
                Log "Settings changed (.env or config.local.yaml); restarting the pipeline"
                $changed = $true
                Stop-Tree $p
                Stop-Tree $dash
                $dash = Start-Dashboard
                break
            }
            if (-not (Busy)) {
                try { Check-Update } catch { Log "Update check failed: $_" }
                try { Check-GitHub } catch { Log "GitHub check failed: $_" }   # every 10 s; "nothing new" is free
            }
        }
        Get-ChildItem $Logs -Filter "clipfactory-*.log" | Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-14) } |
            Remove-Item -ErrorAction SilentlyContinue
        if ($changed) { continue }   # restart right away with the new settings
        Log "Pipeline stopped (exit code $($p.ExitCode)); restarting in 30 seconds"
        Start-Sleep -Seconds 30
    }
} finally {
    Stop-Tree $dash
    $mutex.ReleaseMutex()
}
