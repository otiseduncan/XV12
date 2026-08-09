param(
    [ValidateSet('Ensure','Stop','Status')][string]$Action = 'Status',
    [int]$TimeoutSec = 300
)

. "$PSScriptRoot\xv12-common.ps1"

$imageConfig = $script:RuntimeConfig.media.image
$enabledText = if ($null -ne $env:XV12_COMFYUI_ENABLED) { $env:XV12_COMFYUI_ENABLED } else { [string]$imageConfig.enabled }
$enabled = $enabledText.Trim().ToLowerInvariant() -notin @('0','false','no','off')
$comfyRoot = if ($env:XV12_COMFYUI_ROOT) { $env:XV12_COMFYUI_ROOT } else { [string]$imageConfig.root }
$port = if ($env:XV12_COMFYUI_PORT) { [int]$env:XV12_COMFYUI_PORT } else { [int]$imageConfig.port }
$baseUrl = if ($env:XV12_COMFYUI_BASE_URL) { $env:XV12_COMFYUI_BASE_URL.TrimEnd('/') } else { "http://127.0.0.1:$port" }
$checkpoint = if ($env:XV12_COMFYUI_CHECKPOINT) { $env:XV12_COMFYUI_CHECKPOINT } else { [string]$imageConfig.checkpoint }
$python = Join-Path $comfyRoot 'python_embeded\python.exe'
$main = Join-Path $comfyRoot 'ComfyUI\main.py'
$checkpointPath = Join-Path $comfyRoot "ComfyUI\models\checkpoints\$checkpoint"
$stateName = 'comfyui'

if ($baseUrl -notmatch '^http://(127\.0\.0\.1|localhost):\d+$') { throw 'ComfyUI must use a loopback HTTP endpoint.' }

function Test-ComfyUIHealth {
    try {
        $stats = Invoke-RestMethod -Uri "$baseUrl/system_stats" -TimeoutSec 5
        $objects = Invoke-RestMethod -Uri "$baseUrl/object_info/CheckpointLoaderSimple" -TimeoutSec 10
        $choices = @($objects.CheckpointLoaderSimple.input.required.ckpt_name[0])
        return [pscustomobject]@{
            healthy = ($null -ne $stats.system -and $choices -contains $checkpoint)
            api_reachable = $true
            checkpoint_available = ($choices -contains $checkpoint)
            comfyui_version = [string]$stats.system.comfyui_version
            device = if ($stats.devices.Count) { [string]$stats.devices[0].name } else { '' }
        }
    } catch {
        return [pscustomobject]@{ healthy=$false; api_reachable=$false; checkpoint_available=$false; comfyui_version=''; device='' }
    }
}

function Test-OwnedComfyUIState($state) {
    if (-not $state -or $state.managed_by -ne 'XV12' -or $state.root -ne $script:XV12Root -or -not $state.pid) { return $false }
    try {
        $process = Get-Process -Id ([int]$state.pid) -ErrorAction Stop
        $recorded = [datetime]::Parse([string]$state.process_started_at).ToUniversalTime()
        return [math]::Abs(($process.StartTime.ToUniversalTime() - $recorded).TotalSeconds) -lt 4
    } catch { return $false }
}

function Get-ComfyUIStatus {
    $health = Test-ComfyUIHealth
    $state = Get-XV12State -Name $stateName
    $listener = if ($health.healthy) { Get-XV12PortOwner -Port $port } else { $null }
    [pscustomobject]@{
        enabled = $enabled
        configured = [bool]($comfyRoot -and $checkpoint)
        healthy = [bool]$health.healthy
        status = if (-not $enabled) { 'disabled' } elseif ($health.healthy) { 'healthy' } else { 'unavailable' }
        api_reachable = [bool]$health.api_reachable
        checkpoint = $checkpoint
        checkpoint_file_present = Test-Path -LiteralPath $checkpointPath -PathType Leaf
        checkpoint_available = [bool]$health.checkpoint_available
        runtime_present = (Test-Path -LiteralPath $python -PathType Leaf) -and (Test-Path -LiteralPath $main -PathType Leaf)
        url = $baseUrl
        port = $port
        managed_by_xv12 = Test-OwnedComfyUIState $state
        pid = if ($listener) { $listener.ProcessId } else { $null }
        comfyui_version = [string]$health.comfyui_version
        device = [string]$health.device
    }
}

switch ($Action) {
    'Status' {
        Get-ComfyUIStatus
        exit 0
    }
    'Ensure' {
        if (-not $enabled) { Write-XV12Log 'ComfyUI integration is disabled by configuration.'; exit 0 }
        $health = Test-ComfyUIHealth
        if ($health.healthy) {
            $state = Get-XV12State -Name $stateName
            $ownership = if (Test-OwnedComfyUIState $state) { 'XV12-owned' } else { 'external/unowned' }
            Write-XV12Log "ComfyUI is healthy at $baseUrl ($ownership)."
            exit 0
        }
        $owner = Get-XV12PortOwner -Port $port
        if ($owner) { throw "Port $port is occupied by PID $($owner.ProcessId), but the configured ComfyUI health contract failed." }
        if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or -not (Test-Path -LiteralPath $main -PathType Leaf)) {
            throw "Configured ComfyUI runtime is unavailable at $comfyRoot."
        }
        if (-not (Test-Path -LiteralPath $checkpointPath -PathType Leaf)) {
            throw "Configured ComfyUI checkpoint is unavailable: $checkpoint"
        }
        Get-ChildItem -LiteralPath $script:LogDirectory -Filter 'comfyui-*.log' -File -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -Skip 20 | Remove-Item -Force -ErrorAction SilentlyContinue
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $stdout = Join-Path $script:LogDirectory "comfyui-$stamp.out.log"
        $stderr = Join-Path $script:LogDirectory "comfyui-$stamp.err.log"
        Write-XV12Log "Starting XV12-owned ComfyUI on 127.0.0.1:$port."
        $process = Start-Process -FilePath $python -ArgumentList @('-s', $main, '--listen', '127.0.0.1', '--port', "$port") `
            -WorkingDirectory (Join-Path $comfyRoot 'ComfyUI') -RedirectStandardOutput $stdout -RedirectStandardError $stderr `
            -WindowStyle Hidden -PassThru
        Set-XV12State -Name $stateName -Value ([pscustomobject]@{
            pid=$process.Id; process_started_at=$process.StartTime.ToUniversalTime().ToString('o'); root=$script:XV12Root;
            runtime_root=$comfyRoot; port=$port; url=$baseUrl; checkpoint=$checkpoint; managed_by='XV12'; stdout=$stdout; stderr=$stderr
        })
        $deadline = (Get-Date).AddSeconds($TimeoutSec)
        while ((Get-Date) -lt $deadline) {
            $health = Test-ComfyUIHealth
            if ($health.healthy) { Write-XV12Log "ComfyUI is healthy at $baseUrl (XV12-owned)."; exit 0 }
            if ($process.HasExited) { break }
            Start-Sleep -Seconds 2
        }
        if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
        Remove-XV12State -Name $stateName
        throw "ComfyUI failed to become healthy. Inspect $stdout and $stderr."
    }
    'Stop' {
        $state = Get-XV12State -Name $stateName
        if (Test-OwnedComfyUIState $state) {
            Write-XV12Log "Stopping XV12-owned ComfyUI runtime (PID $($state.pid))."
            Stop-Process -Id ([int]$state.pid) -Force -ErrorAction SilentlyContinue
            try { Wait-Process -Id ([int]$state.pid) -Timeout 20 -ErrorAction SilentlyContinue } catch {}
        } elseif ((Test-ComfyUIHealth).healthy) {
            Write-XV12Log 'ComfyUI is healthy but was not started by XV12; leaving the external runtime running.'
        } else {
            Write-XV12Log 'XV12-owned ComfyUI is already stopped.'
        }
        Remove-XV12State -Name $stateName
        exit 0
    }
}
