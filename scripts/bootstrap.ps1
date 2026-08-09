. "$PSScriptRoot\xv12-common.ps1"

$pythonRuntime = Join-Path $script:XV12Root 'runtime\python'
if (-not (Test-Path -LiteralPath (Join-Path $pythonRuntime 'Scripts\python.exe') -PathType Leaf)) {
    Write-XV12Log 'Creating the XV12-owned Python environment.'
    & python -m venv $pythonRuntime
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the XV12 Python environment.' }
}
$python = Join-Path $pythonRuntime 'Scripts\python.exe'
Write-XV12Log 'Installing locked XV12 dependencies.'
& $python -m pip install --disable-pip-version-check -r (Join-Path $script:XV12Root 'requirements-dev.txt')
if ($LASTEXITCODE -ne 0) { throw 'XV12 dependency installation failed.' }
Write-XV12Log 'XV12 bootstrap is complete.'
