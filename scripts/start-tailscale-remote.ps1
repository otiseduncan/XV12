param(
    [ValidateRange(1, 65535)][int]$FrontendPort = 8120,
    [ValidateSet(443, 8443, 10000)][int]$ServeHttpsPort = 10000,
    [switch]$ValidateOnly,
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'
$tailscale = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
if (-not (Test-Path -LiteralPath $tailscale -PathType Leaf)) {
    $command = Get-Command tailscale.exe -ErrorAction SilentlyContinue
    if (-not $command) { throw 'Tailscale CLI is not installed.' }
    $tailscale = $command.Source
}

$status = (& $tailscale status --json | ConvertFrom-Json)
if ($status.BackendState -ne 'Running' -or -not $status.Self.Online) {
    throw 'Tailscale must be authenticated, running, and online.'
}
$dnsName = [string]$status.Self.DNSName
if (-not $dnsName) { throw 'Tailscale did not report this device DNS name.' }
$dnsName = $dnsName.TrimEnd('.')
$origin = if ($ServeHttpsPort -eq 443) { "https://$dnsName" } else { "https://${dnsName}:$ServeHttpsPort" }

$envPath = Join-Path (Split-Path $PSScriptRoot -Parent) 'config\.env.local'
if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
    throw 'Create config/.env.local and configure Google OIDC plus XV12_TAILSCALE_SERVE_ORIGIN first.'
}
$values = @{}
foreach ($line in Get-Content -LiteralPath $envPath) {
    if ($line.Trim() -and -not $line.TrimStart().StartsWith('#') -and $line.Contains('=')) {
        $key, $value = $line.Split('=', 2)
        $values[$key.Trim()] = $value.Trim()
    }
}
if ($values.XV12_TAILSCALE_SERVE_ORIGIN -ne $origin) { throw "XV12_TAILSCALE_SERVE_ORIGIN must be exactly $origin" }
if ($values.XV12_AUTH_MODE -ne 'google') { throw 'XV12_AUTH_MODE must be google for production remote access.' }
if (-not $values.XV12_GOOGLE_CLIENT_ID -or -not $values.XV12_GOOGLE_CLIENT_SECRET -or -not $values.XV12_GOOGLE_REDIRECT_URI) { throw 'Google OIDC client, secret, and redirect URI are required.' }
if ($values.XV12_GOOGLE_REDIRECT_URI -ne "$origin/api/auth/google/callback") { throw "XV12_GOOGLE_REDIRECT_URI must be exactly $origin/api/auth/google/callback" }

$listeners = Get-NetTCPConnection -State Listen -LocalPort $FrontendPort -ErrorAction SilentlyContinue
if ($listeners -and @($listeners | Where-Object { $_.LocalAddress -notin @('127.0.0.1', '::1') }).Count -gt 0) {
    throw "XV12 port $FrontendPort is listening beyond loopback. Refusing remote setup."
}
if (-not $listeners -and -not $NoStart -and -not $ValidateOnly) {
    & (Join-Path $PSScriptRoot 'start-xv12.ps1') -NoOpen
    $listeners = Get-NetTCPConnection -State Listen -LocalPort $FrontendPort -ErrorAction SilentlyContinue
}
if (-not $listeners) { throw "XV12 is not listening on loopback port $FrontendPort." }
$health = Invoke-RestMethod -Uri "http://127.0.0.1:$FrontendPort/api/health" -TimeoutSec 15
if (-not $health.application -or $health.application.name -ne 'XODUZ XV12') {
    throw "The loopback service on port $FrontendPort is not XV12."
}

$serve = (& $tailscale serve status --json | ConvertFrom-Json)
$selected = $serve.Web.PSObject.Properties | Where-Object { $_.Name -eq "${dnsName}:$ServeHttpsPort" } | Select-Object -First 1
$expectedProxy = "http://127.0.0.1:$FrontendPort"
if ($selected) {
    $proxy = [string]$selected.Value.Handlers.'/'.Proxy
    if ($proxy -ne $expectedProxy) {
        throw "HTTPS port $ServeHttpsPort already serves another target ($proxy). Existing routes were not changed."
    }
}

if (-not $ValidateOnly -and -not $selected) {
    & $tailscale serve --bg --yes "--https=$ServeHttpsPort" $expectedProxy | Out-Host
}
$verified = (& $tailscale serve status --json | ConvertFrom-Json)
$route = $verified.Web.PSObject.Properties | Where-Object { $_.Name -eq "${dnsName}:$ServeHttpsPort" } | Select-Object -First 1
if (-not $ValidateOnly -and [string]$route.Value.Handlers.'/'.Proxy -ne $expectedProxy) {
    throw 'Tailscale Serve did not persist the expected XV12 loopback route.'
}

Write-Host "XV12 loopback target verified: $expectedProxy"
Write-Host "Tailscale Serve URL: $origin"
if ($ValidateOnly) { Write-Host 'Validation only: Tailscale Serve configuration was not changed.' }
else { Write-Host 'Serve is private to the tailnet. Funnel was not enabled or invoked.' }
