param(
    [ValidateSet('Ensure','Stop','Status')][string]$Action = 'Ensure'
)

. "$PSScriptRoot\xv12-common.ps1"

$appConfig = $script:RuntimeConfig.application
$port = if ($env:XV12_APP_PORT) { [int]$env:XV12_APP_PORT } else { [int]$appConfig.port }
$pythonSetting = if ($env:XV12_BACKEND_PYTHON) { $env:XV12_BACKEND_PYTHON } else { 'runtime\python\Scripts\python.exe' }
$python = Resolve-XV12Path $pythonSetting
$configuredStartupTimeout = Get-XV12Property $appConfig 'startup_timeout_seconds'
$startupTimeout = if ($configuredStartupTimeout) { [int]$configuredStartupTimeout } else { 60 }
$identityTokens = if ($python) { @($python, '-m uvicorn', 'app.main:app', "--port $port") } else { @() }

function Get-BackendHealth {
    try {
        $payload = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/health" -TimeoutSec 8
        $contractOk = $payload.ok -and $payload.application.name -eq 'XODUZ XV12' -and $payload.model.alias_ok -and ([int]$payload.model.context_tokens -eq [int]$script:RuntimeConfig.model.context_tokens)
        [pscustomobject]@{ reachable=$true; contract_ok=[bool]$contractOk; payload=$payload }
    } catch { [pscustomobject]@{ reachable=$false; contract_ok=$false; payload=$null } }
}

function Test-BackendOwner($owner) {
    return $python -and (Test-XV12ProcessRecord -Process $owner -CommandLineContains $identityTokens)
}

function Get-BackendLauncherRecord($listener) {
    if (-not $listener -or -not $listener.ParentProcessId) { return $null }
    $parent = Get-XV12ProcessRecord -ProcessId ([int]$listener.ParentProcessId)
    if (Test-XV12ProcessRecord -Process $parent -ExpectedExecutable $python -CommandLineContains $identityTokens) { return $parent }
    return $null
}

function Save-BackendState($launcher, $listener, [string]$stdout, [string]$stderr) {
    $primary = if ($launcher) { $launcher } else { $listener }
    Set-XV12State -Name 'backend' -Value ([ordered]@{
        root = $script:XV12Root
        pid = [int]$primary.ProcessId
        process_started_at = [string]$primary.ProcessStartedAt
        executable = if ($launcher) { $python } else { [string]$primary.ExecutablePath }
        listener_pid = if ($listener) { [int]$listener.ProcessId } else { $null }
        listener_started_at = if ($listener) { [string]$listener.ProcessStartedAt } else { $null }
        port = $port
        stdout = $stdout
        stderr = $stderr
    })
}

function Stop-VerifiedBackendProcesses {
    $records = @()
    $owner = Get-XV12PortOwner -Port $port
    if (Test-BackendOwner $owner) {
        $records += $owner
        $parent = Get-BackendLauncherRecord $owner
        if ($parent) { $records += $parent }
    }
    if ($python) {
        $records += @(Find-XV12Processes -ExpectedExecutable $python -CommandLineContains $identityTokens)
    }
    $stopped = 0
    foreach ($record in @($records | Sort-Object ProcessId -Unique -Descending)) {
        $current = Get-XV12ProcessRecord -ProcessId ([int]$record.ProcessId)
        $valid = (Test-BackendOwner $current) -or (Test-XV12ProcessRecord -Process $current -ExpectedExecutable $python -CommandLineContains $identityTokens)
        if ($valid) {
            Stop-Process -Id ([int]$record.ProcessId) -Force -ErrorAction SilentlyContinue
            try { Wait-Process -Id ([int]$record.ProcessId) -Timeout 20 -ErrorAction SilentlyContinue } catch {}
            $stopped++
        }
    }
    Remove-XV12State -Name 'backend'
    return $stopped
}

function Wait-BackendReady {
    param([Parameter(Mandatory)][int]$ProcessId, [Parameter(Mandatory)][datetimeoffset]$Deadline, [string]$Stdout, [string]$Stderr)
    do {
        if (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
            $details = if ($Stderr -and (Test-Path -LiteralPath $Stderr)) { (Get-Content -LiteralPath $Stderr -Tail 40) -join "`n" } else { '' }
            throw "backend launcher PID $ProcessId exited during startup. stdout=$Stdout stderr=$Stderr`n$details"
        }
        $health = Get-BackendHealth
        if ($health.reachable -and $health.contract_ok) {
            $listener = Get-XV12PortOwner -Port $port
            if (-not (Test-BackendOwner $listener)) { throw "Backend port $port became owned by unexpected PID $($listener.ProcessId)." }
            $launcher = Get-BackendLauncherRecord $listener
            Save-BackendState $launcher $listener $Stdout $Stderr
            Write-XV12Log "BACKEND READINESS CONFIRMED: listener PID $($listener.ProcessId), port $port, application/model health ok."
            return
        }
        Start-Sleep -Milliseconds 400
    } while ([DateTimeOffset]::Now -lt $Deadline)
    throw "XV12 backend did not satisfy /api/health within $startupTimeout seconds. stdout=$Stdout stderr=$Stderr"
}

if ($Action -eq 'Stop') {
    $count = Stop-VerifiedBackendProcesses
    if ($count) { Write-XV12Log "Stopped $count verified XV12 backend process(es)." }
    else { Write-XV12Log 'XV12 backend and UI service is already stopped.' }
    return
}

$state = Get-XV12State -Name 'backend'
$health = Get-BackendHealth
$owner = Get-XV12PortOwner -Port $port
$ownedOwner = Test-BackendOwner $owner

if ($Action -eq 'Status') {
    [pscustomobject]@{
        status = if ($health.contract_ok -and $ownedOwner) { 'healthy' } elseif ($owner) { 'foreign_or_unhealthy_service' } else { 'stopped' }
        port = $port
        owned_process = [bool]$ownedOwner
        pid = if ($owner) { $owner.ProcessId } else { $null }
        application = if ($health.payload) { $health.payload.application.name } else { $null }
        model_ok = if ($health.payload) { [bool]$health.payload.model.alias_ok } else { $false }
        health_ok = if ($health.payload) { [bool]$health.payload.ok } else { $false }
    } | ConvertTo-Json -Depth 5
    return
}

Write-XV12Log "BACKEND STATE CHECK: port $port, state PID $(if ($state) { Get-XV12Property $state 'pid' } else { 'none' }), listener PID $(if ($owner) { $owner.ProcessId } else { 'none' })."
if (-not $python) { throw "XV12 Python runtime is missing: $pythonSetting. Run scripts\bootstrap.ps1 once." }

if ($health.reachable) {
    if (-not $ownedOwner) { throw "Foreign process conflict on backend port ${port}: PID $($owner.ProcessId), executable '$($owner.ExecutablePath)'. XV12 will not reuse or stop it." }
    if (-not $health.contract_ok) { throw "XV12 backend PID $($owner.ProcessId) answered /api/health but failed the required application/model health contract." }
    $launcher = Get-BackendLauncherRecord $owner
    $savedListener = Get-XV12Property $state 'listener_pid'
    if (-not $state -or [int]$savedListener -ne [int]$owner.ProcessId) {
        Write-XV12Log 'BACKEND STATE RECOVERY: replacing a stale or missing backend state file from the verified listener.'
        Save-BackendState $launcher $owner ([string](Get-XV12Property $state 'stdout')) ([string](Get-XV12Property $state 'stderr'))
    }
    Write-XV12Log "BACKEND REUSE: verified XV12 backend listener PID $($owner.ProcessId) on port $port."
    Write-XV12Log 'BACKEND READINESS CONFIRMED: /api/health application/model contract passed.'
    return
}

if ($owner) { throw "Foreign process conflict on backend port ${port}: PID $($owner.ProcessId), executable '$($owner.ExecutablePath)'. XV12 will not reuse or stop it." }

$candidate = $null
if ($state -and (Test-XV12Process -State $state -ExpectedExecutable $python -CommandLineContains $identityTokens)) {
    $candidate = Get-XV12ProcessRecord -ProcessId ([int](Get-XV12Property $state 'pid'))
}
if (-not $candidate) {
    $matches = @(Find-XV12Processes -ExpectedExecutable $python -CommandLineContains $identityTokens)
    if ($matches.Count -eq 1) {
        $candidate = $matches[0]
        Write-XV12Log "BACKEND STATE RECOVERY: found verified in-progress backend PID $($candidate.ProcessId)."
    } elseif ($matches.Count -gt 1) {
        Write-XV12Log "BACKEND SELF-CORRECTION: stopping $($matches.Count) duplicate verified XV12 backend launchers before restart."
        Stop-VerifiedBackendProcesses | Out-Null
    }
}

$deadline = [DateTimeOffset]::Now.AddSeconds($startupTimeout)
if ($candidate) {
    $stdout = [string](Get-XV12Property $state 'stdout')
    $stderr = [string](Get-XV12Property $state 'stderr')
    Save-BackendState $candidate $null $stdout $stderr
    Write-XV12Log "BACKEND REUSE: waiting for verified in-progress backend PID $($candidate.ProcessId)."
    try { Wait-BackendReady -ProcessId $candidate.ProcessId -Deadline $deadline -Stdout $stdout -Stderr $stderr; return }
    catch {
        Stop-VerifiedBackendProcesses | Out-Null
        Write-XV12Log "BACKEND SELF-CORRECTION: in-progress backend failed readiness and was stopped: $($_.Exception.Message)"
    }
} elseif ($state) {
    Write-XV12Log 'BACKEND STATE RECOVERY: removed stale backend state; no matching process exists.'
    Remove-XV12State -Name 'backend'
}

$logDir = Join-Path $script:LogDirectory 'backend'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$stdout = Join-Path $logDir "backend-$stamp.out.log"
$stderr = Join-Path $logDir "backend-$stamp.err.log"
$arguments = @('-m','uvicorn','app.main:app','--host',[string]$appConfig.host,'--port',[string]$port,'--no-access-log')
Write-XV12Log "BACKEND LAUNCH: '$python' on port $port; stdout=$stdout stderr=$stderr"
$process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $script:XV12Root -RedirectStandardOutput $stdout -RedirectStandardError $stderr -WindowStyle Hidden -PassThru
try {
    $record = Get-XV12ProcessRecord -ProcessId $process.Id
    if (-not $record) { throw "backend was launched but PID $($process.Id) could not be inspected." }
    Save-BackendState $record $null $stdout $stderr
    Wait-BackendReady -ProcessId $process.Id -Deadline $deadline -Stdout $stdout -Stderr $stderr
}
catch {
    $exit = if ($process.HasExited) { [string]$process.ExitCode } else { 'still-running' }
    Stop-VerifiedBackendProcesses | Out-Null
    throw "BACKEND STARTUP FAILED (launcher PID $($process.Id), exit=$exit): $($_.Exception.Message) stdout=$stdout stderr=$stderr"
}
