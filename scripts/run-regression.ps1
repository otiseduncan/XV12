param(
    [ValidateSet('all','chat-core','auth','authorization','session','memory-isolation','context','model-runtime','launcher','registry-gateway','ui-shell','user-identity','voice','voice-output','project-context','capability-registry','web','databases','attachments')]
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
}
$arguments = @('-m','pytest')
if ($Pack -ne 'all') { $arguments += @('-m', $markers[$Pack]) }
& $python @arguments
exit $LASTEXITCODE
