Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:XV12Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$script:RuntimeConfigPath = Join-Path $script:XV12Root 'config\runtime.json'
$script:RuntimeConfig = Get-Content -LiteralPath $script:RuntimeConfigPath -Raw | ConvertFrom-Json
$script:StateDirectory = Join-Path $script:XV12Root 'runtime\state'
$script:LogDirectory = Join-Path $script:XV12Root 'logs'

function Import-XV12LocalEnvironment {
    $path = Join-Path $script:XV12Root 'config\.env.local'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return }
    foreach ($raw in Get-Content -LiteralPath $path) {
        $line = $raw.Trim()
        if (-not $line -or $line.StartsWith('#') -or -not $line.Contains('=')) { continue }
        $name, $value = $line.Split('=', 2)
        [Environment]::SetEnvironmentVariable($name.Trim(), $value.Trim(), 'Process')
    }
}

function Initialize-XV12RuntimeDirectories {
    foreach ($path in @($script:StateDirectory, $script:LogDirectory, (Join-Path $script:XV12Root 'data'))) {
        New-Item -ItemType Directory -Force -Path $path | Out-Null
    }
}

function Write-XV12Log {
    param([Parameter(Mandatory)][string]$Message)
    $line = "[$([DateTimeOffset]::Now.ToString('o'))] $Message"
    Add-Content -LiteralPath (Join-Path $script:LogDirectory 'launcher.log') -Value $line -Encoding utf8
    Write-Host $Message
}

function Get-XV12State {
    param([Parameter(Mandatory)][string]$Name)
    $path = Join-Path $script:StateDirectory "$Name.json"
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    try { return Get-Content -LiteralPath $path -Raw | ConvertFrom-Json } catch { return $null }
}

function Set-XV12State {
    param([Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)]$Value)
    $Value | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $script:StateDirectory "$Name.json") -Encoding UTF8
}

function Remove-XV12State {
    param([Parameter(Mandatory)][string]$Name)
    Remove-Item -LiteralPath (Join-Path $script:StateDirectory "$Name.json") -Force -ErrorAction SilentlyContinue
}

function Test-XV12Process {
    param([Parameter(Mandatory)][AllowNull()]$State)
    if (-not $State -or -not $State.pid) { return $false }
    $process = Get-Process -Id ([int]$State.pid) -ErrorAction SilentlyContinue
    if (-not $process) { return $false }
    return [string]$State.root -eq $script:XV12Root
}

function Stop-XV12OwnedProcess {
    param([Parameter(Mandatory)][string]$Name)
    $state = Get-XV12State -Name $Name
    if (-not (Test-XV12Process -State $state)) {
        Remove-XV12State -Name $Name
        return $false
    }
    Stop-Process -Id ([int]$state.pid) -Force
    try { Wait-Process -Id ([int]$state.pid) -Timeout 20 -ErrorAction SilentlyContinue } catch {}
    Remove-XV12State -Name $Name
    return $true
}

function Get-XV12PortOwner {
    param([Parameter(Mandatory)][int]$Port)
    $listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $listener) { return $null }
    $process = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
    [pscustomobject]@{ Port = $Port; ProcessId = $listener.OwningProcess; ProcessName = if ($process) { $process.ProcessName } else { 'unknown' } }
}

Import-XV12LocalEnvironment
Initialize-XV12RuntimeDirectories
