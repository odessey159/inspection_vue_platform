#!/usr/bin/env python3
"""Continuously display an RTSP stream and print its per-frame pose SEI.

The image_uploader sender inserts one H.264 ``user_data_unregistered`` SEI
message before each encoded image.  The SEI payload is decoded by
``app.services.rtsp_sei`` as the following network-byte-order structure::

    >Qfff = timestamp_ns, x, y, yaw

PyAV is required here because OpenCV's ``VideoCapture`` exposes decoded images
but not the compressed H.264 packets that contain the SEI NAL unit.
"""

from __future__ import annotations

import argparse
import datetime
import sys
import time
from collections import OrderedDict, deque
from pathlib import Path
from typing import Deque, Optional

try:
    import av
except ImportError as error:
    raise SystemExit(
        "PyAV is required. In the TidePilot Docker install package python3-av."
    ) from error

try:
    import cv2
except ImportError as error:
    raise SystemExit(
        "OpenCV Python bindings are required. In the TidePilot Docker install "
        "package python3-opencv."
    ) from error

# Canonical parser lives with the inspection backend so RTSP recording and this
# viewer share the same >Qfff user_data_unregistered payload layout.
def _load_sei_parser():
    try:
        from app.services.rtsp_sei import FrameMetadata, parse_packet_metadata

        return FrameMetadata, parse_packet_metadata
    except ImportError:
        backend_root = Path(__file__).resolve().parent / "backend"
        if str(backend_root) not in sys.path:
            sys.path.insert(0, str(backend_root))
        from app.services.rtsp_sei import FrameMetadata, parse_packet_metadata

        return FrameMetadata, parse_packet_metadata


FrameMetadata, parse_packet_metadata = _load_sei_parser()


class MetadataBuffer:
    """Temporarily associate compressed-packet metadata with decoded frames.

    FFmpeg may return the SEI and decoded image at different moments.  A PTS
    lookup provides the exact association when timestamps are available.  The
    caller may associate metadata without PTS only with a frame
    decoded from that same packet.  Metadata is deliberately not carried to a
    later packet because one dropped frame would otherwise shift every later
    pose by one image.
    """

    def __init__(self, maximum_pts_entries: int = 128) -> None:
        self._maximum_pts_entries = maximum_pts_entries
        self._by_pts: "OrderedDict[int, Deque[FrameMetadata]]" = OrderedDict()

    def add(self, packet_pts: int, metadata: FrameMetadata) -> None:
        """Store metadata under the compressed packet's valid PTS."""

        metadata_at_pts = self._by_pts.setdefault(packet_pts, deque())
        metadata_at_pts.append(metadata)
        self._by_pts.move_to_end(packet_pts)

        # Bound memory if packets are lost or a decoder never produces their
        # corresponding frames.
        while len(self._by_pts) > self._maximum_pts_entries:
            self._by_pts.popitem(last=False)

    def _pop_pts(self, pts: Optional[int]) -> Optional[FrameMetadata]:
        if pts is None:
            return None

        metadata_at_pts = self._by_pts.get(pts)
        if not metadata_at_pts:
            return None

        metadata = metadata_at_pts.popleft()
        if not metadata_at_pts:
            del self._by_pts[pts]
        return metadata

    def pop_for_frame(
        self,
        frame_pts: Optional[int],
        packet_pts: Optional[int],
    ) -> Optional[FrameMetadata]:
        """Return only metadata that can be associated with this frame."""

        metadata = self._pop_pts(frame_pts)
        if metadata is None and packet_pts != frame_pts:
            metadata = self._pop_pts(packet_pts)
        return metadata


def format_unix_time(timestamp_ns: int) -> str:
    """Format an epoch-nanosecond value in the receiver's local time zone."""

    try:
        seconds, nanoseconds = divmod(timestamp_ns, 1_000_000_000)
        local_time = datetime.datetime.fromtimestamp(
            seconds, tz=datetime.timezone.utc
        ).astimezone()
    except (OSError, OverflowError, ValueError):
        return "INVALID_TIMESTAMP"

    timezone = local_time.strftime("%z")
    return f"{local_time:%Y-%m-%d %H:%M:%S}.{nanoseconds:09d}{timezone}"


def draw_metadata(image, metadata: Optional[FrameMetadata], latency_ms: float) -> None:
    """Overlay the exact SEI values used for the displayed image."""

    if metadata is None:
        lines = ("SEI: unavailable",)
        color = (0, 0, 255)
    else:
        lines = (
            f"timestamp_ns: {metadata.timestamp_ns}",
            f"x: {metadata.x:.3f}  y: {metadata.y:.3f}  "
            f"yaw: {metadata.yaw:.3f}  latency: {latency_ms:.1f} ms",
        )
        color = (0, 255, 0)

    for line_index, line in enumerate(lines):
        cv2.putText(
            image,
            line,
            (16, 30 + line_index * 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            color,
            2,
            cv2.LINE_AA,
        )


def receive_until_stopped(args: argparse.Namespace) -> bool:
    """Run one RTSP session; return True when the user requested exit."""

    print(f"opening RTSP stream: {args.url}", flush=True)
    container = av.open(
        args.url,
        mode="r",
        options={"rtsp_transport": args.transport},
        timeout=(args.open_timeout, args.read_timeout),
    )
    video_stream = next(
        (stream for stream in container.streams if stream.type == "video"), None
    )
    if video_stream is None:
        container.close()
        raise RuntimeError("RTSP session contains no video stream")

    metadata_buffer = MetadataBuffer()
    decoded_count = 0
    matched_count = 0
    missing_count = 0

    try:
        for packet in container.demux(video_stream):
            packet_data = bytes(packet)
            packet_metadata = (
                parse_packet_metadata(packet_data) if packet_data else ()
            )
            decoded_frames = tuple(packet.decode())
            for metadata in packet_metadata:
                if packet.pts is not None:
                    metadata_buffer.add(packet.pts, metadata)

            # Without PTS there is no durable frame identity.  Association is
            # safe only for the unambiguous case where this packet contains one
            # TidePilot SEI and immediately decodes to one image.
            metadata_without_pts = None
            if (
                packet.pts is None
                and len(packet_metadata) == 1
                and len(decoded_frames) == 1
            ):
                metadata_without_pts = packet_metadata[0]

            for frame in decoded_frames:
                decoded_count += 1
                metadata = metadata_buffer.pop_for_frame(frame.pts, packet.pts)
                if metadata is None and metadata_without_pts is not None:
                    metadata = metadata_without_pts
                    metadata_without_pts = None
                image = frame.to_ndarray(format="bgr24")

                if metadata is None:
                    missing_count += 1
                    latency_ms = float("nan")
                    print(
                        f"frame={decoded_count} timestamp_ns=NO_SEI "
                        "x=NO_SEI y=NO_SEI yaw=NO_SEI",
                        flush=True,
                    )
                else:
                    matched_count += 1
                    latency_ms = (time.time_ns() - metadata.timestamp_ns) / 1.0e6
                    print(
                        f"frame={decoded_count} "
                        f"timestamp_ns={metadata.timestamp_ns} "
                        f"time='{format_unix_time(metadata.timestamp_ns)}' "
                        f"x={metadata.x:.6f} y={metadata.y:.6f} "
                        f"yaw={metadata.yaw:.6f} latency_ms={latency_ms:.1f}",
                        flush=True,
                    )

                if not args.no_overlay:
                    draw_metadata(image, metadata, latency_ms)

                cv2.imshow(args.window_name, image)
                key = cv2.waitKey(1) & 0xFF
                # WND_PROP_VISIBLE is not reliable with the OpenCV GUI backend
                # used in the TidePilot Docker (it reports 0 for an open
                # window), so use keyboard/Ctrl-C termination here.
                if key in (27, ord("q"), ord("Q")):
                    print(
                        f"stopped: decoded={decoded_count} matched={matched_count} "
                        f"missing_sei={missing_count}",
                        flush=True,
                    )
                    return True

        # Reaching EOF on a live RTSP stream means the connection ended.
        raise RuntimeError("RTSP stream ended")
    finally:
        container.close()


def wait_before_reconnect(delay_seconds: float) -> bool:
    """Keep the GUI responsive while waiting; return True for q/Esc."""

    deadline = time.monotonic() + delay_seconds
    while True:
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0.0:
            return False

        wait_ms = max(1, min(100, int(remaining_seconds * 1000.0)))
        key = cv2.waitKey(wait_ms) & 0xFF
        if key in (27, ord("q"), ord("Q")):
            return True


def run(args: argparse.Namespace) -> int:
    """Reconnect after transient failures until q/Esc or Ctrl-C is pressed."""

    try:
        cv2.namedWindow(args.window_name, cv2.WINDOW_NORMAL)
    except cv2.error as error:
        print(f"cannot create OpenCV display window: {error}", file=sys.stderr)
        return 1

    try:
        while True:
            try:
                if receive_until_stopped(args):
                    return 0
            except KeyboardInterrupt:
                return 0
            except Exception as error:
                print(f"RTSP receive error: {error}", file=sys.stderr, flush=True)
                if args.no_reconnect:
                    return 1
                print(
                    f"reconnecting in {args.reconnect_delay:.1f} seconds; "
                    "press Ctrl-C to exit",
                    file=sys.stderr,
                    flush=True,
                )
                try:
                    if wait_before_reconnect(args.reconnect_delay):
                        return 0
                except KeyboardInterrupt:
                    return 0
    finally:
        cv2.destroyAllWindows()


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Continuously display image_uploader RTSP video and print each "
            "frame's timestamp/x/y/yaw H.264 SEI metadata."
        )
    )
    parser.add_argument(
        "--url",
        default="rtsp://10.0.0.2:8554/robot",
        help="RTSP input URL",
    )
    parser.add_argument(
        "--transport",
        choices=("tcp", "udp"),
        default="tcp",
        help="RTSP transport (default: tcp)",
    )
    parser.add_argument(
        "--window-name",
        default="TidePilot RTSP + SEI",
        help="OpenCV display window title",
    )
    parser.add_argument(
        "--reconnect-delay",
        type=float,
        default=2.0,
        help="seconds before reconnecting after stream failure",
    )
    parser.add_argument(
        "--open-timeout",
        type=float,
        default=5.0,
        help="seconds allowed for opening the RTSP connection",
    )
    parser.add_argument(
        "--read-timeout",
        type=float,
        default=5.0,
        help="seconds without RTSP data before reconnecting",
    )
    parser.add_argument(
        "--no-reconnect",
        action="store_true",
        help="exit instead of reconnecting when the stream fails",
    )
    parser.add_argument(
        "--no-overlay",
        action="store_true",
        help="do not draw timestamp and pose over the displayed image",
    )
    args = parser.parse_args()
    if args.reconnect_delay < 0.0:
        parser.error("--reconnect-delay must be non-negative")
    if args.open_timeout <= 0.0:
        parser.error("--open-timeout must be greater than zero")
    if args.read_timeout <= 0.0:
        parser.error("--read-timeout must be greater than zero")
    return args


if __name__ == "__main__":
    sys.exit(run(parse_arguments()))
