param(
    [int]$Port = 18554
)

$ErrorActionPreference = "Stop"

Write-Host "Starting MediaMTX RTSP server"
Write-Host "  video path (after publish): rtsp://127.0.0.1:$Port/live"
Write-Host ""
Write-Host "Then run the default SEI publisher:"
Write-Host "  python backend/tests/generate_rtsp_sei_stream.py"
Write-Host "Barcode fallback ( /live + /time ):"
Write-Host "  python backend/tests/generate_rtsp_stream.py"
Write-Host "Keep this window open while testing. Press Ctrl+C to stop."
Write-Host ""

docker run --rm -it `
    -p "${Port}:8554" `
    bluenviron/mediamtx:latest
