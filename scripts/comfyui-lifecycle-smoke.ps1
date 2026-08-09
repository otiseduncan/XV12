param([int]$Port = 18188, [int]$TimeoutSec = 120)

$ErrorActionPreference = 'Stop'
$comfyScript = Join-Path $PSScriptRoot 'xv12-comfyui.ps1'
$previousPort = $env:XV12_COMFYUI_PORT
$previousUrl = $env:XV12_COMFYUI_BASE_URL
$previousEnabled = $env:XV12_COMFYUI_ENABLED

try {
    if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
        throw "Lifecycle smoke port $Port is already occupied."
    }
    $env:XV12_COMFYUI_ENABLED = '1'
    $env:XV12_COMFYUI_PORT = [string]$Port
    $env:XV12_COMFYUI_BASE_URL = "http://127.0.0.1:$Port"
    & $comfyScript -Action Ensure -TimeoutSec $TimeoutSec
    if ($LASTEXITCODE -ne 0) { throw 'XV12 ComfyUI ensure failed.' }
    $running = & $comfyScript -Action Status
    if (-not $running.healthy -or -not $running.managed_by_xv12 -or [int]$running.port -ne $Port) {
        throw 'XV12 did not prove an owned healthy ComfyUI runtime.'
    }
    $ownedPid = [int]$running.pid
    & $comfyScript -Action Stop
    if ($LASTEXITCODE -ne 0) { throw 'XV12 ComfyUI stop failed.' }
    Start-Sleep -Milliseconds 500
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($listener -or (Get-Process -Id $ownedPid -ErrorAction SilentlyContinue)) {
        throw 'XV12-owned ComfyUI process remained after stop.'
    }
    [pscustomobject]@{
        result = 'PASS'
        port = $Port
        ensure_healthy = $true
        owned_pid = $ownedPid
        stop_removed_owned_process = $true
        primary_external_runtime_untouched = [bool](Get-NetTCPConnection -LocalPort 8188 -State Listen -ErrorAction SilentlyContinue)
    } | ConvertTo-Json -Depth 4
} finally {
    try { & $comfyScript -Action Stop | Out-Null } catch {}
    $env:XV12_COMFYUI_PORT = $previousPort
    $env:XV12_COMFYUI_BASE_URL = $previousUrl
    $env:XV12_COMFYUI_ENABLED = $previousEnabled
}
