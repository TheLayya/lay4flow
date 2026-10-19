"""Read uploaded MP4 metadata from bounded container boxes, without a subprocess."""

import base64
import struct

MAX_VIDEO_BYTES = 50 * 1024**2
MAX_ENCODED_VIDEO = 4 * ((MAX_VIDEO_BYTES + 2) // 3)
INVALID_VIDEO = "A valid MP4 up to 50 MiB with a 3–60 second video track is required"
MP4_BRANDS = {
    b"isom",
    b"iso2",
    b"iso3",
    b"iso4",
    b"iso5",
    b"iso6",
    b"mp41",
    b"mp42",
    b"avc1",
    b"dash",
    b"M4V ",
    b"MSNV",
}


def _boxes(data, start, end):
    boxes = []
    while start < end:
        if len(boxes) >= 10000 or end - start < 8:
            raise ValueError
        size, kind = struct.unpack_from(">I4s", data, start)
        header = 8
        if size == 1:
            if end - start < 16:
                raise ValueError
            size = struct.unpack_from(">Q", data, start + 8)[0]
            header = 16
        elif size == 0:
            size = end - start
        if size < header or size > end - start:
            raise ValueError
        boxes.append((kind, start + header, start + size))
        start += size
    return boxes


def _one(boxes, kind):
    matches = [bounds for name, *bounds in boxes if name == kind]
    if len(matches) != 1:
        raise ValueError
    return matches[0]


def _presentation_bounds(data, mdia, media_ticks):
    """Read decode/composition timing runs without inspecting compressed samples."""
    table = _boxes(data, *_one(_boxes(data, *_one(mdia, b"minf")), b"stbl"))

    def runs(kind):
        start, end = _one(table, kind)
        version = data[start] if start < end else -1
        if (
            end - start < 8
            or version not in ((0,) if kind == b"stts" else (0, 1))
            or data[start + 1 : start + 4] != bytes(3)
        ):
            raise ValueError
        count = struct.unpack_from(">I", data, start + 4)[0]
        if not 1 <= count <= 10000 or end - start != 8 + 8 * count:
            raise ValueError
        values = [
            struct.unpack_from(
                ">Ii" if kind == b"ctts" and version else ">II", data, start + 8 + 8 * index
            )
            for index in range(count)
        ]
        if any(samples == 0 or kind == b"stts" and value == 0 for samples, value in values):
            raise ValueError
        return values

    decoding, composition = runs(b"stts"), runs(b"ctts")
    if sum(samples * delta for samples, delta in decoding) != media_ticks or sum(
        samples for samples, _ in decoding
    ) != sum(samples for samples, _ in composition):
        raise ValueError
    decode_index = composition_index = decode_time = 0
    remaining_decode, delta = decoding[0]
    remaining_composition, offset = composition[0]
    first, last = media_ticks, 0
    while decode_index < len(decoding) and composition_index < len(composition):
        samples = min(remaining_decode, remaining_composition)
        first = min(first, decode_time + offset)
        last = max(last, decode_time + samples * delta + offset)
        decode_time += samples * delta
        remaining_decode -= samples
        remaining_composition -= samples
        if not remaining_decode:
            decode_index += 1
            if decode_index < len(decoding):
                remaining_decode, delta = decoding[decode_index]
        if not remaining_composition:
            composition_index += 1
            if composition_index < len(composition):
                remaining_composition, offset = composition[composition_index]
    return first, last


def mp4_metadata(data, *, minimum_duration=3, maximum_duration=60):
    """Validate MP4 and return the displayed video-track duration and size.

    The movie header can include trailing audio.  Trust the video track's
    movie-time duration (``tkhd``), cross-checking its media duration
    (``mdhd``); this inspects metadata only and never decodes samples.
    """
    try:
        if (
            not isinstance(data, bytes)
            or not 0 < len(data) <= MAX_VIDEO_BYTES
            or type(minimum_duration) not in (int, float)
            or type(maximum_duration) not in (int, float)
            or not 0 < minimum_duration < maximum_duration <= 60
        ):
            raise ValueError
        top = _boxes(data, 0, len(data))
        if not top or top[0][0] != b"ftyp":
            raise ValueError
        start, end = _one(top, b"ftyp")
        if end - start < 8 or (end - start) % 4:
            raise ValueError
        brands = {data[start : start + 4]}
        brands.update(data[offset : offset + 4] for offset in range(start + 8, end, 4))
        if not brands.intersection(MP4_BRANDS):
            raise ValueError
        if not any(kind == b"mdat" and end > start for kind, start, end in top):
            raise ValueError
        start, end = _one(top, b"moov")
        movie = _boxes(data, start, end)
        start, end = _one(movie, b"mvhd")
        version = data[start] if start < end else -1
        if (
            version not in (0, 1)
            or end - start < (100 if version == 0 else 112)
            or data[start + 1 : start + 4] != bytes(3)
        ):
            raise ValueError
        offset = start + (12 if version == 0 else 20)
        timescale = struct.unpack_from(">I", data, offset)[0]
        ticks = struct.unpack_from(">I" if version == 0 else ">Q", data, offset + 4)[0]
        if not timescale or not ticks or ticks == (1 << (32 if version == 0 else 64)) - 1:
            raise ValueError
        if any(kind == b"moof" for kind, _, _ in top) or any(
            kind == b"mvex" for kind, _, _ in movie
        ):
            raise ValueError
        movie_duration = ticks / timescale
        dimensions = []
        video_duration = None
        for kind, start, end in movie:
            if kind != b"trak":
                continue
            track = _boxes(data, start, end)
            mdia = _boxes(data, *_one(track, b"mdia"))
            handler_start, handler_end = _one(mdia, b"hdlr")
            if handler_end - handler_start < 24 or data[handler_start : handler_start + 4] != bytes(
                4
            ):
                raise ValueError
            if data[handler_start + 8 : handler_start + 12] != b"vide":
                continue
            tkhd_start, tkhd_end = _one(track, b"tkhd")
            version = data[tkhd_start] if tkhd_start < tkhd_end else -1
            expected = 84 if version == 0 else 96 if version == 1 else -1
            if expected < 0 or tkhd_end - tkhd_start != expected:
                raise ValueError
            track_flags = int.from_bytes(data[tkhd_start + 1 : tkhd_start + 4], "big")
            if track_flags & 3 != 3 or track_flags & ~7:
                raise ValueError
            track_id = struct.unpack_from(">I", data, tkhd_start + (12 if version == 0 else 20))[0]
            if not track_id:
                raise ValueError
            duration_offset = 20 if version == 0 else 28
            track_ticks = struct.unpack_from(
                ">I" if version == 0 else ">Q", data, tkhd_start + duration_offset
            )[0]
            if not track_ticks or track_ticks == (1 << (32 if version == 0 else 64)) - 1:
                raise ValueError
            track_duration = track_ticks / timescale
            width, height = struct.unpack_from(">II", data, tkhd_end - 8)
            if (
                width % 65536
                or height % 65536
                or not 1 <= width // 65536 <= 8192
                or not 1 <= height // 65536 <= 8192
            ):
                raise ValueError
            width, height = width // 65536, height // 65536
            matrix = tkhd_start + (40 if version == 0 else 52)
            a, b, u, c, d, v, _, _, w = struct.unpack_from(">iiiiiiiii", data, matrix)
            if (u, v, w) != (0, 0, 1 << 30):
                raise ValueError
            if (a, b, c, d) in ((0, 65536, -65536, 0), (0, -65536, 65536, 0)):
                width, height = height, width
            elif (a, b, c, d) not in ((65536, 0, 0, 65536), (-65536, 0, 0, -65536)):
                raise ValueError
            mdhd_start, mdhd_end = _one(mdia, b"mdhd")
            mdhd_version = data[mdhd_start] if mdhd_start < mdhd_end else -1
            mdhd_expected = 24 if mdhd_version == 0 else 36 if mdhd_version == 1 else -1
            if (
                mdhd_expected < 0
                or mdhd_end - mdhd_start != mdhd_expected
                or data[mdhd_start + 1 : mdhd_start + 4] != bytes(3)
            ):
                raise ValueError
            media_scale_offset = 12 if mdhd_version == 0 else 20
            media_duration_offset = 16 if mdhd_version == 0 else 24
            media_scale = struct.unpack_from(">I", data, mdhd_start + media_scale_offset)[0]
            media_ticks = struct.unpack_from(
                ">I" if mdhd_version == 0 else ">Q", data, mdhd_start + media_duration_offset
            )[0]
            if (
                not media_scale
                or not media_ticks
                or media_ticks == (1 << (32 if mdhd_version == 0 else 64)) - 1
            ):
                raise ValueError
            media_duration = media_ticks / media_scale
            # Allow header tick rounding, capped so coarse timescales cannot
            # disguise seconds of missing video media.
            tolerance = min(0.01, 1 / timescale + 1 / media_scale)
            edits = [bounds for name, *bounds in track if name == b"edts"]
            if len(edits) > 1:
                raise ValueError
            if edits:
                edit_boxes = _boxes(data, *edits[0])
                elst_start, elst_end = _one(edit_boxes, b"elst")
                elst_version = data[elst_start] if elst_start < elst_end else -1
                if (
                    elst_version not in (0, 1)
                    or elst_end - elst_start < 8
                    or data[elst_start + 1 : elst_start + 4] != bytes(3)
                ):
                    raise ValueError
                count = struct.unpack_from(">I", data, elst_start + 4)[0]
                entry_size = 12 if elst_version == 0 else 20
                if count != 1 or elst_end - elst_start != 8 + count * entry_size:
                    raise ValueError
                entry = elst_start + 8
                segment_ticks = struct.unpack_from(
                    ">I" if elst_version == 0 else ">Q", data, entry
                )[0]
                media_time_offset = entry + (4 if elst_version == 0 else 8)
                media_time = struct.unpack_from(
                    ">i" if elst_version == 0 else ">q", data, media_time_offset
                )[0]
                rate_offset = media_time_offset + (4 if elst_version == 0 else 8)
                rate_integer, rate_fraction = struct.unpack_from(">hh", data, rate_offset)
                if not segment_ticks or media_time < 0 or (rate_integer, rate_fraction) != (1, 0):
                    raise ValueError
                edited_duration = segment_ticks / timescale
                if segment_ticks != track_ticks:
                    raise ValueError
                if media_time / media_scale + edited_duration > media_duration + tolerance:
                    first, last = _presentation_bounds(data, mdia, media_ticks)
                    if media_time + tolerance * media_scale < first or (
                        media_time + edited_duration * media_scale > last + tolerance * media_scale
                    ):
                        raise ValueError
            elif abs(track_duration - media_duration) > tolerance:
                raise ValueError
            if movie_duration + tolerance < track_duration:
                raise ValueError
            video_duration = track_duration
            dimensions.append((width, height))
        if len(dimensions) != 1:
            raise ValueError
        width, height = dimensions[0]
        if video_duration is None or not minimum_duration <= video_duration <= maximum_duration:
            raise ValueError
        return {"duration": video_duration, "width": width, "height": height}
    except (ValueError, struct.error, OverflowError):
        raise ValueError(INVALID_VIDEO) from None


def video_input(encoded):
    """Accept strict plain Base64 and return bytes plus verified MP4 metadata."""
    try:
        if not isinstance(encoded, str) or len(encoded) > MAX_ENCODED_VIDEO:
            raise ValueError
        data = base64.b64decode(encoded, validate=True)
        return data, mp4_metadata(data)
    except ValueError:
        raise ValueError(INVALID_VIDEO) from None
