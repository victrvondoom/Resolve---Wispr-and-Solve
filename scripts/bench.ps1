$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
if (-not $env:RESOLVE_BROWSER_CHANNEL) { $env:RESOLVE_BROWSER_CHANNEL = 'msedge' }
.\.venv\Scripts\python.exe .\benchmarks\web_bench.py
.\.venv\Scripts\python.exe .\benchmarks\ablations.py
