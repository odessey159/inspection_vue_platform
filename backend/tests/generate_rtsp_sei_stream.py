#!/usr/bin/env python3
"""Default local RTSP test publisher: H.264 pose SEI on ``/live``.

Each encoded picture gets one ``user_data_unregistered`` SEI matching
``app.services.rtsp_sei`` / ``rtsp_sei_view.py``::

    >Qfff = timestamp_ns, x, y, yaw

Timestamps follow the same mapped scene epoch as the barcode publisher
(first frame 2026-03-24 05:05:48.518 UTC). Pose is interpolated from
``backend/tests/pcd/scene.json`` when present.

The barcode publisher ``generate_rtsp_stream.py`` remains the fallback.

Examples:
    powershell -ExecutionPolicy Bypass -File backend/tests/start_rtsp_server.ps1
    python backend/tests/generate_rtsp_sei_stream.py
    python backend/tests/generate_rtsp_sei_stream.py --mode testsrc --no-scene
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import mmap
import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent
BACKEND_ROOT = TESTS_DIR.parent
REPO_ROOT = BACKEND_ROOT.parent
DEFAULT_SCENE_PATH = TESTS_DIR / "pcd" / "scene.json"

if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.rtsp_sei import AnnexBPoseSeiInjector, FrameMetadata  # noqa: E402

_BARCODE_SPEC = importlib.util.spec_from_file_location(
    "generate_rtsp_stream",
    TESTS_DIR / "generate_rtsp_stream.py",
)
assert _BARCODE_SPEC is not None and _BARCODE_SPEC.loader is not None
barcode_stream = importlib.util.module_from_spec(_BARCODE_SPEC)
_BARCODE_SPEC.loader.exec_module(barcode_stream)


@dataclass(frozen=True)
class ScenePoseTable:
    timestamps_ms: tuple[int, ...]
    xs: tuple[float, ...]
    ys: tuple[float, ...]
    yaws: tuple[float, ...]

    def at_ms(self, timestamp_ms: int) -> tuple[float, float, float]:
        if not self.timestamps_ms:
            return 0.0, 0.0, 0.0
        if timestamp_ms <= self.timestamps_ms[0]:
            return self.xs[0], self.ys[0], self.yaws[0]
        if timestamp_ms >= self.timestamps_ms[-1]:
            return self.xs[-1], self.ys[-1], self.yaws[-1]
        high = _bisect_right(self.timestamps_ms, timestamp_ms)
        low = high - 1
        before_ts = self.timestamps_ms[low]
        after_ts = self.timestamps_ms[high]
        span = max(1, after_ts - before_ts)
        ratio = (timestamp_ms - before_ts) / span
        yaw = _lerp_angle(self.yaws[low], self.yaws[high], ratio)
        x = self.xs[low] + (self.xs[high] - self.xs[low]) * ratio
        y = self.ys[low] + (self.ys[high] - self.ys[low]) * ratio
        return x, y, yaw


def _bisect_right(values: tuple[int, ...], target: int) -> int:
    low = 0
    high = len(values)
    while low < high:
        mid = (low + high) // 2
        if values[mid] <= target:
            low = mid + 1
        else:
            high = mid
    return low


def _lerp_angle(before: float, after: float, ratio: float) -> float:
    delta = ((after - before + math.pi) % (2 * math.pi)) - math.pi
    return before + delta * ratio


def yaw_from_quaternion(qx: float, qy: float, qz: float, qw: float) -> float:
    return math.atan2(2.0 * (qw * qz + qx * qy), 1.0 - 2.0 * (qy * qy + qz * qz))


def frame_timestamp_ns(frame_index: int, *, start_s: float, fps: int, duration_s: float | None) -> int:
    media_s = frame_index / max(int(fps), 1)
    if duration_s is not None and duration_s > 0:
        media_s = media_s % duration_s
    timestamp_ms = int(round((start_s + media_s) * 1000.0))
    return timestamp_ms * 1_000_000


def metadata_for_frame(
    frame_index: int,
    *,
    start_s: float,
    fps: int,
    duration_s: float | None,
    poses: ScenePoseTable | None,
) -> FrameMetadata:
    timestamp_ns = frame_timestamp_ns(frame_index, start_s=start_s, fps=fps, duration_s=duration_s)
    if poses is None:
        x = y = yaw = 0.0
    else:
        x, y, yaw = poses.at_ms(timestamp_ns // 1_000_000)
    return FrameMetadata(timestamp_ns=timestamp_ns, x=x, y=y, yaw=yaw)


def load_scene_pose_table(path: Path) -> ScenePoseTable:
    timestamps = _mmap_json_array(path, b'"trajectory_timestamps"')
    trajectory = _mmap_json_array(path, b'"trajectory"')
    orientations = _mmap_json_array(path, b'"trajectory_orientations"')
    if not isinstance(timestamps, list) or not timestamps:
        raise RuntimeError(f"No trajectory_timestamps in {path}")
    if not isinstance(trajectory, list) or len(trajectory) != len(timestamps):
        raise RuntimeError(f"trajectory length does not match timestamps in {path}")

    xs: list[float] = []
    ys: list[float] = []
    yaws: list[float] = []
    for index, point in enumerate(trajectory):
        if not isinstance(point, list) or len(point) < 2:
            raise RuntimeError(f"Invalid trajectory point at {index} in {path}")
        xs.append(float(point[0]))
        ys.append(float(point[1]))
        if isinstance(orientations, list) and index < len(orientations):
            quat = orientations[index]
            if isinstance(quat, list) and len(quat) >= 4:
                yaws.append(yaw_from_quaternion(float(quat[0]), float(quat[1]), float(quat[2]), float(quat[3])))
                continue
        if index > 0:
            yaws.append(math.atan2(ys[index] - ys[index - 1], xs[index] - xs[index - 1]))
        else:
            yaws.append(0.0)
    if len(yaws) >= 2 and yaws[0] == 0.0:
        yaws[0] = math.atan2(ys[1] - ys[0], xs[1] - xs[0])
    return ScenePoseTable(
        timestamps_ms=tuple(int(value) for value in timestamps),
        xs=tuple(xs),
        ys=tuple(ys),
        yaws=tuple(yaws[: len(xs)]),
    )


def _mmap_json_array(path: Path, key: bytes) -> object | None:
    with path.open("rb") as handle:
        mapped = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            marker = mapped.find(key)
            if marker < 0:
                return None
            colon = mapped.find(b":", marker + len(key))
            if colon < 0:
                return None
            start = mapped.find(b"[", colon)
            if start < 0:
                return None
            depth = 0
            cursor = start
            length = mapped.size()
            while cursor < length:
                byte = mapped[cursor]
                if byte == 0x5B:  # [
                    depth += 1
                elif byte == 0x5D:  # ]
                    depth -= 1
                    if depth == 0:
                        return json.loads(mapped[start : cursor + 1])
                cursor += 1
            return None
        finally:
            mapped.close()


def resolve_scene_path(scene: Path | None, *, disable: bool) -> Path | None:
    if disable:
        return None
    if scene is not None:
        path = scene.expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Scene file not found: {path}")
        return path
    if DEFAULT_SCENE_PATH.is_file():
        return DEFAULT_SCENE_PATH
    return None


def _h264_encode_args(*, fps: int) -> list[str]:
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
        "-bf",
        "0",
        "-x264-params",
        "aud=1:repeat-headers=1:bframes=0",
        "-fps_mode",
        "cfr",
        "-an",
        "-f",
        "h264",
        "pipe:1",
    ]


def build_encode_command(
    *,
    ffmpeg_bin: str,
    mode: str,
    width: int,
    height: int,
    fps: int,
    video_path: Path | None,
    time_start: str = barcode_stream.DEFAULT_TIME_START,
    loop_duration_s: float | None = None,
) -> list[str]:
    """Build ffmpeg command that encodes Annex-B H.264 to stdout (no RTSP)."""
    fontfile = barcode_stream._escape_drawtext_fontfile(barcode_stream.resolve_drawtext_font())
    if not fontfile and os.name == "nt":
        raise RuntimeError(
            "No TrueType font found for FFmpeg drawtext on Windows. "
            "Install Arial or set RTSP_TEST_FONTFILE to a .ttf/.ttc path."
        )
    start_s = barcode_stream.timeline_unix_seconds(time_start)
    duration_s = _resolve_loop_duration(
        mode=mode,
        video_path=video_path,
        ffmpeg_bin=ffmpeg_bin,
        loop_duration_s=loop_duration_s,
    )
    video_chain = barcode_stream._video_overlay_chain(
        width=width,
        height=height,
        fontfile=fontfile,
        start_s=start_s,
        duration_s=duration_s,
        fps=fps,
        include_barcode=False,
    )
    encode_args = _h264_encode_args(fps=fps)

    if mode == "testsrc":
        return [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "info",
            "-re",
            "-f",
            "lavfi",
            "-i",
            barcode_stream._labeled_testsrc("SEI STREAM", width=width, height=height, fps=fps),
            "-vf",
            video_chain,
            *encode_args,
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
            f"[top][bottom]vstack=inputs=2,{video_chain}[vout]"
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
            mosaic,
            "-map",
            "[vout]",
            *encode_args,
        ]

    if mode == "video":
        if video_path is None:
            raise ValueError("video mode requires --video")
        if not video_path.is_file():
            raise FileNotFoundError(f"Video file not found: {video_path}")
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
            *encode_args,
        ]

    raise ValueError(f"Unsupported mode: {mode}")


def build_publish_command(*, ffmpeg_bin: str, fps: int, rtsp_url: str) -> list[str]:
    rate = max(int(fps), 1)
    return [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "info",
        "-fflags",
        "+genpts",
        "-f",
        "h264",
        "-r",
        str(rate),
        "-i",
        "pipe:0",
        "-an",
        "-c:v",
        "copy",
        "-f",
        "rtsp",
        "-rtsp_transport",
        "tcp",
        rtsp_url,
    ]


def _resolve_loop_duration(
    *,
    mode: str,
    video_path: Path | None,
    ffmpeg_bin: str,
    loop_duration_s: float | None,
) -> float | None:
    if loop_duration_s is not None:
        duration_s = float(loop_duration_s)
        if duration_s <= 0:
            raise ValueError(f"loop duration must be positive, got {duration_s}")
        return duration_s
    if mode == "video":
        if video_path is None:
            raise ValueError("video mode requires --video")
        return barcode_stream.probe_media_duration_seconds(video_path, ffmpeg_bin=ffmpeg_bin)
    return None


def _pump_stderr(process: subprocess.Popen[bytes], label: str, stop: threading.Event) -> None:
    stream = process.stderr
    if stream is None:
        return
    for raw in stream:
        if stop.is_set():
            break
        line = raw.decode("utf-8", errors="replace").rstrip() if isinstance(raw, (bytes, bytearray)) else str(raw).rstrip()
        if line:
            print(f"[{label}] {line}", flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Publish a local RTSP test stream with per-frame H.264 pose SEI."
    )
    parser.add_argument(
        "--mode",
        choices=("testsrc", "quad", "video"),
        default="video",
        help="video: loop rtsp_test video (default); testsrc: test pattern; quad: 2x2 mosaic",
    )
    parser.add_argument("--host", default=barcode_stream.DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=barcode_stream.DEFAULT_PORT)
    parser.add_argument("--path", default=barcode_stream.DEFAULT_PATH)
    parser.add_argument("--time-start", default=barcode_stream.DEFAULT_TIME_START)
    parser.add_argument("--skip-server-check", action="store_true")
    parser.add_argument("--width", type=int, default=barcode_stream.DEFAULT_WIDTH)
    parser.add_argument("--height", type=int, default=barcode_stream.DEFAULT_HEIGHT)
    parser.add_argument("--fps", type=int, default=barcode_stream.DEFAULT_FPS)
    parser.add_argument("--video", type=Path)
    parser.add_argument("--video-dir", type=Path, default=barcode_stream.DEFAULT_VIDEO_DIR)
    parser.add_argument(
        "--scene",
        type=Path,
        help=f"scene.json used for x/y/yaw (default: {DEFAULT_SCENE_PATH} if present)",
    )
    parser.add_argument(
        "--no-scene",
        action="store_true",
        help="Do not load a map; SEI pose fields stay 0.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    encoder: subprocess.Popen[bytes] | None = None
    publisher: subprocess.Popen[bytes] | None = None
    stop = threading.Event()

    try:
        ffmpeg_bin = barcode_stream.resolve_ffmpeg_bin()
        rtsp_url = barcode_stream.build_rtsp_url(args.host, args.port, args.path)
        if not args.skip_server_check:
            barcode_stream.check_rtsp_server(args.host, args.port)
        video_path = (
            barcode_stream.resolve_video_path(args.video, args.video_dir) if args.mode == "video" else args.video
        )
        start_s = barcode_stream.timeline_unix_seconds(args.time_start)
        duration_s = _resolve_loop_duration(
            mode=args.mode,
            video_path=video_path,
            ffmpeg_bin=ffmpeg_bin,
            loop_duration_s=None,
        )
        scene_path = resolve_scene_path(args.scene, disable=args.no_scene)
        poses = load_scene_pose_table(scene_path) if scene_path is not None else None
        encode_command = build_encode_command(
            ffmpeg_bin=ffmpeg_bin,
            mode=args.mode,
            width=args.width,
            height=args.height,
            fps=args.fps,
            video_path=video_path,
            time_start=args.time_start,
        )
        publish_command = build_publish_command(ffmpeg_bin=ffmpeg_bin, fps=args.fps, rtsp_url=rtsp_url)
    except (RuntimeError, ValueError, FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print("Starting RTSP SEI test stream")
    print(f"  mode   : {args.mode}")
    if args.mode == "video" and video_path is not None:
        print(f"  input  : {video_path}")
    print(f"  ffmpeg : {ffmpeg_bin}")
    print(f"  video  : {rtsp_url}")
    print("  sei    : user_data_unregistered >Qfff (timestamp_ns, x, y, yaw)")
    if scene_path is not None and poses is not None:
        print(f"  pose   : {scene_path} ({len(poses.timestamps_ms)} samples)")
    else:
        print("  pose   : 0,0,0 (no scene)")
    print(f"  size   : {args.width}x{args.height} @ {args.fps} fps")
    print(f"  t0     : {args.time_start} UTC  ({int(start_s * 1000)} ms)")
    print("  alt    : python backend/tests/generate_rtsp_stream.py  (barcode /time)")
    print("")
    print("Press Ctrl+C to stop.")
    print("")

    def metadata_cb(frame_index: int) -> FrameMetadata:
        return metadata_for_frame(
            frame_index,
            start_s=start_s,
            fps=args.fps,
            duration_s=duration_s,
            poses=poses,
        )

    injector = AnnexBPoseSeiInjector(metadata_cb)

    try:
        encoder = subprocess.Popen(
            encode_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        publisher = subprocess.Popen(
            publish_command,
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            bufsize=0,
        )
    except OSError as exc:
        print(f"error: failed to start ffmpeg: {exc}", file=sys.stderr)
        return 1

    assert encoder.stdout is not None
    assert publisher.stdin is not None

    threads = [
        threading.Thread(target=_pump_stderr, args=(encoder, "encode", stop), daemon=True),
        threading.Thread(target=_pump_stderr, args=(publisher, "publish", stop), daemon=True),
    ]
    for thread in threads:
        thread.start()

    def stop_stream(*_args: object) -> None:
        stop.set()
        if encoder is not None and encoder.poll() is None:
            encoder.terminate()
        if publisher is not None and publisher.poll() is None:
            publisher.terminate()

    signal.signal(signal.SIGINT, stop_stream)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop_stream)

    try:
        stdout = encoder.stdout
        stdin = publisher.stdin
        try:
            while not stop.is_set():
                chunk = stdout.read(64 * 1024)
                if not chunk:
                    break
                injected = injector.feed(chunk)
                if injected:
                    stdin.write(injected)
                    stdin.flush()
            remaining = injector.flush()
            if remaining:
                stdin.write(remaining)
                stdin.flush()
            stdin.close()
        except (BrokenPipeError, OSError):
            stop.set()
        encoder.wait()
        publisher.wait()
        if publisher.returncode not in (0, None) and not stop.is_set():
            return publisher.returncode or 1
        if encoder.returncode not in (0, None) and not stop.is_set():
            return encoder.returncode or 1
        return 0
    except KeyboardInterrupt:
        stop_stream()
        if encoder is not None:
            encoder.wait()
        if publisher is not None:
            publisher.wait()
        return 0
    finally:
        stop.set()


if __name__ == "__main__":
    raise SystemExit(main())
