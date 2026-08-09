param(
    [ValidateSet('Ensure','Stop','Status')][string]$Action = 'Ensure'
)

. "$PSScriptRoot\xv12-common.ps1"

$appConfig = $script:RuntimeConfig.application
$port = if ($env:XV12_APP_PORT) { [int]$env:XV12_APP_PORT } else { [int]$appConfig.port }
$python = Join-Path $script:XV12Root 'runtime\python\Scripts\python.exe'

function Get-BackendHealth {
    try {
        $payload = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/health" -TimeoutSec 8
        [pscustomobject]@{ reachable = $true; payload = $payload }
    } catch { [pscustomobject]@{ reachable = $false; payload = $null } }
}

if ($Action -eq 'Stop') {
    $stopped = Stop-XV12OwnedProcess -Name 'backend'
    if ($stopped) { Write-XV12Log 'Stopped the XV12 backend and UI service.' }
    return
}

$state = Get-XV12State -Name 'backend'
$health = Get-BackendHealth
if ($Action -eq 'Status') {
    [pscustomobject]@{
        status = if ($health.reachable -and (Test-XV12Process $state)) { 'healthy' } elseif ($health.reachable) { 'foreign_service' } else { 'stopped' }
        port = $port
        owned_process = [bool](Test-XV12Process $state)
        pid = if ($state) { $state.pid } else { $null }
        application = if ($health.payload) { $health.payload.application.name } else { $null }
        model_ok = if ($health.payload) { $health.payload.model.alias_ok } else { $false }
    } | ConvertTo-Json -Depth 5
    return
}

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'XV12 Python runtime is missing. Run scripts\bootstrap.ps1 once.' }
if ($health.reachable) {
    if ((Test-XV12Process $state) -and $health.payload.application.name -eq 'XODUZ XV12') {
        Write-XV12Log "XV12 backend is healthy on port $port."
        return
    }
    $owner = Get-XV12PortOwner -Port $port
    throw "Port $port is serving a process not verified as XV12-owned. Owner: $($owner.ProcessName) PID $($owner.ProcessId)."
}
if (Test-XV12Process $state) { Stop-XV12OwnedProcess -Name 'backend' | Out-Null }
$portOwner = Get-XV12PortOwner -Port $port
if ($portOwner) { throw "Port $port is occupied by $($portOwner.ProcessName) PID $($portOwner.ProcessId). XV12 will not stop it." }

$logDir = Join-Path $script:LogDirectory 'backend'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$stdout = Join-Path $logDir "backend-$stamp.out.log"
$stderr = Join-Path $logDir "backend-$stamp.err.log"
$arguments = @('-m','uvicorn','app.main:app','--host',[string]$appConfig.host,'--port',[string]$port,'--no-access-log')
Write-XV12Log "Starting XV12 backend and UI service on port $port."
$process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $script:XV12Root -RedirectStandardOutput $stdout -RedirectStandardError $stderr -WindowStyle Hidden -PassThru
Set-XV12State -Name 'backend' -Value ([ordered]@{ root=$script:XV12Root; pid=$process.Id; port=$port; started_at=[DateTimeOffset]::Now.ToString('o'); stdout=$stdout; stderr=$stderr })
$deadline = [DateTimeOffset]::Now.AddSeconds(60)
do {
    if ($process.HasExited) {
        Remove-XV12State -Name 'backend'
        $details = if (Test-Path -LiteralPath $stderr) { (Get-Content -LiteralPath $stderr -Tail 40) -join "`n" } else { '' }
        throw "XV12 backend exited during startup with code $($process.ExitCode).`n$details"
    }
    Start-Sleep -Milliseconds 400
    $health = Get-BackendHealth
    if ($health.reachable -and $health.payload.application.name -eq 'XODUZ XV12') {
        Write-XV12Log "XV12 backend is healthy. PID $($process.Id)."
        return
    }
} while ([DateTimeOffset]::Now -lt $deadline)
Stop-XV12OwnedProcess -Name 'backend' | Out-Null
throw "XV12 backend did not become healthy within 60 seconds. Review $stderr."
