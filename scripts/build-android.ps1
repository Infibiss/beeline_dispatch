param(
    [Parameter(Mandatory=$true)][string]$ServerUrl,
    [string]$LocalCa = '',
    [string]$ToolsDirectory = ''
)
$ErrorActionPreference = 'Stop'
$repository = Split-Path -Parent $PSScriptRoot
$pythonCandidates = @((Join-Path $repository '.venv-win311\Scripts\python.exe'), (Join-Path $repository '.venv\Scripts\python.exe'))
$pythonExecutable = $pythonCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $pythonExecutable) { throw 'Create a native Windows Python environment first.' }
$env:PYTHONUTF8 = '1'
$arguments = @('-B', (Join-Path $PSScriptRoot 'build-android.py'), '--server-url', $ServerUrl)
if ($LocalCa) { $arguments += @('--local-ca', (Resolve-Path -LiteralPath $LocalCa).Path) }
if ($ToolsDirectory) { $arguments += @('--tools-dir', $ToolsDirectory) }
& $pythonExecutable @arguments
if ($LASTEXITCODE -ne 0) { throw 'Android APK build failed.' }
