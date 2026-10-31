"""SQLite-backed storage for the music library."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, fields
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from implicated.models import (
    FORMATS,
    SPECIAL_PLAYLISTS,
    Album,
    AlbumIn,
    AlbumSummary,
    Artist,
    ArtistIn,
    ArtistSummary,
    Budget,
    BudgetDetail,
    BudgetIn,
    BudgetItem,
    BudgetOverlap,
    Cart,
    CartItem,
    CartItemIn,
    CartLine,
    CartOption,
    LibraryStats,
    Member,
    MemberIn,
    Play,
    Playlist,
    PlaylistDetail,
    PlaylistIn,
    PlaylistSummary,
    Point,
    Song,
    SongIn,
    SongSummary,
    SongVideo,
    SongVideoIn,
    lyrics_file_name,
    shop_link,
    validate_formed_year,
    validate_kind,
    validate_ownership,
    validate_price,
    validate_quantity,
    validate_rating,
    validate_wiki,
    youtube_id,
    youtube_start,
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
    wiki        TEXT,
    favorite    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    image       TEXT,
    notes       TEXT,
    listen_later INTEGER NOT NULL DEFAULT 0,
    rating      INTEGER
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
    cover       TEXT,
    cd          TEXT,
    vinyl       TEXT,
    listen_later INTEGER NOT NULL DEFAULT 0,
    unreleased  INTEGER NOT NULL DEFAULT 0
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
    created_at       TEXT NOT NULL,
    lyrics           TEXT,
    no_lyrics        INTEGER NOT NULL DEFAULT 0,
    mv               TEXT,
    listen_later     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS song_videos (
    id            INTEGER PRIMARY KEY,
    song_id       INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
    position      INTEGER NOT NULL,
    youtube       TEXT NOT NULL,
    title         TEXT,
    start_seconds INTEGER,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plays (
    id        INTEGER PRIMARY KEY,
    song_id   INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
    played_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS playlists (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    kind       TEXT
);
CREATE TABLE IF NOT EXISTS playlist_songs (
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    song_id     INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    PRIMARY KEY (playlist_id, song_id)
);
CREATE TABLE IF NOT EXISTS cart_items (
    id          INTEGER PRIMARY KEY,
    album_id    INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    format      TEXT NOT NULL,
    price_cents INTEGER NOT NULL,
    quantity    INTEGER NOT NULL DEFAULT 1,
    link        TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE (album_id, format)
);
CREATE TABLE IF NOT EXISTS cart (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    shipping_cents INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS budgets (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    bought_at  TEXT,
    in_cart    INTEGER NOT NULL DEFAULT 0,
    shipping_cents INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS budget_items (
    id          INTEGER PRIMARY KEY,
    budget_id   INTEGER NOT NULL REFERENCES budgets(id) ON DELETE CASCADE,
    album_id    INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
    format      TEXT NOT NULL,
    price_cents INTEGER NOT NULL,
    quantity    INTEGER NOT NULL DEFAULT 1,
    link        TEXT
);
CREATE INDEX IF NOT EXISTS idx_budget_items_budget ON budget_items(budget_id);
CREATE INDEX IF NOT EXISTS idx_albums_artist ON albums(artist_id);
CREATE INDEX IF NOT EXISTS idx_members_artist ON members(artist_id);
CREATE INDEX IF NOT EXISTS idx_songs_album ON songs(album_id);
CREATE INDEX IF NOT EXISTS idx_songs_artist ON songs(artist_id);
CREATE INDEX IF NOT EXISTS idx_plays_song ON plays(song_id);
CREATE INDEX IF NOT EXISTS idx_playlist_songs_song ON playlist_songs(song_id);
CREATE INDEX IF NOT EXISTS idx_song_videos_song ON song_videos(song_id);
"""

# Columns added after the first release: (table, column, declaration). Older databases get them
# through ``ALTER TABLE`` when the library opens.
MIGRATIONS = [
    ("artists", "languages", "TEXT NOT NULL DEFAULT '[]'"),
    ("albums", "cover", "TEXT"),
    ("artists", "wiki", "TEXT"),
    ("artists", "image", "TEXT"),
    ("songs", "lyrics", "TEXT"),
    ("songs", "no_lyrics", "INTEGER NOT NULL DEFAULT 0"),
    ("artists", "notes", "TEXT"),
    ("artists", "listen_later", "INTEGER NOT NULL DEFAULT 0"),
    ("albums", "cd", "TEXT"),
    ("albums", "vinyl", "TEXT"),
    ("albums", "listen_later", "INTEGER NOT NULL DEFAULT 0"),
    ("songs", "mv", "TEXT"),
    ("songs", "listen_later", "INTEGER NOT NULL DEFAULT 0"),
    ("playlists", "kind", "TEXT"),
    ("artists", "rating", "INTEGER"),
    ("albums", "unreleased", "INTEGER NOT NULL DEFAULT 0"),
    ("cart_items", "quantity", "INTEGER NOT NULL DEFAULT 1"),
    ("budgets", "shipping_cents", "INTEGER NOT NULL DEFAULT 0"),
]
# Columns that changed names: (table, old name, new name), renamed before ``MIGRATIONS`` run.
RENAMES = [("songs", "youtube", "mv")]

# Who ``list_artists`` and ``count_artists`` look at, and which songs ``list_songs`` and
# ``count_songs`` do. The filters are ``None`` (or 0) when not in use.
ARTISTS_WHERE = """
    FROM artists a
   WHERE (:q IS NULL OR a.name LIKE '%' || :q || '%')
     AND (:country IS NULL OR a.country = :country COLLATE NOCASE)
     AND (:genre IS NULL OR EXISTS (SELECT 1 FROM json_each(a.genres)
                                     WHERE value = :genre COLLATE NOCASE))
     AND (:language IS NULL OR EXISTS (SELECT 1 FROM json_each(a.languages)
                                        WHERE value = :language COLLATE NOCASE))
"""
SONGS_WHERE = """
    FROM songs s
    JOIN artists ar ON ar.id = s.artist_id
    LEFT JOIN albums al ON al.id = s.album_id
    LEFT JOIN playlist_songs ps ON ps.song_id = s.id AND ps.playlist_id = :playlist_id
   WHERE (:artist_id IS NULL OR s.artist_id = :artist_id)
     AND (:album_id IS NULL OR s.album_id = :album_id)
     AND (:playlist_id IS NULL OR ps.playlist_id IS NOT NULL)
     AND (:song_ids IS NULL OR s.id IN (SELECT value FROM json_each(:song_ids)))
     AND (:favorites = 0 OR s.favorite = 1)
     AND (:listen_later = 0 OR s.listen_later = 1)
     AND (:missing_lyrics = 0 OR (s.lyrics IS NULL AND s.no_lyrics = 0))
     AND (:q IS NULL OR s.title LIKE '%' || :q || '%'
          OR ar.name LIKE '%' || :q || '%'
          OR al.title LIKE '%' || :q || '%')
"""

# Cover file names are ``<album id>-<content hash><suffix>`` (see ``_store_image``), so two albums
# share a cover when their names agree after the first dash. Compares ``al`` with ``o``.
SAME_COVER = "substr(o.cover, instr(o.cover, '-')) = substr(al.cover, instr(al.cover, '-'))"

# Dashes that count as the same one when looking a title up, so an en dash turned into a hyphen
# in ``albums.yaml`` renames the album instead of importing it again. See ``_same_title``.
DASHES = ("\u2013", "\u2014")


def _same_title(column: str) -> str:
    """SQL matching ``column`` to a ``_dashless`` title, ignoring case and kinds of dash."""
    for dash in DASHES:
        column = f"replace({column}, '{dash}', '-')"
    return f"{column} = ? COLLATE NOCASE"


def _dashless(title: str) -> str:
    title = title.strip()
    for dash in DASHES:
        title = title.replace(dash, "-")
    return title


FAVORITE_TABLES = {"artist": "artists", "album": "albums", "song": "songs"}
# Yes/no marks that can be switched with one click.
FLAGS = ("favorite", "listen_later")

# Album covers and artist images.
IMAGE_TYPES = {
    ".avif": "image/avif",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
# Guitar Pro files a song's tab can be (TuxGuitar saves ``.gp5``); the first one found wins.
TAB_TYPES = (".gp5", ".gp", ".gpx", ".gp4", ".gp3")


class NotFound(LookupError):
    """Raised when a record does not exist."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _artist(row: sqlite3.Row) -> Artist:
    data = dict(row)
    data["genres"] = json.loads(data["genres"])
    data["languages"] = json.loads(data["languages"])
    data["favorite"] = bool(data["favorite"])
    data["listen_later"] = bool(data["listen_later"])
    return Artist(**data)


def _video(row: sqlite3.Row) -> SongVideo:
    data = dict(row)
    del data["position"]
    return SongVideo(**data)


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
    data["listen_later"] = bool(data["listen_later"])
    data["unreleased"] = bool(data["unreleased"])
    return Album(**data)


def _cents(price: float) -> int:
    return round(price * 100)


def _cart_item(row: sqlite3.Row) -> CartItem:
    data = dict(row)
    data["price"] = data.pop("price_cents") / 100
    return CartItem(**data)


def _budget_item(row: sqlite3.Row) -> BudgetItem:
    data = dict(row)
    data["price"] = data.pop("price_cents") / 100
    return BudgetItem(**data)


def _budget(row: sqlite3.Row) -> Budget:
    data = dict(row)
    data["in_cart"] = bool(data["in_cart"])
    data["shipping"] = data.pop("shipping_cents") / 100
    return Budget(**data)


def _budget_name(data: BudgetIn) -> str:
    name = (data.name or "").strip()
    if not name:
        raise ValueError("name is required")
    if len(name) > 100:
        raise ValueError("name must be at most 100 characters")
    return name


def _total(lines: list[CartLine], shipping: float) -> float:
    # Added up in cents, so 0.1 + 0.2 makes 0.3.
    return (sum(round(line.subtotal * 100) for line in lines) + _cents(shipping)) / 100


def _song(row: sqlite3.Row) -> Song:
    data = dict(row)
    data["favorite"] = bool(data["favorite"])
    data["no_lyrics"] = bool(data["no_lyrics"])
    data["listen_later"] = bool(data["listen_later"])
    return Song(**data)


def _find_file(folder: Path, names: list[str]) -> Path | None:
    """The first of ``names`` in ``folder``, matching exactly or else ignoring case, like the
    importer does (an existing ``our lady of sorrows.txt`` is "Our Lady of Sorrows.txt")."""
    for name in names:
        if (folder / name).exists():
            return folder / name
    if not folder.is_dir():
        return None
    files = {path.name.casefold(): path for path in folder.iterdir()}
    return next((files[n.casefold()] for n in names if n.casefold() in files), None)


def _move_file(old: Path, new: Path, copy: bool) -> None:
    """Move (or copy) ``old`` to ``new``, unless ``new`` is already there."""
    if new.exists():
        return
    new.parent.mkdir(parents=True, exist_ok=True)
    if copy:
        new.write_bytes(old.read_bytes())
    else:
        old.rename(new)


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
    def __init__(self, path: str | Path, info_dir: str | Path | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.covers_dir = self.path.parent / "covers"
        self.images_dir = self.path.parent / "artists"
        # The folder ``implicated.importer`` reads. When set, every song has a lyrics file in
        # ``bands/<artist>/songs/``: one is created (empty) along with the song if missing.
        self.info_dir = Path(info_dir) if info_dir is not None else None
        self._local = threading.local()
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            for table, old, new in RENAMES:
                columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                if old in columns and new not in columns:
                    conn.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
            for table, column, declaration in MIGRATIONS:
                columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_playlists_kind ON playlists(kind)")
            for kind, (name, _field) in SPECIAL_PLAYLISTS.items():
                conn.execute(
                    """INSERT INTO playlists (name, created_at, kind) SELECT ?, ?, ?
                        WHERE NOT EXISTS (SELECT 1 FROM playlists WHERE kind = ?)""",
                    (name, _now(), kind, kind),
                )
            conn.execute("INSERT OR IGNORE INTO cart (id) VALUES (1)")
        self._sync_marks()

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

    def list_artists(
        self,
        q: str | None = None,
        *,
        country: str | None = None,
        genre: str | None = None,
        language: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[ArtistSummary]:
        """Artists, optionally only those from ``country`` or with ``genre`` or ``language``
        among theirs (all ignoring case), and only ``limit`` of them after the first ``offset``."""
        sql = f"""
            SELECT a.*,
                   (SELECT COUNT(*) FROM albums WHERE artist_id = a.id) AS x_albums,
                   (SELECT COUNT(*) FROM songs WHERE artist_id = a.id) AS x_songs,
                   (SELECT COUNT(*) FROM plays p JOIN songs s ON s.id = p.song_id
                     WHERE s.artist_id = a.id) AS x_plays
              {ARTISTS_WHERE}
             ORDER BY a.favorite DESC, a.name COLLATE NOCASE, a.id
             LIMIT :limit OFFSET :offset
        """
        params = {
            **self._artist_filters(q, country, genre, language),
            "limit": -1 if limit is None else limit,
            "offset": offset,
        }
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        out = []
        for row in rows:
            own, extra = _split(row, "x_")
            out.append(ArtistSummary(_artist(own), extra["albums"], extra["songs"], extra["plays"]))
        return out

    def count_artists(
        self,
        q: str | None = None,
        *,
        country: str | None = None,
        genre: str | None = None,
        language: str | None = None,
    ) -> int:
        """How many artists ``list_artists`` finds without a ``limit``."""
        params = self._artist_filters(q, country, genre, language)
        with self.connect() as conn:
            return conn.execute(f"SELECT COUNT(*) {ARTISTS_WHERE}", params).fetchone()[0]

    @staticmethod
    def _artist_filters(
        q: str | None, country: str | None, genre: str | None, language: str | None
    ) -> dict:
        return {
            "q": q or None,
            "country": country or None,
            "genre": genre or None,
            "language": language or None,
        }

    def artist_options(self) -> dict[str, list[str]]:
        """The origins, genres and languages of every artist, for the Artists page filters."""
        with self.connect() as conn:
            rows = conn.execute("SELECT country, genres, languages FROM artists").fetchall()

        def sorted_set(values) -> list[str]:
            unique = {v.casefold(): v for v in values if v}
            return sorted(unique.values(), key=str.casefold)

        return {
            "countries": sorted_set(row["country"] for row in rows),
            "genres": sorted_set(g for row in rows for g in json.loads(row["genres"])),
            "languages": sorted_set(lang for row in rows for lang in json.loads(row["languages"])),
        }

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
                                        wiki, favorite, notes, listen_later, rating,
                                        created_at)
                   VALUES (:name, :country, :formed_year, :genres, :languages, :bio,
                           :wiki, :favorite, :notes, :listen_later, :rating, :now)""",
                {**values, "now": _now()},
            )
        return self.get_artist(cur.lastrowid)

    def update_artist(self, artist_id: int, data: ArtistIn) -> Artist:
        self.get_artist(artist_id)
        with self.connect() as conn:
            conn.execute(
                """UPDATE artists SET name = :name, country = :country,
                       formed_year = :formed_year, genres = :genres,
                       languages = :languages, bio = :bio, wiki = :wiki,
                       favorite = :favorite, notes = :notes, listen_later = :listen_later,
                       rating = :rating
                   WHERE id = :id""",
                {**self._artist_values(data), "id": artist_id},
            )
        return self.get_artist(artist_id)

    def delete_artist(self, artist_id: int) -> None:
        image = self.get_artist(artist_id).image
        covers = [a.album.cover for a in self.list_albums(artist_id)]
        self._delete("artists", artist_id)
        self._remove_files(self.covers_dir, covers)
        self._remove_files(self.images_dir, [image])

    def set_image(self, artist_id: int, source: str | Path) -> Artist:
        """Copy ``source`` into the artist images directory as the artist's picture; works
        like ``set_cover``."""
        artist = self.get_artist(artist_id)
        name = self._store_image(self.images_dir, artist_id, Path(source), artist.image)
        if name != artist.image:
            with self.connect() as conn:
                conn.execute("UPDATE artists SET image = ? WHERE id = ?", (name, artist_id))
            self._remove_files(self.images_dir, [artist.image])
        return self.get_artist(artist_id)

    def image_path(self, artist_id: int) -> Path:
        artist = self.get_artist(artist_id)
        path = self.images_dir / artist.image if artist.image else None
        if path is None or not path.is_file():
            raise NotFound(f"artist {artist_id} has no image")
        return path

    @staticmethod
    def _artist_values(data: ArtistIn) -> dict:
        if not data.name.strip():
            raise ValueError("name is required")
        wiki = data.wiki.strip() if data.wiki else None
        validate_wiki(wiki)
        validate_rating(data.rating)
        validate_formed_year(data.formed_year)
        values = asdict(data)
        values["name"] = data.name.strip()
        values["wiki"] = wiki
        values["genres"] = _clean_list(data.genres)
        values["languages"] = _clean_list(data.languages)
        values["favorite"] = int(data.favorite)
        values["listen_later"] = int(data.listen_later)
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
        self,
        artist_id: int | None = None,
        favorites: bool = False,
        q: str | None = None,
        *,
        cd: str | None = None,
        vinyl: str | None = None,
        listen_later: bool = False,
        missing_lyrics: bool = False,
        no_own_cover: bool = False,
    ) -> list[AlbumSummary]:
        """Albums, optionally only those with the given CD or vinyl ownership (see
        ``OWNERSHIP``), the favorites, the ones marked to listen to later, the ones with songs
        still missing their lyrics or the ones without a cover of their own (none, or the same
        image as another album)."""
        sql = f"""
            SELECT al.*, ar.name AS x_artist,
                   (SELECT COUNT(*) FROM songs WHERE album_id = al.id) AS x_songs,
                   (SELECT COALESCE(SUM(duration_seconds), 0) FROM songs
                     WHERE album_id = al.id) AS x_seconds,
                   (SELECT COUNT(*) FROM plays p JOIN songs s ON s.id = p.song_id
                     WHERE s.album_id = al.id) AS x_plays,
                   (SELECT COUNT(*) FROM songs WHERE album_id = al.id
                       AND lyrics IS NOT NULL AND no_lyrics = 0) AS x_lyrics,
                   (SELECT COUNT(*) FROM songs WHERE album_id = al.id
                       AND no_lyrics = 1) AS x_no_lyrics,
                   EXISTS (SELECT 1 FROM albums o WHERE o.id != al.id
                              AND {SAME_COVER}) AS x_shared_cover
              FROM albums al JOIN artists ar ON ar.id = al.artist_id
             WHERE (:artist_id IS NULL OR al.artist_id = :artist_id)
               AND (:favorites = 0 OR al.favorite = 1)
               AND (:listen_later = 0 OR al.listen_later = 1)
               AND (:cd IS NULL OR al.cd = :cd)
               AND (:vinyl IS NULL OR al.vinyl = :vinyl)
               AND (:missing_lyrics = 0 OR EXISTS (SELECT 1 FROM songs WHERE album_id = al.id
                                                       AND lyrics IS NULL AND no_lyrics = 0))
               AND (:no_own_cover = 0 OR al.cover IS NULL OR x_shared_cover)
               AND (:q IS NULL OR al.title LIKE '%' || :q || '%'
                    OR ar.name LIKE '%' || :q || '%')
             ORDER BY al.year IS NULL, al.year, al.title COLLATE NOCASE
        """
        params = {
            "artist_id": artist_id,
            "favorites": int(favorites),
            "listen_later": int(listen_later),
            "cd": cd,
            "vinyl": vinyl,
            "missing_lyrics": int(missing_lyrics),
            "no_own_cover": int(no_own_cover),
            "q": q or None,
        }
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        out = []
        for row in rows:
            own, extra = _split(row, "x_")
            out.append(
                AlbumSummary(
                    _album(own),
                    extra["artist"],
                    extra["songs"],
                    extra["seconds"],
                    extra["plays"],
                    extra["lyrics"],
                    extra["no_lyrics"],
                    bool(extra["shared_cover"]),
                )
            )
        return out

    def album_neighbors(self, album_id: int) -> tuple[Album | None, Album | None]:
        """The artist's albums before and after this one, in the order ``list_albums`` gives
        (by year, then title); ``None`` past either end of the discography."""
        album = self.get_album(album_id)
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM albums WHERE artist_id = ?
                    ORDER BY year IS NULL, year, title COLLATE NOCASE, id""",
                (album.artist_id,),
            ).fetchall()
        i = [row["id"] for row in rows].index(album_id)
        return (
            _album(rows[i - 1]) if i > 0 else None,
            _album(rows[i + 1]) if i + 1 < len(rows) else None,
        )

    def same_cover_albums(self, album_id: int) -> list[Album]:
        """The other albums whose cover is the same image as this album's."""
        with self.connect() as conn:
            rows = conn.execute(
                f"""SELECT o.* FROM albums al JOIN albums o ON o.id != al.id AND {SAME_COVER}
                     WHERE al.id = ? ORDER BY o.year IS NULL, o.year, o.title COLLATE NOCASE""",
                (album_id,),
            ).fetchall()
        return [_album(row) for row in rows]

    def get_album(self, album_id: int) -> Album:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM albums WHERE id = ?", (album_id,)).fetchone()
        if row is None:
            raise NotFound(f"album {album_id} not found")
        return _album(row)

    def find_album(self, artist_id: int, title: str) -> Album | None:
        """Look one of the artist's albums up by title, ignoring case and kinds of dash."""
        with self.connect() as conn:
            row = conn.execute(
                f"""SELECT * FROM albums WHERE artist_id = ? AND {_same_title("title")}
                     ORDER BY id""",
                (artist_id, _dashless(title)),
            ).fetchone()
        return _album(row) if row else None

    def create_album(self, data: AlbumIn) -> Album:
        values = self._album_values(data)
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO albums (artist_id, title, year, kind, unreleased, rating, notes,
                                       favorite, cd, vinyl, listen_later, created_at)
                   VALUES (:artist_id, :title, :year, :kind, :unreleased, :rating, :notes,
                           :favorite, :cd, :vinyl, :listen_later, :now)""",
                {**values, "now": _now()},
            )
        return self.get_album(cur.lastrowid)

    def copy_album(self, album_id: int, title: str) -> Album:
        """Make a new album of the same artist called ``title`` with everything the album has:
        its details, cover and songs, with their details, lyrics and videos. Plays and playlist
        entries stay with the original (copied favorites and listen later marks still land in
        those playlists)."""
        album = self.get_album(album_id)
        title = title.strip()
        if _dashless(title).casefold() == _dashless(album.title).casefold():
            raise ValueError("the copy needs a name of its own")
        if self.find_album(album.artist_id, title):
            raise ValueError(f"there is already an album called {title}")
        details = {f.name: getattr(album, f.name) for f in fields(AlbumIn)}
        songs = self.list_songs(album_id=album_id, order="track")
        with self.transaction():
            copy = self.create_album(AlbumIn(**{**details, "title": title}))
            if album.cover:
                copy = self.set_cover(copy.id, self.covers_dir / album.cover)
            for summary in songs:
                song = summary.song
                values = {f.name: getattr(song, f.name) for f in fields(SongIn)}
                new = self.create_song(SongIn(**{**values, "album_id": copy.id}))
                self.set_lyrics(new.id, song.lyrics)
                for video in self.list_videos(song.id):
                    self.add_video(
                        new.id, SongVideoIn(video.youtube, video.title, video.start_seconds)
                    )
        return copy

    def update_album(self, album_id: int, data: AlbumIn) -> Album:
        self.get_album(album_id)
        with self.connect() as conn:
            conn.execute(
                """UPDATE albums SET artist_id = :artist_id, title = :title, year = :year,
                       kind = :kind, unreleased = :unreleased, rating = :rating, notes = :notes,
                       favorite = :favorite, cd = :cd, vinyl = :vinyl, listen_later = :listen_later
                   WHERE id = :id""",
                {**self._album_values(data), "id": album_id},
            )
        return self.get_album(album_id)

    def delete_album(self, album_id: int) -> None:
        cover = self.get_album(album_id).cover
        self._delete("albums", album_id)
        self._remove_files(self.covers_dir, [cover])

    def set_cover(self, album_id: int, source: str | Path) -> Album:
        """Copy ``source`` into the covers directory as the album's cover.

        Files are named after their content, so setting the same image again is a no-op and a
        changed image gets a new URL (no stale browser caches).
        """
        album = self.get_album(album_id)
        name = self._store_image(self.covers_dir, album_id, Path(source), album.cover, "cover")
        if name != album.cover:
            with self.connect() as conn:
                conn.execute("UPDATE albums SET cover = ? WHERE id = ?", (name, album_id))
            self._remove_files(self.covers_dir, [album.cover])
        return self.get_album(album_id)

    def cover_path(self, album_id: int) -> Path:
        album = self.get_album(album_id)
        path = self.covers_dir / album.cover if album.cover else None
        if path is None or not path.is_file():
            raise NotFound(f"album {album_id} has no cover")
        return path

    @staticmethod
    def _store_image(
        directory: Path, record_id: int, source: Path, current: str | None, what: str = "image"
    ) -> str:
        """Copy ``source`` into ``directory`` under a name made from its content, unless it is
        ``current`` already, and return that name."""
        suffix = source.suffix.lower()
        if suffix not in IMAGE_TYPES:
            raise ValueError(f"{what} must be one of {', '.join(IMAGE_TYPES)}")
        data = source.read_bytes()
        name = f"{record_id}-{hashlib.sha256(data).hexdigest()[:12]}{suffix}"
        if name != current:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / name).write_bytes(data)
        return name

    @staticmethod
    def _remove_files(directory: Path, names: list[str | None]) -> None:
        for name in names:
            if name:
                (directory / name).unlink(missing_ok=True)

    def _album_values(self, data: AlbumIn) -> dict:
        if not data.title.strip():
            raise ValueError("title is required")
        validate_rating(data.rating)
        validate_kind(data.kind)
        validate_ownership("cd", data.cd)
        validate_ownership("vinyl", data.vinyl)
        self.get_artist(data.artist_id)
        values = asdict(data)
        values["title"] = data.title.strip()
        values["favorite"] = int(data.favorite)
        values["listen_later"] = int(data.listen_later)
        values["unreleased"] = int(data.unreleased)
        return values

    def set_format(self, album_id: int, fmt: str, state: str | None) -> Album:
        """Say whether you want or own the album on ``fmt`` ("cd" or "vinyl"); ``None`` for
        neither."""
        if fmt not in FORMATS:
            raise ValueError("format must be cd or vinyl")
        validate_ownership(fmt, state)
        self.get_album(album_id)
        with self.connect() as conn:
            conn.execute(f"UPDATE albums SET {fmt} = ? WHERE id = ?", (state, album_id))
        return self.get_album(album_id)

    # Songs

    def list_songs(
        self,
        *,
        artist_id: int | None = None,
        album_id: int | None = None,
        q: str | None = None,
        favorites: bool = False,
        listen_later: bool = False,
        missing_lyrics: bool = False,
        playlist_id: int | None = None,
        song_ids: list[int] | None = None,
        order: str = "title",
        favorites_first: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[SongSummary]:
        """Songs matching every filter given (``missing_lyrics``: songs with words but no lyrics
        yet), and only ``limit`` of them after the first ``offset``; ``order="playlist"`` keeps
        the order of the songs in ``playlist_id``, and ``favorites_first`` goes before it."""
        order_by = {
            "title": "s.title COLLATE NOCASE",
            "track": "s.track_number IS NULL, s.track_number, s.title COLLATE NOCASE",
            "plays": "x_plays DESC, s.title COLLATE NOCASE",
            "recent": "x_last_played IS NULL, x_last_played DESC",
            "playlist": "ps.position",
        }[order]
        sql = f"""
            SELECT s.*, ar.name AS x_artist, al.title AS x_album, al.cover AS x_album_cover,
                   (SELECT COUNT(*) FROM plays WHERE song_id = s.id) AS x_plays,
                   (SELECT MAX(played_at) FROM plays WHERE song_id = s.id) AS x_last_played,
                   (SELECT COUNT(*) FROM song_videos WHERE song_id = s.id) AS x_videos
              {SONGS_WHERE}
             ORDER BY {"s.favorite DESC," if favorites_first else ""} {order_by}, s.id
             LIMIT :limit OFFSET :offset
        """
        params = {
            **self._song_filters(
                artist_id,
                album_id,
                q,
                favorites,
                listen_later,
                missing_lyrics,
                playlist_id,
                song_ids,
            ),
            "limit": -1 if limit is None else limit,
            "offset": offset,
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
                    extra["videos"],
                    extra["album_cover"],
                )
            )
        return out

    def count_songs(
        self,
        *,
        q: str | None = None,
        favorites: bool = False,
        listen_later: bool = False,
        missing_lyrics: bool = False,
    ) -> int:
        """How many songs ``list_songs`` finds without a ``limit``."""
        params = self._song_filters(None, None, q, favorites, listen_later, missing_lyrics)
        with self.connect() as conn:
            return conn.execute(f"SELECT COUNT(*) {SONGS_WHERE}", params).fetchone()[0]

    @staticmethod
    def _song_filters(
        artist_id: int | None,
        album_id: int | None,
        q: str | None,
        favorites: bool,
        listen_later: bool,
        missing_lyrics: bool,
        playlist_id: int | None = None,
        song_ids: list[int] | None = None,
    ) -> dict:
        return {
            "artist_id": artist_id,
            "album_id": album_id,
            "playlist_id": playlist_id,
            "song_ids": None if song_ids is None else json.dumps(song_ids),
            "favorites": int(favorites),
            "listen_later": int(listen_later),
            "missing_lyrics": int(missing_lyrics),
            "q": q or None,
        }

    def get_song(self, song_id: int) -> Song:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM songs WHERE id = ?", (song_id,)).fetchone()
        if row is None:
            raise NotFound(f"song {song_id} not found")
        return _song(row)

    def song_neighbors(self, song_id: int) -> tuple[Song | None, Song | None]:
        """The songs before and after this one on its album, in track order; ``None`` past
        either end of the album, and both ``None`` for songs without one."""
        song = self.get_song(song_id)
        if song.album_id is None:
            return None, None
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM songs WHERE album_id = ?
                    ORDER BY track_number IS NULL, track_number, title COLLATE NOCASE, id""",
                (song.album_id,),
            ).fetchall()
        i = [row["id"] for row in rows].index(song_id)
        return (
            _song(rows[i - 1]) if i > 0 else None,
            _song(rows[i + 1]) if i + 1 < len(rows) else None,
        )

    def find_song(self, artist_id: int, album_id: int | None, title: str) -> Song | None:
        """Look a song up by title within an album (or the artist's singles), ignoring case and
        kinds of dash."""
        with self.connect() as conn:
            row = conn.execute(
                f"""SELECT * FROM songs WHERE artist_id = ? AND album_id IS ?
                       AND {_same_title("title")} ORDER BY id""",
                (artist_id, album_id, _dashless(title)),
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
                                      duration_seconds, rating, notes, favorite, no_lyrics,
                                      mv, listen_later, created_at)
                   VALUES (:artist_id, :album_id, :title, :track_number, :duration_seconds,
                           :rating, :notes, :favorite, :no_lyrics, :mv, :listen_later,
                           :now)""",
                {**values, "now": _now()},
            )
        self._sync_marks(cur.lastrowid)
        song = self.get_song(cur.lastrowid)
        self.ensure_lyrics_file(song.id)
        return song

    def update_song(self, song_id: int, data: SongIn) -> Song:
        old_path = self.lyrics_path(song_id)
        old_tab = self.tab_path(song_id)
        shared = self._shares_lyrics_file(self.get_song(song_id))
        with self.connect() as conn:
            conn.execute(
                """UPDATE songs SET artist_id = :artist_id, album_id = :album_id,
                       title = :title, track_number = :track_number,
                       duration_seconds = :duration_seconds, rating = :rating,
                       notes = :notes, favorite = :favorite, no_lyrics = :no_lyrics,
                       mv = :mv, listen_later = :listen_later
                   WHERE id = :id""",
                {**self._song_values(data), "id": song_id},
            )
        self._sync_marks(song_id)
        # A renamed song takes its lyrics and tab files along (copies, if another song still
        # uses them).
        new_path = self.lyrics_path(song_id)
        if old_path and new_path and old_path.is_file():
            _move_file(old_path, new_path, copy=shared)
        if old_tab and self.tab_path(song_id) is None:
            _move_file(old_tab, self.new_tab_path(song_id, old_tab.suffix), copy=shared)
        self.ensure_lyrics_file(song_id)
        return self.get_song(song_id)

    def delete_song(self, song_id: int) -> None:
        path = self.lyrics_path(song_id)
        shared = self._shares_lyrics_file(self.get_song(song_id))
        self._delete("songs", song_id)
        # Lyrics someone wrote are kept; an empty file only belonged to this song.
        if path and not shared and path.is_file() and path.stat().st_size == 0:
            path.unlink()

    def lyrics_path(self, song_id: int) -> Path | None:
        """The song's lyrics file, whether it exists or not; ``None`` without ``info_dir``.

        Names match ignoring case, like the importer does, so an existing
        ``our lady of sorrows.txt`` is the file of "Our Lady of Sorrows".
        """
        song = self.get_song(song_id)
        if self.info_dir is None:
            return None
        folder = self._song_folder(song, "songs")
        name = lyrics_file_name(song.title)
        return _find_file(folder, [name]) or folder / name

    def tab_path(self, song_id: int) -> Path | None:
        """The song's tab, a Guitar Pro file in ``bands/<artist>/tabs/`` named like its lyrics
        file (any of ``TAB_TYPES``, ignoring case); ``None`` when it has none."""
        song = self.get_song(song_id)
        if self.info_dir is None:
            return None
        stem = lyrics_file_name(song.title).removesuffix(".txt")
        return _find_file(self._song_folder(song, "tabs"), [stem + t for t in TAB_TYPES])

    def new_tab_path(self, song_id: int, suffix: str = ".gp5") -> Path | None:
        """Where a tab for the song goes; ``None`` without ``info_dir``."""
        song = self.get_song(song_id)
        if self.info_dir is None:
            return None
        stem = lyrics_file_name(song.title).removesuffix(".txt")
        return self._song_folder(song, "tabs") / f"{stem}{suffix}"

    def _song_folder(self, song: Song, kind: str) -> Path:
        assert self.info_dir is not None
        return self.info_dir / "bands" / self.get_artist(song.artist_id).name / kind

    def _shares_lyrics_file(self, song: Song) -> bool:
        """Whether another song of the artist (the same song on a deluxe edition, say) has the
        same lyrics file."""
        name = lyrics_file_name(song.title).casefold()
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT title FROM songs WHERE artist_id = ? AND id != ?",
                (song.artist_id, song.id),
            ).fetchall()
        return any(lyrics_file_name(title).casefold() == name for (title,) in rows)

    def ensure_lyrics_file(self, song_id: int) -> Path | None:
        """Create the song's lyrics file when it is missing, holding the lyrics the library
        already has (usually none, so the file is empty)."""
        path = self.lyrics_path(song_id)
        if path is not None and not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            lyrics = self.get_song(song_id).lyrics
            path.write_text(f"{lyrics}\n" if lyrics else "", encoding="utf-8")
        return path

    def reload_lyrics(self, song_id: int) -> Song:
        """Read the song's lyrics again from its file, so edits show without a new import.

        Raises ``ValueError`` when the file is not UTF-8 text; the stored lyrics stay as they are.
        """
        path = self.ensure_lyrics_file(song_id)
        if path is None:
            return self.get_song(song_id)
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ValueError(f"{path.name} is not UTF-8 text") from None
        return self.set_lyrics(song_id, text)

    def set_lyrics(self, song_id: int, lyrics: str | None) -> Song:
        """Store the song's lyrics; blank text marks them as missing again."""
        self.get_song(song_id)
        lyrics = lyrics.strip() if lyrics else None
        with self.connect() as conn:
            conn.execute("UPDATE songs SET lyrics = ? WHERE id = ?", (lyrics or None, song_id))
        return self.get_song(song_id)

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
        values["no_lyrics"] = int(data.no_lyrics)
        values["mv"] = youtube_id(data.mv, "mv")
        values["listen_later"] = int(data.listen_later)
        return values

    # Song videos

    def list_videos(self, song_id: int) -> list[SongVideo]:
        self.get_song(song_id)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM song_videos WHERE song_id = ? ORDER BY position, id", (song_id,)
            ).fetchall()
        return [_video(row) for row in rows]

    def get_video(self, video_id: int) -> SongVideo:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM song_videos WHERE id = ?", (video_id,)).fetchone()
        if row is None:
            raise NotFound(f"video {video_id} not found")
        return _video(row)

    def add_video(self, song_id: int, data: SongVideoIn) -> SongVideo:
        """Add a video at the end of the song's list."""
        self.get_song(song_id)
        values = self._video_values(data)
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO song_videos (song_id, position, youtube, title, start_seconds,
                                            created_at)
                   VALUES (:song_id, (SELECT COALESCE(MAX(position), -1) + 1 FROM song_videos
                                       WHERE song_id = :song_id),
                           :youtube, :title, :start_seconds, :now)""",
                {**values, "song_id": song_id, "now": _now()},
            )
        return self.get_video(cur.lastrowid)

    def update_video(self, video_id: int, data: SongVideoIn) -> SongVideo:
        self.get_video(video_id)
        with self.connect() as conn:
            conn.execute(
                """UPDATE song_videos SET youtube = :youtube, title = :title,
                       start_seconds = :start_seconds
                   WHERE id = :id""",
                {**self._video_values(data), "id": video_id},
            )
        return self.get_video(video_id)

    def delete_video(self, video_id: int) -> None:
        self._delete("song_videos", video_id)

    def move_video(self, video_id: int, offset: int) -> None:
        """Move a video ``offset`` places later in its song's list (earlier when negative)."""
        video = self.get_video(video_id)
        ids = [v.id for v in self.list_videos(video.song_id)]
        index = ids.index(video_id)
        ids.insert(max(0, min(index + offset, len(ids) - 1)), ids.pop(index))
        with self.connect() as conn:
            conn.executemany(
                "UPDATE song_videos SET position = ? WHERE id = ?",
                [(position, vid) for position, vid in enumerate(ids)],
            )

    @staticmethod
    def _video_values(data: SongVideoIn) -> dict:
        video = youtube_id(data.youtube)
        if video is None:
            raise ValueError("video is required: a YouTube link or video id")
        start = data.start_seconds
        if start is None:
            start = youtube_start(data.youtube)
        if start is not None and start < 0:
            raise ValueError("start must be positive")
        return {
            "youtube": video,
            "title": data.title.strip() if data.title and data.title.strip() else None,
            "start_seconds": start or None,
        }

    # Plays

    def record_play(self, song_id: int, played_at: str | None = None) -> Play:
        """Log a listen of the song; it is no longer waiting to be listened to later."""
        self.get_song(song_id)
        played_at = played_at or _now()
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO plays (song_id, played_at) VALUES (?, ?)", (song_id, played_at)
            )
            conn.execute("UPDATE songs SET listen_later = 0 WHERE id = ?", (song_id,))
        self._sync_marks(song_id)
        return Play(cur.lastrowid, song_id, played_at)

    def record_album_play(self, album_id: int) -> list[Play]:
        """Log a listen of every song on the album, in track order."""
        self.get_album(album_id)
        played_at = _now()
        songs = self.list_songs(album_id=album_id, order="track")
        with self.transaction():
            return [self.record_play(s.song.id, played_at) for s in songs]

    def recent_plays(self, limit: int = 10) -> list[tuple[Play, SongSummary]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM plays ORDER BY played_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [(Play(**dict(row)), self.get_song_summary(row["song_id"])) for row in rows]

    # Budget

    def get_cart(self) -> Cart:
        """The albums in the Budget cart, in the order they were added, with the shipping, the
        total and the budget it is editing, if any."""
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM cart_items ORDER BY id").fetchall()
            editing = conn.execute("SELECT * FROM budgets WHERE in_cart = 1").fetchone()
            shipping = conn.execute("SELECT shipping_cents FROM cart").fetchone()[0] / 100
        lines = [self._cart_line(_cart_item(row)) for row in rows]
        total = _total(lines, shipping)
        return Cart(lines, total, _budget(editing) if editing else None, shipping)

    def set_shipping(self, shipping: float) -> Cart:
        """Say what shipping the cart costs in all; it adds up to the total."""
        validate_price(shipping, "shipping")
        with self.connect() as conn:
            conn.execute("UPDATE cart SET shipping_cents = ?", (_cents(shipping),))
        return self.get_cart()

    def cart_options(self, fmt: str | None = None) -> list[CartOption]:
        """Every album on every format (or only on ``fmt``) that is not in the cart yet: the
        wanted ones first, then the ones neither wanted nor owned, then the owned ones; by
        artist, year and title."""
        if fmt is not None and fmt not in FORMATS:
            raise ValueError(f"format must be one of {', '.join(FORMATS)}")
        with self.connect() as conn:
            taken = {tuple(row) for row in conn.execute("SELECT album_id, format FROM cart_items")}
            rows = conn.execute(
                """SELECT al.*, ar.name AS x_artist FROM albums al
                     JOIN artists ar ON ar.id = al.artist_id
                    ORDER BY ar.name COLLATE NOCASE, al.year IS NULL, al.year,
                             al.title COLLATE NOCASE, al.id"""
            ).fetchall()
        options = []
        for row in rows:
            own, extra = _split(row, "x_")
            album = _album(own)
            for each in [fmt] if fmt else FORMATS:
                if (album.id, each) not in taken:
                    options.append(CartOption(album, extra["artist"], each, getattr(album, each)))
        rank = {"wanted": 0, None: 1, "owned": 2}
        return sorted(options, key=lambda option: rank[option.state])  # stable: keeps the order

    def get_cart_item(self, item_id: int) -> CartItem:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM cart_items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise NotFound(f"cart item {item_id} not found")
        return _cart_item(row)

    def add_to_cart(self, data: CartItemIn) -> CartItem:
        values = self._cart_values(data)
        try:
            with self.connect() as conn:
                cur = conn.execute(
                    """INSERT INTO cart_items (album_id, format, price_cents, quantity, link,
                                               created_at)
                       VALUES (:album_id, :format, :price_cents, :quantity, :link, :now)""",
                    {**values, "now": _now()},
                )
        except sqlite3.IntegrityError:
            raise ValueError(self._in_cart_message(data)) from None
        return self.get_cart_item(cur.lastrowid)

    def update_cart_item(self, item_id: int, data: CartItemIn) -> CartItem:
        self.get_cart_item(item_id)
        values = self._cart_values(data)
        try:
            with self.connect() as conn:
                conn.execute(
                    """UPDATE cart_items SET album_id = :album_id, format = :format,
                           price_cents = :price_cents, quantity = :quantity, link = :link
                       WHERE id = :id""",
                    {**values, "id": item_id},
                )
        except sqlite3.IntegrityError:
            raise ValueError(self._in_cart_message(data)) from None
        return self.get_cart_item(item_id)

    def remove_from_cart(self, item_id: int) -> None:
        self._delete("cart_items", item_id)

    def change_cart_quantity(self, item_id: int, offset: int) -> CartItem | None:
        """Buy ``offset`` more copies (fewer when negative); with none left the item leaves the
        cart, and ``None`` is returned."""
        item = self.get_cart_item(item_id)
        if item.quantity + offset < 1:
            self.remove_from_cart(item_id)
            return None
        with self.connect() as conn:
            conn.execute(
                "UPDATE cart_items SET quantity = quantity + ? WHERE id = ?", (offset, item_id)
            )
        return self.get_cart_item(item_id)

    def clear_cart(self) -> None:
        """Empty the cart. A budget it was editing stays as it was saved."""
        with self.connect() as conn:
            conn.execute("DELETE FROM cart_items")
            conn.execute("UPDATE cart SET shipping_cents = 0")
            conn.execute("UPDATE budgets SET in_cart = 0")

    def make_budget(self, data: BudgetIn) -> Budget:
        """Save the cart as a budget, or back into the budget it is editing, and empty it."""
        name = _budget_name(data)
        cart = self.get_cart()
        if not cart.lines:
            raise ValueError("the cart is empty: add albums to it first")
        with self.transaction(), self.connect() as conn:
            if cart.editing:
                budget_id = cart.editing.id
                conn.execute("UPDATE budgets SET name = ? WHERE id = ?", (name, budget_id))
                conn.execute("DELETE FROM budget_items WHERE budget_id = ?", (budget_id,))
            else:
                cur = conn.execute(
                    "INSERT INTO budgets (name, created_at) VALUES (?, ?)", (name, _now())
                )
                budget_id = cur.lastrowid
            conn.execute(
                "UPDATE budgets SET shipping_cents = ? WHERE id = ?",
                (_cents(cart.shipping), budget_id),
            )
            conn.execute(
                """INSERT INTO budget_items (budget_id, album_id, format, price_cents, quantity,
                                             link)
                   SELECT ?, album_id, format, price_cents, quantity, link
                     FROM cart_items ORDER BY id""",
                (budget_id,),
            )
            self.clear_cart()
        return self.get_budget(budget_id)

    def buy_cart(self, data: BudgetIn, drop_from_others: bool = False) -> Budget:
        """Save the cart as a budget (see ``make_budget``) and buy it (see ``buy_budget``)."""
        with self.transaction():
            budget = self.make_budget(data)
            return self.buy_budget(budget.id, drop_from_others)

    def list_budgets(self) -> list[BudgetDetail]:
        """Every budget, the ones still to buy first, newest first."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id FROM budgets ORDER BY bought_at IS NOT NULL, bought_at DESC, id DESC"
            ).fetchall()
        return [self.get_budget_detail(budget_id) for (budget_id,) in rows]

    def get_budget(self, budget_id: int) -> Budget:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM budgets WHERE id = ?", (budget_id,)).fetchone()
        if row is None:
            raise NotFound(f"budget {budget_id} not found")
        return _budget(row)

    def get_budget_detail(self, budget_id: int) -> BudgetDetail:
        budget = self.get_budget(budget_id)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM budget_items WHERE budget_id = ? ORDER BY id", (budget_id,)
            ).fetchall()
        lines = [self._cart_line(_budget_item(row), bought=bool(budget.bought_at)) for row in rows]
        return BudgetDetail(budget, lines, _total(lines, budget.shipping))

    def edit_budget(self, budget_id: int) -> Cart:
        """Load the budget into the cart, replacing what the cart held, to change it there."""
        self._open_budget(budget_id, "edited")
        with self.transaction(), self.connect() as conn:
            self.clear_cart()
            conn.execute(
                """INSERT INTO cart_items (album_id, format, price_cents, quantity, link,
                                           created_at)
                   SELECT album_id, format, price_cents, quantity, link, ? FROM budget_items
                    WHERE budget_id = ? ORDER BY id""",
                (_now(), budget_id),
            )
            conn.execute(
                """UPDATE cart SET shipping_cents = (SELECT shipping_cents FROM budgets
                                                       WHERE id = ?)""",
                (budget_id,),
            )
            conn.execute("UPDATE budgets SET in_cart = 1 WHERE id = ?", (budget_id,))
        return self.get_cart()

    def buy_budget(self, budget_id: int, drop_from_others: bool = False) -> Budget:
        """Mark the budget bought, and every album in it owned on its format. With
        ``drop_from_others``, those albums also leave the other budgets still to buy (see
        ``budget_overlaps``), and the budgets that leaves empty are deleted."""
        budget = self._open_budget(budget_id, "bought")
        if budget.in_cart:
            raise ValueError(f"{budget.name} is being edited in the cart: save or delete it there")
        with self.transaction(), self.connect() as conn:
            if drop_from_others:
                for overlap in self.budget_overlaps(budget_id):
                    if overlap.emptied:
                        self.delete_budget(overlap.budget.id)
                    for line in overlap.lines:
                        conn.execute("DELETE FROM budget_items WHERE id = ?", (line.item.id,))
            for line in self.get_budget_detail(budget_id).lines:
                self.set_format(line.album.id, line.item.format, "owned")
            conn.execute("UPDATE budgets SET bought_at = ? WHERE id = ?", (_now(), budget_id))
        return self.get_budget(budget_id)

    def budget_overlaps(self, budget_id: int | None = None) -> list[BudgetOverlap]:
        """The other budgets still to buy that hold some of the same albums on the same formats
        as the budget (or as the cart, without ``budget_id``), with those albums. Bought budgets
        are records and never count, nor does the budget the cart is editing."""
        if budget_id is None:
            cart = self.get_cart()
            lines, exclude = cart.lines, cart.editing.id if cart.editing else None
        else:
            lines, exclude = self.get_budget_detail(budget_id).lines, budget_id
        wanted = {(line.item.album_id, line.item.format) for line in lines}
        overlaps = []
        for detail in self.list_budgets():
            if detail.budget.bought_at or detail.budget.id == exclude:
                continue
            same = [
                line for line in detail.lines if (line.item.album_id, line.item.format) in wanted
            ]
            if same:
                overlaps.append(BudgetOverlap(detail.budget, same, len(same) == len(detail.lines)))
        return overlaps

    def delete_budget(self, budget_id: int) -> None:
        """Delete the budget. If the cart is editing it, the cart keeps its items unsaved."""
        self._delete("budgets", budget_id)

    def _open_budget(self, budget_id: int, change: str) -> Budget:
        budget = self.get_budget(budget_id)
        if budget.bought_at:
            raise ValueError(f"{budget.name} was already bought and cannot be {change}")
        return budget

    def _cart_line(self, item: CartItem | BudgetItem, bought: bool = False) -> CartLine:
        album = self.get_album(item.album_id)
        artist = self.get_artist(album.artist_id)
        owned = not bought and getattr(album, item.format) == "owned"
        subtotal = _cents(item.price) * item.quantity / 100
        return CartLine(item, album, artist.name, owned, subtotal)

    def _cart_values(self, data: CartItemIn) -> dict:
        self.get_album(data.album_id)
        if data.format not in FORMATS:
            raise ValueError(f"format must be one of {', '.join(FORMATS)}")
        validate_price(data.price)
        validate_quantity(data.quantity)
        return {
            "album_id": data.album_id,
            "format": data.format,
            "price_cents": _cents(data.price),
            "quantity": data.quantity,
            "link": shop_link(data.link),
        }

    def _in_cart_message(self, data: CartItemIn) -> str:
        album = self.get_album(data.album_id)
        return f"{album.title} on {FORMATS[data.format]} is already in the cart"

    # Playlists

    def list_playlists(self) -> list[PlaylistSummary]:
        sql = """
            SELECT pl.*, COUNT(s.id) AS x_songs,
                   COALESCE(SUM(s.duration_seconds), 0) AS x_seconds
              FROM playlists pl
              LEFT JOIN playlist_songs ps ON ps.playlist_id = pl.id
              LEFT JOIN songs s ON s.id = ps.song_id
             GROUP BY pl.id
             ORDER BY pl.kind IS NULL, pl.kind, pl.name COLLATE NOCASE, pl.id
        """
        with self.connect() as conn:
            rows = conn.execute(sql).fetchall()
        out = []
        for row in rows:
            own, extra = _split(row, "x_")
            out.append(PlaylistSummary(Playlist(**own), extra["songs"], extra["seconds"]))
        return out

    def get_playlist(self, playlist_id: int) -> Playlist:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
        if row is None:
            raise NotFound(f"playlist {playlist_id} not found")
        return Playlist(**dict(row))

    def get_playlist_detail(self, playlist_id: int) -> PlaylistDetail:
        playlist = self.get_playlist(playlist_id)
        return PlaylistDetail(playlist, self.list_songs(playlist_id=playlist_id, order="playlist"))

    def create_playlist(self, data: PlaylistIn) -> Playlist:
        name = self._playlist_name(data)
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO playlists (name, created_at) VALUES (?, ?)", (name, _now())
            )
        return self.get_playlist(cur.lastrowid)

    def update_playlist(self, playlist_id: int, data: PlaylistIn) -> Playlist:
        self._own_playlist(playlist_id, "renamed")
        name = self._playlist_name(data)
        with self.connect() as conn:
            conn.execute("UPDATE playlists SET name = ? WHERE id = ?", (name, playlist_id))
        return self.get_playlist(playlist_id)

    def delete_playlist(self, playlist_id: int) -> None:
        self._own_playlist(playlist_id, "deleted")
        self._delete("playlists", playlist_id)

    def special_playlist(self, kind: str) -> Playlist:
        """The built-in playlist of one of ``SPECIAL_PLAYLISTS``."""
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM playlists WHERE kind = ?", (kind,)).fetchone()
        return Playlist(**dict(row))

    def _own_playlist(self, playlist_id: int, change: str) -> Playlist:
        playlist = self.get_playlist(playlist_id)
        if playlist.kind:
            raise ValueError(f"{playlist.name} is a built-in playlist and cannot be {change}")
        return playlist

    def playlist_song_ids(self, playlist_id: int) -> list[int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT song_id FROM playlist_songs WHERE playlist_id = ? ORDER BY position",
                (playlist_id,),
            ).fetchall()
        return [song_id for (song_id,) in rows]

    def playlists_with_song(self, song_id: int) -> list[Playlist]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT pl.* FROM playlists pl JOIN playlist_songs ps ON ps.playlist_id = pl.id
                    WHERE ps.song_id = ?
                    ORDER BY pl.kind IS NULL, pl.kind, pl.name COLLATE NOCASE, pl.id""",
                (song_id,),
            ).fetchall()
        return [Playlist(**dict(row)) for row in rows]

    def add_to_playlist(self, playlist_id: int, song_ids: list[int]) -> int:
        """Append the songs to the end of the playlist, in order, and return how many were
        added: songs already in it stay where they are."""
        self.get_playlist(playlist_id)
        for song_id in song_ids:
            self.get_song(song_id)
        current = self.playlist_song_ids(playlist_id)
        new = [song_id for song_id in dict.fromkeys(song_ids) if song_id not in current]
        self._write_playlist(playlist_id, current + new)
        return len(new)

    def add_album_to_playlist(self, playlist_id: int, album_id: int) -> int:
        """Append the album's songs, in track order; see ``add_to_playlist``."""
        self.get_album(album_id)
        songs = self.list_songs(album_id=album_id, order="track")
        return self.add_to_playlist(playlist_id, [s.song.id for s in songs])

    def remove_from_playlist(self, playlist_id: int, song_id: int) -> None:
        self.get_playlist(playlist_id)
        with self.connect() as conn:
            cur = conn.execute(
                "DELETE FROM playlist_songs WHERE playlist_id = ? AND song_id = ?",
                (playlist_id, song_id),
            )
        if cur.rowcount == 0:
            raise NotFound(f"song {song_id} is not in playlist {playlist_id}")
        self._marks_from_playlist(playlist_id)

    def set_playlist_songs(self, playlist_id: int, song_ids: list[int]) -> PlaylistDetail:
        """Replace the playlist's songs with ``song_ids``, in that order."""
        self.get_playlist(playlist_id)
        if len(set(song_ids)) != len(song_ids):
            raise ValueError("a song can only be once in a playlist")
        for song_id in song_ids:
            self.get_song(song_id)
        self._write_playlist(playlist_id, song_ids)
        return self.get_playlist_detail(playlist_id)

    def move_in_playlist(self, playlist_id: int, song_id: int, offset: int) -> None:
        """Move a song ``offset`` places later (earlier when negative), stopping at the ends."""
        self.get_playlist(playlist_id)
        song_ids = self.playlist_song_ids(playlist_id)
        if song_id not in song_ids:
            raise NotFound(f"song {song_id} is not in playlist {playlist_id}")
        index = song_ids.index(song_id)
        song_ids.insert(max(0, min(index + offset, len(song_ids) - 1)), song_ids.pop(index))
        self._write_playlist(playlist_id, song_ids)

    def _write_playlist(self, playlist_id: int, song_ids: list[int]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM playlist_songs WHERE playlist_id = ?", (playlist_id,))
            conn.executemany(
                "INSERT INTO playlist_songs (playlist_id, song_id, position) VALUES (?, ?, ?)",
                [(playlist_id, song_id, position) for position, song_id in enumerate(song_ids)],
            )
        self._marks_from_playlist(playlist_id)

    def _marks_from_playlist(self, playlist_id: int) -> None:
        """After a built-in playlist changed, mark exactly the songs in it."""
        kind = self.get_playlist(playlist_id).kind
        if kind is None:
            return
        field = SPECIAL_PLAYLISTS[kind][1]
        with self.connect() as conn:
            conn.execute(
                f"""UPDATE songs SET {field} = EXISTS (SELECT 1 FROM playlist_songs
                        WHERE playlist_id = ? AND song_id = songs.id)""",
                (playlist_id,),
            )

    def _sync_marks(self, song_id: int | None = None) -> None:
        """After songs' marks changed, put the marked songs (all of them, or just ``song_id``)
        at the end of their built-in playlist and take out the others."""
        for kind, (_name, field) in SPECIAL_PLAYLISTS.items():
            params = {"playlist_id": self.special_playlist(kind).id, "song_id": song_id}
            with self.connect() as conn:
                conn.execute(
                    f"""DELETE FROM playlist_songs
                         WHERE playlist_id = :playlist_id AND song_id IN (
                               SELECT id FROM songs WHERE {field} = 0
                                  AND (:song_id IS NULL OR id = :song_id))""",
                    params,
                )
                conn.execute(
                    f"""INSERT INTO playlist_songs (playlist_id, song_id, position)
                        SELECT :playlist_id, s.id,
                               (SELECT COALESCE(MAX(position), -1) FROM playlist_songs
                                 WHERE playlist_id = :playlist_id)
                               + ROW_NUMBER() OVER (ORDER BY s.id)
                          FROM songs s
                         WHERE s.{field} = 1 AND (:song_id IS NULL OR s.id = :song_id)
                           AND s.id NOT IN (SELECT song_id FROM playlist_songs
                                             WHERE playlist_id = :playlist_id)""",
                    params,
                )

    @staticmethod
    def _playlist_name(data: PlaylistIn) -> str:
        name = data.name.strip()
        if not name:
            raise ValueError("name is required")
        if name.casefold() in {n.casefold() for n, _field in SPECIAL_PLAYLISTS.values()}:
            raise ValueError(f"{name} is the name of a built-in playlist")
        return name

    # Shared

    def toggle_favorite(self, kind: str, record_id: int) -> bool:
        return self.toggle(kind, record_id, "favorite")

    def toggle(self, kind: str, record_id: int, flag: str) -> bool:
        """Switch one of ``FLAGS`` of an artist, album or song, and return its new value."""
        table = FAVORITE_TABLES[kind]
        if flag not in FLAGS:
            raise ValueError(f"flag must be one of {', '.join(FLAGS)}")
        with self.connect() as conn:
            cur = conn.execute(f"UPDATE {table} SET {flag} = 1 - {flag} WHERE id = ?", (record_id,))
            if cur.rowcount == 0:
                raise NotFound(f"{kind} {record_id} not found")
            row = conn.execute(f"SELECT {flag} FROM {table} WHERE id = ?", (record_id,))
            value = bool(row.fetchone()[0])
        if kind == "song":
            self._sync_marks(record_id)
        return value

    def set_rating(self, kind: str, record_id: int, rating: int | None) -> int | None:
        """Rate an artist, album or song from 1 to 5 stars; ``None`` takes the rating away."""
        table = FAVORITE_TABLES[kind]
        validate_rating(rating)
        with self.connect() as conn:
            cur = conn.execute(f"UPDATE {table} SET rating = ? WHERE id = ?", (rating, record_id))
        if cur.rowcount == 0:
            raise NotFound(f"{kind} {record_id} not found")
        return rating

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
            ratings=[Point("★" * r, ratings.get(r, 0)) for r in range(5, 0, -1)],
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
