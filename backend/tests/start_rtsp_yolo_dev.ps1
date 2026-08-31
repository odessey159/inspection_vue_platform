param(
    [int]$RtspPort = 18554,
    [string]$YoloHost = "127.0.0.1",
    [int]$YoloPort = 8001,
    [switch]$SkipYolo,
    [switch]$SkipRtsp,
    [switch]$RecreateYoloVenv,
    [switch]$NoNewWindows
)

<#
.SYNOPSIS
    One-click local RTSP + YOLO test stack.

.DESCRIPTION
    Starts MediaMTX, the RTSP publisher (H.264 pose SEI on /live), and the YOLO
    service. By default each process gets its own PowerShell window so logs stay
    readable. Press Enter in this window to stop everything.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File backend/tests/start_rtsp_yolo_dev.ps1
#>

$ErrorActionPreference = "Stop"

$TestsDir = $PSScriptRoot
$BackendRoot = Split-Path -Parent $TestsDir
$RepoRoot = Split-Path -Parent $BackendRoot

$script:ChildProcesses = @()
$script:DockerContainerName = "inspection-rtsp-mediamtx"

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Test-TcpPort {
    param(
        [string]$HostName,
        [int]$Port,
        [int]$TimeoutMs = 500
    )
    try {
        $client = [System.Net.Sockets.TcpClient]::new()
        $async = $client.BeginConnect($HostName, $Port, $null, $null)
        $ok = $async.AsyncWaitHandle.WaitOne($TimeoutMs, $false)
        if (-not $ok) {
            return $false
        }
        $client.EndConnect($async)
        $client.Close()
        return $true
    } catch {
        return $false
    }
}

function Wait-TcpPort {
    param(
        [string]$HostName,
        [int]$Port,
        [int]$TimeoutSec = 60,
        [string]$Label = "service"
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-TcpPort -HostName $HostName -Port $Port) {
            Write-Host "  $Label is up on ${HostName}:${Port}"
            return
        }
        Start-Sleep -Milliseconds 400
    }
    throw "Timed out waiting for $Label at ${HostName}:${Port}"
}

function Start-DevWindow {
    param(
        [string]$Title,
        [string]$WorkingDirectory,
        [string]$Command
    )

    $encoded = @"
`$Host.UI.RawUI.WindowTitle = '$Title'
Set-Location -LiteralPath '$WorkingDirectory'
Write-Host '[$Title]' -ForegroundColor Green
$Command
"@

    if ($NoNewWindows) {
        Write-Host "  starting in background job: $Title"
        $job = Start-Job -Name $Title -ScriptBlock {
            param($WorkDir, $Cmd)
            Set-Location -LiteralPath $WorkDir
            Invoke-Expression $Cmd
        } -ArgumentList $WorkingDirectory, $Command
        return [pscustomobject]@{ Kind = "job"; Job = $job; Title = $Title }
    }

    $proc = Start-Process `
        -FilePath "powershell.exe" `
        -PassThru `
        -WorkingDirectory $WorkingDirectory `
        -ArgumentList @(
            "-NoExit",
            "-ExecutionPolicy", "Bypass",
            "-Command", $encoded
        )
    Write-Host "  opened window PID $($proc.Id): $Title"
    return [pscustomobject]@{ Kind = "process"; Process = $proc; Title = $Title }
}

function Stop-DevStack {
    Write-Step "Stopping test stack"

    foreach ($child in $script:ChildProcesses) {
        if ($null -eq $child) { continue }
        try {
            if ($child.Kind -eq "job") {
                Stop-Job -Job $child.Job -ErrorAction SilentlyContinue
                Remove-Job -Job $child.Job -Force -ErrorAction SilentlyContinue
                Write-Host "  stopped job: $($child.Title)"
            } elseif ($child.Process -and -not $child.Process.HasExited) {
                Stop-Process -Id $child.Process.Id -Force -ErrorAction SilentlyContinue
                Write-Host "  stopped window: $($child.Title) (PID $($child.Process.Id))"
            }
        } catch {
            Write-Host "  warn: could not stop $($child.Title): $_"
        }
    }

    $existing = docker ps -aq --filter "name=$($script:DockerContainerName)" 2>$null
    if ($existing) {
        docker rm -f $script:DockerContainerName 2>$null | Out-Null
        Write-Host "  removed docker container: $($script:DockerContainerName)"
    }
}

function Start-MediaMtx {
    param([int]$Port)

    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "Docker is required for MediaMTX. Install Docker Desktop or start MediaMTX yourself."
    }

    $existing = docker ps -aq --filter "name=$($script:DockerContainerName)" 2>$null
    if ($existing) {
        Write-Host "  removing previous container $($script:DockerContainerName)..."
        docker rm -f $script:DockerContainerName | Out-Null
    }

    if (Test-TcpPort -HostName "127.0.0.1" -Port $Port) {
        Write-Host "  port $Port already open; reusing existing RTSP listener"
        return
    }

    Write-Host "  starting MediaMTX container on host port $Port..."
    docker run -d --rm `
        --name $script:DockerContainerName `
        -p "${Port}:8554" `
        bluenviron/mediamtx:latest | Out-Null

    # First pull can be slow; allow up to 2 minutes.
    Wait-TcpPort -HostName "127.0.0.1" -Port $Port -TimeoutSec 120 -Label "MediaMTX"
}

try {
    Write-Host "RTSP + YOLO local test stack"
    Write-Host "  repo:   $RepoRoot"
    Write-Host "  backend:$BackendRoot"
    Write-Host "  RTSP:   rtsp://127.0.0.1:$RtspPort/live  (pose SEI)"
    Write-Host "  YOLO:   http://${YoloHost}:${YoloPort}/healthz"

    if (-not $SkipRtsp) {
        Write-Step "MediaMTX RTSP server"
        Start-MediaMtx -Port $RtspPort

        Write-Step "RTSP publisher (pose SEI)"
        $publishCmd = "python `"$TestsDir\generate_rtsp_sei_stream.py`" --port $RtspPort"
        $script:ChildProcesses += Start-DevWindow `
            -Title "RTSP Publisher" `
            -WorkingDirectory $RepoRoot `
            -Command $publishCmd

        Start-Sleep -Seconds 2
    } else {
        Write-Step "Skipping RTSP server / publisher (-SkipRtsp)"
    }

    if (-not $SkipYolo) {
        Write-Step "YOLO service"
        $yoloExtra = ""
        if ($RecreateYoloVenv) {
            $yoloExtra = " -RecreateVenv"
        }
        $yoloCmd = "& `"$BackendRoot\scripts\run_yolo_service.ps1`" -HostAddress `"$YoloHost`" -Port $YoloPort$yoloExtra"
        $script:ChildProcesses += Start-DevWindow `
            -Title "YOLO Service" `
            -WorkingDirectory $BackendRoot `
            -Command $yoloCmd
    } else {
        Write-Step "Skipping YOLO service (-SkipYolo)"
    }

    Write-Host ""
    Write-Host "Stack is starting." -ForegroundColor Green
    Write-Host "  video: rtsp://127.0.0.1:$RtspPort/live  (H.264 pose SEI)"
    Write-Host "  barcode fallback: python backend/tests/generate_rtsp_stream.py"
    if (-not $SkipYolo) {
        Write-Host "  yolo:  http://${YoloHost}:${YoloPort}/healthz"
    }
    Write-Host ""
    Write-Host "Press Enter in this window to stop the stack (closes child windows / container)."
    [void](Read-Host)
}
finally {
    Stop-DevStack
}
