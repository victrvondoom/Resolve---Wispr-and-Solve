$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
Set-Location $project
$env:RESOLVE_GUI_TEST = '1'
try {
    python -m unittest tests.test_windows_adapter.WindowsPhysicalInputTest -v
} finally {
    Remove-Item Env:RESOLVE_GUI_TEST -ErrorAction SilentlyContinue
}
