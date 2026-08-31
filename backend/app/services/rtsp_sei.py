"""Parse TidePilot-style pose SEI from H.264 / H.265 RTSP bitstreams.

The sender inserts one ``user_data_unregistered`` SEI (NAL type 6 / HEVC
prefix SEI 39) before each encoded picture.  The application payload is
network-endian::

    >Qfff = timestamp_ns, x, y, yaw

A 16-byte ISO/IEC 11578 UUID may precede that struct.  The parser accepts
the documented UUID, any other 16-byte UUID followed by a plausible
timestamp, or a bare 20-byte ``>Qfff`` payload (as described by
``rtsp_sei_view.py``).

Recorded files are walked picture-by-picture so each encoded image has a
matching metadata row (missing SEI still occupies a frame_index slot).
"""

from __future__ import annotations

import logging
import os
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..settings import FFMPEG_BIN


logger = logging.getLogger(__name__)

POSE_SEI_STRUCT = struct.Struct(">Qfff")
POSE_SEI_PAYLOAD_SIZE = POSE_SEI_STRUCT.size
USER_DATA_UNREGISTERED = 5
H264_SEI_NAL_TYPE = 6
H264_AUD_NAL_TYPE = 9
H264_VCL_NAL_TYPES = frozenset({1, 2, 3, 4, 5})
HEVC_PREFIX_SEI = 39
HEVC_SUFFIX_SEI = 40
HEVC_AUD_NAL_TYPE = 35
HEVC_VCL_NAL_TYPES = frozenset(range(32))

# UUID identifying this platform's pose SEI. Unknown UUIDs are still accepted
# when the following 20 bytes look like a capture timestamp plus pose.
POSE_SEI_UUID = bytes.fromhex("7c3a9e104b2f4d819e6a0c1d2e3f4a5b")

# Unix nanoseconds for ~2001-09-09 .. ~2096-10-16 (matches barcode sanity window).
_MIN_TIMESTAMP_NS = 1_000_000_000_000_000_000
_MAX_TIMESTAMP_NS = 4_000_000_000_000_000_000

RTSP_SEI_PROBE_FRAMES = int(os.getenv("RTSP_SEI_PROBE_FRAMES", "30"))
RTSP_SEI_PROBE_TIMEOUT_SEC = float(
    os.getenv("RTSP_SEI_PROBE_TIMEOUT_SEC", os.getenv("RTSP_TIMELINE_PROBE_TIMEOUT_SEC", "12"))
)
RTSP_SEI_EXTRACT_TIMEOUT_SEC = float(os.getenv("RTSP_SEI_EXTRACT_TIMEOUT_SEC", "120"))
RTSP_TRANSPORT = os.getenv("RTSP_TRANSPORT", "tcp").strip() or "tcp"
_RTSP_FFMPEG_RW_TIMEOUT_US = int(os.getenv("RTSP_FFMPEG_RW_TIMEOUT_US", "15000000"))

_MUXER_MISMATCH_MARKERS = (
    "is not suitable",
    "invalid argument",
    "could not find codec",
    "incompatible with",
    "codec not currently supported",
)


@dataclass(frozen=True)
class FrameMetadata:
    timestamp_ns: int
    x: float
    y: float
    yaw: float

    @property
    def timestamp_ms(self) -> int:
        return int(self.timestamp_ns // 1_000_000)


@dataclass(frozen=True)
class PictureMetadata:
    """One encoded picture and the pose SEI attached to that access unit, if any."""

    frame_index: int
    metadata: FrameMetadata | None

    def to_record(self) -> dict[str, int | float | None]:
        pose = self.metadata
        return {
            "frame_index": int(self.frame_index),
            "timestamp_ns": None if pose is None else int(pose.timestamp_ns),
            "timestamp_ms": None if pose is None else int(pose.timestamp_ms),
            "x": None if pose is None else float(pose.x),
            "y": None if pose is None else float(pose.y),
            "yaw": None if pose is None else float(pose.yaw),
        }

    @classmethod
    def from_record(cls, payload: object) -> PictureMetadata | None:
        if not isinstance(payload, dict):
            return None
        try:
            frame_index = int(payload.get("frame_index"))
        except (TypeError, ValueError):
            return None
        timestamp_ns = payload.get("timestamp_ns")
        if timestamp_ns in (None, ""):
            return cls(frame_index=frame_index, metadata=None)
        try:
            pose = FrameMetadata(
                timestamp_ns=int(timestamp_ns),
                x=_coerce_float(payload.get("x")),
                y=_coerce_float(payload.get("y")),
                yaw=_coerce_float(payload.get("yaw")),
            )
        except (TypeError, ValueError):
            return cls(frame_index=frame_index, metadata=None)
        return cls(frame_index=frame_index, metadata=pose)


def parse_packet_metadata(packet_data: bytes) -> tuple[FrameMetadata, ...]:
    """Return every plausible pose SEI found in a compressed packet or dump."""
    if not packet_data:
        return ()

    found: list[FrameMetadata] = []
    seen: set[tuple[int, int, int, int]] = set()

    def _collect(nals: list[bytes]) -> None:
        for nal in nals:
            for metadata in _metadata_from_nal(nal):
                key = (
                    metadata.timestamp_ns,
                    round(metadata.x, 6),
                    round(metadata.y, 6),
                    round(metadata.yaw, 6),
                )
                if key in seen:
                    continue
                seen.add(key)
                found.append(metadata)

    if _has_annexb_start_code(packet_data):
        _collect(list(_iter_annexb_nals(packet_data)))
    if not found:
        _collect(list(_iter_avcc_nals(packet_data, 4)))
    if not found:
        _collect(list(_iter_avcc_nals(packet_data, 2)))
    return tuple(found)


def build_pose_sei_nal(
    *,
    timestamp_ns: int,
    x: float = 0.0,
    y: float = 0.0,
    yaw: float = 0.0,
    uuid_bytes: bytes = POSE_SEI_UUID,
    include_uuid: bool = True,
    hevc: bool = False,
) -> bytes:
    """Build a single SEI NAL (without an Annex-B start code)."""
    user_data = bytes(uuid_bytes[:16] if include_uuid else b"") + POSE_SEI_STRUCT.pack(
        int(timestamp_ns), float(x), float(y), float(yaw)
    )
    rbsp = bytearray()
    rbsp.extend(_encode_sei_uint(USER_DATA_UNREGISTERED))
    rbsp.extend(_encode_sei_uint(len(user_data)))
    rbsp.extend(user_data)
    rbsp.append(0x80)
    ebsp = _rbsp_to_ebsp(bytes(rbsp))
    if hevc:
        return bytes([(HEVC_PREFIX_SEI << 1) & 0xFF, 0x01]) + ebsp
    return bytes([H264_SEI_NAL_TYPE]) + ebsp


def build_pose_sei_annexb(
    *,
    timestamp_ns: int,
    x: float = 0.0,
    y: float = 0.0,
    yaw: float = 0.0,
    uuid_bytes: bytes = POSE_SEI_UUID,
    include_uuid: bool = True,
    hevc: bool = False,
) -> bytes:
    nal = build_pose_sei_nal(
        timestamp_ns=timestamp_ns,
        x=x,
        y=y,
        yaw=yaw,
        uuid_bytes=uuid_bytes,
        include_uuid=include_uuid,
        hevc=hevc,
    )
    return b"\x00\x00\x00\x01" + nal


class AnnexBPoseSeiInjector:
    """Insert one pose SEI in front of each H.264 access unit (live Annex-B)."""

    def __init__(self, metadata_for_frame: Callable[[int], FrameMetadata]) -> None:
        self._metadata_for_frame = metadata_for_frame
        self._buffer = bytearray()
        self.frame_index = 0
        self._awaiting_picture = True
        self._use_aud_boundaries = False

    def feed(self, chunk: bytes) -> bytes:
        if chunk:
            self._buffer.extend(chunk)
        return self._drain(complete_only=True)

    def flush(self) -> bytes:
        emitted = self._drain(complete_only=False)
        self._buffer.clear()
        return emitted

    def _drain(self, *, complete_only: bool) -> bytes:
        starts = find_annexb_start_codes(bytes(self._buffer))
        if not starts:
            if complete_only and len(self._buffer) > 3:
                prefix = bytes(self._buffer[:-3])
                del self._buffer[:-3]
                return prefix
            if complete_only:
                return b""
            leftover = bytes(self._buffer)
            self._buffer.clear()
            return leftover

        output = bytearray()
        first_pos = starts[0][0]
        if first_pos > 0:
            output.extend(self._buffer[:first_pos])

        last_index = len(starts) - 1 if complete_only else len(starts)
        if complete_only and last_index <= 0:
            if first_pos > 0:
                del self._buffer[:first_pos]
            return bytes(output)

        emit_count = last_index if complete_only else len(starts)
        for index in range(emit_count):
            position, start_len = starts[index]
            nal_start = position + start_len
            nal_end = starts[index + 1][0] if index + 1 < len(starts) else len(self._buffer)
            start_code = bytes(self._buffer[position:nal_start])
            nal = bytes(self._buffer[nal_start:nal_end])
            output.extend(self._handle_nal(start_code, nal))

        if complete_only:
            keep_from = starts[-1][0]
            del self._buffer[:keep_from]
        else:
            self._buffer.clear()
        return bytes(output)

    def _handle_nal(self, start_code: bytes, nal: bytes) -> bytes:
        if not nal:
            return start_code
        nal_type = nal[0] & 0x1F
        if nal_type == H264_AUD_NAL_TYPE:
            self._use_aud_boundaries = True
            self._awaiting_picture = True
            return start_code + nal
        if nal_type in H264_VCL_NAL_TYPES:
            prefix = b""
            if self._awaiting_picture:
                metadata = self._metadata_for_frame(self.frame_index)
                prefix = build_pose_sei_annexb(
                    timestamp_ns=metadata.timestamp_ns,
                    x=metadata.x,
                    y=metadata.y,
                    yaw=metadata.yaw,
                )
                self.frame_index += 1
                self._awaiting_picture = False
            if not self._use_aud_boundaries:
                self._awaiting_picture = True
            return prefix + start_code + nal
        return start_code + nal


def inject_pose_sei_annexb(
    packet_data: bytes,
    metadata_for_frame: Callable[[int], FrameMetadata],
) -> bytes:
    """Insert pose SEI before every H.264 picture in a complete Annex-B buffer."""
    injector = AnnexBPoseSeiInjector(metadata_for_frame)
    return injector.feed(packet_data) + injector.flush()


class AnnexBPictureSeiParser:
    """Walk Annex-B access units and pair each picture with its pose SEI.

    Pictures without a pose SEI still emit a row so frame_index stays 1:1 with
    decoded images.  H.264 AUD (or HEVC AUD) marks access-unit boundaries when
    present; otherwise each VCL NAL is treated as one picture.
    """

    def __init__(self, *, hevc: bool = False) -> None:
        self._hevc = hevc
        self._buffer = bytearray()
        self.frame_index = 0
        self._awaiting_picture = True
        self._use_aud_boundaries = False
        self._pending: list[FrameMetadata] = []

    def feed(self, chunk: bytes) -> tuple[PictureMetadata, ...]:
        if chunk:
            self._buffer.extend(chunk)
        return self._drain(complete_only=True)

    def flush(self) -> tuple[PictureMetadata, ...]:
        emitted = self._drain(complete_only=False)
        self._buffer.clear()
        return emitted

    def _drain(self, *, complete_only: bool) -> tuple[PictureMetadata, ...]:
        starts = find_annexb_start_codes(bytes(self._buffer))
        if not starts:
            if complete_only and len(self._buffer) > 3:
                del self._buffer[:-3]
            elif not complete_only:
                self._buffer.clear()
            return ()

        found: list[PictureMetadata] = []
        last_index = len(starts) - 1 if complete_only else len(starts)
        if complete_only and last_index <= 0:
            first_pos = starts[0][0]
            if first_pos > 0:
                del self._buffer[:first_pos]
            return ()

        emit_count = last_index if complete_only else len(starts)
        for index in range(emit_count):
            position, start_len = starts[index]
            nal_start = position + start_len
            nal_end = starts[index + 1][0] if index + 1 < len(starts) else len(self._buffer)
            nal = bytes(self._buffer[nal_start:nal_end])
            picture = self._handle_nal(nal)
            if picture is not None:
                found.append(picture)

        if complete_only:
            del self._buffer[: starts[-1][0]]
        else:
            self._buffer.clear()
        return tuple(found)

    def _handle_nal(self, nal: bytes) -> PictureMetadata | None:
        if not nal:
            return None
        kind = _nal_kind(nal, hevc=self._hevc)
        if kind == "aud":
            self._use_aud_boundaries = True
            self._awaiting_picture = True
            return None
        if kind == "sei":
            self._pending.extend(_metadata_from_nal(nal))
            return None
        if kind != "vcl":
            return None
        if not self._awaiting_picture:
            if not self._use_aud_boundaries:
                self._awaiting_picture = True
            else:
                return None
        pose = self._pending[0] if self._pending else None
        self._pending.clear()
        picture = PictureMetadata(frame_index=self.frame_index, metadata=pose)
        self.frame_index += 1
        self._awaiting_picture = False
        if not self._use_aud_boundaries:
            self._awaiting_picture = True
        return picture


def iter_picture_metadata(packet_data: bytes, *, hevc: bool = False) -> tuple[PictureMetadata, ...]:
    """Return one row per encoded picture in a compressed packet or dump."""
    if not packet_data:
        return ()
    if _has_annexb_start_code(packet_data):
        parser = AnnexBPictureSeiParser(hevc=hevc)
        return parser.feed(packet_data) + parser.flush()

    parser = AnnexBPictureSeiParser(hevc=hevc)
    found: list[PictureMetadata] = []
    for length_size in (4, 2):
        nals = list(_iter_avcc_nals(packet_data, length_size))
        if not nals:
            continue
        for nal in nals:
            picture = parser._handle_nal(nal)
            if picture is not None:
                found.append(picture)
        if found:
            return tuple(found)
    return ()


def extract_picture_metadata_from_video(
    video_path: Path,
    *,
    timeout_sec: float | None = None,
) -> tuple[PictureMetadata, ...]:
    """Copy a recorded elementary stream and return one row per picture."""
    if not video_path.is_file() or video_path.stat().st_size <= 16:
        return ()
    timeout = RTSP_SEI_EXTRACT_TIMEOUT_SEC if timeout_sec is None else float(timeout_sec)
    for muxer, hevc in (("h264", False), ("hevc", True)):
        pictures = _extract_pictures_via_ffmpeg(video_path, muxer=muxer, hevc=hevc, timeout_sec=timeout)
        if pictures:
            return pictures
    return ()


def first_pose_from_pictures(pictures: tuple[PictureMetadata, ...] | list[PictureMetadata]) -> FrameMetadata | None:
    for picture in pictures:
        if picture.metadata is not None:
            return picture.metadata
    return None


def sample_sei_from_rtsp(
    rtsp_url: str,
    *,
    timeout_sec: float | None = None,
) -> FrameMetadata | None:
    """Copy a short RTSP bitstream and return the first pose SEI, if any."""
    normalized = rtsp_url.strip()
    if not normalized.lower().startswith("rtsp://"):
        return None
    timeout = RTSP_SEI_PROBE_TIMEOUT_SEC if timeout_sec is None else float(timeout_sec)
    with tempfile.TemporaryDirectory(prefix="rtsp-sei-") as tmp:
        dump_path = Path(tmp) / "stream.bin"
        if not capture_rtsp_annexb(normalized, dump_path, timeout_sec=timeout):
            return None
        try:
            packet_data = dump_path.read_bytes()
        except OSError:
            return None
    metadata = parse_packet_metadata(packet_data)
    if not metadata:
        logger.info("RTSP SEI probe found no pose SEI in %s (%s bytes)", normalized, len(packet_data))
        return None
    return metadata[0]


def capture_rtsp_annexb(
    rtsp_url: str,
    output_path: Path,
    *,
    timeout_sec: float = RTSP_SEI_PROBE_TIMEOUT_SEC,
    frame_count: int | None = None,
) -> bool:
    """Dump a short copy of the live video elementary stream (Annex-B)."""
    frames = RTSP_SEI_PROBE_FRAMES if frame_count is None else int(frame_count)
    if frames <= 0:
        frames = RTSP_SEI_PROBE_FRAMES
    process_timeout = max(1.0, float(timeout_sec))
    timeout_us = max(1, _RTSP_FFMPEG_RW_TIMEOUT_US)

    for muxer in ("h264", "hevc"):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.exists():
            output_path.unlink()
        command = [
            FFMPEG_BIN,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-rtsp_transport",
            RTSP_TRANSPORT,
            "-timeout",
            str(timeout_us),
            "-i",
            rtsp_url,
            "-frames:v",
            str(frames),
            "-an",
            "-c:v",
            "copy",
            "-f",
            muxer,
            str(output_path),
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=process_timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            logger.debug("RTSP SEI dump via %s failed for %s: %s", muxer, rtsp_url, error)
            return False

        if result.returncode == 0 and output_path.is_file() and output_path.stat().st_size > 16:
            return True

        stderr = (result.stderr or "").strip()
        if not _looks_like_muxer_mismatch(stderr):
            logger.debug("RTSP SEI dump failed for %s: %s", rtsp_url, stderr or result.returncode)
            return False

    return False


def _looks_like_muxer_mismatch(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(marker in lowered for marker in _MUXER_MISMATCH_MARKERS)


def _extract_pictures_via_ffmpeg(
    video_path: Path,
    *,
    muxer: str,
    hevc: bool,
    timeout_sec: float,
) -> tuple[PictureMetadata, ...]:
    command = [
        FFMPEG_BIN,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        str(video_path),
        "-an",
        "-c:v",
        "copy",
        "-f",
        muxer,
        "pipe:1",
    ]
    process_timeout = max(1.0, float(timeout_sec))
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        logger.debug("RTSP SEI extract via %s failed for %s: %s", muxer, video_path, error)
        return ()

    parser = AnnexBPictureSeiParser(hevc=hevc)
    found: list[PictureMetadata] = []
    stderr = b""
    try:
        assert process.stdout is not None
        while True:
            chunk = process.stdout.read(64 * 1024)
            if not chunk:
                break
            found.extend(parser.feed(chunk))
        found.extend(parser.flush())
        stderr = process.stderr.read() if process.stderr is not None else b""
        process.wait(timeout=process_timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        logger.debug("RTSP SEI extract timed out via %s for %s", muxer, video_path)
        return ()
    except OSError as error:
        process.kill()
        logger.debug("RTSP SEI extract via %s failed for %s: %s", muxer, video_path, error)
        return ()
    finally:
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass

    if process.returncode not in (0, None) and not found:
        text = stderr.decode("utf-8", errors="replace").strip()
        if not _looks_like_muxer_mismatch(text):
            logger.debug("RTSP SEI extract failed for %s: %s", video_path, text or process.returncode)
        return ()
    return tuple(found)


def _nal_kind(nal: bytes, *, hevc: bool) -> str:
    if not nal:
        return "other"
    if hevc:
        if len(nal) < 2:
            return "other"
        nal_type = (nal[0] >> 1) & 0x3F
        if nal_type == HEVC_AUD_NAL_TYPE:
            return "aud"
        if nal_type in {HEVC_PREFIX_SEI, HEVC_SUFFIX_SEI}:
            return "sei"
        if nal_type in HEVC_VCL_NAL_TYPES:
            return "vcl"
        return "other"
    nal_type = nal[0] & 0x1F
    if nal_type == H264_AUD_NAL_TYPE:
        return "aud"
    if nal_type == H264_SEI_NAL_TYPE:
        return "sei"
    if nal_type in H264_VCL_NAL_TYPES:
        return "vcl"
    return "other"


def _metadata_from_nal(nal: bytes) -> list[FrameMetadata]:
    if not nal:
        return []
    h264_type = nal[0] & 0x1F
    if h264_type == H264_SEI_NAL_TYPE and len(nal) > 1:
        return _parse_sei_rbsp(_ebsp_to_rbsp(nal[1:]))
    if len(nal) >= 3:
        hevc_type = (nal[0] >> 1) & 0x3F
        if hevc_type in {HEVC_PREFIX_SEI, HEVC_SUFFIX_SEI}:
            return _parse_sei_rbsp(_ebsp_to_rbsp(nal[2:]))
    return []


def _parse_sei_rbsp(rbsp: bytes) -> list[FrameMetadata]:
    found: list[FrameMetadata] = []
    index = 0
    length = len(rbsp)
    while index < length:
        if rbsp[index] == 0x80:
            break
        payload_type, index = _read_sei_uint(rbsp, index)
        if index is None or payload_type is None:
            break
        payload_size, index = _read_sei_uint(rbsp, index)
        if index is None or payload_size is None:
            break
        if index + payload_size > length:
            break
        payload = rbsp[index : index + payload_size]
        index += payload_size
        if payload_type != USER_DATA_UNREGISTERED:
            continue
        metadata = _parse_unregistered_payload(payload)
        if metadata is not None:
            found.append(metadata)
    return found


def _parse_unregistered_payload(payload: bytes) -> FrameMetadata | None:
    if len(payload) >= 16 + POSE_SEI_PAYLOAD_SIZE:
        after_uuid = _unpack_pose(payload[16 : 16 + POSE_SEI_PAYLOAD_SIZE])
        if after_uuid is not None:
            return after_uuid
    if len(payload) >= POSE_SEI_PAYLOAD_SIZE:
        return _unpack_pose(payload[:POSE_SEI_PAYLOAD_SIZE])
    return None


def _unpack_pose(raw: bytes) -> FrameMetadata | None:
    if len(raw) < POSE_SEI_PAYLOAD_SIZE:
        return None
    timestamp_ns, x, y, yaw = POSE_SEI_STRUCT.unpack(raw[:POSE_SEI_PAYLOAD_SIZE])
    if not _plausible_timestamp_ns(timestamp_ns):
        return None
    return FrameMetadata(timestamp_ns=int(timestamp_ns), x=float(x), y=float(y), yaw=float(yaw))


def _plausible_timestamp_ns(timestamp_ns: int) -> bool:
    return _MIN_TIMESTAMP_NS <= int(timestamp_ns) <= _MAX_TIMESTAMP_NS


def _coerce_float(value: object) -> float:
    if value is None or value == "":
        return 0.0
    return float(value)


def _encode_sei_uint(value: int) -> bytes:
    remaining = int(value)
    encoded = bytearray()
    while remaining >= 255:
        encoded.append(0xFF)
        remaining -= 255
    encoded.append(remaining)
    return bytes(encoded)


def _read_sei_uint(buffer: bytes, index: int) -> tuple[int | None, int | None]:
    if index >= len(buffer):
        return None, None
    value = 0
    while index < len(buffer) and buffer[index] == 0xFF:
        value += 255
        index += 1
    if index >= len(buffer):
        return None, None
    value += buffer[index]
    return value, index + 1


def _has_annexb_start_code(data: bytes) -> bool:
    return b"\x00\x00\x01" in data


def find_annexb_start_codes(data: bytes) -> list[tuple[int, int]]:
    """Return ``(offset, start_code_length)`` for every Annex-B start code."""
    starts: list[tuple[int, int]] = []
    index = 0
    length = len(data)
    while index < length - 2:
        if data[index] != 0 or data[index + 1] != 0:
            index += 1
            continue
        if index + 3 < length and data[index + 2] == 0 and data[index + 3] == 1:
            starts.append((index, 4))
            index += 4
            continue
        if data[index + 2] == 1:
            starts.append((index, 3))
            index += 3
            continue
        index += 1
    return starts


def _iter_annexb_nals(data: bytes):
    starts = find_annexb_start_codes(data)
    for start_index, (position, start_len) in enumerate(starts):
        nal_start = position + start_len
        nal_end = starts[start_index + 1][0] if start_index + 1 < len(starts) else len(data)
        nal = data[nal_start:nal_end]
        if nal:
            yield nal


def _iter_avcc_nals(data: bytes, length_size: int):
    index = 0
    total = len(data)
    if length_size not in {1, 2, 3, 4}:
        return
    while index + length_size <= total:
        nal_length = int.from_bytes(data[index : index + length_size], "big")
        index += length_size
        if nal_length <= 0 or index + nal_length > total:
            return
        yield data[index : index + nal_length]
        index += nal_length


def _ebsp_to_rbsp(ebsp: bytes) -> bytes:
    output = bytearray()
    index = 0
    length = len(ebsp)
    while index < length:
        if index + 2 < length and ebsp[index] == 0 and ebsp[index + 1] == 0 and ebsp[index + 2] == 3:
            output.append(0)
            output.append(0)
            index += 3
            continue
        output.append(ebsp[index])
        index += 1
    return bytes(output)


def _rbsp_to_ebsp(rbsp: bytes) -> bytes:
    output = bytearray()
    zero_count = 0
    for byte in rbsp:
        if zero_count == 2 and byte <= 3:
            output.append(0x03)
            zero_count = 0
        output.append(byte)
        zero_count = zero_count + 1 if byte == 0 else 0
    return bytes(output)
