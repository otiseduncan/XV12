. "$PSScriptRoot\xv12-common.ps1"

& "$PSScriptRoot\xv12-backend.ps1" -Action Stop
& "$PSScriptRoot\xv12-model.ps1" -Action Stop
Write-XV12Log 'XODUZ XV12 services are stopped.'
