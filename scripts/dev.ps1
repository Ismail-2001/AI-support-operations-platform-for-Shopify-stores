<#
    scripts/dev.ps1 - start / stop / status for the local demo stack.

    Usage:
        .\scripts\dev.ps1 start        # start API (8001) + dashboard (5173)
        .\scripts\dev.ps1 status       # show what's running
        .\scripts\dev.ps1 stop         # stop both
        .\scripts\dev.ps1 start -api   # API only
        .\scripts\dev.ps1 start -dash  # dashboard only

    Logs are written to $env:TEMP\cs_agent_dev\.
    PIDs are recorded in $env:TEMP\cs_agent_dev\*.pid so `stop` can target them.
#>
param(
    [ValidateSet("start", "stop", "status")] [string]$Action = "status",
    [switch]$Api,
    [switch]$Dash
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $env:TEMP "cs_agent_dev"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$Py = Join-Path $Root ".venv\Scripts\python.exe"
$ApiPort = 8001
$DashPort = 5173

function Get-PidFile($name) { Join-Path $LogDir "$name.pid" }

function Read-Pid($name) {
    $f = Get-PidFile $name
    if (-not (Test-Path $f)) { return $null }
    $pid_ = (Get-Content $f | Select-Object -First 1).Trim()
    if ($pid_ -match '^\d+$') { return [int]$pid_ }
    return $null
}

function Is-Alive($pid_) {
    if (-not $pid_) { return $false }
    return [bool](Get-Process -Id $pid_ -ErrorAction SilentlyContinue)
}

function Is-PortListening($port) {
    return [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

function Start-One($name, $filePath, $argsList, $port, $workDir) {
    if (-not $workDir) { $workDir = $Root }
    $existing = Read-Pid $name
    if ((Is-Alive $existing) -or (Is-PortListening $port)) {
        Write-Host "$name already running on port $port" -ForegroundColor Yellow
        return
    }
    $out = Join-Path $LogDir "$name.out.log"
    $err = Join-Path $LogDir "$name.err.log"
    $proc = Start-Process -FilePath $filePath -ArgumentList $argsList -WorkingDirectory $workDir `
        -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden -PassThru
    Set-Content -Path (Get-PidFile $name) -Value $proc.Id
    Write-Host "$name started (PID $($proc.Id), port $port)" -ForegroundColor Green
}

function Stop-One($name, $port) {
    $pid_ = Read-Pid $name
    $killed = $false
    if (Is-Alive $pid_) {
        Stop-Process -Id $pid_ -Force -ErrorAction SilentlyContinue
        $killed = $true
    }
    # Belt-and-braces: also stop whatever holds the port (uvicorn spawns a child process).
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($conn) {
        $conn.OwningProcess | Sort-Object -Unique | ForEach-Object {
            Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue
            $killed = $true
        }
    }
    Remove-Item -Path (Get-PidFile $name) -ErrorAction SilentlyContinue
    Write-Host "$name stopped ($killed)" -ForegroundColor Green
}

switch ($Action) {
    "start" {
        $doApi = $Api -or (-not $Dash)
        $doDash = $Dash -or (-not $Api)
        if ($doApi) { Start-One "api" $Py @("-m", "uvicorn", "api.main:app", "--host", "127.0.0.1", "--port", "$ApiPort") $ApiPort }
        if ($doDash) {
            $npm = (Get-Command npm.cmd -ErrorAction SilentlyContinue).Source
            if (-not $npm) { throw "npm.cmd not found on PATH" }
            Start-One "dash" $npm @("run", "dev") $DashPort (Join-Path $Root "dashboard")
        }
        Write-Host "API:  http://localhost:$ApiPort   Dashboard: http://localhost:$DashPort" -ForegroundColor Cyan
    }
    "stop" {
        Stop-One "api" $ApiPort
        Stop-One "dash" $DashPort
    }
    "status" {
        foreach ($svc in @(@{n="api"; p=$ApiPort}, @{n="dash"; p=$DashPort})) {
            $pid_ = Read-Pid $svc.n
            $alive = Is-Alive $pid_
            $listening = Is-PortListening $svc.p
            $state = if ($alive -and $listening) { "RUNNING" } elseif ($alive -or $listening) { "PARTIAL" } else { "STOPPED" }
            Write-Host ("{0,-5} {1,-8} pid={2} port={3}" -f $svc.n, $state, $(if ($alive) { $pid_ } else { "-" }), $svc.p)
        }
    }
}
