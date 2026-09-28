param(
    [ValidateSet('East', 'SouthEast', 'SouthCenter')]
    [string]$Region = 'East',
    [ValidateRange(1, 65535)]
    [int]$Port = 8766,
    [string]$StateFile = '',
    [ValidateSet('ortools', 'alns', 'hybrid')]
    [string]$Solver = 'hybrid',
    [switch]$LocalOnly,
    [switch]$Network,
    [switch]$Https,
    [switch]$DisableGps,
    [string]$GpsModel = '',
    [string]$GpsRoutingConfig = ''
)

$ErrorActionPreference = 'Stop'
$repository = Split-Path -Parent $PSScriptRoot
$occupiedPort = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners() |
    Where-Object { $_.Port -eq $Port } | Select-Object -First 1
if ($occupiedPort) {
    throw "Port $Port is already in use. Stop the previous server in its terminal (Ctrl+C), then run this command again. Do not start two copies on the same port."
}
$pythonCandidates = @(
    (Join-Path $repository '.venv-win311\Scripts\python.exe'),
    (Join-Path $repository '.venv\Scripts\python.exe')
)
$pythonExecutable = $pythonCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $pythonExecutable) {
    throw 'Native Windows Python environment not found. See docs/planning/mobile-guide.md.'
}

$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = Join-Path $repository 'src'
$regionFiles = @{ East = 'Восток.json'; SouthEast = 'Юго-восток.json'; SouthCenter = 'Югоцентр.json' }
$comparison = Join-Path (Join-Path $repository 'data\demo-road') $regionFiles[$Region]
if (-not $StateFile) {
    $StateFile = Join-Path $repository ('output\local\mobile-' + $Region.ToLowerInvariant() + '.json')
} elseif (-not [IO.Path]::IsPathRooted($StateFile)) {
    $StateFile = Join-Path $repository $StateFile
}
$listenAddress = if ($LocalOnly) { '127.0.0.1' } else { '0.0.0.0' }
$serverArguments = @('-B', '-m', 'field_router.scenario_server', $comparison,
    '--state', $StateFile, '--host', $listenAddress, '--port', "$Port", '--time-limit', '30', '--morning-time-limit', '120', '--solver', $Solver)
if ($Network) { $serverArguments += '--network' }
if ($DisableGps) { $serverArguments += '--disable-gps' }
if ($GpsModel) { $serverArguments += @('--gps-model', $GpsModel) }
if ($GpsRoutingConfig) {
    if (-not [IO.Path]::IsPathRooted($GpsRoutingConfig)) { $GpsRoutingConfig = Join-Path $repository $GpsRoutingConfig }
    $serverArguments += @('--gps-routing', $GpsRoutingConfig)
}
$serverArguments += @('--gps-config', (Join-Path $repository 'config\gps.json'))
if ($Https) {
    $certificate = Join-Path $repository 'output\local\https\server.crt'
    $privateKey = Join-Path $repository 'output\local\https\server.key'
    if (-not (Test-Path -LiteralPath $certificate) -or -not (Test-Path -LiteralPath $privateKey)) {
        throw 'Run scripts/create-local-https.ps1 first. See README_GPS.md for phone trust setup.'
    }
    $serverArguments += @('--certfile', $certificate, '--keyfile', $privateKey)
}

Write-Host 'Field Router: dispatcher + engineer. Stop with Ctrl+C.'
Write-Host "State: $StateFile"
Push-Location $repository
try {
    & $pythonExecutable @serverArguments
    if ($LASTEXITCODE -ne 0) { throw "Server exited with code $LASTEXITCODE." }
} finally {
    Pop-Location
}
