# Clip Factory installer for Windows 10/11.
# Creates "Clipping Tools" on your Desktop, installs everything, asks for your keys,
# and makes Clip Factory start by itself when you log in or the internet connects.
# Safe to run again: it keeps your keys, data and campaigns.

param([switch]$Update)  # -Update: replace the code, keep keys, ask nothing

$ErrorActionPreference = "Stop"
# During an automatic update nobody is watching: if anything fails, still start Clip Factory again.
trap {
    if ($Update) {
        $log = Join-Path ([Environment]::GetFolderPath("Desktop")) "Clipping Tools\logs\update-errors.log"
        Add-Content -Path $log -Value "$(Get-Date -Format s)  Update failed: $_" -ErrorAction SilentlyContinue
        Start-ScheduledTask -TaskName "Clip Factory" -ErrorAction SilentlyContinue
    }
    break
}
$Source = Split-Path -Parent $PSScriptRoot
$Desktop = [Environment]::GetFolderPath("Desktop")
$Target = Join-Path $Desktop "Clipping Tools"
$TaskName = "Clip Factory"

function Step($text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }
function Ok($text) { Write-Host "    $text" -ForegroundColor Green }
function Warn($text) { Write-Host "    $text" -ForegroundColor Yellow }

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}

function Has($cmd) { return [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

function Install-WithWinget($id, $name, $check) {
    if (& $check) { Ok "$name is already installed"; return }
    if (-not (Has "winget")) {
        throw "winget is missing. Install 'App Installer' from the Microsoft Store, then run this again."
    }
    Write-Host "    Installing $name..."
    winget install --id $id -e --silent --accept-package-agreements --accept-source-agreements | Out-Null
    Refresh-Path
    if (& $check) { Ok "$name installed" } else { Warn "$name was installed but isn't on PATH yet. If a later step fails, restart the PC and run this again." }
}

function Find-Python {
    # Returns @(exe, extra args) for a Python 3.10+ on this PC, or $null.
    foreach ($c in @(@("py", "-3.12"), @("py", "-3.11"), @("python"))) {
        $exe = $c[0]
        $extra = @($c | Select-Object -Skip 1)
        if (-not (Has $exe)) { continue }
        try {
            $v = & $exe @extra -c "import sys; print(sys.version_info[:2] >= (3, 10))" 2>$null
            if ($v -eq "True") { return ,@($exe, $extra) }
        } catch { }
    }
    return $null
}

Write-Host "Clip Factory setup" -ForegroundColor White
Write-Host "Installs to: $Target"

# 1. Tools
Step "1/7 Installing the free tools (Python, FFmpeg, Deno)"
Install-WithWinget "Python.Python.3.12" "Python 3.12" { [bool](Find-Python) }
Install-WithWinget "Gyan.FFmpeg" "FFmpeg" { Has "ffmpeg" }
Install-WithWinget "DenoLand.Deno" "Deno (needed by yt-dlp for YouTube)" { Has "deno" }
Install-WithWinget "Microsoft.VCRedist.2015+.x64" "Visual C++ runtime" { Test-Path "$env:WINDIR\System32\msvcp140.dll" }
$py = Find-Python
if (-not $py) { throw "Python 3.10+ not found. Restart the PC and run this installer again." }

# 2. Files
Step "2/7 Copying Clip Factory to Desktop\Clipping Tools"
New-Item -ItemType Directory -Force -Path $Target | Out-Null
if ((Resolve-Path $Source).Path -ne (Resolve-Path $Target).Path) {
    robocopy $Source $Target /E /XD .venv data data-demo __pycache__ secrets logs /XF .env *.pyc /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Copying files failed (robocopy code $LASTEXITCODE)" }
}
foreach ($d in @("secrets", "logs", "assets\music", "assets\fonts")) { New-Item -ItemType Directory -Force -Path (Join-Path $Target $d) | Out-Null }
$recordings = Join-Path $env:USERPROFILE "Videos\GTA6 recordings"
New-Item -ItemType Directory -Force -Path $recordings | Out-Null
Ok "Files ready. Put your own GTA 6 recordings in: $recordings"
$fontFile = Join-Path $Target "assets\fonts\Montserrat.ttf"
if (-not (Test-Path $fontFile)) {
    try {
        Invoke-WebRequest -UseBasicParsing -TimeoutSec 60 -OutFile $fontFile `
            -Uri "https://github.com/google/fonts/raw/main/ofl/montserrat/Montserrat%5Bwght%5D.ttf"
        Ok "Caption font (Montserrat) downloaded"
    } catch { Warn "Couldn't download the caption font; captions use a plain font until it's in assets\fonts" }
}

# 3. Python environment
Step "3/7 Installing Python packages (takes a few minutes the first time)"
# A running copy locks clipfactory.exe, so stop it before updating.
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process | Where-Object {
    ($_.Name -in @("clipfactory.exe", "python.exe", "pythonw.exe") -and $_.CommandLine -match "Clipping Tools") -or
    ($_.Name -eq "powershell.exe" -and $_.CommandLine -match "windows\\start\.ps1")
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 3
$venv = Join-Path $Target ".venv"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) {
    $pyExe = $py[0]
    $pyExtra = @($py[1])
    & $pyExe @pyExtra -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Creating the Python environment failed" }
}
$vpy = Join-Path $venv "Scripts\python.exe"
$cf = Join-Path $venv "Scripts\clipfactory.exe"
# Code-only updates skip pip: the package is installed in editable mode, so new code is used as soon as it's copied.
$depsFile = Join-Path $Target "logs\deps-hash.txt"
$deps = (Get-FileHash -Algorithm SHA256 -InputStream ([IO.MemoryStream]::new([Text.Encoding]::UTF8.GetBytes(
    ((Get-Content (Join-Path $Target "pyproject.toml") -Raw) + (Get-Content (Join-Path $Target "requirements.txt") -Raw)))))).Hash
$oldDeps = if (Test-Path $depsFile) { "$(Get-Content $depsFile -TotalCount 1)".Trim() } else { "" }
if ($Update -and $deps -eq $oldDeps -and (Test-Path $cf)) {
    Ok "Packages unchanged, skipped"
} else {
    if (-not $Update) { & $vpy -m pip install --upgrade pip --quiet }
    & $vpy -m pip install -e $Target --quiet
    if ($LASTEXITCODE -ne 0) { throw "Installing Python packages failed" }
    New-Item -ItemType Directory -Force -Path (Split-Path $depsFile) | Out-Null
    Set-Content -Path $depsFile -Value $deps
    Ok "Packages installed"
}

# 4. Keys
Step "4/7 Your keys (press Enter to skip any you don't have yet)"
$envFile = Join-Path $Target ".env"
$values = [ordered]@{}
if (Test-Path $envFile) {
    foreach ($line in Get-Content $envFile) {
        if ($line -match '^\s*([A-Z0-9_]+)\s*=\s*(.*)$') { $values[$Matches[1]] = $Matches[2].Split("#")[0].Trim() }
    }
}
function Save-Env {
    foreach ($extra in @("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")) { if (-not $values.Contains($extra)) { $values[$extra] = "" } }
    $lines = $values.Keys | ForEach-Object { "$_=$($values[$_])" }
    [IO.File]::WriteAllLines($envFile, [string[]]$lines, (New-Object System.Text.UTF8Encoding($false)))
    # Only your Windows user can read the keys. icacls needs no admin rights; if it fails, keep going.
    try { icacls "$envFile" /inheritance:r /grant:r "$($env:USERNAME):(F)" 2>$null | Out-Null } catch { }
}

function Ask-Keys($keys) {
    # Each answer is saved right away, so closing the window never loses what you typed.
    foreach ($k in $keys) {
        $name, $label, $secret = $k
        $have = $values.Contains($name) -and $values[$name]
        $prompt = $label
        if ($have) { $prompt = "$label [already set, Enter to keep]" }
        if ($secret) {
            $s = Read-Host -Prompt "    $prompt" -AsSecureString
            $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
        } else {
            $plain = Read-Host -Prompt "    $prompt"
        }
        if ($plain) { $values[$name] = $plain.Trim() } elseif (-not $have) { $values[$name] = "" }
        Save-Env
    }
}

function Ask-YesNo($question, $default) {
    $hint = "y/N"
    if ($default) { $hint = "Y/n" }
    $a = Read-Host -Prompt "    $question ($hint)"
    if (-not $a) { return $default }
    return $a -match '^[yY]'
}

$hasKeys = [bool]($values["GEMINI_API_KEY"] -or $values["DISCORD_BOT_TOKEN"])
if ($Update -or ($hasKeys -and -not (Ask-YesNo "Your keys are already saved. Change any of them?" $false))) {
    Ok "Keeping your saved keys"
} else {
    Ask-Keys @(
        @("GEMINI_API_KEY", "Gemini API key (aistudio.google.com > Get API key)", $true),
        @("FREE_LLM_BASE_URL", "Your free LLM API base URL, e.g. http://localhost:PORT/v1", $false),
        @("FREE_LLM_API_KEY", "Your free LLM API key", $true)
    )
    if (-not $values["FREE_LLM_MODEL"]) { $values["FREE_LLM_MODEL"] = "auto:default" }
    Ask-Keys @(
        @("FREE_LLM_MODEL", "Model name (Enter keeps $($values['FREE_LLM_MODEL']))", $false),
        @("YOUTUBE_API_KEY", "YouTube Data API key (Google Cloud console)", $true),
        @("DISCORD_BOT_TOKEN", "Discord bot token (Developer Portal > Bot)", $true),
        @("DISCORD_CHANNEL_ID", "Discord channel ID (right-click the channel > Copy Channel ID)", $false),
        @("DISCORD_OWNER_ID", "Your Discord user ID (right-click your name > Copy User ID)", $false),
        @("APIFY_TOKEN", "Apify token for the campaign finder (apify.com > Settings > Integrations)", $true),
        @("TELEGRAM_BOT_TOKEN", "Telegram bot token for posting clips by hand (@BotFather > /newbot)", $true),
        @("TELEGRAM_CHAT_ID", "Your Telegram chat ID (see SETUP-STEPS.txt)", $false)
    )
    $hasMeta = $values["IG_MAIN_TOKEN"] -or $values["FB_MAIN_PAGE_TOKEN"]
    if (Ask-YesNo "Set up Instagram and Facebook now?" $false) {
        Ask-Keys @(
            @("IG_MAIN_USER_ID", "Instagram user ID", $false),
            @("IG_MAIN_TOKEN", "Instagram long-lived token", $true),
            @("FB_MAIN_PAGE_ID", "Facebook Page ID", $false),
            @("FB_MAIN_PAGE_TOKEN", "Facebook Page token", $true)
        )
    } else {
        foreach ($n in @("IG_MAIN_USER_ID", "IG_MAIN_TOKEN", "FB_MAIN_PAGE_ID", "FB_MAIN_PAGE_TOKEN")) {
            if (-not $values.Contains($n)) { $values[$n] = "" }
        }
        Save-Env
        if (-not $hasMeta) { Warn "Skipped. Instagram clips will come to you on Discord to post yourself; Facebook is skipped until you add its keys." }
    }
}
Ok "Saved to .env (only your Windows user can read it)"

# 5. Campaigns and YouTube login
Step "5/7 Loading campaigns and connecting YouTube"
Push-Location $Target
& $cf sync
$clientSecret = Join-Path $Target "secrets\client_secret.json"
$ytToken = Join-Path $Target "secrets\youtube_main.json"
if ((Test-Path $clientSecret) -and -not (Test-Path $ytToken)) {
    Write-Host "    A browser opens: pick your YouTube channel and allow access."
    & $cf auth-youtube main
} elseif (-not (Test-Path $clientSecret)) {
    Warn "No secrets\client_secret.json yet, so YouTube uploads are off for now. See SETUP-STEPS.txt step 3."
}
Pop-Location

# 6. Auto-start
Step "6/7 Starting Clip Factory automatically at login and when the internet connects"
$startScript = Join-Path $Target "windows\start.ps1"
# Windows 11 opens console programs in Windows Terminal, which ignores -WindowStyle Hidden: an empty window
# appears, and closing it kills the runner (no updates, no restarts). conhost --headless runs it with no window.
function Hidden-Action($script) {
    $conhost = Join-Path $env:WINDIR "System32\conhost.exe"
    $ps = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
    if (Test-Path $conhost) {
        return New-ScheduledTaskAction -Execute $conhost -Argument "--headless powershell.exe $ps" -WorkingDirectory $Target
    }
    return New-ScheduledTaskAction -Execute "powershell.exe" -Argument $ps -WorkingDirectory $Target
}
$action = Hidden-Action $startScript
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$cls = Get-CimClass -ClassName MSFT_TaskEventTrigger -Namespace Root/Microsoft/Windows/TaskScheduler
$onNetwork = $cls | New-CimInstance -ClientOnly
$onNetwork.Enabled = $true
$onNetwork.Subscription = '<QueryList><Query Id="0" Path="Microsoft-Windows-NetworkProfile/Operational"><Select Path="Microsoft-Windows-NetworkProfile/Operational">*[System[EventID=10000]]</Select></Query></QueryList>'
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($atLogon, $onNetwork) -Settings $settings `
    -Principal $principal -Description "Runs Clip Factory: finds campaigns, makes clips, posts them." -Force | Out-Null
Ok "Task '$TaskName' added to Task Scheduler"

# Tells the offline-alert service the moment Windows shuts down, restarts or sleeps. Optional: never stops the install.
try {
    $pingAction = Hidden-Action (Join-Path $Target "windows\offline-ping.ps1")
    $onOff = $cls | New-CimInstance -ClientOnly
    $onOff.Enabled = $true
    $onOff.Subscription = '<QueryList><Query Id="0" Path="System"><Select Path="System">*[System[Provider[@Name=''User32''] and EventID=1074]]</Select><Select Path="System">*[System[Provider[@Name=''Microsoft-Windows-Kernel-Power''] and EventID=42]]</Select></Query></QueryList>'
    $pingSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName "Clip Factory offline notice" -Action $pingAction -Trigger @($onOff) `
        -Settings $pingSettings -Principal $principal -Force `
        -Description "Tells Discord 'Bot is offline' when the laptop shuts down or sleeps." | Out-Null
    Ok "Offline notice added (shutdown, restart, sleep)"
} catch { Write-Host "    Offline notice not added: $($_.Exception.Message)" -ForegroundColor Yellow }

if (-not $Update) {
    $sleep = Read-Host -Prompt "    Stop the laptop from sleeping while plugged in, so posts go out on time? (y/N)"
    if ($sleep -match '^[yY]') { powercfg /change standby-timeout-ac 0; Ok "Sleep on charger turned off" }
}

# 7. Shortcuts and start
Step "7/7 Shortcuts and first start"
Set-Content -Path (Join-Path $Target "Open Dashboard.url") -Value "[InternetShortcut]`r`nURL=http://127.0.0.1:8787/"
$shell = New-Object -ComObject WScript.Shell
foreach ($s in @(@("Start Clip Factory", "start.ps1"), @("Stop Clip Factory", "stop.ps1"))) {
    $lnk = $shell.CreateShortcut((Join-Path $Target "$($s[0]).lnk"))
    $lnk.TargetPath = "powershell.exe"
    $lnk.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $Target "windows\$($s[1])")`""
    $lnk.WorkingDirectory = $Target
    $lnk.Save()
}
Start-ScheduledTask -TaskName $TaskName

# GPU libraries come last, after Clip Factory is already running again, so the bot never waits on this
# ~1 GB download. They are picked up at the next restart; until then captions run on the CPU.
$gpuLibs = Test-Path (Join-Path $venv "Lib\site-packages\nvidia\cudnn")
if ((Get-Command nvidia-smi -ErrorAction SilentlyContinue) -and -not ($Update -and $gpuLibs)) {
    # NVIDIA GPU: libraries that let captions run on the GPU (about 1 GB, downloaded once).
    # Never fatal: without these, captions simply run on the CPU.
    $ErrorActionPreference = "Continue"
    & $vpy -m pip install --quiet "nvidia-cublas-cu12" "nvidia-cudnn-cu12==9.*" 2>&1 | Out-File -Append -FilePath (Join-Path $Target "logs\gpu-libs.log")
    $ok = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = "Stop"
    if ($ok) { Ok "GPU libraries for captions installed" } else { Warn "GPU libraries didn't install; captions use the CPU" }
}
if (-not $Update) { Start-Sleep -Seconds 8; Start-Process "http://127.0.0.1:8787/" }

Write-Host ""
Write-Host "Done. Clip Factory is running in the background." -ForegroundColor Green
Write-Host "Folder:    $Target"
Write-Host "Dashboard: http://127.0.0.1:8787  (or 'Open Dashboard' in the folder)"
Write-Host "Logs:      $Target\logs"
Write-Host "In Discord, type !help in your channel."
Write-Host "Keys missing? Run this installer again any time; it keeps everything you already set."
if (-not $Update) { Read-Host -Prompt "Press Enter to close" }
