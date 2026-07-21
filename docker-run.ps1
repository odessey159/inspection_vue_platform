param(
    [ValidateSet("up", "down", "logs", "restart")]
    [string]$Action = "up"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Ensure-EnvFile {
    $envFile = Join-Path $Root ".env"
    $envExample = Join-Path $Root ".env.example"
    if (-not (Test-Path $envFile)) {
        if (-not (Test-Path $envExample)) {
            throw ".env.example not found; cannot create .env"
        }
        Copy-Item $envExample $envFile
        Write-Host "Created .env from .env.example"
    }
}

function Ensure-RuntimeDirs {
    $dirs = @(".runtime", "inputs", "inputs/bags", "inputs/standards")
    foreach ($dir in $dirs) {
        $path = Join-Path $Root $dir
        if (-not (Test-Path $path)) {
            New-Item -ItemType Directory -Path $path | Out-Null
            Write-Host "Created directory: $dir"
        }
    }
}

function Show-AccessInfo {
    Write-Host ""
    Write-Host "Access URLs:"
    Write-Host "  Web:      http://127.0.0.1:8700"
    Write-Host "  API:      http://127.0.0.1:8010"
    Write-Host "  Health:   http://127.0.0.1:8010/healthz"
    Write-Host ""
    Write-Host "Useful commands:"
    Write-Host "  .\docker-run.ps1 -Action logs"
    Write-Host "  .\docker-run.ps1 -Action down"
    Write-Host ""
}

switch ($Action) {
    "up" {
        Ensure-EnvFile
        Ensure-RuntimeDirs
        Write-Host "Starting Docker services..."
        docker compose up --build -d
        if ($LASTEXITCODE -ne 0) {
            throw "docker compose up failed with exit code $LASTEXITCODE"
        }
        Show-AccessInfo
    }
    "down" {
        Write-Host "Stopping Docker services..."
        docker compose down
    }
    "logs" {
        docker compose logs -f backend web
    }
    "restart" {
        Ensure-EnvFile
        Ensure-RuntimeDirs
        Write-Host "Restarting Docker services..."
        docker compose down
        docker compose up --build -d
        if ($LASTEXITCODE -ne 0) {
            throw "docker compose up failed with exit code $LASTEXITCODE"
        }
        Show-AccessInfo
    }
}
