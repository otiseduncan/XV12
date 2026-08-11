param([switch]$NoOpen)

. "$PSScriptRoot\xv12-common.ps1"

$mutex = New-Object System.Threading.Mutex($false, 'Local\XODUZ_XV12_Launcher')
$lockTaken = $false
$stage = 'launcher lock'
try {
    try {
        $waitSeconds = [int]$script:RuntimeConfig.model.startup_timeout_seconds + 120
        $lockTaken = $mutex.WaitOne([TimeSpan]::FromSeconds($waitSeconds))
    } catch [System.Threading.AbandonedMutexException] {
        $lockTaken = $true
        Write-XV12Log 'LAUNCHER SELF-CORRECTION: recovered an abandoned startup lock.'
    }
    if (-not $lockTaken) { throw "Another XV12 launch is still running after $waitSeconds seconds." }

    Write-XV12Log 'STARTUP REQUESTED: XODUZ XV12 double-click lifecycle started.'

    $stage = 'model state check and readiness'
    & "$PSScriptRoot\xv12-model.ps1" -Action Ensure

    $stage = 'optional ComfyUI attempt'
    Write-XV12Log 'OPTIONAL COMFYUI ATTEMPT: checking configured image runtime.'
    try {
        & "$PSScriptRoot\xv12-comfyui.ps1" -Action Ensure
        Write-XV12Log 'OPTIONAL COMFYUI RESULT: lifecycle check completed; continuing core startup.'
    } catch {
        Write-XV12Log "OPTIONAL COMFYUI WARNING: unavailable; continuing required core startup. $($_.Exception.Message)"
    }

    $stage = 'backend state check and readiness'
    & "$PSScriptRoot\xv12-backend.ps1" -Action Ensure

    $stage = 'application health contract'
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$($script:RuntimeConfig.application.port)/api/health" -TimeoutSec 15
    if (-not $health.ok -or $health.application.name -ne 'XODUZ XV12' -or -not $health.model.alias_ok -or [int]$health.model.context_tokens -ne [int]$script:RuntimeConfig.model.context_tokens) {
        throw 'XV12 services answered but the required application/model health contract did not pass.'
    }
    $catalog = Invoke-RestMethod -Uri "http://127.0.0.1:$($script:RuntimeConfig.model.port)/v1/models" -TimeoutSec 15
    $expectedModel = @($catalog.data | Where-Object { [string]$_.id -eq [string]$script:RuntimeConfig.model.alias } | Select-Object -First 1)
    if (-not $expectedModel.Count -or [int]$expectedModel[0].meta.n_ctx -ne [int]$script:RuntimeConfig.model.context_tokens) {
        throw 'The live llama.cpp catalog did not prove the configured alias and context size.'
    }
    Write-XV12Log "APPLICATION HEALTH CONTRACT PASSED: backend ok, model alias '$($script:RuntimeConfig.model.alias)', live context $($expectedModel[0].meta.n_ctx)."
    if ($script:RuntimeConfig.media.image.enabled -and -not $health.services.creator.image_provider_status.healthy) {
        Write-XV12Log 'OPTIONAL COMFYUI RESULT: unavailable in application health; XODUZ core remains ready.'
    }

    if (-not $NoOpen) {
        $stage = 'UI launch'
        $url = "http://127.0.0.1:$($script:RuntimeConfig.application.port)"
        Write-XV12Log "UI LAUNCH: opening $url only after required core health passed."
        $edgeCandidates = @(@(
            (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'),
            (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe')
        ) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) })
        if ($edgeCandidates.Count -gt 0) {
            Start-Process -FilePath $edgeCandidates[0] -ArgumentList "--app=$url", '--start-maximized' | Out-Null
        } else {
            Start-Process $url | Out-Null
        }
    } else {
        Write-XV12Log 'UI LAUNCH: suppressed by -NoOpen after required core health passed.'
    }
    Write-XV12Log 'READY: XODUZ XV12 required core stack is healthy and launch is complete.'
} catch {
    Write-XV12Log "STARTUP FAILED at stage '$stage': $($_.Exception.Message)"
    throw
} finally {
    if ($lockTaken) { try { $mutex.ReleaseMutex() } catch {} }
    $mutex.Dispose()
}
