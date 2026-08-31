from __future__ import annotations

import runpy
import struct
import unittest
from pathlib import Path
from unittest.mock import patch

runpy.run_path(str(Path(__file__).with_name("_bootstrap.py")))

from app.services.rtsp_sei import (  # noqa: E402
    AnnexBPoseSeiInjector,
    FrameMetadata,
    PictureMetadata,
    POSE_SEI_UUID,
    build_pose_sei_annexb,
    build_pose_sei_nal,
    inject_pose_sei_annexb,
    iter_picture_metadata,
    parse_packet_metadata,
)


_TS_NS = 1_774_328_748_518_000_000


class RtspSeiParserTests(unittest.TestCase):
    def test_parse_annexb_uuid_payload(self) -> None:
        packet = build_pose_sei_annexb(timestamp_ns=_TS_NS, x=1.5, y=-2.25, yaw=0.75)
        metadata = parse_packet_metadata(packet)
        self.assertEqual(len(metadata), 1)
        self.assertEqual(metadata[0].timestamp_ns, _TS_NS)
        self.assertEqual(metadata[0].timestamp_ms, 1_774_328_748_518)
        self.assertAlmostEqual(metadata[0].x, 1.5, places=5)
        self.assertAlmostEqual(metadata[0].y, -2.25, places=5)
        self.assertAlmostEqual(metadata[0].yaw, 0.75, places=5)

    def test_parse_bare_qfff_without_uuid(self) -> None:
        packet = build_pose_sei_annexb(
            timestamp_ns=_TS_NS,
            x=3.0,
            y=4.0,
            yaw=-0.5,
            include_uuid=False,
        )
        metadata = parse_packet_metadata(packet)
        self.assertEqual(len(metadata), 1)
        self.assertEqual(metadata[0].timestamp_ns, _TS_NS)
        self.assertAlmostEqual(metadata[0].x, 3.0, places=5)

    def test_parse_unknown_uuid_when_timestamp_plausible(self) -> None:
        packet = build_pose_sei_annexb(
            timestamp_ns=_TS_NS,
            x=9.0,
            y=8.0,
            yaw=0.1,
            uuid_bytes=b"\x11" * 16,
        )
        metadata = parse_packet_metadata(packet)
        self.assertEqual(len(metadata), 1)
        self.assertAlmostEqual(metadata[0].x, 9.0, places=5)

    def test_parse_avcc_length_prefixed_nal(self) -> None:
        nal = build_pose_sei_nal(timestamp_ns=_TS_NS, x=0.25, y=0.5, yaw=1.0)
        packet = len(nal).to_bytes(4, "big") + nal
        metadata = parse_packet_metadata(packet)
        self.assertEqual(len(metadata), 1)
        self.assertAlmostEqual(metadata[0].yaw, 1.0, places=5)

    def test_parse_hevc_prefix_sei(self) -> None:
        packet = build_pose_sei_annexb(
            timestamp_ns=_TS_NS,
            x=-1.0,
            y=2.0,
            yaw=3.0,
            hevc=True,
        )
        metadata = parse_packet_metadata(packet)
        self.assertEqual(len(metadata), 1)
        self.assertAlmostEqual(metadata[0].y, 2.0, places=5)

    def test_zero_pose_survives_emulation_prevention(self) -> None:
        packet = build_pose_sei_annexb(
            timestamp_ns=_TS_NS,
            x=0.0,
            y=0.0,
            yaw=0.0,
            include_uuid=False,
        )
        self.assertIn(b"\x00\x00\x03", packet)
        metadata = parse_packet_metadata(packet)
        self.assertEqual(len(metadata), 1)
        self.assertEqual(metadata[0].x, 0.0)
        self.assertEqual(metadata[0].y, 0.0)
        self.assertEqual(metadata[0].yaw, 0.0)

    def test_reject_implausible_timestamp(self) -> None:
        payload = POSE_SEI_UUID + struct.pack(">Qfff", 12345, 1.0, 2.0, 3.0)
        nal = bytes([6, 5, len(payload)]) + payload + b"\x80"
        packet = b"\x00\x00\x00\x01" + nal
        self.assertEqual(parse_packet_metadata(packet), ())

    def test_ignore_unrelated_bytes(self) -> None:
        self.assertEqual(parse_packet_metadata(b"not a video packet"), ())

    def test_inject_sei_before_each_access_unit(self) -> None:
        aud = b"\x00\x00\x00\x01\x09\xf0"
        idr = b"\x00\x00\x00\x01\x65" + b"\x00" * 8
        bitstream = aud + idr + aud + idr

        def metadata_for_frame(frame_index: int) -> FrameMetadata:
            return FrameMetadata(
                timestamp_ns=_TS_NS + frame_index * 40_000_000,
                x=float(frame_index),
                y=1.0,
                yaw=0.1,
            )

        injected = inject_pose_sei_annexb(bitstream, metadata_for_frame)
        parsed = parse_packet_metadata(injected)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0].timestamp_ns, _TS_NS)
        self.assertEqual(parsed[1].timestamp_ns, _TS_NS + 40_000_000)
        self.assertAlmostEqual(parsed[1].x, 1.0, places=5)
        pictures = iter_picture_metadata(injected)
        self.assertEqual(len(pictures), 2)
        self.assertEqual(pictures[0].frame_index, 0)
        self.assertEqual(pictures[1].frame_index, 1)

    def test_iter_picture_metadata_is_one_row_per_access_unit(self) -> None:
        aud = b"\x00\x00\x00\x01\x09\xf0"
        idr = b"\x00\x00\x00\x01\x65" + b"\x00" * 8
        first = build_pose_sei_annexb(timestamp_ns=_TS_NS, x=1.0, y=2.0, yaw=0.1)
        second = build_pose_sei_annexb(timestamp_ns=_TS_NS + 40_000_000, x=3.0, y=4.0, yaw=0.2)
        bitstream = aud + first + idr + aud + second + idr
        pictures = iter_picture_metadata(bitstream)
        self.assertEqual(len(pictures), 2)
        self.assertEqual(pictures[0].frame_index, 0)
        self.assertEqual(pictures[1].frame_index, 1)
        assert pictures[0].metadata is not None
        assert pictures[1].metadata is not None
        self.assertAlmostEqual(pictures[0].metadata.x, 1.0, places=5)
        self.assertAlmostEqual(pictures[1].metadata.x, 3.0, places=5)

    def test_iter_picture_metadata_keeps_slot_when_sei_missing(self) -> None:
        aud = b"\x00\x00\x00\x01\x09\xf0"
        idr = b"\x00\x00\x00\x01\x65" + b"\x00" * 8
        sei = build_pose_sei_annexb(timestamp_ns=_TS_NS, x=8.0, y=9.0, yaw=0.0)
        bitstream = aud + sei + idr + aud + idr
        pictures = iter_picture_metadata(bitstream)
        self.assertEqual(len(pictures), 2)
        self.assertIsNotNone(pictures[0].metadata)
        self.assertIsNone(pictures[1].metadata)
        self.assertIsNone(pictures[1].to_record()["timestamp_ns"])

    def test_picture_metadata_roundtrip_record(self) -> None:
        picture = PictureMetadata(
            frame_index=4,
            metadata=FrameMetadata(timestamp_ns=_TS_NS, x=0.0, y=-1.5, yaw=0.25),
        )
        restored = PictureMetadata.from_record(picture.to_record())
        self.assertIsNotNone(restored)
        assert restored is not None and restored.metadata is not None
        self.assertEqual(restored.frame_index, 4)
        self.assertEqual(restored.metadata.x, 0.0)
        self.assertAlmostEqual(restored.metadata.y, -1.5, places=5)

    def test_injector_handles_chunked_input(self) -> None:
        aud = b"\x00\x00\x00\x01\x09\xf0"
        idr = b"\x00\x00\x00\x01\x65" + b"\x11" * 8
        bitstream = aud + idr
        injector = AnnexBPoseSeiInjector(
            lambda _index: FrameMetadata(timestamp_ns=_TS_NS, x=4.0, y=5.0, yaw=0.0)
        )
        output = bytearray()
        for byte in bitstream:
            output.extend(injector.feed(bytes([byte])))
        output.extend(injector.flush())
        parsed = parse_packet_metadata(bytes(output))
        self.assertEqual(len(parsed), 1)
        self.assertAlmostEqual(parsed[0].x, 4.0, places=5)


class RtspSeiDataclassTests(unittest.TestCase):
    def test_timestamp_ms_truncates_nanoseconds(self) -> None:
        metadata = FrameMetadata(timestamp_ns=_TS_NS + 999_999, x=0.0, y=0.0, yaw=0.0)
        self.assertEqual(metadata.timestamp_ms, 1_774_328_748_518)
