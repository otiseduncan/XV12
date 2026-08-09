param(
    [ValidateSet('Ensure','Stop','Status')][string]$Action = 'Ensure'
)

. "$PSScriptRoot\xv12-common.ps1"

$model = $script:RuntimeConfig.model
$port = if ($env:XV12_MODEL_PORT) { [int]$env:XV12_MODEL_PORT } else { [int]$model.port }
$alias = if ($env:XV12_MODEL_ALIAS) { $env:XV12_MODEL_ALIAS } else { [string]$model.alias }
$contextTokens = if ($env:XV12_MODEL_CONTEXT_TOKENS) { [int]$env:XV12_MODEL_CONTEXT_TOKENS } else { [int]$model.context_tokens }
$executable = (Resolve-Path -LiteralPath (Join-Path $script:XV12Root $model.executable) -ErrorAction SilentlyContinue).Path
$modelPath = (Resolve-Path -LiteralPath (Join-Path $script:XV12Root $model.path) -ErrorAction SilentlyContinue).Path

function Get-ModelHealth {
    try {
        $catalog = Invoke-RestMethod -Uri "http://127.0.0.1:$port/v1/models" -TimeoutSec 8
        $ids = @($catalog.data | ForEach-Object { [string]$_.id })
        [pscustomobject]@{ reachable = $true; alias_ok = $ids -contains $alias; models = $ids }
    } catch {
        [pscustomobject]@{ reachable = $false; alias_ok = $false; models = @() }
    }
}

if ($Action -eq 'Stop') {
    $stopped = Stop-XV12OwnedProcess -Name 'model'
    if ($stopped) { Write-XV12Log 'Stopped the XV12-owned model runtime.' }
    return
}

$state = Get-XV12State -Name 'model'
$health = Get-ModelHealth
if ($Action -eq 'Status') {
    [pscustomobject]@{
        status = if ($health.reachable -and $health.alias_ok -and (Test-XV12Process $state)) { 'healthy' } elseif ($health.reachable) { 'foreign_or_wrong_runtime' } else { 'stopped' }
        port = $port
        expected_alias = $alias
        models = $health.models
        owned_process = [bool](Test-XV12Process $state)
        pid = if ($state) { $state.pid } else { $null }
        context_tokens = if ($state) { $state.context_tokens } else { $null }
    } | ConvertTo-Json -Depth 5
    return
}

if (-not $executable) { throw 'XV12-owned llama-server.exe is missing from runtime\llama.cpp.' }
if (-not $modelPath) { throw 'XV12-owned Qwen GGUF is missing from models\qwen3-coder-30b-a3b.' }

if ($health.reachable) {
    if ((Test-XV12Process $state) -and $health.alias_ok -and [int]$state.context_tokens -eq $contextTokens) {
        Write-XV12Log "XV12 model is healthy on port $port with alias '$alias' and $contextTokens-token context."
        return
    }
    $owner = Get-XV12PortOwner -Port $port
    throw "Port $port is serving a model runtime that is not the verified XV12-owned process. Owner: $($owner.ProcessName) PID $($owner.ProcessId)."
}

if (Test-XV12Process $state) {
    Stop-XV12OwnedProcess -Name 'model' | Out-Null
}
$portOwner = Get-XV12PortOwner -Port $port
if ($portOwner) { throw "Port $port is occupied by $($portOwner.ProcessName) PID $($portOwner.ProcessId). XV12 will not connect to or stop it." }

$logDir = Join-Path $script:LogDirectory 'model'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$stdout = Join-Path $logDir "llama-$stamp.out.log"
$stderr = Join-Path $logDir "llama-$stamp.err.log"
$arguments = @(
    '-m', $modelPath,
    '--alias', $alias,
    '--host', '127.0.0.1',
    '--port', [string]$port,
    '-c', [string]$contextTokens,
    '-ngl', [string]$model.gpu_layers,
    '--parallel', [string]$model.parallel,
    '--no-webui'
)
Write-XV12Log "Starting XV12-owned llama-server on port $port."
$process = Start-Process -FilePath $executable -ArgumentList $arguments -WorkingDirectory (Split-Path -Parent $executable) -RedirectStandardOutput $stdout -RedirectStandardError $stderr -WindowStyle Hidden -PassThru
Set-XV12State -Name 'model' -Value ([ordered]@{
    root = $script:XV12Root
    pid = $process.Id
    executable = $executable
    model_path = $modelPath
    alias = $alias
    port = $port
    context_tokens = $contextTokens
    started_at = [DateTimeOffset]::Now.ToString('o')
    stdout = $stdout
    stderr = $stderr
})

$deadline = [DateTimeOffset]::Now.AddSeconds([int]$model.startup_timeout_seconds)
do {
    if ($process.HasExited) {
        Remove-XV12State -Name 'model'
        $details = if (Test-Path -LiteralPath $stderr) { (Get-Content -LiteralPath $stderr -Tail 30) -join "`n" } else { '' }
        throw "llama-server exited during startup with code $($process.ExitCode).`n$details"
    }
    Start-Sleep -Milliseconds 750
    $health = Get-ModelHealth
    if ($health.reachable) {
        if (-not $health.alias_ok) {
            Stop-XV12OwnedProcess -Name 'model' | Out-Null
            throw "XV12 model runtime reported the wrong alias: $($health.models -join ', ')."
        }
        Write-XV12Log "XV12 model is healthy. PID $($process.Id), alias '$alias', context $contextTokens."
        return
    }
} while ([DateTimeOffset]::Now -lt $deadline)

Stop-XV12OwnedProcess -Name 'model' | Out-Null
throw "XV12 model did not become healthy within $($model.startup_timeout_seconds) seconds. Review $stderr."
