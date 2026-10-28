$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
Set-Location $project
$python = Get-Command python -ErrorAction Stop
$version = & $python.Source --version
Write-Host "Using $version"
Write-Host "No third-party dependencies are required. Start RESOLVE with: python .\src\resolve_app.py"
