$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
if (-not $env:RESOLVE_BROWSER_CHANNEL) { $env:RESOLVE_BROWSER_CHANNEL = 'msedge' }
Write-Host 'RESOLVE at http://127.0.0.1:8765  (landing)  and  http://127.0.0.1:8765/app  (Mission Control)'
.\.venv\Scripts\python.exe .\src\resolve_app.py
