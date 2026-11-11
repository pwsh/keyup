"""MPEG audio frames, just enough of them to pace a stream.

The Broadcastify *Feed* exporter (:mod:`app.exporters.broadcastify_feed`) is a live
Icecast source: it has to hand the server a continuous MP3 bitstream at **exactly** real
time, and it has to be able to splice silence in between calls without producing a
corrupt frame.  Both of those need the one thing a stream of MP3 bytes does not carry in
its container — where each frame starts and how long it lasts — so this module reads the
four-byte frame headers and answers that question.

It is deliberately tiny and deliberately tolerant: an unparseable byte is skipped and the
scan resyncs on the next ``0xFF Ex`` pair, because a broken frame in the middle of one
recording must cost that recording, not the connection.

Reference: ISO/IEC 11172-3 (MPEG-1) and 13818-3 (MPEG-2) audio layer III frame headers —
the same tables every decoder carries.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

__all__ = [
    "Frame",
    "MPEG1_BITRATES",
    "MPEG2_BITRATES",
    "frame_duration_s",
    "iter_frames",
    "parse_header",
]

#: Layer III bit rates in kbit/s by the header's four-bit index.  Index 0 is "free" and
#: index 15 is invalid; both are stored as ``0`` and make :func:`parse_header` fail.
MPEG1_BITRATES = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)
MPEG2_BITRATES = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0)

#: Sampling rates by version bits (3 = MPEG-1, 2 = MPEG-2, 0 = MPEG-2.5) and the header's
#: two-bit index.
SAMPLE_RATES = {
    3: (44_100, 48_000, 32_000),
    2: (22_050, 24_000, 16_000),
    0: (11_025, 12_000, 8_000),
}

#: Samples carried by one layer III frame.  MPEG-2 and 2.5 use the short granule, which is
#: why 22.05 kHz frames are 26 ms rather than the 26.1 ms of a 44.1 kHz MPEG-1 frame.
SAMPLES_PER_FRAME = {3: 1152, 2: 576, 0: 576}

#: Layer III is layer bits ``01``.  Nothing here reads any other layer.
LAYER_III = 1


@dataclass(frozen=True, slots=True)
class Frame:
    """One complete MPEG audio frame: its bytes and how long it plays."""

    data: bytes
    duration_s: float
    sample_rate: int
    bitrate_kbps: int

    def __len__(self) -> int:  # pragma: no cover - convenience
        return len(self.data)


def frame_duration_s(sample_rate: int, samples: int) -> float:
    return samples / float(sample_rate)


def parse_header(head: bytes) -> tuple[int, float, int, int] | None:
    """``(frame_length, duration_s, sample_rate, bitrate_kbps)`` for a header, or ``None``.

    ``None`` means "this is not the start of a layer III frame" — a free-format or
    reserved field, a layer we do not read, or plain noise.  The caller resyncs.
    """
    if len(head) < 4 or head[0] != 0xFF or (head[1] & 0xE0) != 0xE0:
        return None
    version = (head[1] >> 3) & 0b11
    layer = (head[1] >> 1) & 0b11
    if version == 1 or layer != LAYER_III:  # version 1 is reserved
        return None
    bitrate_index = head[2] >> 4
    rate_index = (head[2] >> 2) & 0b11
    if rate_index == 3:
        return None
    padding = (head[2] >> 1) & 1
    bitrate = (MPEG1_BITRATES if version == 3 else MPEG2_BITRATES)[bitrate_index]
    if bitrate == 0:
        return None
    sample_rate = SAMPLE_RATES[version][rate_index]
    samples = SAMPLES_PER_FRAME[version]
    # 144 = 1152 samples / 8 bits per byte for MPEG-1; the short granule halves it.
    coefficient = 144 if version == 3 else 72
    length = (coefficient * bitrate * 1000) // sample_rate + padding
    if length < 4:
        return None
    return length, frame_duration_s(sample_rate, samples), sample_rate, bitrate


def iter_frames(data: bytes) -> Iterator[Frame]:
    """Walk ``data``, yielding whole frames and skipping everything that is not one.

    ID3v2 tags are stepped over by their declared size; anything else that fails to parse
    costs one byte and the scan tries again, so a stray tag, a Xing header we did not ask
    ffmpeg to suppress, or a truncated tail all degrade to "fewer frames" rather than to a
    corrupt bitstream on the wire.
    """
    index = 0
    end = len(data)
    while index + 4 <= end:
        if data[index : index + 3] == b"ID3" and index + 10 <= end:
            size = 0
            for byte in data[index + 6 : index + 10]:
                size = (size << 7) | (byte & 0x7F)
            index += 10 + size
            continue
        parsed = parse_header(data[index : index + 4])
        if parsed is None:
            index += 1
            continue
        length, duration, sample_rate, bitrate = parsed
        if index + length > end:  # a truncated final frame is not sent
            return
        yield Frame(
            data=bytes(data[index : index + length]),
            duration_s=duration,
            sample_rate=sample_rate,
            bitrate_kbps=bitrate,
        )
        index += length
