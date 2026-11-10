import struct

import pytest

from implicated.imagesize import image_size, is_square


def png(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", width, height) + b"\x08\x02"


def jpeg(width: int, height: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + bytes(9)
    sof0 = b"\xff\xc0" + struct.pack(">HBHH", 17, 8, height, width) + bytes(12)
    return b"\xff\xd8" + app0 + sof0


def gif(width: int, height: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + bytes(4)


def webp(chunk: bytes, body: bytes) -> bytes:
    return b"RIFF" + bytes(4) + b"WEBP" + chunk + bytes(4) + body


def u24(value: int) -> bytes:
    return value.to_bytes(3, "little")


def avif(width: int, height: int) -> bytes:
    ispe = struct.pack(">I", 20) + b"ispe" + bytes(4) + struct.pack(">II", width, height)
    return struct.pack(">I", 20) + b"ftypavif" + bytes(8) + b"meta" + bytes(30) + ispe


@pytest.mark.parametrize(
    "data, size",
    [
        (png(400, 300), (400, 300)),
        (jpeg(640, 480), (640, 480)),
        (gif(32, 16), (32, 16)),
        (webp(b"VP8 ", bytes(6) + struct.pack("<HH", 300, 200)), (300, 200)),
        (webp(b"VP8L", b"\x2f" + ((299) | (199 << 14)).to_bytes(4, "little")), (300, 200)),
        (webp(b"VP8X", bytes(4) + u24(299) + u24(199)), (300, 200)),
        (avif(1200, 1200), (1200, 1200)),
    ],
)  # fmt: skip
def test_image_size(data, size):
    assert image_size(data) == size


@pytest.mark.parametrize(
    "data", [b"", b"image", b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"RIFF...."]
)
def test_unreadable_images_have_no_size(data):
    assert image_size(data) is None
    assert not is_square(data)


def test_is_square():
    assert is_square(png(400, 400))
    assert is_square(jpeg(1080, 1075))  # cropped by hand, a few pixels off
    assert not is_square(jpeg(1080, 1000))
    assert not is_square(png(0, 0))
