<#
.SYNOPSIS
  Run the Recruitment board (webapp.py) as a per-user background task that starts at logon.

.DESCRIPTION
  Registers a Windows Scheduled Task ("JobDB Recruitment Board") that launches
  run_webapp.cmd hidden, under YOUR account, every time you log on. Running as
  your own user keeps SQL Server Windows Authentication, the Graph token cache
  and OneDrive paths working exactly as when you start webapp.py by hand.

  Usage (PowerShell, from the project folder):
    .\service.ps1 install            register the task and start the board
    .\service.ps1 status             task state, HTTP health, PID, current commit
    .\service.ps1 restart            stop + start (after editing any .py or .env)
    .\service.ps1 deploy [-Pull]     [git pull] -> pip install -> restart -> health check
    .\service.ps1 logs [-Follow]     tail logs\webapp.log
    .\service.ps1 stop | start | uninstall

  From WSL:  powershell.exe -NoProfile -ExecutionPolicy Bypass -File E:\jobdb_scraping\service.ps1 status
#>
[CmdletBinding()]
param(
  [Parameter(Position = 0)]
  [ValidateSet('install', 'uninstall', 'start', 'stop', 'restart', 'status', 'logs', 'deploy', 'help')]
  [string]$Action = 'help',
  [switch]$Pull,
  [switch]$Follow
)

$ErrorActionPreference = 'Stop'
$Root     = $PSScriptRoot
$TaskName = 'JobDB Recruitment Board'
$Url      = 'http://localhost:2757'
$LogDir   = Join-Path $Root 'logs'
$LogFile  = Join-Path $LogDir 'webapp.log'
$StopFlag = Join-Path $LogDir 'stop.flag'
$Vbs      = Join-Path $Root 'run_webapp_hidden.vbs'
$Python   = Join-Path $Root '.venv\Scripts\python.exe'
$User     = "$env:USERDOMAIN\$env:USERNAME"

function Write-Step([string]$m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Get-Task { Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue }

function Get-Http {
  try { (Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 $Url).StatusCode } catch { 0 }
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
  Write-Host ("Logs      : {0}" -f $LogFile)
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
  default     { Get-Help $PSCommandPath -Detailed }
}
