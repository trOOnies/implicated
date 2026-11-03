"""Read time-accurate lyrics from ``.lrc`` files, a text format with a timestamp per line::

    [ar:Portishead]
    [offset:250]
    [00:12.40]First line
    [00:15.10][01:20.00]A chorus, sung twice
    [00:18.00]

Stamps are ``[mm:ss]`` with optional hundredths or thousandths, and a stamp with no words marks an
instrumental gap. ``[offset:ms]`` moves every line: as in the format's usual meaning, a positive
number shows them sooner and a negative one later. Other tags are ignored, and so are the stamps
of single words (``<00:12.80>``), since lines are what gets followed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

STAMP = re.compile(r"\[(\d+):(\d{1,2})(?:[.:](\d{1,3}))?\]")
OFFSET = re.compile(r"\[offset:\s*([+-]?\d+)\s*\]", re.IGNORECASE)
WORD_STAMP = re.compile(r"<\d+:\d{1,2}(?:[.:]\d{1,3})?>")


@dataclass(frozen=True)
class TimedLine:
    ms: int  # milliseconds into the song
    text: str  # empty for an instrumental gap


def _milliseconds(stamp: re.Match) -> int:
    minutes, seconds, fraction = stamp.groups()
    return (int(minutes) * 60 + int(seconds)) * 1000 + int((fraction or "").ljust(3, "0"))


def parse_lrc(text: str) -> list[TimedLine]:
    """The timed lines of an ``.lrc`` text in the order they are sung, if it has any stamps."""
    lines = []
    offset = 0
    for raw in text.removeprefix("\ufeff").splitlines():
        raw = raw.strip()
        if tag := OFFSET.fullmatch(raw):
            offset = int(tag[1])
            continue
        stamps = []
        start = 0
        while stamp := STAMP.match(raw, start):
            stamps.append(_milliseconds(stamp))
            start = stamp.end()
        words = WORD_STAMP.sub("", raw[start:]).strip()
        lines += [(ms, words) for ms in stamps]
    timed = (TimedLine(max(ms - offset, 0), words) for ms, words in lines)
    return sorted(timed, key=lambda line: line.ms)


def read_lrc(path: Path) -> list[TimedLine]:
    """The timed lines of an ``.lrc`` file; raises ``ValueError`` when it is not UTF-8 text."""
    try:
        return parse_lrc(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        raise ValueError(f"{path.name} is not UTF-8 text") from None


def plain_lyrics(lines: list[TimedLine]) -> str:
    """The words of the timed lines as ordinary lyrics, without the gaps."""
    return "\n".join(line.text for line in lines if line.text)
