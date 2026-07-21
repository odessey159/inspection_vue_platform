param()

$ErrorActionPreference = "Stop"
$TestsDir = $PSScriptRoot
$RepoRoot = Split-Path -Parent (Split-Path -Parent $TestsDir)

Set-Location -LiteralPath $RepoRoot
python "$TestsDir\dev_stack_ui.py"
