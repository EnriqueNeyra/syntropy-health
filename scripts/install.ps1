<#
Syntropy Health for Windows, from the command line (PowerShell).

  irm https://health.syntropylabs.io/install.ps1 | iex

installs the Syntropy Health app for your account (the same installer as the download, run silently) and opens it.
For a background server with no app window, pass -Server:

  & ([scriptblock]::Create((irm https://health.syntropylabs.io/install.ps1))) -Server

-Server installs uv (Astral's Python installer) if it's missing, Syntropy Health with its own Python, the
syntropy-health command, and a task that starts the server when you sign in. Data lives in
%LOCALAPPDATA%\Syntropy Health either way. Run it again to update.

Options: -Server  -Port 8000  -NoService  -Source <wheel, path, requirement, or setup .exe>  -Uninstall [-Purge]
#>
param(
  [switch]$Server,
  [int]$Port = 8000,
  [string]$Source = "",
  [switch]$NoService,
  [switch]$Uninstall,
  [switch]$Purge
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$Repo = "https://github.com/EnriqueNeyra/syntropy-health"
$Releases = "$Repo/releases/latest/download"
$Data = if ($env:SYNTROPY_DATA_DIR) { $env:SYNTROPY_DATA_DIR } else { Join-Path $env:LOCALAPPDATA "Syntropy Health" }
$AppDir = Join-Path $env:LOCALAPPDATA "Programs\Syntropy Health"
$AppExe = Join-Path $AppDir "Syntropy Health.exe"
# Where uv puts the syntropy-health command (UV_TOOL_BIN_DIR moves it, as uv itself honours).
$Bin = if ($env:UV_TOOL_BIN_DIR) { $env:UV_TOOL_BIN_DIR } else { Join-Path $env:USERPROFILE ".local\bin" }

function Say($message) { Write-Host "==> $message" -ForegroundColor Cyan }
function Stop-App { Get-Process -Name "Syntropy Health" -ErrorAction SilentlyContinue | Stop-Process -Force; Start-Sleep -Seconds 1 }
function Wait-Server($port) {
  for ($i = 0; $i -lt 60; $i++) {
    try { if ((Invoke-WebRequest -UseBasicParsing "http://localhost:$port/health" -TimeoutSec 2).StatusCode -eq 200) { return $true } } catch {}
    Start-Sleep -Milliseconds 500
  }
  return $false
}
$env:Path = "$Bin;$env:Path"

if ($Uninstall) {
  Stop-App
  if (Get-Command syntropy-health -ErrorAction SilentlyContinue) { syntropy-health service uninstall | Out-Null }
  if (Get-Command uv -ErrorAction SilentlyContinue) { uv tool uninstall syntropy-health 2>$null | Out-Null }
  $uninstaller = Join-Path $AppDir "unins000.exe"
  if (Test-Path $uninstaller) { Start-Process $uninstaller -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART" -Wait }
  if ($Purge) { Remove-Item -Recurse -Force $Data -ErrorAction SilentlyContinue; Say "Removed Syntropy Health and its data." }
  else { Say "Removed Syntropy Health. Your data is still in $Data (delete it with -Uninstall -Purge)." }
  return
}

if (-not $Server) {
  # ---- The app: download the installer and run it silently, for this account (no administrator needed).
  $setup = Join-Path $env:TEMP "Syntropy-Health-setup.exe"
  if ($Source -and (Test-Path $Source)) { Copy-Item $Source $setup -Force }
  else {
    Say "Downloading the Syntropy Health app"
    try { Invoke-WebRequest -UseBasicParsing "$Releases/Syntropy-Health-windows-x64-setup.exe" -OutFile $setup }
    catch { throw "Couldn't download the app. Try again later, or install the background server with -Server." }
  }
  if (Get-ScheduledTask -TaskName "Syntropy Health" -ErrorAction SilentlyContinue) {
    Say "Removing the background server installed earlier: the app runs the server now, with the same data"
    & (Join-Path $Bin "syntropy-health.exe") service uninstall 2>$null | Out-Null
  }
  Stop-App
  Say "Installing into $AppDir"
  Start-Process $setup -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER" -Wait
  Remove-Item $setup -Force -ErrorAction SilentlyContinue
  if (-not (Test-Path $AppExe)) { throw "The installer didn't finish. Run it by hand from $Releases." }
  Start-Process $AppExe
  Write-Host ""
  Say "Syntropy Health is installed and open."
  Write-Host "    It keeps running in the notification area; choose Let Your iPhone and Other Devices Connect there, or in Settings."
  Write-Host "    Command line: `"$AppDir\syntropy-health.exe`" (backup, info, mcp)"
  Write-Host "    Data:         $Data"
  Write-Host "    Update:       run this installer again"
  return
}

# ---- -Server: Syntropy Health with its own Python, and a task that starts it when you sign in.
if (Get-Process -Name "Syntropy Health" -ErrorAction SilentlyContinue) {
  throw "The Syntropy Health app is running, and it already runs the server. Quit it (and uninstall it) first."
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Say "Installing uv (Python installer from astral.sh)"
  $env:UV_NO_MODIFY_PATH = "1"
  Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
  Remove-Item Env:UV_NO_MODIFY_PATH
}
# The latest release's wheel (before the first release, the latest code on main). `syntropy-health update` follows
# releases ("latest"), or reinstalls from -Source if one was given.
$Record = $Source
if (-not $Source) {
  $Record = "latest"
  $Source = "syntropy-health @ $Repo/archive/refs/heads/main.tar.gz"
  try {
    $tag = (Invoke-RestMethod -Headers @{ "User-Agent" = "Syntropy-Health-installer" } "https://api.github.com/repos/EnriqueNeyra/syntropy-health/releases/latest").tag_name
    if ($tag) { $Source = "syntropy-health @ $Repo/releases/download/$tag/syntropy_health-$($tag.TrimStart('v'))-py3-none-any.whl" }
  } catch { }
}
Say "Installing Syntropy Health"
uv tool install --quiet --force --python 3.12 $Source
if ($LASTEXITCODE -ne 0) { throw "uv couldn't install Syntropy Health." }
Set-Content -Path (Join-Path (uv tool dir) "syntropy-health\syntropy-source.txt") -Value $Record
uv tool update-shell 2>$null | Out-Null      # puts ~\.local\bin on your PATH for new terminals

if (-not $NoService) {
  Say "Starting the server (and whenever you sign in)"
  syntropy-health service install --port $Port | Out-Null
  if (Wait-Server $Port) { Start-Process "http://localhost:$Port" }
  $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
  if ($admin) {
    # Let the iPhone and other devices reach it on private (home) networks only.
    if (-not (Get-NetFirewallRule -DisplayName "Syntropy Health" -ErrorAction SilentlyContinue)) {
      New-NetFirewallRule -DisplayName "Syntropy Health" -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow -Profile Private | Out-Null
    }
  } else {
    Write-Host "    For your iPhone and other devices to connect, allow port $Port through Windows Firewall on private networks"
    Write-Host "    (or run this installer once as administrator)."
  }
}
Write-Host ""
Say "Syntropy Health $(syntropy-health version) is installed."
Write-Host "    Open http://localhost:$Port to finish setup."
Write-Host "    Data:    $Data   (back up with: syntropy-health backup `$HOME\Desktop)"
Write-Host "    Update:  syntropy-health update   (or run this installer again)"
