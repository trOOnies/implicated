"""Fill a library with a few demo records: ``python -m implicated.seed [db_path]``."""

from __future__ import annotations

import os
import random
import sys
from datetime import datetime, timedelta, timezone

from implicated.library import Library
from implicated.models import AlbumIn, ArtistIn, SongIn

DEMO = [
    (
        ArtistIn(
            "Massive Attack", "United Kingdom", 1988, ["trip hop", "electronic"], favorite=True
        ),
        [
            (
                AlbumIn(0, "Mezzanine", 1998, rating=5, favorite=True),
                [("Angel", 378), ("Risingson", 298), ("Teardrop", 329)],
            ),
        ],
    ),
    (
        ArtistIn("Portishead", "United Kingdom", 1991, ["trip hop"]),
        [
            (
                AlbumIn(0, "Dummy", 1994, rating=5),
                [("Mysterons", 302), ("Sour Times", 251), ("Glory Box", 306)],
            ),
        ],
    ),
    (
        ArtistIn("Radiohead", "United Kingdom", 1985, ["alternative rock", "art rock"]),
        [
            (
                AlbumIn(0, "OK Computer", 1997, rating=5),
                [("Airbag", 284), ("Paranoid Android", 383), ("Karma Police", 261)],
            ),
            (
                AlbumIn(0, "Kid A", 2000, rating=4),
                [("Everything in Its Right Place", 251), ("Kid A", 284)],
            ),
        ],
    ),
]


def seed(lib: Library, plays: int = 120) -> None:
    song_ids = []
    for artist_in, albums in DEMO:
        artist = lib.create_artist(artist_in)
        for album_in, tracks in albums:
            album_in.artist_id = artist.id
            album = lib.create_album(album_in)
            for number, (title, seconds) in enumerate(tracks, start=1):
                song = lib.create_song(SongIn(artist.id, title, album.id, number, seconds))
                song_ids.append(song.id)
    now = datetime.now(timezone.utc)
    for _ in range(plays):
        played_at = now - timedelta(days=random.randint(0, 29), minutes=random.randint(0, 1439))
        lib.record_play(random.choice(song_ids), played_at.isoformat(timespec="seconds"))


if __name__ == "__main__":
    path = (
        sys.argv[1] if len(sys.argv) > 1 else os.environ.get("IMPLICATED_DB", "local/implicated.db")
    )
    seed(Library(path))
    print(f"Seeded demo data into {path}")
