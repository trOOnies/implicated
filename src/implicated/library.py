"""SQLite-backed storage for the music library."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from implicated.models import (
    Album,
    AlbumIn,
    AlbumSummary,
    Artist,
    ArtistIn,
    ArtistSummary,
    LibraryStats,
    Member,
    MemberIn,
    Play,
    Point,
    Song,
    SongIn,
    SongSummary,
    validate_kind,
    validate_rating,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS artists (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    country     TEXT,
    formed_year INTEGER,
    genres      TEXT NOT NULL DEFAULT '[]',
    languages   TEXT NOT NULL DEFAULT '[]',
    bio         TEXT,
    favorite    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS albums (
    id          INTEGER PRIMARY KEY,
    artist_id   INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    year        INTEGER,
    kind        TEXT NOT NULL DEFAULT 'album',
    rating      INTEGER,
    notes       TEXT,
    favorite    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    cover       TEXT
);
CREATE TABLE IF NOT EXISTS members (
    id         INTEGER PRIMARY KEY,
    artist_id  INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    position   INTEGER NOT NULL,
    name       TEXT NOT NULL,
    roles      TEXT NOT NULL DEFAULT '[]',
    from_year  INTEGER,
    to_year    INTEGER
);
CREATE TABLE IF NOT EXISTS songs (
    id               INTEGER PRIMARY KEY,
    artist_id        INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    album_id         INTEGER REFERENCES albums(id) ON DELETE CASCADE,
    title            TEXT NOT NULL,
    track_number     INTEGER,
    duration_seconds INTEGER,
    rating           INTEGER,
    notes            TEXT,
    favorite         INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plays (
    id        INTEGER PRIMARY KEY,
    song_id   INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
    played_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_albums_artist ON albums(artist_id);
CREATE INDEX IF NOT EXISTS idx_members_artist ON members(artist_id);
CREATE INDEX IF NOT EXISTS idx_songs_album ON songs(album_id);
CREATE INDEX IF NOT EXISTS idx_songs_artist ON songs(artist_id);
CREATE INDEX IF NOT EXISTS idx_plays_song ON plays(song_id);
"""

# Columns added after the first release: (table, column, declaration). Older databases get them
# through ``ALTER TABLE`` when the library opens.
MIGRATIONS = [
    ("artists", "languages", "TEXT NOT NULL DEFAULT '[]'"),
    ("albums", "cover", "TEXT"),
]

FAVORITE_TABLES = {"artist": "artists", "album": "albums", "song": "songs"}

COVER_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


class NotFound(LookupError):
    """Raised when a record does not exist."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _artist(row: sqlite3.Row) -> Artist:
    data = dict(row)
    data["genres"] = json.loads(data["genres"])
    data["languages"] = json.loads(data["languages"])
    data["favorite"] = bool(data["favorite"])
    return Artist(**data)


def _member(row: sqlite3.Row) -> Member:
    data = dict(row)
    del data["position"]
    data["roles"] = json.loads(data["roles"])
    return Member(**data)


def _clean_list(values: list[str]) -> str:
    return json.dumps([v.strip() for v in values if v.strip()])


def _album(row: sqlite3.Row) -> Album:
    data = dict(row)
    data["favorite"] = bool(data["favorite"])
    return Album(**data)


def _song(row: sqlite3.Row) -> Song:
    data = dict(row)
    data["favorite"] = bool(data["favorite"])
    return Song(**data)


def _split(row: sqlite3.Row, prefix: str) -> tuple[dict, dict]:
    """Split a joined row into the record's own columns and the ``prefix``-ed extras."""
    own, extra = {}, {}
    for key in row.keys():
        if key.startswith(prefix):
            extra[key.removeprefix(prefix)] = row[key]
        else:
            own[key] = row[key]
    return own, extra


class Library:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.covers_dir = self.path.parent / "covers"
        self._local = threading.local()
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            for table, column, declaration in MIGRATIONS:
                columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """A connection committed on success; inside ``transaction()``, the shared one."""
        shared = getattr(self._local, "conn", None)
        if shared is not None:
            yield shared
            return
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Run several library calls on one connection, committed together at the end.

        Much faster for bulk work (each call otherwise opens, commits and closes its own
        connection), and an error rolls everything back.
        """
        if getattr(self._local, "conn", None) is not None:
            yield
            return
        with self.connect() as conn:
            self._local.conn = conn
            try:
                yield
            finally:
                self._local.conn = None

    # Artists

    def list_artists(self, q: str | None = None) -> list[ArtistSummary]:
        sql = """
            SELECT a.*,
                   (SELECT COUNT(*) FROM albums WHERE artist_id = a.id) AS x_albums,
                   (SELECT COUNT(*) FROM songs WHERE artist_id = a.id) AS x_songs,
                   (SELECT COUNT(*) FROM plays p JOIN songs s ON s.id = p.song_id
                     WHERE s.artist_id = a.id) AS x_plays
              FROM artists a
             WHERE (:q IS NULL OR a.name LIKE '%' || :q || '%')
             ORDER BY a.favorite DESC, a.name COLLATE NOCASE
        """
        with self.connect() as conn:
            rows = conn.execute(sql, {"q": q or None}).fetchall()
        out = []
        for row in rows:
            own, extra = _split(row, "x_")
            out.append(ArtistSummary(_artist(own), extra["albums"], extra["songs"], extra["plays"]))
        return out

    def get_artist(self, artist_id: int) -> Artist:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM artists WHERE id = ?", (artist_id,)).fetchone()
        if row is None:
            raise NotFound(f"artist {artist_id} not found")
        return _artist(row)

    def find_artist(self, name: str) -> Artist | None:
        """Look an artist up by name, ignoring case."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM artists WHERE name = ? COLLATE NOCASE ORDER BY id", (name.strip(),)
            ).fetchone()
        return _artist(row) if row else None

    def create_artist(self, data: ArtistIn) -> Artist:
        values = self._artist_values(data)
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO artists (name, country, formed_year, genres, languages, bio,
                                        favorite, created_at)
                   VALUES (:name, :country, :formed_year, :genres, :languages, :bio,
                           :favorite, :now)""",
                {**values, "now": _now()},
            )
        return self.get_artist(cur.lastrowid)

    def update_artist(self, artist_id: int, data: ArtistIn) -> Artist:
        self.get_artist(artist_id)
        with self.connect() as conn:
            conn.execute(
                """UPDATE artists SET name = :name, country = :country,
                       formed_year = :formed_year, genres = :genres,
                       languages = :languages, bio = :bio, favorite = :favorite
                   WHERE id = :id""",
                {**self._artist_values(data), "id": artist_id},
            )
        return self.get_artist(artist_id)

    def delete_artist(self, artist_id: int) -> None:
        covers = [a.album.cover for a in self.list_albums(artist_id)]
        self._delete("artists", artist_id)
        self._remove_covers(covers)

    @staticmethod
    def _artist_values(data: ArtistIn) -> dict:
        if not data.name.strip():
            raise ValueError("name is required")
        values = asdict(data)
        values["name"] = data.name.strip()
        values["genres"] = _clean_list(data.genres)
        values["languages"] = _clean_list(data.languages)
        values["favorite"] = int(data.favorite)
        return values

    # Members

    def list_members(self, artist_id: int) -> list[Member]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM members WHERE artist_id = ? ORDER BY position", (artist_id,)
            ).fetchall()
        return [_member(row) for row in rows]

    def set_members(self, artist_id: int, members: list[MemberIn]) -> list[Member]:
        """Replace the artist's line-up with ``members``, in that order."""
        self.get_artist(artist_id)
        rows = [self._member_values(m) for m in members]
        with self.connect() as conn:
            conn.execute("DELETE FROM members WHERE artist_id = ?", (artist_id,))
            conn.executemany(
                """INSERT INTO members (artist_id, position, name, roles, from_year, to_year)
                   VALUES (:artist_id, :position, :name, :roles, :from_year, :to_year)""",
                [
                    {**row, "artist_id": artist_id, "position": position}
                    for position, row in enumerate(rows)
                ],
            )
        return self.list_members(artist_id)

    @staticmethod
    def _member_values(data: MemberIn) -> dict:
        if not data.name.strip():
            raise ValueError("member name is required")
        if data.from_year and data.to_year and data.from_year > data.to_year:
            raise ValueError(f"{data.name} cannot leave before joining")
        values = asdict(data)
        values["name"] = data.name.strip()
        values["roles"] = _clean_list(data.roles)
        return values

    # Albums

    def list_albums(
        self, artist_id: int | None = None, favorites: bool = False, q: str | None = None
    ) -> list[AlbumSummary]:
        sql = """
            SELECT al.*, ar.name AS x_artist,
                   (SELECT COUNT(*) FROM songs WHERE album_id = al.id) AS x_songs,
                   (SELECT COALESCE(SUM(duration_seconds), 0) FROM songs
                     WHERE album_id = al.id) AS x_seconds,
                   (SELECT COUNT(*) FROM plays p JOIN songs s ON s.id = p.song_id
                     WHERE s.album_id = al.id) AS x_plays
              FROM albums al JOIN artists ar ON ar.id = al.artist_id
             WHERE (:artist_id IS NULL OR al.artist_id = :artist_id)
               AND (:favorites = 0 OR al.favorite = 1)
               AND (:q IS NULL OR al.title LIKE '%' || :q || '%'
                    OR ar.name LIKE '%' || :q || '%')
             ORDER BY al.year IS NULL, al.year, al.title COLLATE NOCASE
        """
        params = {"artist_id": artist_id, "favorites": int(favorites), "q": q or None}
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        out = []
        for row in rows:
            own, extra = _split(row, "x_")
            out.append(
                AlbumSummary(
                    _album(own), extra["artist"], extra["songs"], extra["seconds"], extra["plays"]
                )
            )
        return out

    def get_album(self, album_id: int) -> Album:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM albums WHERE id = ?", (album_id,)).fetchone()
        if row is None:
            raise NotFound(f"album {album_id} not found")
        return _album(row)

    def find_album(self, artist_id: int, title: str) -> Album | None:
        """Look one of the artist's albums up by title, ignoring case."""
        with self.connect() as conn:
            row = conn.execute(
                """SELECT * FROM albums WHERE artist_id = ? AND title = ? COLLATE NOCASE
                    ORDER BY id""",
                (artist_id, title.strip()),
            ).fetchone()
        return _album(row) if row else None

    def create_album(self, data: AlbumIn) -> Album:
        values = self._album_values(data)
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO albums (artist_id, title, year, kind, rating, notes, favorite,
                                       created_at)
                   VALUES (:artist_id, :title, :year, :kind, :rating, :notes, :favorite, :now)""",
                {**values, "now": _now()},
            )
        return self.get_album(cur.lastrowid)

    def update_album(self, album_id: int, data: AlbumIn) -> Album:
        self.get_album(album_id)
        with self.connect() as conn:
            conn.execute(
                """UPDATE albums SET artist_id = :artist_id, title = :title, year = :year,
                       kind = :kind, rating = :rating, notes = :notes, favorite = :favorite
                   WHERE id = :id""",
                {**self._album_values(data), "id": album_id},
            )
        return self.get_album(album_id)

    def delete_album(self, album_id: int) -> None:
        cover = self.get_album(album_id).cover
        self._delete("albums", album_id)
        self._remove_covers([cover])

    def set_cover(self, album_id: int, source: str | Path) -> Album:
        """Copy ``source`` into the covers directory as the album's cover.

        Files are named after their content, so setting the same image again is a no-op and a
        changed image gets a new URL (no stale browser caches).
        """
        source = Path(source)
        album = self.get_album(album_id)
        suffix = source.suffix.lower()
        if suffix not in COVER_TYPES:
            raise ValueError(f"cover must be one of {', '.join(COVER_TYPES)}")
        data = source.read_bytes()
        name = f"{album_id}-{hashlib.sha256(data).hexdigest()[:12]}{suffix}"
        if name == album.cover:
            return album
        self.covers_dir.mkdir(parents=True, exist_ok=True)
        (self.covers_dir / name).write_bytes(data)
        with self.connect() as conn:
            conn.execute("UPDATE albums SET cover = ? WHERE id = ?", (name, album_id))
        self._remove_covers([album.cover])
        return self.get_album(album_id)

    def cover_path(self, album_id: int) -> Path:
        album = self.get_album(album_id)
        path = self.covers_dir / album.cover if album.cover else None
        if path is None or not path.is_file():
            raise NotFound(f"album {album_id} has no cover")
        return path

    def _remove_covers(self, names: list[str | None]) -> None:
        for name in names:
            if name:
                (self.covers_dir / name).unlink(missing_ok=True)

    def _album_values(self, data: AlbumIn) -> dict:
        if not data.title.strip():
            raise ValueError("title is required")
        validate_rating(data.rating)
        validate_kind(data.kind)
        self.get_artist(data.artist_id)
        values = asdict(data)
        values["title"] = data.title.strip()
        values["favorite"] = int(data.favorite)
        return values

    # Songs

    def list_songs(
        self,
        *,
        artist_id: int | None = None,
        album_id: int | None = None,
        q: str | None = None,
        favorites: bool = False,
        order: str = "title",
    ) -> list[SongSummary]:
        order_by = {
            "title": "s.title COLLATE NOCASE",
            "track": "s.track_number IS NULL, s.track_number, s.title COLLATE NOCASE",
            "plays": "x_plays DESC, s.title COLLATE NOCASE",
            "recent": "x_last_played IS NULL, x_last_played DESC",
        }[order]
        sql = f"""
            SELECT s.*, ar.name AS x_artist, al.title AS x_album,
                   (SELECT COUNT(*) FROM plays WHERE song_id = s.id) AS x_plays,
                   (SELECT MAX(played_at) FROM plays WHERE song_id = s.id) AS x_last_played
              FROM songs s
              JOIN artists ar ON ar.id = s.artist_id
              LEFT JOIN albums al ON al.id = s.album_id
             WHERE (:artist_id IS NULL OR s.artist_id = :artist_id)
               AND (:album_id IS NULL OR s.album_id = :album_id)
               AND (:favorites = 0 OR s.favorite = 1)
               AND (:q IS NULL OR s.title LIKE '%' || :q || '%'
                    OR ar.name LIKE '%' || :q || '%'
                    OR al.title LIKE '%' || :q || '%')
             ORDER BY {order_by}
        """
        params = {
            "artist_id": artist_id,
            "album_id": album_id,
            "favorites": int(favorites),
            "q": q or None,
        }
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        out = []
        for row in rows:
            own, extra = _split(row, "x_")
            out.append(
                SongSummary(
                    _song(own),
                    extra["artist"],
                    extra["album"],
                    extra["plays"],
                    extra["last_played"],
                )
            )
        return out

    def get_song(self, song_id: int) -> Song:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
        if row is None:
            raise NotFound(f"song {song_id} not found")
        return _song(row)

    def find_song(self, artist_id: int, album_id: int | None, title: str) -> Song | None:
        """Look a song up by title within an album (or the artist's singles), ignoring case."""
        with self.connect() as conn:
            row = conn.execute(
                """SELECT * FROM songs WHERE artist_id = ? AND album_id IS ?
                      AND title = ? COLLATE NOCASE ORDER BY id""",
                (artist_id, album_id, title.strip()),
            ).fetchone()
        return _song(row) if row else None

    def get_song_summary(self, song_id: int) -> SongSummary:
        song = self.get_song(song_id)
        for summary in self.list_songs(artist_id=song.artist_id):
            if summary.song.id == song_id:
                return summary
        raise NotFound(f"song {song_id} not found")

    def create_song(self, data: SongIn) -> Song:
        values = self._song_values(data)
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO songs (artist_id, album_id, title, track_number,
                                      duration_seconds, rating, notes, favorite, created_at)
                   VALUES (:artist_id, :album_id, :title, :track_number, :duration_seconds,
                           :rating, :notes, :favorite, :now)""",
                {**values, "now": _now()},
            )
        return self.get_song(cur.lastrowid)

    def update_song(self, song_id: int, data: SongIn) -> Song:
        self.get_song(song_id)
        with self.connect() as conn:
            conn.execute(
                """UPDATE songs SET artist_id = :artist_id, album_id = :album_id,
                       title = :title, track_number = :track_number,
                       duration_seconds = :duration_seconds, rating = :rating,
                       notes = :notes, favorite = :favorite
                   WHERE id = :id""",
                {**self._song_values(data), "id": song_id},
            )
        return self.get_song(song_id)

    def delete_song(self, song_id: int) -> None:
        self._delete("songs", song_id)

    def _song_values(self, data: SongIn) -> dict:
        if not data.title.strip():
            raise ValueError("title is required")
        validate_rating(data.rating)
        if data.duration_seconds is not None and data.duration_seconds < 0:
            raise ValueError("duration must be positive")
        self.get_artist(data.artist_id)
        if data.album_id is not None:
            album = self.get_album(data.album_id)
            if album.artist_id != data.artist_id:
                raise ValueError("album belongs to a different artist")
        values = asdict(data)
        values["title"] = data.title.strip()
        values["favorite"] = int(data.favorite)
        return values

    # Plays

    def record_play(self, song_id: int, played_at: str | None = None) -> Play:
        self.get_song(song_id)
        played_at = played_at or _now()
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO plays (song_id, played_at) VALUES (?, ?)", (song_id, played_at)
            )
        return Play(cur.lastrowid, song_id, played_at)

    def recent_plays(self, limit: int = 10) -> list[tuple[Play, SongSummary]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM plays ORDER BY played_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [(Play(**dict(row)), self.get_song_summary(row["song_id"])) for row in rows]

    # Shared

    def toggle_favorite(self, kind: str, record_id: int) -> bool:
        table = FAVORITE_TABLES[kind]
        with self.connect() as conn:
            cur = conn.execute(
                f"UPDATE {table} SET favorite = 1 - favorite WHERE id = ?", (record_id,)
            )
            if cur.rowcount == 0:
                raise NotFound(f"{kind} {record_id} not found")
            row = conn.execute(f"SELECT favorite FROM {table} WHERE id = ?", (record_id,))
            return bool(row.fetchone()[0])

    def _delete(self, table: str, record_id: int) -> None:
        with self.connect() as conn:
            cur = conn.execute(f"DELETE FROM {table} WHERE id = ?", (record_id,))
        if cur.rowcount == 0:
            raise NotFound(f"{table[:-1]} {record_id} not found")

    # Stats

    def stats(self, days: int = 30, top: int = 8) -> LibraryStats:
        today = datetime.now(timezone.utc).date()
        start = today - timedelta(days=days - 1)
        with self.connect() as conn:
            counts = conn.execute(
                """SELECT (SELECT COUNT(*) FROM artists), (SELECT COUNT(*) FROM albums),
                          (SELECT COUNT(*) FROM songs), (SELECT COUNT(*) FROM plays),
                          (SELECT COALESCE(SUM(duration_seconds), 0) FROM songs)"""
            ).fetchone()
            per_day = dict(
                conn.execute(
                    """SELECT substr(played_at, 1, 10) AS day, COUNT(*) FROM plays
                        WHERE played_at >= ? GROUP BY day""",
                    (start.isoformat(),),
                ).fetchall()
            )
            top_artists = conn.execute(
                """SELECT ar.name, COUNT(*) AS n FROM plays p
                     JOIN songs s ON s.id = p.song_id JOIN artists ar ON ar.id = s.artist_id
                    GROUP BY ar.id ORDER BY n DESC, ar.name LIMIT ?""",
                (top,),
            ).fetchall()
            songs_per_artist = conn.execute(
                """SELECT ar.name, COUNT(s.id) AS n FROM artists ar
                     LEFT JOIN songs s ON s.artist_id = ar.id
                    GROUP BY ar.id ORDER BY n DESC, ar.name LIMIT ?""",
                (top,),
            ).fetchall()
            genre_lists = [json.loads(g) for (g,) in conn.execute("SELECT genres FROM artists")]
            decades = conn.execute(
                """SELECT (year / 10) * 10 AS decade, COUNT(*) FROM albums
                    WHERE year IS NOT NULL GROUP BY decade ORDER BY decade"""
            ).fetchall()
            ratings = dict(
                conn.execute(
                    "SELECT rating, COUNT(*) FROM albums WHERE rating IS NOT NULL GROUP BY rating"
                ).fetchall()
            )
        return LibraryStats(
            artists=counts[0],
            albums=counts[1],
            songs=counts[2],
            plays=counts[3],
            total_seconds=counts[4],
            plays_per_day=[
                Point(_short_day(day), per_day.get(day.isoformat(), 0))
                for day in (start + timedelta(days=i) for i in range(days))
            ],
            top_artists=[Point(name, n) for name, n in top_artists],
            songs_per_artist=[Point(name, n) for name, n in songs_per_artist],
            genres=_count_genres(genre_lists, top),
            albums_per_decade=[Point(f"{decade}s", n) for decade, n in decades],
            ratings=[Point("★" * r if r else "–", ratings.get(r, 0)) for r in range(5, -1, -1)],
        )


def _count_genres(genre_lists: list[list[str]], top: int) -> list[Point]:
    """How many artists play each genre; spellings differing only in case count together."""
    counts: Counter[str] = Counter()
    names: dict[str, str] = {}
    for genres in genre_lists:
        for genre in {g.casefold(): g for g in genres}.values():
            names.setdefault(genre.casefold(), genre)
            counts[genre.casefold()] += 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [Point(names[key], n) for key, n in ranked[:top]]


def _short_day(day: date) -> str:
    return day.strftime("%b %d")
