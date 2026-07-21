param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8001,
    [switch]$RecreateVenv
)

$ErrorActionPreference = "Stop"
$BackendRoot = Split-Path -Parent $PSScriptRoot
Set-Location $BackendRoot

if (-not (Test-Path ".\models\YOLO\security_check_540.pt")) {
    throw "Missing weights file: backend/models/YOLO/security_check_540.pt"
}

function Test-VenvPython {
    param([string]$PythonPath)
    if (-not (Test-Path $PythonPath)) {
        return $false
    }
    try {
        & $PythonPath -c "import sys; print(sys.executable)" | Out-Null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

$venvPath = Join-Path $BackendRoot ".venv"
$python = Join-Path $venvPath "Scripts\python.exe"
$pip = Join-Path $venvPath "Scripts\pip.exe"

if ($RecreateVenv -and (Test-Path $venvPath)) {
    Write-Host "Removing existing virtual environment..."
    Remove-Item $venvPath -Recurse -Force
}

if (-not (Test-VenvPython $python)) {
    if (Test-Path $venvPath) {
        Write-Host "Existing .venv points to another machine or Python install; recreating..."
        Remove-Item $venvPath -Recurse -Force
    } else {
        Write-Host "Creating virtual environment..."
    }
    python -m venv $venvPath
    $python = Join-Path $venvPath "Scripts\python.exe"
    $pip = Join-Path $venvPath "Scripts\pip.exe"
}

if (-not (Test-VenvPython $python)) {
    throw "Virtual environment is still unusable. Try: .\scripts\run_yolo_service.ps1 -RecreateVenv"
}

Write-Host "Using Python: $(& $python -c 'import sys; print(sys.executable)')"
Write-Host "Installing YOLO service dependencies..."
& $python -m pip install -r requirements-yolo.txt

Write-Host ""
Write-Host "Starting YOLO service:"
Write-Host "  URL:    http://${HostAddress}:${Port}"
Write-Host "  Health: http://${HostAddress}:${Port}/healthz"
Write-Host "  Detect: http://${HostAddress}:${Port}/predict/video"
Write-Host "  RTSP:   http://${HostAddress}:${Port}/predict/rtsp"
Write-Host "  Logs:   .runtime/YOLO_log"
Write-Host ""

& $python -m uvicorn yolo_service.app:app --host $HostAddress --port $Port --reload
