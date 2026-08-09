param([switch]$NoOpen)

. "$PSScriptRoot\xv12-common.ps1"

try {
    Write-XV12Log 'XODUZ XV12 startup requested.'
    & "$PSScriptRoot\xv12-model.ps1" -Action Ensure
    & "$PSScriptRoot\xv12-backend.ps1" -Action Ensure
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$($script:RuntimeConfig.application.port)/api/health" -TimeoutSec 15
    if (-not $health.ok -or -not $health.model.alias_ok -or [int]$health.model.context_tokens -ne [int]$script:RuntimeConfig.model.context_tokens) {
        throw 'XV12 services started but the verified application/model health contract did not pass.'
    }
    Write-XV12Log 'XODUZ XV12 is ready.'
    if (-not $NoOpen) {
        $url = "http://127.0.0.1:$($script:RuntimeConfig.application.port)"
        $edgeCandidates = @(@(
            (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'),
            (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe')
        ) | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) })
        if ($edgeCandidates.Count -gt 0) {
            Start-Process -FilePath $edgeCandidates[0] -ArgumentList "--app=$url", '--start-maximized' | Out-Null
        } else {
            Start-Process $url | Out-Null
        }
    }
} catch {
    Write-XV12Log "STARTUP FAILED: $($_.Exception.Message)"
    throw
}
