# Runs physical-input tests against controlled windows. Moves the real cursor; do not touch mouse/keyboard while it runs.
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
python scripts/smoke_windows.py
exit $LASTEXITCODE
