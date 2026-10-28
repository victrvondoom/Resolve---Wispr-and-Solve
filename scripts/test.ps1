$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
if (-not $env:RESOLVE_BROWSER_CHANNEL) { $env:RESOLVE_BROWSER_CHANNEL = 'msedge' }
.\.venv\Scripts\python.exe -m pytest tests -q -rs
exit $LASTEXITCODE
