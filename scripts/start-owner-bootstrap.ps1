param(
    [ValidateRange(2,30)][int]$ExpiresMinutes = 10
)

. "$PSScriptRoot\xv12-common.ps1"

$python = Join-Path $script:XV12Root 'runtime\python\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'XV12 Python runtime is missing. Run scripts\bootstrap.ps1 once.'
}

$temporary = [System.IO.Path]::GetTempFileName()
try {
    & $python (Join-Path $PSScriptRoot 'issue-owner-bootstrap.py') --output $temporary --expires-minutes $ExpiresMinutes
    if ($LASTEXITCODE -ne 0) { throw 'Owner bootstrap could not be issued.' }
    $bootstrapUrl = (Get-Content -LiteralPath $temporary -Raw).Trim()
    if (-not $bootstrapUrl.StartsWith('https://', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Owner bootstrap did not produce a private HTTPS URL.'
    }
    Start-Process $bootstrapUrl | Out-Null
    Write-XV12Log "Opened the one-time Owner bootstrap in the browser. It expires in $ExpiresMinutes minutes."
} finally {
    Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
}
