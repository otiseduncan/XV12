param(
    [ValidateSet('Ensure','Stop','Status')][string]$Action = 'Ensure'
)

. "$PSScriptRoot\xv12-common.ps1"

$model = $script:RuntimeConfig.model
$port = if ($env:XV12_MODEL_PORT) { [int]$env:XV12_MODEL_PORT } else { [int]$model.port }
$alias = if ($env:XV12_MODEL_ALIAS) { $env:XV12_MODEL_ALIAS } else { [string]$model.alias }
$contextTokens = if ($env:XV12_MODEL_CONTEXT_TOKENS) { [int]$env:XV12_MODEL_CONTEXT_TOKENS } else { [int]$model.context_tokens }
$executableSetting = if ($env:XV12_MODEL_EXECUTABLE) { $env:XV12_MODEL_EXECUTABLE } else { [string]$model.executable }
$modelPathSetting = if ($env:XV12_MODEL_PATH) { $env:XV12_MODEL_PATH } else { [string]$model.path }
$executable = Resolve-XV12Path $executableSetting
$modelPath = Resolve-XV12Path $modelPathSetting
$identityTokens = if ($modelPath) { @($modelPath, "--alias $alias", "--port $port", "-c $contextTokens") } else { @() }

function Get-ModelHealth {
    try {
        $catalog = Invoke-RestMethod -Uri "http://127.0.0.1:$port/v1/models" -TimeoutSec 8
        $entries = @($catalog.data)
        $ids = @($entries | ForEach-Object { [string]$_.id })
        $expected = @($entries | Where-Object { [string]$_.id -eq $alias } | Select-Object -First 1)
        $reportedContext = if ($expected.Count -and $expected[0].meta -and $expected[0].meta.n_ctx) { [int]$expected[0].meta.n_ctx } else { $null }
        [pscustomobject]@{
            reachable = $true
            alias_ok = $ids -contains $alias
            context_ok = $null -ne $reportedContext -and [int]$reportedContext -eq $contextTokens
            context_tokens = $reportedContext
            models = $ids
        }
    } catch {
        [pscustomobject]@{ reachable=$false; alias_ok=$false; context_ok=$false; context_tokens=$null; models=@() }
    }
}

function Test-ModelOwner($owner) {
    return $executable -and $modelPath -and (Test-XV12ProcessRecord -Process $owner -ExpectedExecutable $executable -CommandLineContains $identityTokens)
}

function Save-ModelState($processRecord, [string]$stdout, [string]$stderr) {
    Set-XV12State -Name 'model' -Value ([ordered]@{
        root = $script:XV12Root
        pid = [int]$processRecord.ProcessId
        process_started_at = [string]$processRecord.ProcessStartedAt
        executable = $executable
        model_path = $modelPath
        alias = $alias
        port = $port
        context_tokens = $contextTokens
        stdout = $stdout
        stderr = $stderr
    })
}

function Stop-VerifiedModelProcesses {
    $ids = @()
    $owner = Get-XV12PortOwner -Port $port
    if (Test-ModelOwner $owner) { $ids += [int]$owner.ProcessId }
    if ($executable -and $modelPath) {
        $ids += @(Find-XV12Processes -ExpectedExecutable $executable -CommandLineContains $identityTokens | ForEach-Object { [int]$_.ProcessId })
    }
    foreach ($processId in @($ids | Sort-Object -Unique)) {
        $record = Get-XV12ProcessRecord -ProcessId $processId
        if (Test-ModelOwner $record) {
            Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
            try { Wait-Process -Id $processId -Timeout 20 -ErrorAction SilentlyContinue } catch {}
        }
    }
    Remove-XV12State -Name 'model'
    return @($ids | Sort-Object -Unique).Count
}

function Wait-ModelReady {
    param([Parameter(Mandatory)][int]$ProcessId, [Parameter(Mandatory)][datetimeoffset]$Deadline, [string]$Stdout, [string]$Stderr)
    do {
        $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
        if (-not $process) {
            $details = if ($Stderr -and (Test-Path -LiteralPath $Stderr)) { (Get-Content -LiteralPath $Stderr -Tail 30) -join "`n" } else { '' }
            throw "llama-server PID $ProcessId exited during startup. stdout=$Stdout stderr=$Stderr`n$details"
        }
        $health = Get-ModelHealth
        if ($health.reachable) {
            if (-not $health.alias_ok) { throw "XV12 model runtime reported the wrong alias: $($health.models -join ', ')." }
            if (-not $health.context_ok) { throw "XV12 model runtime reported context $($health.context_tokens); expected $contextTokens." }
            $owner = Get-XV12PortOwner -Port $port
            if (-not (Test-ModelOwner $owner)) {
                throw "Port $port became owned by an unexpected process. Conflicting PID $($owner.ProcessId), executable '$($owner.ExecutablePath)'."
            }
            Save-ModelState $owner $Stdout $Stderr
            Write-XV12Log "MODEL READINESS CONFIRMED: PID $($owner.ProcessId), port $port, alias '$alias', live context $($health.context_tokens)."
            return
        }
        Start-Sleep -Milliseconds 750
    } while ([DateTimeOffset]::Now -lt $Deadline)
    throw "XV12 model did not become healthy within $([int]$model.startup_timeout_seconds) seconds. stdout=$Stdout stderr=$Stderr"
}

if ($Action -eq 'Stop') {
    $count = Stop-VerifiedModelProcesses
    if ($count) { Write-XV12Log "Stopped $count verified XV12 model process(es)." }
    else { Write-XV12Log 'XV12-owned model runtime is already stopped.' }
    return
}

$state = Get-XV12State -Name 'model'
$health = Get-ModelHealth
$owner = Get-XV12PortOwner -Port $port
$ownedOwner = Test-ModelOwner $owner
$stateOwned = if ($executable) { Test-XV12Process -State $state -ExpectedExecutable $executable -CommandLineContains $identityTokens } else { $false }

if ($Action -eq 'Status') {
    [pscustomobject]@{
        status = if ($health.reachable -and $health.alias_ok -and $health.context_ok -and $ownedOwner) { 'healthy' } elseif ($owner) { 'foreign_or_wrong_runtime' } else { 'stopped' }
        port = $port
        expected_alias = $alias
        models = $health.models
        owned_process = [bool]$ownedOwner
        pid = if ($owner) { $owner.ProcessId } else { $null }
        context_tokens = $health.context_tokens
        expected_context_tokens = $contextTokens
    } | ConvertTo-Json -Depth 5
    return
}

Write-XV12Log "MODEL STATE CHECK: port $port, state PID $(if ($state) { Get-XV12Property $state 'pid' } else { 'none' }), listener PID $(if ($owner) { $owner.ProcessId } else { 'none' })."
if (-not $executable) { throw "XV12-owned llama-server.exe is missing: $executableSetting" }
if (-not $modelPath) { throw "XV12-owned Qwen GGUF is missing: $modelPathSetting" }

if ($health.reachable) {
    if (-not $ownedOwner) { throw "Foreign process conflict on model port ${port}: PID $($owner.ProcessId), executable '$($owner.ExecutablePath)'. XV12 will not reuse or stop it." }
    if (-not $health.alias_ok) { throw "XV12-owned process on port $port reported the wrong model alias: $($health.models -join ', ')." }
    if (-not $health.context_ok) { throw "XV12-owned model on port $port reported context $($health.context_tokens); expected $contextTokens." }
    if (-not $stateOwned) {
        Write-XV12Log 'MODEL STATE RECOVERY: replacing a stale or missing model state file from the verified listener.'
        Save-ModelState $owner ([string](Get-XV12Property $state 'stdout')) ([string](Get-XV12Property $state 'stderr'))
    }
    Write-XV12Log "MODEL REUSE: verified XV12 llama-server PID $($owner.ProcessId) on port $port."
    Write-XV12Log "MODEL READINESS CONFIRMED: alias '$alias', live context $($health.context_tokens)."
    return
}

if ($owner) { throw "Foreign process conflict on model port ${port}: PID $($owner.ProcessId), executable '$($owner.ExecutablePath)'. XV12 will not reuse or stop it." }

$candidate = $null
if ($stateOwned) { $candidate = Get-XV12ProcessRecord -ProcessId ([int](Get-XV12Property $state 'pid')) }
if (-not $candidate) {
    $matches = @(Find-XV12Processes -ExpectedExecutable $executable -CommandLineContains $identityTokens)
    if ($matches.Count -eq 1) {
        $candidate = $matches[0]
        Write-XV12Log "MODEL STATE RECOVERY: found verified in-progress llama-server PID $($candidate.ProcessId)."
    } elseif ($matches.Count -gt 1) {
        Write-XV12Log "MODEL SELF-CORRECTION: stopping $($matches.Count) duplicate verified XV12 model processes before restart."
        Stop-VerifiedModelProcesses | Out-Null
    }
}

$deadline = [DateTimeOffset]::Now.AddSeconds([int]$model.startup_timeout_seconds)
if ($candidate) {
    $stdout = [string](Get-XV12Property $state 'stdout')
    $stderr = [string](Get-XV12Property $state 'stderr')
    Save-ModelState $candidate $stdout $stderr
    Write-XV12Log "MODEL REUSE: waiting for verified in-progress llama-server PID $($candidate.ProcessId)."
    try { Wait-ModelReady -ProcessId $candidate.ProcessId -Deadline $deadline -Stdout $stdout -Stderr $stderr; return }
    catch {
        Stop-VerifiedModelProcesses | Out-Null
        Write-XV12Log "MODEL SELF-CORRECTION: in-progress runtime failed readiness and was stopped: $($_.Exception.Message)"
    }
} elseif ($state) {
    Write-XV12Log 'MODEL STATE RECOVERY: removed stale model state; no matching process exists.'
    Remove-XV12State -Name 'model'
}

$logDir = Join-Path $script:LogDirectory 'model'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$stdout = Join-Path $logDir "llama-$stamp.out.log"
$stderr = Join-Path $logDir "llama-$stamp.err.log"
$arguments = @('-m',$modelPath,'--alias',$alias,'--host','127.0.0.1','--port',[string]$port,'-c',[string]$contextTokens,'-ngl',[string]$model.gpu_layers,'--parallel',[string]$model.parallel,'--no-webui')
Write-XV12Log "MODEL LAUNCH: '$executable' on port $port; stdout=$stdout stderr=$stderr"
$process = Start-Process -FilePath $executable -ArgumentList $arguments -WorkingDirectory (Split-Path -Parent $executable) -RedirectStandardOutput $stdout -RedirectStandardError $stderr -WindowStyle Hidden -PassThru
try {
    $record = Get-XV12ProcessRecord -ProcessId $process.Id
    if (-not $record) { throw "llama-server was launched but PID $($process.Id) could not be inspected." }
    Save-ModelState $record $stdout $stderr
    Wait-ModelReady -ProcessId $process.Id -Deadline $deadline -Stdout $stdout -Stderr $stderr
}
catch {
    $exit = if ($process.HasExited) { [string]$process.ExitCode } else { 'still-running' }
    Stop-VerifiedModelProcesses | Out-Null
    throw "MODEL STARTUP FAILED (PID $($process.Id), exit=$exit): $($_.Exception.Message) stdout=$stdout stderr=$stderr"
}
