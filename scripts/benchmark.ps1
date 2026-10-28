$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
Set-Location $project
python .\src\benchmark_lab.py --output .\data\benchmarks @args
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
