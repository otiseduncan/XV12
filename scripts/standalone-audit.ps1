. "$PSScriptRoot\xv12-common.ps1"

$donorTokens = @(
    ('X' + 'V11'),
    ('X' + ':\X 11'),
    ('B' + 'B1'),
    ('X' + ':\BB1'),
    ('X' + 'V11-ai-first-recovery')
)
$scanFiles = Get-ChildItem -LiteralPath $script:XV12Root -Recurse -File | Where-Object {
    $_.FullName -notmatch '\\.git\\|\\runtime\\python\\|\\runtime\\llama.cpp\\|\\models\\|\\logs\\|\\data\\|\\test-results\\' -and
    $_.Name -ne 'standalone-audit.ps1' -and
    $_.Extension -in @('.py','.js','.css','.html','.json','.ps1','.cmd','.md','.txt','.example')
}
$hits = @()
foreach ($file in $scanFiles) {
    foreach ($token in $donorTokens) {
        $matches = Select-String -LiteralPath $file.FullName -SimpleMatch -Pattern $token -ErrorAction SilentlyContinue
        foreach ($match in $matches) {
            if ($file.FullName -match '\\docs\\' -and $match.Line -match 'provenance|donor|migration|historical') { continue }
            $hits += [pscustomobject]@{ file=$file.FullName; line=$match.LineNumber; token=$token; text=$match.Line.Trim() }
        }
    }
}
$runtimeConfig = Get-Content -LiteralPath $script:RuntimeConfigPath -Raw | ConvertFrom-Json
$ownedPaths = @(
    (Join-Path $script:XV12Root $runtimeConfig.model.executable),
    (Join-Path $script:XV12Root $runtimeConfig.model.path),
    (Join-Path $script:XV12Root $runtimeConfig.storage.database),
    (Join-Path $script:XV12Root $runtimeConfig.storage.attachments)
)
$outside = @($ownedPaths | Where-Object { -not [IO.Path]::GetFullPath($_).StartsWith($script:XV12Root, [StringComparison]::OrdinalIgnoreCase) })
if ($hits.Count -gt 0 -or $outside.Count -gt 0) {
    $hits | Format-Table -AutoSize
    $outside | ForEach-Object { Write-Error "Configured runtime path is outside XV12: $_" }
    throw "Standalone audit failed with $($hits.Count) donor reference(s) and $($outside.Count) outside path(s)."
}
[pscustomobject]@{
    result='PASS'
    scanned_files=$scanFiles.Count
    runtime_paths=$ownedPaths
    donor_runtime_references=0
    outside_runtime_paths=0
} | ConvertTo-Json -Depth 5
