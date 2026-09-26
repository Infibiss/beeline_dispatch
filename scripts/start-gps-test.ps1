param(
    [ValidateRange(1, 65535)] [int]$Port = 8768,
    [string]$SourceState = 'output\local\mobile-east.json',
    [string]$EngineerId = '',
    [string]$GpsHistoryFile = '',
    [ValidateRange(1, 60)] [int]$StartInMinutes = 5,
    [switch]$PrepareOnly,
    [switch]$Resume
)
$ErrorActionPreference = 'Stop'
$repository = Split-Path -Parent $PSScriptRoot
$pythonExecutable = @(
    (Join-Path $repository '.venv-win311\Scripts\python.exe'),
    (Join-Path $repository '.venv\Scripts\python.exe')
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $pythonExecutable) { throw 'Native Windows Python environment not found.' }
if (-not [IO.Path]::IsPathRooted($SourceState)) { $SourceState = Join-Path $repository $SourceState }
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = Join-Path $repository 'src'
$arguments = @('-B', '-m', 'field_router.gps.test_plan', '--repository', $repository,
    '--source-state', $SourceState, '--port', "$Port", '--lead-minutes', "$StartInMinutes")
if ($EngineerId) { $arguments += @('--engineer-id', $EngineerId) }
if ($GpsHistoryFile) {
    if (-not [IO.Path]::IsPathRooted($GpsHistoryFile)) { $GpsHistoryFile = Join-Path $repository $GpsHistoryFile }
    $arguments += @('--gps-history', $GpsHistoryFile)
}
if ($PrepareOnly) { $arguments += '--prepare-only' }
if ($Resume) { $arguments += '--resume' }
Push-Location $repository
try {
    & $pythonExecutable @arguments
    if ($LASTEXITCODE -ne 0) { throw "GPS test exited with code $LASTEXITCODE." }
} finally { Pop-Location }
