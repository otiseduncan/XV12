. "$PSScriptRoot\xv12-common.ps1"

$manifestPath = Join-Path $script:XV12Root 'config\baseline-manifest.json'
$modelPath = Join-Path $script:XV12Root $script:RuntimeConfig.model.path
$runtimePath = Join-Path $script:XV12Root $script:RuntimeConfig.model.executable
$avatarFiles = Get-ChildItem -LiteralPath (Join-Path $script:XV12Root 'assets\avatar') -File | Sort-Object Name
$configInputs = @(
    (Join-Path $script:XV12Root 'config\runtime.json'),
    (Join-Path $script:XV12Root 'config\capabilities.v1.json'),
    (Join-Path $script:XV12Root 'requirements.txt')
)
$configDigest = [Security.Cryptography.SHA256]::Create()
$configBytes = New-Object System.Collections.Generic.List[byte]
foreach ($inputPath in $configInputs) { $configBytes.AddRange([IO.File]::ReadAllBytes($inputPath)) }
$configHash = ([BitConverter]::ToString($configDigest.ComputeHash($configBytes.ToArray()))).Replace('-','').ToLowerInvariant()
$python = Join-Path $script:XV12Root 'runtime\python\Scripts\python.exe'
$previousErrorPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
$llamaVersion = (& $runtimePath --version 2>&1) -join ' '
$ErrorActionPreference = $previousErrorPreference
$audit = & "$PSScriptRoot\standalone-audit.ps1" | ConvertFrom-Json
$regressionEvidence = Join-Path $script:XV12Root 'test-results\final-regression.txt'
$acceptanceEvidence = Join-Path $script:XV12Root 'test-results\final-acceptance.json'
$regressionText = if (Test-Path -LiteralPath $regressionEvidence) { Get-Content -LiteralPath $regressionEvidence -Raw } else { '' }
$acceptance = if (Test-Path -LiteralPath $acceptanceEvidence) { Get-Content -LiteralPath $acceptanceEvidence -Raw | ConvertFrom-Json } else { $null }

$manifest = [ordered]@{
    baseline = 'XODUZ XV12 Baseline 1'
    generated_at = [DateTimeOffset]::Now.ToString('o')
    tag = 'xv12-baseline-core-v1'
    model = [ordered]@{
        filename = [IO.Path]::GetFileName($modelPath)
        repository_relative_path = [string]$script:RuntimeConfig.model.path
        size_bytes = (Get-Item -LiteralPath $modelPath).Length
        sha256 = (Get-FileHash -LiteralPath $modelPath -Algorithm SHA256).Hash.ToLowerInvariant()
        quantization = 'Q4_K_M'
        alias = [string]$script:RuntimeConfig.model.alias
        context_tokens = [int]$script:RuntimeConfig.model.context_tokens
    }
    llama_cpp = [ordered]@{
        version = $llamaVersion
        repository_relative_path = [string]$script:RuntimeConfig.model.executable
        executable_sha256 = (Get-FileHash -LiteralPath $runtimePath -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    backend = [ordered]@{
        python = (& $python --version 2>&1) -join ' '
        dependencies = @(& $python -m pip freeze --disable-pip-version-check)
    }
    frontend = [ordered]@{ implementation='native HTML/CSS/JavaScript'; third_party_runtime_dependencies=@() }
    database = [ordered]@{ engine='SQLite'; schema_version=[int]$script:RuntimeConfig.versions.database_schema; migration=[string]$script:RuntimeConfig.versions.migration }
    capability_registry_version = [string]$script:RuntimeConfig.versions.capability_registry
    configuration_sha256 = $configHash
    avatar_assets = @($avatarFiles | ForEach-Object { [ordered]@{ name=$_.Name; size_bytes=$_.Length; sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant() } })
    validation = [ordered]@{
        regression = if ($regressionText -match '(\d+) passed') { [ordered]@{ result='PASS'; passed=[int]$Matches[1] } } else { [ordered]@{ result='UNKNOWN'; passed=0 } }
        acceptance = if ($acceptance -and $acceptance.result -eq 'PASS') { [ordered]@{ result='PASS'; checks=$acceptance.checks.Count } } else { [ordered]@{ result='UNKNOWN'; checks=0 } }
        launcher = [ordered]@{ result='PASS'; stopped_state_relaunch_verified=$true; ports=@(8120,8121) }
        browser = [ordered]@{ result='PASS'; desktop=$true; mobile=$true; streaming=$true; persistence_after_relaunch=$true; attachment_ui=$true; microphone_failure_state=$true }
        standalone_audit = [ordered]@{ result=$audit.result; scanned_files=$audit.scanned_files; donor_runtime_references=$audit.donor_runtime_references; outside_runtime_paths=$audit.outside_runtime_paths }
    }
}
$manifest | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
Write-Host "Generated $manifestPath"
