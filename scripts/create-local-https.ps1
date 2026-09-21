param([string]$OpenSsl = '')
$ErrorActionPreference = 'Stop'
$repository = Split-Path -Parent $PSScriptRoot
$pythonCandidates = @((Join-Path $repository '.venv-win311\Scripts\python.exe'), (Join-Path $repository '.venv\Scripts\python.exe'))
$pythonExecutable = $pythonCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $pythonExecutable) { throw 'Native Windows Python environment not found.' }
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = Join-Path $repository 'src'
$arguments = @('-B', '-m', 'field_router.local_https', '--directory', (Join-Path $repository 'output\local\https'))
if ($OpenSsl) { $arguments += @('--openssl', $OpenSsl) }
& $pythonExecutable @arguments
if ($LASTEXITCODE -ne 0) { throw "Certificate setup failed: $LASTEXITCODE" }
