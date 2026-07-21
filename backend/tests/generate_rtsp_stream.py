#!/usr/bin/env python3
"""Local RTSP test stream generator for inspection platform development.

Uses FFmpeg to publish to an RTSP server such as MediaMTX.
Requires FFmpeg on PATH, or set FFMPEG_BIN (same env var as the backend).

Publishes two synchronized paths from one FFmpeg process:
  - video: rtsp://127.0.0.1:18554/live
  - time:  rtsp://127.0.0.1:18554/time  (mapped scene timeline, loops with video)

Default mapped timeline (UTC, matches rosbag scene.json first pose):
  first frame -> 2026-03-24 05:05:48.518
  later frames advance 1:1 with video PTS (no end clamp)
When the source video loops, the mapped timeline restarts from the first frame.

Examples:
    powershell -ExecutionPolicy Bypass -File backend/tests/start_rtsp_server.ps1
    python backend/tests/generate_rtsp_stream.py
    python backend/tests/generate_rtsp_stream.py --mode quad
    python backend/tests/generate_rtsp_stream.py --video backend/tests/rtsp_test/inspection.mp4
    python backend/tests/generate_rtsp_stream.py --no-time-stream

Default input: first video file in backend/tests/rtsp_test/
Default output: rtsp://127.0.0.1:18554/live (+ /time)
"""

from __future__ import annotations

import argparse
import os
import socket
import shutil
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18554
DEFAULT_PATH = "live"
DEFAULT_TIME_PATH = "time"
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720
DEFAULT_TIME_WIDTH = 640
DEFAULT_TIME_HEIGHT = 360
DEFAULT_FPS = 25
DEFAULT_VIDEO_DIR = Path(__file__).resolve().parent / "rtsp_test"
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".ts", ".m4v"}
DEFAULT_FONT_CANDIDATES = (
    Path("C:/Windows/Fonts/arial.ttf"),
    Path("C:/Windows/Fonts/segoeui.ttf"),
    Path("C:/Windows/Fonts/msyh.ttc"),
)

# Align with rosbag-derived scene.json first trajectory timestamp.
DEFAULT_TIME_START = "2026-03-24 05:05:48.518"


def resolve_ffmpeg_bin() -> str:
    configured = os.environ.get("FFMPEG_BIN", "").strip()
    if configured:
        return configured
    discovered = shutil.which("ffmpeg")
    if discovered:
        return discovered
    raise RuntimeError("FFmpeg not found. Install FFmpeg or set FFMPEG_BIN.")


def build_rtsp_url(host: str, port: int, path: str) -> str:
    normalized_path = path.strip("/") or DEFAULT_PATH
    return f"rtsp://{host}:{port}/{normalized_path}"


def check_rtsp_server(host: str, port: int, *, timeout: float = 1.5) -> None:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return
    except OSError as exc:
        raise RuntimeError(
            f"No RTSP server is listening at {host}:{port}. "
            "Start one first, for example: "
            "powershell -ExecutionPolicy Bypass -File backend/tests/start_rtsp_server.ps1"
        ) from exc


def resolve_video_path(video: Path | None, video_dir: Path) -> Path:
    if video is not None:
        path = video.expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Video file not found: {path}")
        return path

    directory = video_dir.expanduser().resolve()
    if not directory.is_dir():
        raise FileNotFoundError(f"Video directory not found: {directory}")

    candidates = sorted(
        p for p in directory.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    )
    if not candidates:
        raise FileNotFoundError(
            f"No video files ({', '.join(sorted(VIDEO_EXTENSIONS))}) found in {directory}"
        )
    return candidates[0]


def resolve_drawtext_font() -> Path | None:
    configured = os.environ.get("RTSP_TEST_FONTFILE", "").strip()
    if configured:
        path = Path(configured).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"RTSP_TEST_FONTFILE does not exist: {path}")
        return path

    for candidate in DEFAULT_FONT_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def parse_timeline_timestamp(value: str) -> datetime:
    text = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(
        f"Invalid timeline timestamp {value!r}. Expected UTC like '2026-03-24 05:05:48.518'."
    )


def timeline_unix_seconds(value: str | datetime) -> float:
    moment = value if isinstance(value, datetime) else parse_timeline_timestamp(value)
    return moment.timestamp()


def probe_media_duration_seconds(path: Path, *, ffmpeg_bin: str) -> float:
    ffprobe_bin = Path(ffmpeg_bin).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    probe = str(ffprobe_bin) if ffprobe_bin.is_file() else shutil.which("ffprobe")
    if not probe:
        raise RuntimeError("ffprobe not found. Install FFmpeg/ffprobe to map looping timestamps.")

    completed = subprocess.run(
        [
            probe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"ffprobe failed for {path}: {detail or completed.returncode}")

    raw = (completed.stdout or "").strip()
    try:
        duration = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"ffprobe returned a non-numeric duration for {path}: {raw!r}") from exc
    if duration <= 0:
        raise RuntimeError(f"Invalid media duration for {path}: {duration}")
    return duration


def _escape_drawtext_fontfile(path: Path | None) -> str:
    if path is None:
        return ""
    escaped = path.as_posix().replace(":", r"\:").replace("'", r"\'")
    return f":fontfile='{escaped}'"


def _drawtext_mapped_clock_expr() -> str:
    """Human-readable UTC clock from remapped PTS.

    Use default ``pts:gmtime`` formatting. Custom strftime (e.g. ``%H:%M:%S``)
    is treated as a numeric delta in FFmpeg 8+ and also breaks filtergraph
    parsing when ':' is escaped on Windows.
    """
    return "%{pts\\:gmtime}"


def _drawtext_unix_seconds_expr() -> str:
    """Remapped timeline as unix seconds (PTS after setpts)."""
    return "%{pts}"


def _mapped_timeline_setpts(*, start_s: float, duration_s: float | None = None) -> str:
    """Remap PTS so t=0 maps to start_s and time advances 1:1 with media PTS.

    When duration_s is set (looping file sources), wrap with mod so each replay
    restarts at start_s.

    Only use this for drawtext overlays. Always follow with
    ``_encode_timeline_setpts`` before encoding, otherwise FFmpeg treats the
    absolute unix PTS as a multi-year frame gap ("frame duplication too large").
    """
    if duration_s is None:
        return f"setpts=({start_s:.6f}+PTS*TB)/TB"
    safe_duration = max(float(duration_s), 1e-6)
    return f"setpts=({start_s:.6f}+mod(PTS*TB\\,{safe_duration:.6f}))/TB"


def _encode_timeline_setpts(*, fps: int) -> str:
    """Restore a normal CFR encode timeline after absolute display remapping."""
    rate = max(int(fps), 1)
    return f"setpts=N/{rate}/TB"


def _labeled_testsrc(label: str, *, width: int, height: int, fps: int) -> str:
    """Test pattern with a static label only (timeline clock is applied later)."""
    safe_label = label.replace(":", r"\:").replace("'", r"\'")
    fontfile = _escape_drawtext_fontfile(resolve_drawtext_font())
    return (
        f"testsrc=size={width}x{height}:rate={fps},"
        f"drawtext=text='{safe_label}':x=24:y=24:fontsize=36:fontcolor=white{fontfile}:"
        f"box=1:boxcolor=black@0.45"
    )


def _video_encode_args(*, fps: int) -> list[str]:
    rate = max(int(fps), 1)
    return [
        "-r",
        str(rate),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-pix_fmt",
        "yuv420p",
        "-g",
        str(max(1, rate * 2)),
        "-fps_mode",
        "cfr",
        "-an",
    ]


def _rtsp_publish_args(rtsp_url: str) -> list[str]:
    return ["-f", "rtsp", "-rtsp_transport", "tcp", rtsp_url]


def _time_channel_filter(
    *,
    input_label: str,
    width: int,
    height: int,
    fontfile: str,
    start_s: float,
    duration_s: float | None,
    fps: int,
) -> str:
    """Build a filter that turns a video pad into a looping mapped-timeline time channel."""
    display_pts = _mapped_timeline_setpts(start_s=start_s, duration_s=duration_s)
    encode_pts = _encode_timeline_setpts(fps=fps)
    clock = _drawtext_mapped_clock_expr()
    unix_s = _drawtext_unix_seconds_expr()
    return (
        f"[{input_label}]{display_pts},"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        f"eq=brightness=-0.35:saturation=0.35,"
        f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.35:t=fill,"
        f"drawtext=text='TIME SYNC':x=24:y=20:fontsize=28:fontcolor=white{fontfile}:"
        f"box=1:boxcolor=black@0.55,"
        f"drawtext=text='{clock}':"
        f"x=(w-text_w)/2:y=(h-text_h)/2-18:fontsize=40:fontcolor=yellow{fontfile},"
        f"drawtext=text='unix_s {unix_s}':"
        f"x=(w-text_w)/2:y=(h-text_h)/2+28:fontsize=30:fontcolor=cyan{fontfile},"
        f"{encode_pts},"
        f"drawtext=text='enc PTS %{{pts\\:hms}}  n=%{{n}}':"
        f"x=24:y=h-36:fontsize=22:fontcolor=white{fontfile}[tout]"
    )


def _video_overlay_chain(
    *,
    width: int,
    height: int,
    fontfile: str,
    start_s: float,
    duration_s: float | None,
    fps: int,
) -> str:
    display_pts = _mapped_timeline_setpts(start_s=start_s, duration_s=duration_s)
    encode_pts = _encode_timeline_setpts(fps=fps)
    clock = _drawtext_mapped_clock_expr()
    return (
        f"{display_pts},"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        f"drawtext=text='{clock}':"
        f"x=24:y=h-48:fontsize=28:fontcolor=yellow{fontfile},"
        f"{encode_pts}"
    )


def _append_dual_outputs(
    command: list[str],
    *,
    fps: int,
    video_rtsp_url: str,
    time_rtsp_url: str | None,
) -> list[str]:
    command.extend(["-map", "[vout]", *_video_encode_args(fps=fps), *_rtsp_publish_args(video_rtsp_url)])
    if time_rtsp_url:
        command.extend(["-map", "[tout]", *_video_encode_args(fps=fps), *_rtsp_publish_args(time_rtsp_url)])
    return command


def build_ffmpeg_command(
    *,
    ffmpeg_bin: str,
    mode: str,
    rtsp_url: str,
    width: int,
    height: int,
    fps: int,
    video_path: Path | None,
    time_rtsp_url: str | None = None,
    time_width: int = DEFAULT_TIME_WIDTH,
    time_height: int = DEFAULT_TIME_HEIGHT,
    time_start: str = DEFAULT_TIME_START,
    loop_duration_s: float | None = None,
) -> list[str]:
    fontfile = _escape_drawtext_fontfile(resolve_drawtext_font())
    if not fontfile and os.name == "nt":
        raise RuntimeError(
            "No TrueType font found for FFmpeg drawtext on Windows. "
            "Install Arial or set RTSP_TEST_FONTFILE to a .ttf/.ttc path."
        )
    include_time = bool(time_rtsp_url)
    start_s = timeline_unix_seconds(time_start)

    if loop_duration_s is not None:
        duration_s: float | None = float(loop_duration_s)
    elif mode == "video":
        if video_path is None:
            raise ValueError("video mode requires --video")
        duration_s = probe_media_duration_seconds(video_path, ffmpeg_bin=ffmpeg_bin)
    else:
        duration_s = None

    if duration_s is not None and duration_s <= 0:
        raise ValueError(f"loop duration must be positive, got {duration_s}")

    if mode == "testsrc":
        if include_time:
            video_chain = _video_overlay_chain(
                width=width,
                height=height,
                fontfile=fontfile,
                start_s=start_s,
                duration_s=duration_s,
                fps=fps,
            )
            filter_complex = (
                f"[0:v]split=2[vraw][traw];"
                f"[vraw]{video_chain}[vout];"
                f"{_time_channel_filter(input_label='traw', width=time_width, height=time_height, fontfile=fontfile, start_s=start_s, duration_s=duration_s, fps=fps)}"
            )
            command = [
                ffmpeg_bin,
                "-hide_banner",
                "-loglevel",
                "info",
                "-re",
                "-f",
                "lavfi",
                "-i",
                _labeled_testsrc("TEST STREAM", width=width, height=height, fps=fps),
                "-filter_complex",
                filter_complex,
            ]
            return _append_dual_outputs(
                command, fps=fps, video_rtsp_url=rtsp_url, time_rtsp_url=time_rtsp_url
            )

        mapped_src = (
            f"testsrc=size={width}x{height}:rate={fps},"
            f"{_mapped_timeline_setpts(start_s=start_s, duration_s=duration_s)},"
            f"drawtext=text='TEST STREAM':x=24:y=24:fontsize=36:fontcolor=white{fontfile}:box=1:boxcolor=black@0.45,"
            f"drawtext=text='{_drawtext_mapped_clock_expr()}':x=24:y=h-48:fontsize=28:fontcolor=yellow{fontfile},"
            f"{_encode_timeline_setpts(fps=fps)}"
        )
        return [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "info",
            "-re",
            "-f",
            "lavfi",
            "-i",
            mapped_src,
            *_video_encode_args(fps=fps),
            *_rtsp_publish_args(rtsp_url),
        ]

    if mode == "quad":
        tile_w = max(2, width // 2)
        tile_h = max(2, height // 2)
        mosaic = (
            f"[0:v]drawtext=text='FRONT':x=24:y=24:fontsize=32:fontcolor=white{fontfile}:box=1:boxcolor=black@0.45[v0];"
            f"[1:v]drawtext=text='REAR':x=24:y=24:fontsize=32:fontcolor=white{fontfile}:box=1:boxcolor=black@0.45[v1];"
            f"[2:v]drawtext=text='LEFT':x=24:y=24:fontsize=32:fontcolor=white{fontfile}:box=1:boxcolor=black@0.45[v2];"
            f"[3:v]drawtext=text='RIGHT':x=24:y=24:fontsize=32:fontcolor=white{fontfile}:box=1:boxcolor=black@0.45[v3];"
            "[v0][v1]hstack=inputs=2[top];"
            "[v2][v3]hstack=inputs=2[bottom];"
            "[top][bottom]vstack=inputs=2"
        )
        if include_time:
            video_chain = _video_overlay_chain(
                width=width,
                height=height,
                fontfile=fontfile,
                start_s=start_s,
                duration_s=duration_s,
                fps=fps,
            )
            filter_complex = (
                f"{mosaic}[mosaic];"
                f"[mosaic]split=2[vraw][traw];"
                f"[vraw]{video_chain}[vout];"
                f"{_time_channel_filter(input_label='traw', width=time_width, height=time_height, fontfile=fontfile, start_s=start_s, duration_s=duration_s, fps=fps)}"
            )
            command = [
                ffmpeg_bin,
                "-hide_banner",
                "-loglevel",
                "info",
                "-re",
                "-f",
                "lavfi",
                "-i",
                f"testsrc=size={tile_w}x{tile_h}:rate={fps}",
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=size={tile_w}x{tile_h}:rate={fps}",
                "-f",
                "lavfi",
                "-i",
                f"haldclutsrc=8,scale={tile_w}:{tile_h},fps={fps}",
                "-f",
                "lavfi",
                "-i",
                f"color=c=0x224466:s={tile_w}x{tile_h}:rate={fps}",
                "-filter_complex",
                filter_complex,
            ]
            return _append_dual_outputs(
                command, fps=fps, video_rtsp_url=rtsp_url, time_rtsp_url=time_rtsp_url
            )

        filter_complex = f"{mosaic}[outv]"
        return [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "info",
            "-re",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size={tile_w}x{tile_h}:rate={fps}",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={tile_w}x{tile_h}:rate={fps}",
            "-f",
            "lavfi",
            "-i",
            f"haldclutsrc=8,scale={tile_w}:{tile_h},fps={fps}",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x224466:s={tile_w}x{tile_h}:rate={fps}",
            "-filter_complex",
            filter_complex,
            "-map",
            "[outv]",
            *_video_encode_args(fps=fps),
            *_rtsp_publish_args(rtsp_url),
        ]

    if mode == "video":
        if video_path is None:
            raise ValueError("video mode requires --video")
        if not video_path.is_file():
            raise FileNotFoundError(f"Video file not found: {video_path}")

        video_chain = _video_overlay_chain(
            width=width,
            height=height,
            fontfile=fontfile,
            start_s=start_s,
            duration_s=duration_s,
            fps=fps,
        )
        if include_time:
            filter_complex = (
                f"[0:v]split=2[vraw][traw];"
                f"[vraw]{video_chain}[vout];"
                f"{_time_channel_filter(input_label='traw', width=time_width, height=time_height, fontfile=fontfile, start_s=start_s, duration_s=duration_s, fps=fps)}"
            )
            command = [
                ffmpeg_bin,
                "-hide_banner",
                "-loglevel",
                "info",
                "-re",
                "-stream_loop",
                "-1",
                "-i",
                str(video_path),
                "-filter_complex",
                filter_complex,
            ]
            return _append_dual_outputs(
                command, fps=fps, video_rtsp_url=rtsp_url, time_rtsp_url=time_rtsp_url
            )

        return [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "info",
            "-re",
            "-stream_loop",
            "-1",
            "-i",
            str(video_path),
            "-vf",
            video_chain,
            *_video_encode_args(fps=fps),
            *_rtsp_publish_args(rtsp_url),
        ]

    raise ValueError(f"Unsupported mode: {mode}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a local RTSP test video stream plus a synced looping time channel."
    )
    parser.add_argument(
        "--mode",
        choices=("testsrc", "quad", "video"),
        default="video",
        help="video: loop rtsp_test video (default); testsrc: test pattern; quad: 2x2 mosaic",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"RTSP bind host (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"RTSP port (default: {DEFAULT_PORT})")
    parser.add_argument("--path", default=DEFAULT_PATH, help=f"Video RTSP path (default: {DEFAULT_PATH})")
    parser.add_argument(
        "--time-path",
        default=DEFAULT_TIME_PATH,
        help=f"Synced time-channel RTSP path (default: {DEFAULT_TIME_PATH})",
    )
    parser.add_argument(
        "--time-start",
        default=DEFAULT_TIME_START,
        help=f"UTC timestamp for the first video frame (default: {DEFAULT_TIME_START})",
    )
    parser.add_argument(
        "--no-time-stream",
        action="store_true",
        help="Publish only the video path (disable the synced time channel).",
    )
    parser.add_argument(
        "--skip-server-check",
        action="store_true",
        help="Skip the preflight TCP check for an RTSP server.",
    )
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH, help=f"Output width (default: {DEFAULT_WIDTH})")
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT, help=f"Output height (default: {DEFAULT_HEIGHT})")
    parser.add_argument(
        "--time-width",
        type=int,
        default=DEFAULT_TIME_WIDTH,
        help=f"Time-channel width (default: {DEFAULT_TIME_WIDTH})",
    )
    parser.add_argument(
        "--time-height",
        type=int,
        default=DEFAULT_TIME_HEIGHT,
        help=f"Time-channel height (default: {DEFAULT_TIME_HEIGHT})",
    )
    parser.add_argument("--fps", type=int, default=DEFAULT_FPS, help=f"Frame rate (default: {DEFAULT_FPS})")
    parser.add_argument(
        "--video",
        type=Path,
        help=f"Video file for --mode video (default: first file in {DEFAULT_VIDEO_DIR.name}/)",
    )
    parser.add_argument(
        "--video-dir",
        type=Path,
        default=DEFAULT_VIDEO_DIR,
        help=f"Folder to pick input video from (default: {DEFAULT_VIDEO_DIR})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        ffmpeg_bin = resolve_ffmpeg_bin()
        rtsp_url = build_rtsp_url(args.host, args.port, args.path)
        time_rtsp_url = (
            None
            if args.no_time_stream
            else build_rtsp_url(args.host, args.port, args.time_path)
        )
        if not args.skip_server_check:
            check_rtsp_server(args.host, args.port)
        video_path = resolve_video_path(args.video, args.video_dir) if args.mode == "video" else args.video
        start_s = timeline_unix_seconds(args.time_start)
        command = build_ffmpeg_command(
            ffmpeg_bin=ffmpeg_bin,
            mode=args.mode,
            rtsp_url=rtsp_url,
            width=args.width,
            height=args.height,
            fps=args.fps,
            video_path=video_path,
            time_rtsp_url=time_rtsp_url,
            time_width=args.time_width,
            time_height=args.time_height,
            time_start=args.time_start,
        )
    except (RuntimeError, ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print("Starting RTSP test stream")
    print(f"  mode   : {args.mode}")
    if args.mode == "video" and video_path is not None:
        print(f"  input  : {video_path}")
    print(f"  ffmpeg : {ffmpeg_bin}")
    print(f"  video  : {rtsp_url}")
    if time_rtsp_url:
        print(f"  time   : {time_rtsp_url}  (start-anchored timeline, loops with video)")
    else:
        print("  time   : disabled")
    print(f"  size   : {args.width}x{args.height} @ {args.fps} fps")
    print(f"  t0     : {args.time_start} UTC  ({int(start_s * 1000)} ms)")
    print("  clock  : +1s media time => +1s timeline (no end clamp)")
    print("")
    print("Press Ctrl+C to stop.")
    print("")

    process = subprocess.Popen(command)

    def stop_stream(*_args: object) -> None:
        if process.poll() is None:
            process.terminate()

    signal.signal(signal.SIGINT, stop_stream)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop_stream)

    try:
        return process.wait()
    except KeyboardInterrupt:
        stop_stream()
        return process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
