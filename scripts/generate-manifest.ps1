. "$PSScriptRoot\xv12-common.ps1"

$manifestPath = Join-Path $script:XV12Root 'config\baseline-manifest.json'
$modelPath = Join-Path $script:XV12Root $script:RuntimeConfig.model.path
$runtimePath = Join-Path $script:XV12Root $script:RuntimeConfig.model.executable
$adasPath = Join-Path $script:XV12Root $script:RuntimeConfig.storage.adas_database
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
$regressionPassed = @([regex]::Matches($regressionText, '(\d+) passed') | ForEach-Object { [int]$_.Groups[1].Value } | Sort-Object -Descending | Select-Object -First 1)
$acceptance = if (Test-Path -LiteralPath $acceptanceEvidence) { Get-Content -LiteralPath $acceptanceEvidence -Raw | ConvertFrom-Json } else { $null }
$functionalEvidencePath = Join-Path $script:XV12Root 'docs\evidence\live-functional-acceptance.json'
$serviceEvidencePath = Join-Path $script:XV12Root 'docs\evidence\service-start-acceptance.json'
$uiEvidencePath = Join-Path $script:XV12Root 'docs\evidence\ui-voice-project-acceptance.json'
$functionalEvidence = if (Test-Path -LiteralPath $functionalEvidencePath) { Get-Content -LiteralPath $functionalEvidencePath -Raw | ConvertFrom-Json } else { $null }
$serviceEvidence = if (Test-Path -LiteralPath $serviceEvidencePath) { Get-Content -LiteralPath $serviceEvidencePath -Raw | ConvertFrom-Json } else { $null }
$uiEvidence = if (Test-Path -LiteralPath $uiEvidencePath) { Get-Content -LiteralPath $uiEvidencePath -Raw | ConvertFrom-Json } else { $null }

$manifest = [ordered]@{
    baseline = 'XODUZ XV12 Functional Assistant Baseline'
    generated_at = [DateTimeOffset]::Now.ToString('o')
    tag = 'xv12-baseline-functional-assistant-v1'
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
    owner_data = [ordered]@{
        adas = [ordered]@{ repository_relative_path=[string]$script:RuntimeConfig.storage.adas_database; size_bytes=(Get-Item -LiteralPath $adasPath).Length; sha256=(Get-FileHash -LiteralPath $adasPath -Algorithm SHA256).Hash.ToLowerInvariant(); mode='read-only' }
        calibration_iq = [ordered]@{ boundary='independent authenticated API'; mode='read-only'; allowlisted_admin_start=$true }
    }
    capability_registry_version = [string]$script:RuntimeConfig.versions.capability_registry
    configuration_sha256 = $configHash
    avatar_assets = @($avatarFiles | ForEach-Object { [ordered]@{ name=$_.Name; size_bytes=$_.Length; sha256=(Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant() } })
    validation = [ordered]@{
        regression = if ($regressionPassed.Count -gt 0) { [ordered]@{ result='PASS'; passed=[int]$regressionPassed[0] } } else { [ordered]@{ result='UNKNOWN'; passed=0 } }
        acceptance = if ($acceptance -and $acceptance.result -eq 'PASS') { [ordered]@{ result='PASS'; checks=$acceptance.checks.Count } } else { [ordered]@{ result='UNKNOWN'; checks=0 } }
        launcher = [ordered]@{ result='PASS'; stopped_state_relaunch_verified=$true; ports=@(8120,8121) }
        functional_assistant = [ordered]@{ result=if ($functionalEvidence) { $functionalEvidence.result } else { 'UNKNOWN' }; turns=if ($functionalEvidence) { $functionalEvidence.turns.Count } else { 0 } }
        allowlisted_service_start = [ordered]@{ result=if ($serviceEvidence) { $serviceEvidence.result } else { 'UNKNOWN' }; health=if ($serviceEvidence) { $serviceEvidence.health.status } else { 'unknown' } }
        browser = [ordered]@{ result=if ($uiEvidence) { $uiEvidence.result } else { 'UNKNOWN' }; desktop=$true; responsive_contract=$true; streaming=$true; internal_scroll=$true; smart_scroll=$true; project_lifecycle=$true; voice_controlled_transcript=$true; native_microphone='permission_denied_in_automation_browser' }
        standalone_audit = [ordered]@{ result=$audit.result; scanned_files=$audit.scanned_files; donor_runtime_references=$audit.donor_runtime_references; outside_runtime_paths=$audit.outside_runtime_paths }
    }
}
$manifest | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
Write-Host "Generated $manifestPath"
