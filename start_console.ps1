param(
    [switch]$Check
)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
$AppPath = Join-Path $ProjectRoot "start_console.py"
$VenvPython = Join-Path $ProjectRoot "backend\.venv\Scripts\python.exe"

Set-Location -LiteralPath $ProjectRoot

$Python = $null
if (Test-Path -LiteralPath $VenvPython) {
    try {
        & $VenvPython -c "import tkinter" 2>$null
        if ($LASTEXITCODE -eq 0) {
            $Python = $VenvPython
        }
    } catch {
        $Python = $null
    }
}

if (-not $Python) {
    foreach ($Name in @("python", "py")) {
        $Command = Get-Command $Name -ErrorAction SilentlyContinue
        if ($Command) {
            $Python = $Command.Source
            break
        }
    }
}

if (-not $Python) {
    throw "Python was not found. Install Python 3.10 or newer and try again."
}

if ($Check) {
    & $Python $AppPath --check
    exit $LASTEXITCODE
}

& $Python $AppPath
exit $LASTEXITCODE
