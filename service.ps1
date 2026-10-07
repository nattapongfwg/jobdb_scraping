<#
.SYNOPSIS
  Run the Recruitment board (webapp.py) as a per-user background task that starts at logon.

.DESCRIPTION
  Registers a Windows Scheduled Task that launches run_webapp.cmd hidden, under YOUR
  account, every time you log on. Running as your own user keeps SQL Server Windows
  Authentication, the Graph token cache and OneDrive paths working exactly as when
  you start webapp.py by hand.

  Per-copy settings come from this folder's .env:
    SERVICE_NAME  task name   (blank = "JobDB Recruitment Board")
    PORT          port        (blank = 2757)
    HOST          127.0.0.1 = this PC only (default); 0.0.0.0 = the office LAN too
  Two copies on one PC (e.g. live + a dev copy) need different SERVICE_NAME and PORT;
  install refuses to take over a task that belongs to another folder.

  Usage (PowerShell, from the project folder):
    .\service.ps1 install            register the task and start the board
    .\service.ps1 status             task state, HTTP health, PID, current commit
    .\service.ps1 restart            stop + start (after editing any .py or .env)
    .\service.ps1 deploy [-Pull]     [git pull] -> pip install -> restart -> health check
    .\service.ps1 logs [-Follow]     tail logs\webapp.log
    .\service.ps1 stop | start | uninstall
    .\service.ps1 firewall           allow other LAN computers in on PORT (asks for Administrator)
    .\service.ps1 firewall-remove    close that port again (asks for Administrator)

  From WSL:  powershell.exe -NoProfile -ExecutionPolicy Bypass -File E:\jobdb_scraping\service.ps1 status
             (or the same path in any other copy of the board, e.g. E:\jobdb_multiuser)
#>
[CmdletBinding()]
param(
  [Parameter(Position = 0)]
  [ValidateSet('install', 'uninstall', 'start', 'stop', 'restart', 'status', 'logs', 'deploy',
               'firewall', 'firewall-remove', 'help')]
  [string]$Action = 'help',
  [switch]$Pull,
  [switch]$Follow
)

$ErrorActionPreference = 'Stop'
$Root     = $PSScriptRoot

# KEY=value lines of this folder's .env (comments and blanks skipped, quotes stripped).
function Read-DotEnv {
  $vals = @{}
  $path = Join-Path $Root '.env'
  if (Test-Path $path) {
    foreach ($line in Get-Content $path -Encoding UTF8) {
      if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') {
        $vals[$Matches[1]] = $Matches[2].Trim('"').Trim("'")
      }
    }
  }
  return $vals
}
$DotEnv   = Read-DotEnv
$TaskName = if ($DotEnv['SERVICE_NAME']) { $DotEnv['SERVICE_NAME'] } else { 'JobDB Recruitment Board' }
$Port     = if ($DotEnv['PORT']) { [int]$DotEnv['PORT'] } else { 2757 }
$BindHost = if ($DotEnv['HOST']) { $DotEnv['HOST'] } else { '127.0.0.1' }
$Url      = "http://localhost:$Port"
$Health   = "http://127.0.0.1:$Port/login"
$FwRule   = "$TaskName - LAN port $Port"
$LogDir   = Join-Path $Root 'logs'
$LogFile  = Join-Path $LogDir 'webapp.log'
$StopFlag = Join-Path $LogDir 'stop.flag'
$Vbs      = Join-Path $Root 'run_webapp_hidden.vbs'
$Python   = Join-Path $Root '.venv\Scripts\python.exe'
$User     = "$env:USERDOMAIN\$env:USERNAME"

function Write-Step([string]$m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Get-Task { Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue }

function Get-Http {
  try { (Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 $Health).StatusCode } catch { 0 }
}

# Every process that belongs to this app: the hidden launcher, the keep-alive cmd, and
# webapp.py itself (only when run by THIS project's .venv python, so other Pythons are safe).
function Get-AppProcesses {
  $venv = Join-Path $Root '.venv'
  Get-CimInstance Win32_Process | Where-Object {
    ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($venv, 'OrdinalIgnoreCase') -and $_.CommandLine -like '*webapp.py*') -or
    ($_.CommandLine -like '*run_webapp.cmd*'        -and $_.CommandLine -like "*$Root*") -or
    ($_.CommandLine -like '*run_webapp_hidden.vbs*' -and $_.CommandLine -like "*$Root*")
  }
}

function Wait-Until([scriptblock]$Condition, [int]$Seconds) {
  $deadline = (Get-Date).AddSeconds($Seconds)
  while ((Get-Date) -lt $deadline) {
    if (& $Condition) { return $true }
    Start-Sleep -Milliseconds 500
  }
  return [bool](& $Condition)
}

function Show-Logs([int]$Tail = 40) {
  if (-not (Test-Path $LogFile)) { Write-Host "No log yet at $LogFile"; return }
  if ($Follow) { Get-Content $LogFile -Tail $Tail -Wait } else { Get-Content $LogFile -Tail $Tail }
}

function Stop-App {
  Write-Step "Stopping $TaskName"
  New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
  Set-Content -Path $StopFlag -Value (Get-Date)          # tells run_webapp.cmd not to relaunch
  if (Get-Task) { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue }
  # Wrappers first (so nothing can relaunch), then the python server itself.
  $procs = Get-AppProcesses | Sort-Object { if ($_.CommandLine -like '*webapp.py*') { 1 } else { 0 } }
  foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
  if (Wait-Until { (Get-Http) -eq 0 -and -not (Get-AppProcesses) } 15) {
    Write-Host 'Stopped.' -ForegroundColor Green
  } else {
    Write-Warning "Something is still listening on $Url"
  }
}

function Start-App {
  if (-not (Get-Task)) { throw "Task '$TaskName' is not installed. Run:  .\service.ps1 install" }
  if (-not (Test-Path $Python)) { throw "Missing $Python - create the venv first (see INSTALL.md)" }
  Remove-Item $StopFlag -ErrorAction SilentlyContinue
  if ((Get-Http) -eq 200) { Write-Host "Already running at $Url" -ForegroundColor Green; return }
  Write-Step "Starting $TaskName"
  Start-ScheduledTask -TaskName $TaskName
  if (Wait-Until { (Get-Http) -eq 200 } 45) {
    Write-Host "Running at $Url" -ForegroundColor Green
  } else {
    Write-Warning "Not responding yet. Last log lines:"
    Show-Logs 30
    exit 1
  }
}

function Install-App {
  # Never take over another copy's task (e.g. the live board's) by re-registering its name.
  $existing = Get-Task
  if ($existing) {
    $dir = $existing.Actions[0].WorkingDirectory
    if ($dir -and ((Resolve-Path $dir -ErrorAction SilentlyContinue).Path -ne (Resolve-Path $Root).Path)) {
      throw "Task '$TaskName' already runs the board in $dir. Set a different SERVICE_NAME (and PORT) in $Root\.env, then run install again."
    }
  }
  Write-Step "Registering scheduled task '$TaskName' (at logon of $User, hidden window)"
  $action    = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$Vbs`"" -WorkingDirectory $Root
  $trigger   = New-ScheduledTaskTrigger -AtLogOn -User $User
  $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                 -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
  $principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Force `
    -Description "Recruitment board web server ($Url). Managed by service.ps1 in $Root" | Out-Null
  Write-Host 'Task installed.' -ForegroundColor Green
  Stop-App      # replace any manually started webapp.py so the task owns the port
  Start-App
}

function Uninstall-App {
  Stop-App
  if (Get-Task) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Task '$TaskName' removed." -ForegroundColor Green
  } else {
    Write-Host 'Task was not installed.'
  }
}

function Show-Status {
  $t = Get-Task
  if ($t) {
    $info = $t | Get-ScheduledTaskInfo
    Write-Host ("Task      : {0}  [{1}]" -f $TaskName, $t.State)
    Write-Host ("Last run  : {0}  (result {1})" -f $info.LastRunTime, $info.LastTaskResult)
  } else {
    Write-Host "Task      : NOT INSTALLED  (run  .\service.ps1 install)" -ForegroundColor Yellow
  }
  $code = Get-Http
  $py   = Get-AppProcesses | Where-Object { $_.CommandLine -like '*webapp.py*' }
  $web  = if ($code) { "HTTP $code" } else { 'no response' }
  Write-Host ("Web       : {0}  -> {1}" -f $Url, $web) -ForegroundColor $(if ($code -eq 200) { 'Green' } else { 'Red' })
  Write-Host ("Python PID: {0}" -f $(if ($py) { ($py.ProcessId -join ', ') } else { '-' }))
  if (Get-Command git -ErrorAction SilentlyContinue) {
    Write-Host ("Code      : {0}" -f (git -C $Root log -1 --format='%h %s (%cr)' 2>$null))
  }
  Write-Host ("Listens on: {0}:{1}" -f $BindHost, $Port)
  if ($BindHost -ne '127.0.0.1' -and $BindHost -ne 'localhost') {
    foreach ($ip in Get-LanAddresses) { Write-Host ("LAN       : http://{0}:{1}" -f $ip, $Port) -ForegroundColor Green }
    $rule = Get-NetFirewallRule -DisplayName $FwRule -ErrorAction SilentlyContinue
    Write-Host ("Firewall  : {0}" -f $(if ($rule) { "rule '$FwRule' present" } else { 'no port rule (run  .\service.ps1 firewall  as Administrator)' }))
  }
  Write-Host ("Logs      : {0}" -f $LogFile)
}

# This PC's IPv4 address on each network with a gateway (Wi-Fi / Ethernet), i.e. what
# colleagues type in their browser. Virtual adapters (WSL, Hyper-V) have no gateway.
function Get-LanAddresses {
  Get-NetIPConfiguration -ErrorAction SilentlyContinue |
    Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq 'Up' } |
    ForEach-Object { $_.IPv4Address.IPAddress }
}

function Test-Admin {
  ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
}

# Re-run this script elevated (UAC prompt) for the firewall actions, then return.
function Invoke-Elevated([string]$what) {
  Write-Step "Windows will ask for Administrator permission to $what"
  $p = Start-Process powershell.exe -Verb RunAs -Wait -PassThru -ArgumentList @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", $Action)
  if ($p.ExitCode) { throw "The elevated step failed (exit $($p.ExitCode))." }
}

# Inbound TCP on PORT, only on Domain/Private networks (never Public Wi-Fi), and only
# from private LAN addresses.
function Open-Firewall {
  if (-not (Test-Admin)) { Invoke-Elevated "open port $Port"; Show-Status; return }
  Remove-NetFirewallRule -DisplayName $FwRule -ErrorAction SilentlyContinue
  New-NetFirewallRule -DisplayName $FwRule -Direction Inbound -Action Allow -Protocol TCP `
    -LocalPort $Port -Profile Domain, Private `
    -RemoteAddress @('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16') `
    -Description "Recruitment board ($Root) for other PCs on the office LAN. Managed by service.ps1." | Out-Null
  Write-Host "Firewall rule '$FwRule' added (TCP $Port, Domain/Private networks, LAN addresses only)." -ForegroundColor Green
}

function Close-Firewall {
  if (-not (Get-NetFirewallRule -DisplayName $FwRule -ErrorAction SilentlyContinue)) { Write-Host 'No firewall rule to remove.'; return }
  if (-not (Test-Admin)) { Invoke-Elevated "close port $Port"; return }
  Remove-NetFirewallRule -DisplayName $FwRule
  Write-Host "Firewall rule '$FwRule' removed." -ForegroundColor Green
}

function Deploy-App {
  Push-Location $Root
  try {
    if ($Pull) {
      if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw 'git not found on PATH' }
      Write-Step 'git pull --ff-only'
      git pull --ff-only
      if ($LASTEXITCODE) { throw 'git pull failed (uncommitted changes or non-fast-forward?)' }
    }
    Write-Step 'Installing Python dependencies (requirements.txt)'
    & $Python -m pip install -q --disable-pip-version-check -r requirements.txt
    if ($LASTEXITCODE) { throw 'pip install failed' }
    Stop-App
    Start-App
    Write-Host ''
    Show-Status
  } finally { Pop-Location }
}

switch ($Action) {
  'install'   { Install-App }
  'uninstall' { Uninstall-App }
  'start'     { Start-App }
  'stop'      { Stop-App }
  'restart'   { Stop-App; Start-App }
  'status'    { Show-Status }
  'logs'      { Show-Logs 60 }
  'deploy'    { Deploy-App }
  'firewall'  { Open-Firewall }
  'firewall-remove' { Close-Firewall }
  default     { Get-Help $PSCommandPath -Detailed }
}
