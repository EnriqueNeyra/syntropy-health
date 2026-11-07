<#
Syntropy Health for Windows, from the command line (PowerShell).

  irm https://health.syntropylabs.io/install.ps1 | iex

installs the Syntropy Health app for your account (the same installer as the download, run silently) and opens it.
For a background server with no app window, pass -Server:

  & ([scriptblock]::Create((irm https://health.syntropylabs.io/install.ps1))) -Server

-Server installs uv (Astral's Python installer) if it's missing, Syntropy Health with its own Python, the
syntropy-health command, and a task that starts the server when you sign in. Data lives in
%LOCALAPPDATA%\Syntropy Health either way. Run it again to update: the server keeps the port and address it has.
What it downloads from a release is checked against that release's SHA256SUMS first. With -Server -Source,
-Constraints <requirements file> installs its dependencies at the versions in that file (as `uv export` writes it).

Options: -Server  -Port N (default: the port it has now, or 8000)  -NoService
         -Source <wheel, path, requirement, or setup .exe, as a path or address>  -Uninstall [-Purge]
#>
param(
  [switch]$Server,
  [int]$Port = 0,
  [string]$Source = "",
  [string]$Constraints = "",
  [switch]$NoService,
  [switch]$Uninstall,
  [switch]$Purge
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$Repo = "https://github.com/EnriqueNeyra/syntropy-health"
$Releases = "$Repo/releases/latest/download"
$UvVersion = "0.11.15"      # the same in install.sh, the Dockerfile and the workflows (tests/test_deployment_files.py)
$Requirements = "Syntropy-Health-requirements.txt"
$SetupAsset = "Syntropy-Health-windows-x64-setup.exe"
$FirewallRule = "Syntropy Health"
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
function Get-LatestTag {
  $tag = $null
  try {
    $tag = (Invoke-RestMethod -Headers @{ "User-Agent" = "Syntropy-Health-installer" } "https://api.github.com/repos/EnriqueNeyra/syntropy-health/releases/latest").tag_name
  } catch { }
  if (-not $tag) { throw "Couldn't find the latest release on GitHub. Check the internet connection and try again in a few minutes." }
  return $tag
}
# A file from a release, into $folder, once its checksum matches the release's SHA256SUMS. "" if $optional and it isn't listed.
function Get-CheckedFile($base, $name, $folder, [switch]$optional) {
  $sums = Join-Path $folder "SHA256SUMS"
  if (-not (Test-Path $sums)) {
    try { Invoke-WebRequest -UseBasicParsing "$base/SHA256SUMS" -OutFile $sums }
    catch { throw "Couldn't download the release's checksums. Try again later." }
  }
  $line = Get-Content $sums | Where-Object { ($_ -split '\s+', 2)[1] -eq $name -or ($_ -split '\s+', 2)[1] -eq "*$name" } | Select-Object -First 1
  if (-not $line) {
    if ($optional) { return "" }
    throw "The release doesn't list a checksum for $name, so it wasn't installed. Try again later."
  }
  $file = Join-Path $folder $name
  try { Invoke-WebRequest -UseBasicParsing "$base/$name" -OutFile $file }
  catch { throw "Couldn't download $name. Try again later, or install the background server with -Server." }
  if ((Get-FileHash -Algorithm SHA256 $file).Hash -ne ($line -split '\s+', 2)[0]) {
    Remove-Item $file -Force -ErrorAction SilentlyContinue
    throw "The download of $name doesn't match the checksum the release published, so it wasn't installed. Try again; if this keeps happening, tell us: $Repo/issues"
  }
  return $file
}
function New-WorkFolder {
  $folder = Join-Path $env:TEMP ("syntropy-health-" + [Guid]::NewGuid().ToString("N"))
  New-Item -ItemType Directory -Force $folder | Out-Null
  return $folder
}
function Test-Admin { ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) }
# Windows PowerShell treats a redirected line of stderr as a stopping error here, and uv reports its progress on stderr.
function Invoke-Quiet($program) {
  $saved = $ErrorActionPreference
  $ErrorActionPreference = "SilentlyContinue"
  try { & $program @args 2>$null | Out-Null } catch { } finally { $ErrorActionPreference = $saved }
}
$env:Path = "$Bin;$env:Path"

if ($Uninstall) {
  Stop-App
  if (Get-Command syntropy-health -ErrorAction SilentlyContinue) { syntropy-health service uninstall | Out-Null }
  if (Get-Command uv -ErrorAction SilentlyContinue) { Invoke-Quiet uv tool uninstall syntropy-health }
  $uninstaller = Join-Path $AppDir "unins000.exe"
  if (Test-Path $uninstaller) { Start-Process $uninstaller -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART" -Wait }
  if (Get-NetFirewallRule -DisplayName $FirewallRule -ErrorAction SilentlyContinue) {
    if (Test-Admin) { Remove-NetFirewallRule -DisplayName $FirewallRule }
    else { Write-Host "    To close the port it opened, run this as administrator: Remove-NetFirewallRule -DisplayName `"$FirewallRule`"" }
  }
  if ($Purge) {
    # The key lives only in key.json, protected for this Windows account (DPAPI): nothing of it is kept anywhere else. It
    # goes first, so that a file something still holds open, which stops the folder going, can't keep the key here.
    foreach ($kept in "key.json", "secret.key") { Remove-Item -Force (Join-Path $Data $kept) -ErrorAction SilentlyContinue }
    Remove-Item -Recurse -Force $Data -ErrorAction SilentlyContinue
    Say "Removed Syntropy Health and its data."
  }
  else { Say "Removed Syntropy Health. Your data is still in $Data (delete it with -Uninstall -Purge)." }
  return
}

if (-not $Server) {
  # ---- The app: download the installer and run it silently, for this account (no administrator needed).
  $work = New-WorkFolder
  $expected = $null
  if ($Source -match '^https?://') {
    Say "Downloading $Source"
    $setup = Join-Path $work "Syntropy-Health-setup.exe"
    try { Invoke-WebRequest -UseBasicParsing $Source -OutFile $setup }
    catch { throw "Couldn't download $Source." }
  } elseif ($Source) {
    if (-not (Test-Path $Source)) { throw "There's no file at $Source." }
    $setup = Join-Path $work "Syntropy-Health-setup.exe"
    Copy-Item $Source $setup -Force
  } else {
    Say "Downloading the Syntropy Health app"
    $tag = Get-LatestTag
    $expected = $tag.TrimStart('v')
    $setup = Get-CheckedFile "$Repo/releases/download/$tag" $SetupAsset $work
    # Not every release's installer is signed yet. One that is has to be intact.
    $signature = Get-AuthenticodeSignature $setup
    if ($signature.Status -ne "NotSigned" -and $signature.Status -ne "Valid") {
      throw "Windows says the downloaded installer's signature isn't good ($($signature.Status)), so it wasn't run."
    }
  }
  if (Get-ScheduledTask -TaskName "Syntropy Health" -ErrorAction SilentlyContinue) {
    Say "Removing the background server installed earlier: the app runs the server now, with the same data"
    Invoke-Quiet (Join-Path $Bin "syntropy-health.exe") service uninstall
  }
  Stop-App
  Say "Installing into $AppDir"
  $installing = Start-Process $setup -ArgumentList "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CURRENTUSER" -Wait -PassThru
  $finished = $installing.ExitCode
  Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
  if ($finished -ne 0) { throw "The installer stopped with an error (code $finished), so your app may not have changed. Run it by hand from $Releases." }
  if (-not (Test-Path $AppExe)) { throw "The installer didn't finish. Run it by hand from $Releases." }
  if ($expected) {
    $installed = (& (Join-Path $AppDir "syntropy-health.exe") version | Out-String).Trim()
    if ($installed -ne $expected) { throw "The installer finished, but version $installed is in place instead of $expected. Run it by hand from $Releases." }
  }
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
  Invoke-RestMethod "https://astral.sh/uv/$UvVersion/install.ps1" | Invoke-Expression
  Remove-Item Env:UV_NO_MODIFY_PATH
}
# The latest release's wheel, checked, with the exact dependencies that release was built with (none for one from before
# they were published). `syntropy-health update` follows releases ("latest"), or reinstalls from -Source if one was
# given. Never the source code instead: only a release build carries Syntropy's sign-in registrations.
$Record = $Source
$work = New-WorkFolder
$pinned = @()
if ($Constraints) {
  if (-not $Source) { throw "-Constraints goes with -Source: a release brings its own." }
  if (-not (Test-Path $Constraints)) { throw "There's no file at $Constraints." }
  $pinned = @("--constraints", (Resolve-Path $Constraints).Path)
}
if (-not $Source) {
  $Record = "latest"
  $tag = Get-LatestTag
  $base = "$Repo/releases/download/$tag"
  $Source = Get-CheckedFile $base "syntropy_health-$($tag.TrimStart('v'))-py3-none-any.whl" $work
  $lock = Get-CheckedFile $base $Requirements $work -optional
  if ($lock) { $pinned = @("--constraints", $lock) }
}
# A running server holds its files open, and Windows won't let them be replaced.
$Command = Join-Path $Bin "syntropy-health.exe"
if (Test-Path $Command) { Invoke-Quiet $Command service stop; Start-Sleep -Seconds 1 }
Say "Installing Syntropy Health"
uv tool install --quiet --force --python 3.12 @pinned $Source
if ($LASTEXITCODE -ne 0) {
  if (Test-Path $Command) { Invoke-Quiet $Command service start }
  throw "uv couldn't install Syntropy Health."
}
Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
$toolDir = Join-Path (uv tool dir) "syntropy-health"
Set-Content -Path (Join-Path $toolDir "syntropy-source.txt") -Value $Record
Set-Content -Path (Join-Path $toolDir "syntropy-uv.txt") -Value (Get-Command uv).Source
Invoke-Quiet uv tool update-shell      # puts ~\.local\bin on your PATH for new terminals

if (-not $NoService) {
  Say "Starting the server (and whenever you sign in)"
  $portArgs = if ($Port) { @("--port", $Port) } else { @() }
  # Keeps the port and address it has unless -Port asks for another, and says which it's using.
  $said = (syntropy-health service install @portArgs | Out-String)
  if ($LASTEXITCODE -ne 0) { throw "Couldn't set up the server to start when you sign in: $said" }
  if ($said -match 'http://localhost:(\d+)') { $Port = [int]$Matches[1] }
  if (Wait-Server $Port) { Start-Process "http://localhost:$Port" }
  else { Write-Host "    The server hasn't answered yet. A big database can take a few minutes to upgrade. If it stays that way, look in $Data\server.log" }
  if (Test-Admin) {
    # Let the iPhone and other devices reach it on private (home) networks only.
    $rule = Get-NetFirewallRule -DisplayName $FirewallRule -ErrorAction SilentlyContinue
    if (-not $rule) {
      New-NetFirewallRule -DisplayName $FirewallRule -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow -Profile Private | Out-Null
    } elseif (($rule | Get-NetFirewallPortFilter).LocalPort -ne "$Port") {
      Set-NetFirewallRule -DisplayName $FirewallRule -LocalPort $Port -Protocol TCP
    }
  } else {
    Write-Host "    For your phone and other devices to connect, allow port $Port through Windows Firewall on private networks"
    Write-Host "    (or run this installer once as administrator)."
  }
}
if (-not $Port) { $Port = 8000 }
Write-Host ""
Say "Syntropy Health $(syntropy-health version) is installed."
Write-Host "    Open http://localhost:$Port to finish setup."
Write-Host "    Data:    $Data   (back up with: syntropy-health backup D:\A\Folder\On\Another\Drive)"
Write-Host "    Update:  syntropy-health update   (or run this installer again)"
