"""Read an image's width and height from the start of its file, without decoding it.

Enough to tell whether a picture is square (see ``is_square``), for the image types the library
takes: JPEG, PNG, GIF, WebP and AVIF. Anything it cannot read gives ``None``.
"""

from __future__ import annotations

import struct

# How far apart the sides may be, as a share of the longer one, and still count as square:
# photos cropped by hand are often a pixel or so off.
SQUARE_TOLERANCE = 0.01


def image_size(data: bytes) -> tuple[int, int] | None:
    """The ``(width, height)`` of the image in ``data``, or ``None`` when it is not one of the
    types read here, or is broken."""
    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return struct.unpack(">II", data[16:24])
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return struct.unpack("<HH", data[6:10])
        if data.startswith(b"\xff\xd8"):
            return _jpeg_size(data)
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return _webp_size(data)
        if data[4:8] == b"ftyp":
            return _avif_size(data)
    except (struct.error, IndexError, ValueError):
        return None
    return None


def is_square(data: bytes) -> bool:
    """Whether the image in ``data`` is square (see ``SQUARE_TOLERANCE``); ``False`` when its
    size cannot be read."""
    size = image_size(data)
    if not size or min(size) <= 0:
        return False
    width, height = size
    return abs(width - height) <= SQUARE_TOLERANCE * max(width, height)


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    """From the first start-of-frame marker (any of SOF0 to SOF15 but DHT, JPG and DAC)."""
    at = 2
    while at + 9 < len(data):
        if data[at] != 0xFF:
            return None
        marker = data[at + 1]
        if marker == 0xFF:  # padding
            at += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:  # no length
            at += 2
            continue
        (length,) = struct.unpack(">H", data[at + 2 : at + 4])
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[at + 5 : at + 9])
            return width, height
        at += 2 + length
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    chunk = data[12:16]
    if chunk == b"VP8 ":  # lossy
        width, height = struct.unpack("<HH", data[26:30])
        return width & 0x3FFF, height & 0x3FFF
    if chunk == b"VP8L":  # lossless: 14 bits each, minus one
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X":  # extended: 24 bits each, minus one
        return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
    return None


def _avif_size(data: bytes) -> tuple[int, int] | None:
    """From the first ``ispe`` (image spatial extent) property, the main image's."""
    at = data.find(b"ispe")
    if at < 0:
        return None
    # The box type is followed by its version and flags (4 bytes), then width and height.
    return struct.unpack(">II", data[at + 8 : at + 16])
