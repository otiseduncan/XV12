. "$PSScriptRoot\xv12-common.ps1"

Write-Host 'Backend/UI:'
& "$PSScriptRoot\xv12-backend.ps1" -Action Status
Write-Host 'Model runtime:'
& "$PSScriptRoot\xv12-model.ps1" -Action Status
