"""Uploaded MP4 bounds use real container boxes, not byte-marker searches."""

import base64
import struct
import unittest
from unittest.mock import patch

from src.services.vids.video_input import mp4_metadata, video_input


def box(kind, payload, *, extended=False, to_end=False):
    if extended:
        return struct.pack(">I4sQ", 1, kind, len(payload) + 16) + payload
    return struct.pack(">I4s", 0 if to_end else len(payload) + 8, kind) + payload


def track(
    width,
    height,
    *,
    handler=b"vide",
    version=0,
    rotated=False,
    seconds=4,
    media_seconds=None,
    movie_timescale=1000,
    media_timescale=1000,
    flags=3,
    edits=b"",
    timing=b"",
):
    header = bytearray(84 if version == 0 else 96)
    header[0] = version
    header[1:4] = flags.to_bytes(3, "big")
    struct.pack_into(">I", header, 12 if version == 0 else 20, 1)
    struct.pack_into(
        ">I" if version == 0 else ">Q",
        header,
        20 if version == 0 else 28,
        round(seconds * movie_timescale),
    )
    matrix = 40 if version == 0 else 52
    values = (
        (0, 65536, 0, -65536, 0, 0, 0, 0, 1 << 30)
        if rotated
        else (65536, 0, 0, 0, 65536, 0, 0, 0, 1 << 30)
    )
    struct.pack_into(">iiiiiiiii", header, matrix, *values)
    struct.pack_into(">II", header, len(header) - 8, width << 16, height << 16)
    media_header = bytearray(24 if version == 0 else 36)
    media_header[0] = version
    struct.pack_into(">I", media_header, 12 if version == 0 else 20, media_timescale)
    struct.pack_into(
        ">I" if version == 0 else ">Q",
        media_header,
        16 if version == 0 else 24,
        round((seconds if media_seconds is None else media_seconds) * media_timescale),
    )
    media = box(b"mdhd", media_header) + box(b"hdlr", bytes(8) + handler + bytes(12))
    if timing:
        media += box(b"minf", box(b"stbl", timing))
    return box(b"trak", box(b"tkhd", header) + edits + box(b"mdia", media))


def mp4(
    seconds=4,
    width=1280,
    height=720,
    *,
    version=0,
    tracks=None,
    extended=False,
    movie_timescale=1000,
    media_timescale=1000,
):
    header = bytearray(100 if version == 0 else 112)
    header[0] = version
    struct.pack_into(">I", header, 12 if version == 0 else 20, movie_timescale)
    struct.pack_into(
        ">I" if version == 0 else ">Q",
        header,
        16 if version == 0 else 24,
        round(seconds * movie_timescale),
    )
    movie = box(b"mvhd", header) + (
        tracks
        if tracks is not None
        else track(
            width,
            height,
            version=version,
            seconds=seconds,
            movie_timescale=movie_timescale,
            media_timescale=media_timescale,
        )
    )
    return (
        box(b"ftyp", b"isom" + bytes(4) + b"mp42")
        + box(b"mdat", b"synthetic-video-samples")
        + box(b"moov", movie, extended=extended)
    )


def edit_list(seconds, media_time=0, *, version=0, rate=(1, 0), entries=None):
    entries = [(round(seconds * 1000), media_time, rate)] if entries is None else entries
    payload = bytes([version, 0, 0, 0]) + struct.pack(">I", len(entries))
    for duration, start, media_rate in entries:
        payload += struct.pack(">Ii hh" if version == 0 else ">Qq hh", duration, start, *media_rate)
    return box(b"edts", box(b"elst", payload))


def timing_boxes(decoding, composition, *, version=0):
    stts = bytes(4) + struct.pack(">I", len(decoding))
    for samples, delta in decoding:
        stts += struct.pack(">II", samples, delta)
    ctts = bytes([version, 0, 0, 0]) + struct.pack(">I", len(composition))
    for samples, offset in composition:
        ctts += struct.pack(">II" if version == 0 else ">Ii", samples, offset)
    return box(b"stts", stts) + box(b"ctts", ctts)


class VidsVideoInputTests(unittest.TestCase):
    def test_parses_both_versions_landscape_portrait_and_extended_box(self):
        for version, width, height in ((0, 1280, 720), (1, 720, 1280)):
            with self.subTest(version=version):
                data = mp4(12.5, width, height, version=version, extended=True)
                actual, resource = video_input(base64.b64encode(data).decode())
                self.assertEqual(actual, data)
                self.assertEqual(resource, {"duration": 12.5, "width": width, "height": height})

    def test_audio_track_cannot_supply_video_dimensions_and_rotation_is_applied(self):
        data = mp4(tracks=track(0, 0, handler=b"soun") + track(1280, 720, rotated=True))
        self.assertEqual(mp4_metadata(data), {"duration": 4, "width": 720, "height": 1280})
        with self.assertRaises(ValueError):
            mp4_metadata(mp4(tracks=track(1280, 720, handler=b"soun")))

    def test_video_duration_comes_from_track_not_longer_movie_or_audio(self):
        for version in (0, 1):
            data = mp4(
                12,
                version=version,
                tracks=track(0, 0, handler=b"soun", seconds=12, version=version)
                + track(1280, 720, seconds=4, version=version),
            )
            self.assertEqual(mp4_metadata(data)["duration"], 4)
            with self.assertRaises(ValueError):
                mp4_metadata(mp4(12, tracks=track(1280, 720, seconds=2, version=version)))
        with self.assertRaises(ValueError):
            mp4_metadata(mp4(3, tracks=track(1280, 720, seconds=4)))

    def test_media_timing_is_checked_in_its_own_timescale(self):
        for version in (0, 1):
            self.assertEqual(
                mp4_metadata(mp4(12.5, version=version, media_timescale=24000))["duration"],
                12.5,
            )
            for seconds, media_seconds in ((4, 2), (4, 12), (4, 0)):
                with (
                    self.subTest(version=version, media=media_seconds),
                    self.assertRaises(ValueError),
                ):
                    mp4_metadata(
                        mp4(
                            12,
                            tracks=track(
                                1280,
                                720,
                                seconds=seconds,
                                media_seconds=media_seconds,
                                version=version,
                            ),
                        )
                    )
        with self.assertRaises(ValueError):
            mp4_metadata(
                mp4(
                    12,
                    movie_timescale=1,
                    tracks=track(
                        1280, 720, seconds=4, media_seconds=2, movie_timescale=1, media_timescale=1
                    ),
                )
            )

    def test_single_normal_edit_can_trim_video_media_but_unsupported_timelines_are_rejected(self):
        for version in (0, 1):
            data = mp4(
                4,
                version=version,
                tracks=track(
                    1280,
                    720,
                    version=version,
                    seconds=4,
                    media_seconds=12,
                    edits=edit_list(4, 8000, version=version),
                ),
            )
            self.assertEqual(mp4_metadata(data)["duration"], 4)
            for edits in (
                edit_list(4, -1, version=version),
                edit_list(4, 9000, version=version),
                edit_list(3, version=version),
                edit_list(4, version=version, rate=(2, 0)),
                edit_list(4, version=version, entries=[(2000, 0, (1, 0)), (2000, 2000, (1, 0))]),
            ):
                with self.assertRaises(ValueError):
                    mp4_metadata(
                        mp4(tracks=track(1280, 720, version=version, media_seconds=12, edits=edits))
                    )

    def test_output_duration_range_is_explicit_and_does_not_weaken_input_bounds(self):
        for seconds in (2.9, 20.1):
            self.assertEqual(
                mp4_metadata(mp4(seconds), minimum_duration=2.9, maximum_duration=20.1)["duration"],
                seconds,
            )
        with self.assertRaises(ValueError):
            mp4_metadata(mp4(2.9))
        for seconds in (2.899, 20.101):
            with self.assertRaises(ValueError):
                mp4_metadata(mp4(seconds), minimum_duration=2.9, maximum_duration=20.1)

    def test_composition_timing_supports_real_encoder_lead_without_guessing_a_tolerance(self):
        for version in (0, 1):
            data = mp4(
                tracks=track(
                    1280,
                    720,
                    edits=edit_list(4, 100),
                    timing=timing_boxes(
                        [(2, 1000), (4, 500)], [(3, 100), (3, 100)], version=version
                    ),
                )
            )
            self.assertEqual(mp4_metadata(data)["duration"], 4)
        for decoding, composition in (
            ([(4, 1000)], [(3, 100)]),
            ([(4, 1000)], [(4, 0)]),
            ([(2, 1000)], [(2, 100)]),
            ([(4, 0)], [(4, 100)]),
            ([(0, 1000)], [(0, 100)]),
        ):
            with self.assertRaises(ValueError):
                mp4_metadata(
                    mp4(
                        tracks=track(
                            1280,
                            720,
                            edits=edit_list(4, 100),
                            timing=timing_boxes(decoding, composition),
                        )
                    )
                )

    def test_duration_boundaries_and_invalid_headers(self):
        self.assertEqual(mp4_metadata(mp4(3))["duration"], 3)
        self.assertEqual(mp4_metadata(mp4(60))["duration"], 60)
        for seconds in (2.999, 60.001):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                mp4_metadata(mp4(seconds))
        for width, height in ((0, 720), (1280, 0), (8193, 720)):
            with self.subTest(size=(width, height)), self.assertRaises(ValueError):
                mp4_metadata(mp4(width=width, height=height))
        valid = mp4()
        movie = valid.index(b"mvhd")
        for offset, replacement in ((movie + 4, b"\x02"), (movie + 16, bytes(4))):
            data = bytearray(valid)
            data[offset : offset + len(replacement)] = replacement
            with self.assertRaises(ValueError):
                mp4_metadata(bytes(data))

    def test_video_track_flags_versions_ids_and_timing_headers_are_not_ignored(self):
        for flags in (3, 7):
            self.assertEqual(mp4_metadata(mp4(tracks=track(1280, 720, flags=flags)))["duration"], 4)
        for flags in (0, 1, 2, 8, 11, 16):
            with self.assertRaises(ValueError):
                mp4_metadata(mp4(tracks=track(1280, 720, flags=flags)))
        for version in (0, 1):
            valid = mp4(version=version)
            cases = [
                (b"mvhd", 1, b"\x01"),
                (b"tkhd", 0, b"\x02"),
                (b"tkhd", 12 if version == 0 else 20, bytes(4)),
                (b"tkhd", 20 if version == 0 else 28, b"\xff" * (4 if version == 0 else 8)),
                (b"mdhd", 0, b"\x02"),
                (b"mdhd", 1, b"\x01"),
                (b"mdhd", 12 if version == 0 else 20, bytes(4)),
                (b"mdhd", 16 if version == 0 else 24, b"\xff" * (4 if version == 0 else 8)),
                (b"hdlr", 1, b"\x01"),
            ]
            for kind, offset, replacement in cases:
                data = bytearray(valid)
                start = data.index(kind) + 4 + offset
                data[start : start + len(replacement)] = replacement
                with (
                    self.subTest(version=version, kind=kind, offset=offset),
                    self.assertRaises(ValueError),
                ):
                    mp4_metadata(bytes(data))
        with self.assertRaises(ValueError):
            mp4_metadata(mp4().replace(b"mdhd", b"free"))
        with self.assertRaises(ValueError):
            mp4_metadata(mp4() + box(b"moof", b"metadata"))

    def test_rotations_preserve_size_and_non_affine_matrix_cannot_forge_dimensions(self):
        for values, expected in (
            ((0, -65536, 0, 65536, 0, 0, 0, 0, 1 << 30), (720, 1280)),
            ((-65536, 0, 0, 0, -65536, 0, 0, 0, 1 << 30), (1280, 720)),
            ((65536, 0, 1, 0, 65536, 0, 0, 0, 1 << 30), None),
            ((65536, 0, 0, 0, 65536, 0, 0, 0, 0), None),
            ((32768, 0, 0, 0, 65536, 0, 0, 0, 1 << 30), None),
        ):
            data = bytearray(mp4())
            struct.pack_into(">iiiiiiiii", data, data.index(b"tkhd") + 44, *values)
            if expected is None:
                with self.assertRaises(ValueError):
                    mp4_metadata(bytes(data))
            else:
                metadata = mp4_metadata(bytes(data))
                self.assertEqual((metadata["width"], metadata["height"]), expected)

    def test_markers_in_sample_data_cannot_forge_a_movie_or_video_track(self):
        fake = box(b"ftyp", b"isom" + bytes(4)) + box(b"mdat", mp4())
        with self.assertRaises(ValueError):
            mp4_metadata(fake)
        with self.assertRaises(ValueError):
            mp4_metadata(mp4(tracks=box(b"free", track(1280, 720))))

    def test_rejects_truncated_boxes_duplicates_and_ambiguous_tracks(self):
        valid = mp4()
        invalid = [
            valid[:-1],
            valid + b"trailing",
            valid + box(b"moov", b""),
            valid + box(b"ftyp", b"isom" + bytes(4)),
            valid + struct.pack(">I4s", 7, b"free"),
            valid + struct.pack(">I4s", 1, b"free"),
            mp4(tracks=track(1280, 720) + track(720, 1280)),
            valid.replace(b"isom", b"xxxx").replace(b"mp42", b"yyyy"),
            valid.replace(b"mdat", b"free"),
        ]
        for index, data in enumerate(invalid):
            with self.subTest(index=index), self.assertRaises(ValueError):
                mp4_metadata(data)
        # A zero-size terminal box extends exactly to its parent boundary.
        self.assertEqual(mp4_metadata(valid + box(b"free", b"extra", to_end=True))["duration"], 4)

    def test_rejects_invalid_base64_and_checks_encoded_and_decoded_limits(self):
        for value in (None, "", "data:video/mp4;base64,AA==", "AA==\n", "私人标识"):
            with self.subTest(value=value), self.assertRaises(ValueError) as caught:
                video_input(value)
            self.assertNotIn("私人", str(caught.exception))
        valid = mp4()
        with patch("src.services.vids.video_input.MAX_VIDEO_BYTES", len(valid) - 1):
            with self.assertRaises(ValueError):
                video_input(base64.b64encode(valid).decode())
        with patch("src.services.vids.video_input.MAX_ENCODED_VIDEO", 1):
            with self.assertRaises(ValueError):
                video_input("AA==")

    def test_rejects_excessive_tiny_boxes_before_allocating_large_metadata_lists(self):
        with self.assertRaises(ValueError):
            mp4_metadata(mp4() + box(b"free", b"") * 10001)


if __name__ == "__main__":
    unittest.main()
