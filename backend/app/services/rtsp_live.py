"""MJPEG multipart streaming for browser RTSP preview via ffmpeg."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from typing import BinaryIO

from ..settings import FFMPEG_BIN
from .rtsp_recorder import DEFAULT_RTSP_TRANSPORT, resolve_recording_rtsp_url

MJPEG_BOUNDARY = "frame"
READ_CHUNK_SIZE = 16384


def build_rtsp_mjpeg_ffmpeg_command(
    *,
    rtsp_url: str,
    rtsp_transport: str = DEFAULT_RTSP_TRANSPORT,
) -> list[str]:
    return [
        FFMPEG_BIN,
        "-hide_banner",
        "-loglevel",
        "error",
        "-fflags",
        "nobuffer",
        "-flags",
        "low_delay",
        "-rtsp_transport",
        rtsp_transport,
        "-i",
        rtsp_url,
        "-an",
        "-vf",
        "fps=15",
        "-f",
        "mjpeg",
        "-q:v",
        "5",
        "pipe:1",
    ]


def iter_mjpeg_multipart_frames(
    rtsp_url: str,
    *,
    rtsp_transport: str = DEFAULT_RTSP_TRANSPORT,
) -> Iterator[bytes]:
    """Yield multipart JPEG frames suitable for HTTP StreamingResponse."""
    resolved_url = resolve_recording_rtsp_url(rtsp_url)
    command = build_rtsp_mjpeg_ffmpeg_command(rtsp_url=resolved_url, rtsp_transport=rtsp_transport)
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )
    if process.stdout is None:
        process.kill()
        raise RuntimeError("Failed to start ffmpeg RTSP live stream")

    try:
        yield from _stream_jpeg_frames(process.stdout)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def _stream_jpeg_frames(stdout: BinaryIO) -> Iterator[bytes]:
    buffer = b""
    while True:
        chunk = stdout.read(READ_CHUNK_SIZE)
        if not chunk:
            break
        buffer += chunk
        while True:
            start = buffer.find(b"\xff\xd8")
            if start == -1:
                last_marker = buffer.rfind(b"\xff")
                buffer = buffer[last_marker:] if last_marker != -1 else b""
                break
            end = buffer.find(b"\xff\xd9", start + 2)
            if end == -1:
                buffer = buffer[start:]
                break
            frame = buffer[start : end + 2]
            buffer = buffer[end + 2 :]
            yield (
                f"--{MJPEG_BOUNDARY}\r\n".encode("ascii")
                + b"Content-Type: image/jpeg\r\n\r\n"
                + frame
                + b"\r\n"
            )
