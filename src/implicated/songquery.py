"""Song searches with filters, like ``artist:abba from:1975 stars:5 waterloo``.

A filter is ``key:value``, ending at a space; put a value with spaces in quotes
(``artist:"the cure"``). ``fav:`` and ``later:`` take no value. Whatever is not a filter is
the text searched in titles, artists and albums.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Filters that take a value, and the ones that are only there or not.
VALUE_KEYS = ("artist", "album", "from", "to", "language", "genre", "stars")
FLAG_KEYS = ("fav", "later")
KEYS = VALUE_KEYS + FLAG_KEYS

# A filter in the search text: starts after a space (or at the start) and ends before one.
FILTER = re.compile(
    r"""(?<!\S)(?:
          (?P<key>artist|album|from|to|language|genre|stars):(?:"(?P<quoted>[^"]*)"|(?P<plain>[^\s"]+))
        | (?P<flag>fav|later):
        )(?!\S)""",
    re.IGNORECASE | re.VERBOSE,
)


@dataclass
class SongQuery:
    text: str | None = None
    artist: str | None = None
    album: str | None = None
    from_year: int | None = None
    to_year: int | None = None
    language: str | None = None
    genre: str | None = None
    stars: int | None = None
    favorites: bool = False
    listen_later: bool = False
    # The filters as given, in order, each key once: what the search box shows as tags.
    chips: list[tuple[str, str]] = field(default_factory=list)
    # Why a filter was left out, such as a year that is not a number.
    errors: list[str] = field(default_factory=list)

    @property
    def filtered(self) -> bool:
        return bool(self.chips)


def parse_song_query(filters: list[str], text: str | None) -> SongQuery:
    """The search made of ``filters`` (each a whole ``key:value``, so values may have spaces)
    and the filters found in ``text``, whose other words are searched as they are. A later
    filter replaces an earlier one with the same key."""
    found: dict[str, str] = {}
    for item in filters:
        key, sep, value = item.partition(":")
        key = key.strip().lower()
        if sep and key in KEYS:
            _put(found, key, value.strip())

    def take(match: re.Match) -> str:
        if match["flag"]:
            _put(found, match["flag"].lower(), "")
        else:
            value = match["quoted"] if match["quoted"] is not None else match["plain"]
            _put(found, match["key"].lower(), value.strip())
        return " "

    rest = " ".join(FILTER.sub(take, text or "").split())
    query = SongQuery(text=rest or None)
    for key, value in found.items():
        if key in FLAG_KEYS:
            setattr(query, "favorites" if key == "fav" else "listen_later", True)
        elif key in ("from", "to"):
            year = _number(value)
            if year is None:
                query.errors.append(f"{key}: needs a year, like {key}:1999.")
                continue
            setattr(query, f"{key}_year", year)
        elif key == "stars":
            stars = _number(value)
            if stars is None or not 1 <= stars <= 5:
                query.errors.append("stars: needs a number of stars from 1 to 5.")
                continue
            query.stars = stars
        else:
            setattr(query, key, value)
        query.chips.append((key, value))
    return query


def _put(found: dict[str, str], key: str, value: str) -> None:
    if key in VALUE_KEYS and not value:
        return  # "artist:" alone filters nothing
    found.pop(key, None)  # keep the order the filters were last given in
    found[key] = value


def _number(value: str) -> int | None:
    return int(value) if value.isdecimal() else None
