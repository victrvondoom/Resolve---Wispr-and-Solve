# Creates a project-local venv; never touches global Python. No execution-policy changes:
# if scripts are blocked, run this once per shell:  powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path .venv)) { python -m venv .venv }
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
# Optional Windows semantic inspection (UIA):
.\.venv\Scripts\python.exe -m pip install -r requirements-optional.txt
Write-Host "Browser: set `$env:RESOLVE_BROWSER_CHANNEL='msedge' to use installed Edge (no download),"
Write-Host "or run: .\.venv\Scripts\python.exe -m playwright install chromium"
