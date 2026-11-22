#Requires -Version 5.1
<#
.SYNOPSIS
    Pocket Relay Bridge installer for Windows.

    irm https://raw.githubusercontent.com/bpollak/pocket-relay-bridge/main/install.ps1 | iex

    Installs the bridge to %USERPROFILE%\.antigravity-mobile\runtime, sets it to start at logon
    (a Scheduled Task named PocketRelayBridge), and adds the Pocket Relay plugin to Antigravity.
    It does not need administrator rights.
#>
$ErrorActionPreference = "Stop"
$Repo = "bpollak/pocket-relay-bridge"
$Port = if ($env:POCKET_RELAY_PORT) { [int]$env:POCKET_RELAY_PORT } else { 8765 }
$InstallDir = Join-Path $env:USERPROFILE ".antigravity-mobile\runtime"
$TaskName = "PocketRelayBridge"

function Say($m) { Write-Host "==> $m" -ForegroundColor Magenta }

# 1. Antigravity command-line tool
$Agy = (Get-Command agy -ErrorAction SilentlyContinue).Source
if (-not $Agy) {
    $Candidate = Join-Path $env:LOCALAPPDATA "agy\bin\agy.exe"
    if (Test-Path $Candidate) { $Agy = $Candidate }
}
if (-not $Agy) {
    throw "The Google Antigravity command-line tool (agy) was not found. Install Google Antigravity and its command-line tool, sign in, then run this installer again."
}
Say "Found the Antigravity command-line tool: $Agy"

# 2. uv
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Say "Installing uv (Python package manager) from astral.sh"
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    $env:PATH = "$env:USERPROFILE\.local\bin;$env:PATH"
}

# 3. Get the files
$Work = Join-Path $env:TEMP ("pocket-relay-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $Work | Out-Null
try {
    Say "Downloading Pocket Relay Bridge"
    $Zip = Join-Path $Work "bridge.zip"
    Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/$Repo/archive/refs/heads/main.zip" -OutFile $Zip
    Expand-Archive -Path $Zip -DestinationPath $Work -Force
    $Src = (Get-ChildItem -Path $Work -Directory | Select-Object -First 1).FullName

    Say "Installing to $InstallDir"
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir }
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    Copy-Item -Recurse -Force (Join-Path $Src "bridge\antigravity_bridge") $InstallDir
    Copy-Item -Force (Join-Path $Src "bridge\pyproject.toml") $InstallDir
    Copy-Item -Force (Join-Path $Src "bridge\uv.lock") $InstallDir -ErrorAction SilentlyContinue

    Push-Location $InstallDir
    try { uv sync --python 3.13 --no-dev } finally { Pop-Location }
    $Bin = Join-Path $InstallDir ".venv\Scripts\antigravity-bridge.exe"
    if (-not (Test-Path $Bin)) { throw "The bridge did not install correctly ($Bin is missing)." }

    # 4. Start at logon
    $Action = New-ScheduledTaskAction -Execute $Bin -Argument "--host 0.0.0.0 --port $Port" -WorkingDirectory $InstallDir
    $Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Force | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    Say "Pocket Relay Bridge will start when you log on (Scheduled Task: $TaskName)"

    # 5. Antigravity plugin
    Say "Adding the Pocket Relay plugin to Antigravity"
    & $Agy plugin uninstall pocket-relay 2>$null | Out-Null
    & $Agy plugin install (Join-Path $Src "plugin\pocket-relay")
}
finally {
    Remove-Item -Recurse -Force $Work -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "Next: pair your iPhone or iPad"
Write-Host "  1. In Antigravity, open the Pocket Relay panel. It shows a QR code."
Write-Host "     (No panel? Open http://127.0.0.1:$Port/pair in a browser on this computer.)"
Write-Host "  2. In the Pocket Relay app, tap Scan QR Code and point it at the code."
Write-Host ""
Write-Host "Windows may ask whether to allow the bridge through the firewall on private networks. Allow it so your phone can connect."
