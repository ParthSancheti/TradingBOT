$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $projectRoot
$pythonExecutable = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExecutable)) { throw 'Run the installation steps in RUNNING.md first.' }
& $pythonExecutable -m uvicorn jarvis.app:app --host 127.0.0.1 --port 8765 --no-proxy-headers --no-access-log
