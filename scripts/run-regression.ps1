param(
    [ValidateSet('all','chat-core','auth','authorization','session','memory-isolation','context','model-runtime','launcher','registry-gateway')]
    [string]$Pack = 'all'
)

. "$PSScriptRoot\xv12-common.ps1"
$python = Join-Path $script:XV12Root 'runtime\python\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Run scripts\bootstrap.ps1 first.' }
$markers = @{
    'chat-core'='chat_core'; auth='auth'; authorization='authorization'; session='session';
    'memory-isolation'='memory_isolation'; context='context'; 'model-runtime'='model_runtime';
    launcher='launcher'; 'registry-gateway'='registry_gateway'
}
$arguments = @('-m','pytest')
if ($Pack -ne 'all') { $arguments += @('-m', $markers[$Pack]) }
& $python @arguments
exit $LASTEXITCODE
