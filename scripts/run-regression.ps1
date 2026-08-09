param(
    [ValidateSet('all','x-core','chat-core','auth','authorization','permissions','admin-capabilities','session','memory-isolation','context','model-runtime','launcher','registry-gateway','registry','gateway','ui-shell','user-identity','voice','voice-output','stt','tts','voice-settings','project-context','capability-registry','web','files','adas','calibration-iq','standalone','databases','attachments','artifacts')]
    [string]$Pack = 'all'
)

. "$PSScriptRoot\xv12-common.ps1"
$python = Join-Path $script:XV12Root 'runtime\python\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Run scripts\bootstrap.ps1 first.' }
$markers = @{
    'chat-core'='chat_core'; auth='auth'; authorization='authorization'; session='session';
    'memory-isolation'='memory_isolation'; context='context'; 'model-runtime'='model_runtime';
    launcher='launcher'; 'registry-gateway'='registry_gateway'; 'ui-shell'='ui_shell';
    'user-identity'='user_identity'; voice='voice'; 'voice-output'='voice_output'; 'project-context'='project_context';
    'capability-registry'='capability_registry'; web='web'; databases='databases'; attachments='attachments'
    permissions='permissions'; 'admin-capabilities'='admin_capabilities'; registry='registry or capability_registry'; gateway='gateway or registry_gateway';
    files='files'; adas='adas'; 'calibration-iq'='calibration_iq'; standalone='standalone'; artifacts='artifacts'; stt='stt or voice'; tts='tts or voice_output'; 'voice-settings'='voice_settings or voice_output'
}
$arguments = @('-m','pytest')
if ($Pack -eq 'x-core') { $arguments += @('-m', 'x_core or chat_core or auth or memory_isolation or context or model_runtime or user_identity') }
elseif ($Pack -ne 'all') { $arguments += @('-m', $markers[$Pack]) }
& $python @arguments
exit $LASTEXITCODE
