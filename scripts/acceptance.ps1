. "$PSScriptRoot\xv12-common.ps1"

$base = "http://127.0.0.1:$($script:RuntimeConfig.application.port)"
$checks = New-Object System.Collections.Generic.List[object]

function Add-Check {
    param([string]$Name, [bool]$Passed, [string]$Detail)
    $checks.Add([pscustomobject]@{ name=$Name; passed=$Passed; detail=$Detail })
    if (-not $Passed) { throw "Acceptance check failed: $Name - $Detail" }
}

$health = Invoke-RestMethod -Uri "$base/api/health" -TimeoutSec 20
Add-Check 'runtime-health' ($health.ok -eq $true) 'application and model report healthy'
Add-Check 'model-alias' ($health.model.alias_ok -eq $true -and $health.model.expected_alias -eq $script:RuntimeConfig.model.alias) ([string]$health.model.expected_alias)
Add-Check 'model-context' ([int]$health.model.context_tokens -eq 32768) ([string]$health.model.context_tokens)
Add-Check 'sole-admin' ([int]$health.auth.admin_count -eq 1) ([string]$health.auth.admin_count)

$authMode = [string]$health.auth.mode
if ($authMode -ne 'test') {
    throw @"
Acceptance script requires controlled test identity mode (XV12_AUTH_MODE=test).
Current mode: $authMode

This script exercises sole-admin binding, multi-user memory isolation, and session
revocation with the three fixed local personas (admin / user-a / user-b).

To run acceptance against a live production Google stack:
  1. Stop XODUZ (Stop-XODUZ.cmd)
  2. In config/.env.local set XV12_AUTH_MODE=test (keep OWNER_GOOGLE_SUB)
  3. Start XODUZ again
  4. Re-run scripts\acceptance.ps1
  5. Restore XV12_AUTH_MODE=google and restart when done

For production Google-auth live checks use scripts\live_functional_acceptance.py
after signing in through the UI, or the focused pytest packs (chat-core, web, auth).
"@
}

$adminSession = New-Object Microsoft.PowerShell.Commands.WebRequestSession
try {
    $login = Invoke-RestMethod -Uri "$base/api/auth/test-login" -Method Post -ContentType 'application/json' -Body '{"persona":"admin"}' -WebSession $adminSession
} catch {
    throw "Test identity login failed while auth mode is '$authMode'. Ensure the backend was started with XV12_AUTH_MODE=test. $_"
}
Add-Check 'admin-binding' ($login.role -eq 'admin' -and $login.conversational_name -eq 'Otis') ([string]$login.conversational_name)
Add-Check 'functional-registry' ($health.registry.version -eq $script:RuntimeConfig.versions.capability_registry -and $health.services.adas.status -eq 'available') ([string]$health.registry.version)
$voiceDefault = Invoke-RestMethod -Uri "$base/api/settings/voice" -WebSession $adminSession
Add-Check 'voice-output-default' ($voiceDefault.preferred_voice -eq 'Google US English' -and [int]$voiceDefault.voice_volume -ge 0 -and [int]$voiceDefault.voice_volume -le 100) "$($voiceDefault.voice_name) at $($voiceDefault.voice_volume)"
$conversation = Invoke-RestMethod -Uri "$base/api/conversations" -Method Post -ContentType 'application/json' -Body '{"title":"New conversation"}' -WebSession $adminSession
$chatBody = @{ message='Good morning X.'; attachment_ids=@() } | ConvertTo-Json
$stream = Invoke-WebRequest -UseBasicParsing -Uri "$base/api/conversations/$($conversation.id)/stream" -Method Post -ContentType 'application/json' -Body $chatBody -WebSession $adminSession -TimeoutSec 300
Add-Check 'natural-streaming-chat' ($stream.Content -match 'event: delta' -and $stream.Content -match 'event: done') "conversation $($conversation.id)"
$stored = Invoke-RestMethod -Uri "$base/api/conversations/$($conversation.id)" -WebSession $adminSession
Add-Check 'persistence' ($stored.messages.Count -eq 2 -and $stored.messages[1].status -eq 'complete') ([string]$stored.messages.Count)

$userASession = New-Object Microsoft.PowerShell.Commands.WebRequestSession
$userA = Invoke-RestMethod -Uri "$base/api/auth/test-login" -Method Post -ContentType 'application/json' -Body '{"persona":"user-a"}' -WebSession $userASession
$privateA = Invoke-RestMethod -Uri "$base/api/conversations" -Method Post -ContentType 'application/json' -Body '{"title":"User A private"}' -WebSession $userASession
$userBSession = New-Object Microsoft.PowerShell.Commands.WebRequestSession
$userB = Invoke-RestMethod -Uri "$base/api/auth/test-login" -Method Post -ContentType 'application/json' -Body '{"persona":"user-b"}' -WebSession $userBSession
$isolationStatus = 0
try { Invoke-WebRequest -UseBasicParsing -Uri "$base/api/conversations/$($privateA.id)" -WebSession $userBSession -ErrorAction Stop | Out-Null } catch { $isolationStatus = [int]$_.Exception.Response.StatusCode }
Add-Check 'memory-isolation' ($isolationStatus -eq 404) "cross-user read returned $isolationStatus"

Invoke-WebRequest -UseBasicParsing -Uri "$base/api/auth/logout" -Method Post -WebSession $userBSession | Out-Null
$revokedStatus = 0
try { Invoke-WebRequest -UseBasicParsing -Uri "$base/api/auth/me" -WebSession $userBSession -ErrorAction Stop | Out-Null } catch { $revokedStatus = [int]$_.Exception.Response.StatusCode }
Add-Check 'session-revocation' ($revokedStatus -eq 401) "protected request returned $revokedStatus"

$audit = & "$PSScriptRoot\standalone-audit.ps1" | ConvertFrom-Json
Add-Check 'standalone-audit' ($audit.result -eq 'PASS' -and [int]$audit.donor_runtime_references -eq 0) "$($audit.scanned_files) files"

$result = [pscustomobject]@{
    result = 'PASS'
    timestamp = [DateTimeOffset]::Now.ToString('o')
    checks = $checks
}
$result | ConvertTo-Json -Depth 8
