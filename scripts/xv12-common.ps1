Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:XV12Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$script:RuntimeConfigPath = Join-Path $script:XV12Root 'config\runtime.json'
$script:RuntimeConfig = Get-Content -LiteralPath $script:RuntimeConfigPath -Raw | ConvertFrom-Json
$script:StateDirectory = if ($env:XV12_STATE_DIRECTORY) { [IO.Path]::GetFullPath($env:XV12_STATE_DIRECTORY) } else { Join-Path $script:XV12Root 'runtime\state' }
$script:LogDirectory = if ($env:XV12_LOG_DIRECTORY) { [IO.Path]::GetFullPath($env:XV12_LOG_DIRECTORY) } else { Join-Path $script:XV12Root 'logs' }

function Import-XV12LocalEnvironment {
    $path = Join-Path $script:XV12Root 'config\.env.local'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return }
    foreach ($raw in Get-Content -LiteralPath $path) {
        $line = $raw.Trim()
        if (-not $line -or $line.StartsWith('#') -or -not $line.Contains('=')) { continue }
        $name, $value = $line.Split('=', 2)
        $name = $name.Trim()
        if ($null -ne [Environment]::GetEnvironmentVariable($name, 'Process')) { continue }
        [Environment]::SetEnvironmentVariable($name, $value.Trim(), 'Process')
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

function Get-XV12Property {
    param([AllowNull()]$Object, [Parameter(Mandatory)][string]$Name)
    if ($null -eq $Object) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Resolve-XV12Path {
    param([Parameter(Mandatory)][string]$Path)
    $candidate = if ([IO.Path]::IsPathRooted($Path)) { $Path } else { Join-Path $script:XV12Root $Path }
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { return $null }
    return (Get-Item -LiteralPath $candidate).FullName
}

function Test-XV12PathEqual {
    param([AllowNull()][string]$Left, [AllowNull()][string]$Right)
    if (-not $Left -or -not $Right) { return $false }
    try {
        $leftFull = [IO.Path]::GetFullPath($Left).TrimEnd('\')
        $rightFull = [IO.Path]::GetFullPath($Right).TrimEnd('\')
        return [string]::Equals($leftFull, $rightFull, [StringComparison]::OrdinalIgnoreCase)
    } catch { return $false }
}

function Get-XV12ProcessRecord {
    param([Parameter(Mandatory)][int]$ProcessId)
    try {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction Stop
        if (-not $process) { return $null }
        return [pscustomobject]@{
            ProcessId = [int]$process.ProcessId
            ParentProcessId = [int]$process.ParentProcessId
            ProcessName = [string]$process.Name
            ExecutablePath = [string]$process.ExecutablePath
            CommandLine = [string]$process.CommandLine
            ProcessStartedAt = ([datetime]$process.CreationDate).ToUniversalTime().ToString('o')
        }
    } catch { return $null }
}

function Test-XV12ProcessRecord {
    param(
        [AllowNull()]$Process,
        [AllowNull()][string]$ExpectedExecutable,
        [string[]]$CommandLineContains = @(),
        [AllowNull()][string]$ExpectedStartedAt
    )
    if (-not $Process -or -not $Process.ProcessId) { return $false }
    if ($ExpectedExecutable -and -not (Test-XV12PathEqual $Process.ExecutablePath $ExpectedExecutable)) { return $false }
    $commandLine = [string]$Process.CommandLine
    foreach ($token in $CommandLineContains) {
        if ($token -and $commandLine.IndexOf($token, [StringComparison]::OrdinalIgnoreCase) -lt 0) { return $false }
    }
    if ($ExpectedStartedAt) {
        try {
            $expected = [DateTimeOffset]::Parse($ExpectedStartedAt).UtcDateTime
            $actual = [DateTimeOffset]::Parse([string]$Process.ProcessStartedAt).UtcDateTime
            if ([math]::Abs(($actual - $expected).TotalSeconds) -ge 4) { return $false }
        } catch { return $false }
    }
    return $true
}

function Find-XV12Processes {
    param(
        [AllowNull()][string]$ExpectedExecutable,
        [string[]]$CommandLineContains = @()
    )
    $matches = @()
    foreach ($process in @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) {
        $record = [pscustomobject]@{
            ProcessId = [int]$process.ProcessId
            ParentProcessId = [int]$process.ParentProcessId
            ProcessName = [string]$process.Name
            ExecutablePath = [string]$process.ExecutablePath
            CommandLine = [string]$process.CommandLine
            ProcessStartedAt = try { ([datetime]$process.CreationDate).ToUniversalTime().ToString('o') } catch { $null }
        }
        if (Test-XV12ProcessRecord -Process $record -ExpectedExecutable $ExpectedExecutable -CommandLineContains $CommandLineContains) {
            $matches += $record
        }
    }
    return @($matches)
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
    param(
        [Parameter(Mandatory)][AllowNull()]$State,
        [AllowNull()][string]$ExpectedExecutable,
        [string[]]$CommandLineContains = @()
    )
    $pidValue = Get-XV12Property $State 'pid'
    $root = [string](Get-XV12Property $State 'root')
    if (-not $pidValue -or -not (Test-XV12PathEqual $root $script:XV12Root)) { return $false }
    $stateExecutable = [string](Get-XV12Property $State 'executable')
    if (-not $ExpectedExecutable -and $stateExecutable) { $ExpectedExecutable = $stateExecutable }
    $startedAt = [string](Get-XV12Property $State 'process_started_at')
    if (-not $startedAt) { $startedAt = [string](Get-XV12Property $State 'started_at') }
    $process = Get-XV12ProcessRecord -ProcessId ([int]$pidValue)
    return Test-XV12ProcessRecord -Process $process -ExpectedExecutable $ExpectedExecutable -CommandLineContains $CommandLineContains -ExpectedStartedAt $startedAt
}

function Stop-XV12OwnedProcess {
    param(
        [Parameter(Mandatory)][string]$Name,
        [AllowNull()][string]$ExpectedExecutable,
        [string[]]$CommandLineContains = @()
    )
    $state = Get-XV12State -Name $Name
    if (-not (Test-XV12Process -State $state -ExpectedExecutable $ExpectedExecutable -CommandLineContains $CommandLineContains)) {
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
    $process = Get-XV12ProcessRecord -ProcessId ([int]$listener.OwningProcess)
    [pscustomobject]@{
        Port = $Port
        ProcessId = [int]$listener.OwningProcess
        ParentProcessId = if ($process) { $process.ParentProcessId } else { $null }
        ProcessName = if ($process) { $process.ProcessName } else { 'unknown' }
        ExecutablePath = if ($process) { $process.ExecutablePath } else { $null }
        CommandLine = if ($process) { $process.CommandLine } else { $null }
        ProcessStartedAt = if ($process) { $process.ProcessStartedAt } else { $null }
    }
}

Import-XV12LocalEnvironment
Initialize-XV12RuntimeDirectories
