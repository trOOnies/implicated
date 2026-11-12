"""SQLite-backed storage for the music library."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, fields, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from implicated.concerts import CONCERTS_FILE, read_concerts
from implicated.explicit import is_explicit
from implicated.imagesize import is_square
from implicated.lrc import TimedLine, plain_lyrics, read_lrc
from implicated.models import (
    FORMATS,
    MAX_TIERS,
    MIN_TIERS,
    SPECIAL_FOLDERS,
    SPECIAL_PLAYLISTS,
    TIER_LETTERS,
    TIER_NAME_LENGTH,
    TIER_NAMES,
    TIER_OWNERS,
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
    Concert,
    Folder,
    FolderDetail,
    FolderIn,
    LibraryStats,
    Member,
    MemberIn,
    Picture,
    PictureIn,
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
    Tier,
    TierList,
    TierRow,
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
    unreleased  INTEGER NOT NULL DEFAULT 0,
    -- The main edition of the albums this one is merged with (see ``Album.main_id``).
    main_id     INTEGER REFERENCES albums(id) ON DELETE SET NULL
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
    explicit         INTEGER NOT NULL DEFAULT 0,
    mv               TEXT,
    listen_later     INTEGER NOT NULL DEFAULT 0,
    -- The main version of the songs this one is merged with (see ``Song.main_id``).
    main_id          INTEGER REFERENCES songs(id) ON DELETE SET NULL
);
-- Other artists a song features ("feat."), in the order they are credited.
CREATE TABLE IF NOT EXISTS song_features (
    song_id   INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
    artist_id INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    position  INTEGER NOT NULL,
    PRIMARY KEY (song_id, artist_id)
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
-- Folders of playlists: each playlist is in exactly one (its ``folder_id``). The built-in ones
-- have a ``kind`` (``SPECIAL_FOLDERS``).
CREATE TABLE IF NOT EXISTS folders (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    kind       TEXT UNIQUE
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
-- The levels of a tier list, top first, and the songs put in them. A tier list belongs to one
-- playlist, album or artist (``TIER_OWNERS``): each level has exactly one of the three ids. A
-- song is in at most one level of a tier list; the owner's other songs are unassigned.
CREATE TABLE IF NOT EXISTS tiers (
    id          INTEGER PRIMARY KEY,
    playlist_id INTEGER REFERENCES playlists(id) ON DELETE CASCADE,
    album_id    INTEGER REFERENCES albums(id) ON DELETE CASCADE,
    artist_id   INTEGER REFERENCES artists(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    name        TEXT NOT NULL,
    CHECK ((playlist_id IS NOT NULL) + (album_id IS NOT NULL) + (artist_id IS NOT NULL) = 1)
);
-- A tier list's own settings (whether locking mode is on) and its locked songs: a song locked
-- stays locked wherever it is moved while locking mode is off.
CREATE TABLE IF NOT EXISTS tier_lists (
    id          INTEGER PRIMARY KEY,
    playlist_id INTEGER UNIQUE REFERENCES playlists(id) ON DELETE CASCADE,
    album_id    INTEGER UNIQUE REFERENCES albums(id) ON DELETE CASCADE,
    artist_id   INTEGER UNIQUE REFERENCES artists(id) ON DELETE CASCADE,
    locking     INTEGER NOT NULL DEFAULT 1,
    CHECK ((playlist_id IS NOT NULL) + (album_id IS NOT NULL) + (artist_id IS NOT NULL) = 1)
);
CREATE TABLE IF NOT EXISTS tier_locks (
    tier_list_id INTEGER NOT NULL REFERENCES tier_lists(id) ON DELETE CASCADE,
    song_id      INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
    PRIMARY KEY (tier_list_id, song_id)
);
CREATE TABLE IF NOT EXISTS tier_songs (
    tier_id  INTEGER NOT NULL REFERENCES tiers(id) ON DELETE CASCADE,
    song_id  INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    PRIMARY KEY (tier_id, song_id)
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
    shipping_cents INTEGER NOT NULL DEFAULT 0,
    discount_cents INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS budgets (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    bought_at  TEXT,
    in_cart    INTEGER NOT NULL DEFAULT 0,
    shipping_cents INTEGER NOT NULL DEFAULT 0,
    discount_cents INTEGER NOT NULL DEFAULT 0
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
-- Pictures saved for an artist; the image files are in the library's ``pictures/``.
CREATE TABLE IF NOT EXISTS pictures (
    id          INTEGER PRIMARY KEY,
    artist_id   INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT,
    tags        TEXT NOT NULL DEFAULT '[]',
    file_name   TEXT NOT NULL,
    file        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pictures_artist ON pictures(artist_id);
CREATE INDEX IF NOT EXISTS idx_budget_items_budget ON budget_items(budget_id);
CREATE INDEX IF NOT EXISTS idx_albums_artist ON albums(artist_id);
CREATE INDEX IF NOT EXISTS idx_members_artist ON members(artist_id);
CREATE INDEX IF NOT EXISTS idx_songs_album ON songs(album_id);
CREATE INDEX IF NOT EXISTS idx_songs_artist ON songs(artist_id);
CREATE INDEX IF NOT EXISTS idx_plays_song ON plays(song_id);
CREATE INDEX IF NOT EXISTS idx_playlist_songs_song ON playlist_songs(song_id);
CREATE INDEX IF NOT EXISTS idx_song_videos_song ON song_videos(song_id);
CREATE INDEX IF NOT EXISTS idx_song_features_artist ON song_features(artist_id);
CREATE INDEX IF NOT EXISTS idx_tiers_playlist ON tiers(playlist_id);
CREATE INDEX IF NOT EXISTS idx_tiers_album ON tiers(album_id);
CREATE INDEX IF NOT EXISTS idx_tiers_artist ON tiers(artist_id);
CREATE INDEX IF NOT EXISTS idx_tier_songs_song ON tier_songs(song_id);
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
    ("cart", "discount_cents", "INTEGER NOT NULL DEFAULT 0"),
    ("budgets", "discount_cents", "INTEGER NOT NULL DEFAULT 0"),
    ("artists", "uid", "TEXT"),
    ("albums", "uid", "TEXT"),
    ("songs", "uid", "TEXT"),
    ("playlists", "uid", "TEXT"),
    ("songs", "main_id", "INTEGER REFERENCES songs(id) ON DELETE SET NULL"),
    ("albums", "main_id", "INTEGER REFERENCES albums(id) ON DELETE SET NULL"),
    ("songs", "explicit", "INTEGER NOT NULL DEFAULT 0"),
    ("playlists", "folder_id", "INTEGER REFERENCES folders(id)"),
]
# Tables whose records have a ``uid``: a stable id that survives renames, which exports and
# inbox files use to point at a record (integer ids stay internal to one database).
UID_TABLES = ("artists", "albums", "songs", "playlists")


def new_uid() -> str:
    return str(uuid.uuid4())


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
     AND (:favorites = 0 OR a.favorite = 1)
     AND (:listen_later = 0 OR a.listen_later = 1)
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
     AND (:featuring IS NULL OR EXISTS (SELECT 1 FROM song_features
                                         WHERE song_id = s.id AND artist_id = :featuring))
     AND (:favorites = 0 OR s.favorite = 1)
     AND (:listen_later = 0 OR s.listen_later = 1)
     AND (:missing_lyrics = 0 OR (s.lyrics IS NULL AND s.no_lyrics = 0))
     AND (:mains_only = 0 OR s.main_id IS NULL)
     AND (:q IS NULL OR s.title LIKE '%' || :q || '%'
          OR ar.name LIKE '%' || :q || '%'
          OR al.title LIKE '%' || :q || '%')
"""

# The ids of the songs merged with ``s`` (``s`` included): they count as one song.
SAME_SONG = """
    SELECT id FROM songs WHERE id = COALESCE(s.main_id, s.id) OR main_id = COALESCE(s.main_id, s.id)
"""

# Which of an artist's pictures ``list_pictures`` and ``count_pictures`` look at: ``q`` is found in
# the name, file name, description or any tag, and ``tag`` is one of the tags, ignoring case.
PICTURES_WHERE = """
    FROM pictures p
   WHERE p.artist_id = :artist_id
     AND (:tag IS NULL OR EXISTS (SELECT 1 FROM json_each(p.tags)
                                   WHERE value = :tag COLLATE NOCASE))
     AND (:q IS NULL OR p.name LIKE '%' || :q || '%'
          OR p.file_name LIKE '%' || :q || '%'
          OR p.description LIKE '%' || :q || '%'
          OR EXISTS (SELECT 1 FROM json_each(p.tags) WHERE value LIKE '%' || :q || '%'))
"""

# Cover file names are ``<album id>-<content hash><suffix>`` (see ``_store_image``), so two albums
# share a cover when their names agree after the first dash. Compares ``al`` with ``o``.
SAME_COVER = "substr(o.cover, instr(o.cover, '-')) = substr(al.cover, instr(al.cover, '-'))"

# Which albums ``list_albums`` and ``count_albums`` look at. The filters are ``None`` (or 0) when
# not in use.
ALBUMS_WHERE = f"""
    FROM albums al JOIN artists ar ON ar.id = al.artist_id
   WHERE (:artist_id IS NULL OR al.artist_id = :artist_id)
     AND (:favorites = 0 OR al.favorite = 1)
     AND (:listen_later = 0 OR al.listen_later = 1)
     AND (:mains_only = 0 OR al.main_id IS NULL)
     AND (:cd IS NULL OR al.cd = :cd)
     AND (:vinyl IS NULL OR al.vinyl = :vinyl)
     AND (:missing_lyrics = 0 OR EXISTS (SELECT 1 FROM songs WHERE album_id = al.id
                                             AND lyrics IS NULL AND no_lyrics = 0))
     AND (:no_own_cover = 0 OR al.cover IS NULL
          OR EXISTS (SELECT 1 FROM albums o WHERE o.id != al.id AND {SAME_COVER}))
     AND (:q IS NULL OR al.title LIKE '%' || :q || '%'
          OR ar.name LIKE '%' || :q || '%')
"""

# Dashes that count as the same one when looking a title up, so an en dash turned into a hyphen
# in ``albums.yaml`` renames the album instead of importing it again. See ``_same_title``.
DASHES = ("\u2013", "\u2014")


def _title_key(title: str) -> str:
    """How titles compare, in Python: ignoring case, outer spaces and kinds of dash."""
    return _dashless(title).casefold()


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

# Album covers and artist images, and the media type each is served as.
IMAGE_TYPES = {
    ".avif": "image/avif",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
# What album covers can be: no GIFs (artist images still can).
COVER_TYPES = {suffix: kind for suffix, kind in IMAGE_TYPES.items() if suffix != ".gif"}


def check_image_name(
    file_name: str, what: str = "image", types: dict[str, str] = IMAGE_TYPES
) -> str:
    """The lowercase suffix of ``file_name``; a ``ValueError`` unless it is one of ``types``."""
    suffix = Path(file_name).suffix.lower()
    if suffix not in types:
        raise ValueError(f"{what} must be one of {', '.join(types)}")
    return suffix


def check_cover_name(file_name: str) -> str:
    """``check_image_name`` for an album cover, which takes only ``COVER_TYPES``."""
    return check_image_name(file_name, "cover", COVER_TYPES)


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


def _tags(values: list[str]) -> str:
    """Tags as stored: trimmed, without empty ones or repeats (ignoring case), in their order."""
    tags: dict[str, str] = {}
    for value in values:
        tag = " ".join(value.split())
        if tag:
            tags.setdefault(tag.casefold(), tag)
    return json.dumps(list(tags.values()))


def _content_key(file_name: str) -> str:
    """The content hash in an image file name the library made (``<id>-<hash>.<suffix>``, see
    ``Library._store_image``), the same for the same image wherever it is kept."""
    return file_name.partition("-")[2].rpartition(".")[0]


def _picture(row: sqlite3.Row) -> Picture:
    data = dict(row)
    data["tags"] = json.loads(data["tags"])
    return Picture(**data)


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
    data["discount"] = data.pop("discount_cents") / 100
    return Budget(**data)


def _budget_name(data: BudgetIn) -> str:
    name = (data.name or "").strip()
    if not name:
        raise ValueError("name is required")
    if len(name) > 100:
        raise ValueError("name must be at most 100 characters")
    return name


def _total(lines: list[CartLine], shipping: float, discount: float) -> float:
    # Added up in cents, so 0.1 + 0.2 makes 0.3. A discount can take it down to nothing, no further.
    cents = sum(round(line.subtotal * 100) for line in lines) + _cents(shipping)
    return max(cents - _cents(discount), 0) / 100


def _song(row: sqlite3.Row) -> Song:
    data = dict(row)
    data["favorite"] = bool(data["favorite"])
    data["no_lyrics"] = bool(data["no_lyrics"])
    data["explicit"] = bool(data["explicit"])
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


def _rebuild(conn: sqlite3.Connection, table: str, columns: str) -> None:
    """Make ``table`` again as ``SCHEMA`` has it, keeping ``columns`` of its rows. Made under a
    new name and renamed last, as renaming the old table instead would point the tables that
    refer to it at the old one."""
    create = SCHEMA[SCHEMA.index(f"CREATE TABLE IF NOT EXISTS {table} (") :].split(";")[0]
    conn.execute(create.replace(f" {table} (", f" {table}_new (", 1))
    conn.execute(f"INSERT INTO {table}_new ({columns}) SELECT {columns} FROM {table}")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")


class Library:
    def __init__(self, path: str | Path, media_dir: str | Path | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.covers_dir = self.path.parent / "covers"
        self.images_dir = self.path.parent / "artists"
        self.pictures_dir = self.path.parent / "pictures"
        # Where the library keeps the files you edit by hand: lyrics, timed lyrics and tabs in
        # ``bands/<artist>/songs|tabs/``, and the concerts and tours CSVs. Usually the folder
        # the database is in. When set, every song has a lyrics file: one is created (empty)
        # along with the song if missing. Without it, the library keeps no such files.
        self.media_dir = Path(media_dir) if media_dir is not None else None
        self._local = threading.local()
        self._migrate_tiers()
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
            for table in UID_TABLES:
                missing = conn.execute(f"SELECT id FROM {table} WHERE uid IS NULL").fetchall()
                conn.executemany(
                    f"UPDATE {table} SET uid = ? WHERE id = ?",
                    [(new_uid(), row["id"]) for row in missing],
                )
                conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{table}_uid ON {table}(uid)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_playlists_kind ON playlists(kind)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_songs_main ON songs(main_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_albums_main ON albums(main_id)")
            for kind, (name, _field) in SPECIAL_PLAYLISTS.items():
                conn.execute(
                    """INSERT INTO playlists (name, created_at, kind, uid) SELECT ?, ?, ?, ?
                        WHERE NOT EXISTS (SELECT 1 FROM playlists WHERE kind = ?)""",
                    (name, _now(), kind, new_uid(), kind),
                )
            for kind, name in SPECIAL_FOLDERS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO folders (name, created_at, kind) VALUES (?, ?, ?)",
                    (name, _now(), kind),
                )
            # Playlists from before folders, or whose folder went missing, are in Default.
            conn.execute(
                """UPDATE playlists SET folder_id = (SELECT id FROM folders WHERE kind = 'default')
                    WHERE folder_id IS NULL OR folder_id NOT IN (SELECT id FROM folders)
                       OR kind IS NOT NULL"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_playlists_folder ON playlists(folder_id)")
            conn.execute("INSERT OR IGNORE INTO cart (id) VALUES (1)")
        self._sync_marks()

    def _migrate_tiers(self) -> None:
        """Tier lists were first only of playlists: rebuild an old ``tiers`` table (its
        ``playlist_id`` required, no ``album_id`` or ``artist_id``) as ``SCHEMA`` has it,
        keeping its levels, and a ``tier_songs`` whose levels are not in ``tiers`` (a table
        renamed away from under it). Foreign keys stay off meanwhile, so dropping a table does
        not take the rows pointing at it with it."""
        conn = sqlite3.connect(self.path)
        try:
            with conn:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(tiers)")}
                if columns and "album_id" not in columns:
                    _rebuild(conn, "tiers", "id, playlist_id, position, name")
                targets = {row[2] for row in conn.execute("PRAGMA foreign_key_list(tier_songs)")}
                if targets and targets != {"tiers", "songs"}:
                    _rebuild(conn, "tier_songs", "tier_id, song_id, position")
                    conn.execute("DROP TABLE IF EXISTS tiers_old")
        finally:
            conn.close()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """A connection committed on success; inside ``transaction()``, the shared one."""
        shared = getattr(self._local, "conn", None)
        if shared is not None:
            yield shared
            return
        conn = self._open()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def dry_run(self) -> Iterator[None]:
        """Run library calls that change nothing, to see what they would do: they share one
        connection whose work is rolled back at the end, and no file is written, moved or
        removed meanwhile (see ``dry``). Only this thread is affected."""
        if getattr(self._local, "conn", None) is not None:
            raise RuntimeError("a dry run cannot start inside a transaction")
        conn = self._open()
        self._local.conn, self._local.dry = conn, True
        try:
            yield
        finally:
            self._local.conn, self._local.dry = None, False
            conn.rollback()
            conn.close()

    @property
    def dry(self) -> bool:
        """Whether this thread is inside ``dry_run``, so files must stay as they are."""
        return getattr(self._local, "dry", False)

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
        favorites: bool = False,
        listen_later: bool = False,
        order: str = "name",
        limit: int | None = None,
        offset: int = 0,
    ) -> list[ArtistSummary]:
        """Artists, optionally only those from ``country`` or with ``genre`` or ``language``
        among theirs (all ignoring case), the favorites or the ones marked to listen to later,
        and only ``limit`` of them after the first ``offset``. They go by name, favorites first,
        or with ``order="plays"`` the most played first, or ``order="rating"`` the highest
        rated first (the unrated last)."""
        order_by = {
            "name": "a.favorite DESC",
            "plays": "x_plays DESC",
            "rating": "a.rating IS NULL, a.rating DESC",
        }[order]
        sql = f"""
            SELECT a.*,
                   (SELECT COUNT(*) FROM albums WHERE artist_id = a.id) AS x_albums,
                   (SELECT COUNT(*) FROM songs WHERE artist_id = a.id) AS x_songs,
                   (SELECT COUNT(*) FROM plays p JOIN songs s ON s.id = p.song_id
                     WHERE s.artist_id = a.id) AS x_plays
              {ARTISTS_WHERE}
             ORDER BY {order_by}, a.name COLLATE NOCASE, a.id
             LIMIT :limit OFFSET :offset
        """
        params = {
            **self._artist_filters(q, country, genre, language, favorites, listen_later),
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
        favorites: bool = False,
        listen_later: bool = False,
    ) -> int:
        """How many artists ``list_artists`` finds without a ``limit``."""
        params = self._artist_filters(q, country, genre, language, favorites, listen_later)
        with self.connect() as conn:
            return conn.execute(f"SELECT COUNT(*) {ARTISTS_WHERE}", params).fetchone()[0]

    @staticmethod
    def _artist_filters(
        q: str | None,
        country: str | None,
        genre: str | None,
        language: str | None,
        favorites: bool,
        listen_later: bool,
    ) -> dict:
        return {
            "q": q or None,
            "country": country or None,
            "genre": genre or None,
            "language": language or None,
            "favorites": int(favorites),
            "listen_later": int(listen_later),
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

    def find_uid(self, table: str, uid: str | None) -> int | None:
        """The id of the record of ``table`` (one of ``UID_TABLES``) with that ``uid``."""
        if table not in UID_TABLES:
            raise ValueError(f"{table} records have no uid")
        if not uid:
            return None
        with self.connect() as conn:
            row = conn.execute(f"SELECT id FROM {table} WHERE uid = ?", (uid,)).fetchone()
        return row["id"] if row else None

    def find_artist(self, name: str) -> Artist | None:
        """Look an artist up by name, ignoring case."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM artists WHERE name = ? COLLATE NOCASE ORDER BY id", (name.strip(),)
            ).fetchone()
        return _artist(row) if row else None

    def create_artist(self, data: ArtistIn, uid: str | None = None) -> Artist:
        values = self._artist_values(data)
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO artists (name, country, formed_year, genres, languages, bio,
                                        wiki, favorite, notes, listen_later, rating,
                                        created_at, uid)
                   VALUES (:name, :country, :formed_year, :genres, :languages, :bio,
                           :wiki, :favorite, :notes, :listen_later, :rating, :now, :uid)""",
                {**values, "now": _now(), "uid": uid or new_uid()},
            )
        return self.get_artist(cur.lastrowid)

    def update_artist(self, artist_id: int, data: ArtistIn) -> Artist:
        old_name = self.get_artist(artist_id).name
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
            others = conn.execute(
                "SELECT 1 FROM artists WHERE id != ? AND name = ? COLLATE NOCASE",
                (artist_id, old_name),
            ).fetchone()
        artist = self.get_artist(artist_id)
        if artist.name != old_name and not self.dry:
            # A renamed artist takes its lyrics and tabs along (copies, if another artist still
            # has the old name).
            self._move_artist_folder(old_name, artist.name, copy=others is not None)
        return artist

    def artist_folder(self, name: str) -> Path | None:
        """The artist's folder of hand-edited files in ``media_dir``; ``None`` without it."""
        if self.media_dir is None:
            return None
        bands = self.media_dir / "bands"
        return _find_file(bands, [name]) or bands / name

    def _move_artist_folder(self, old_name: str, new_name: str, copy: bool) -> None:
        old, new = self.artist_folder(old_name), self.artist_folder(new_name)
        if old is None or new is None or not old.is_dir() or old == new:
            return
        if not copy and not new.exists():
            new.parent.mkdir(parents=True, exist_ok=True)
            old.rename(new)
            return
        # Files the new folder already has are kept; the rest move (or are copied) over.
        for path in sorted(old.rglob("*")):
            if path.is_file():
                _move_file(path, new / path.relative_to(old), copy=copy)
        if not copy:
            for path in sorted(old.rglob("*"), reverse=True):
                if path.is_dir() and not any(path.iterdir()):
                    path.rmdir()
            if not any(old.iterdir()):
                old.rmdir()

    def delete_artist(self, artist_id: int) -> None:
        image = self.get_artist(artist_id).image
        covers = [a.album.cover for a in self.list_albums(artist_id)]
        with self.connect() as conn:
            pictures = [
                row["file"]
                for row in conn.execute(
                    "SELECT file FROM pictures WHERE artist_id = ?", (artist_id,)
                )
            ]
        self._hand_over_mains("artist_id = ?", artist_id)
        self._delete("artists", artist_id)
        self._remove_files(self.covers_dir, covers)
        self._remove_files(self.images_dir, [image])
        self._remove_files(self.pictures_dir, pictures)

    def set_image(self, artist_id: int, source: str | Path) -> Artist:
        """Copy ``source`` into the artist images directory as the artist's picture; works
        like ``set_cover``."""
        source = Path(source)
        check_image_name(source.name)
        return self.set_image_data(artist_id, source.read_bytes(), source.name, picture=False)

    def set_image_data(
        self, artist_id: int, data: bytes, file_name: str, picture: bool = True
    ) -> Artist:
        """Make the image ``data`` (say, an upload) the artist's profile picture, its type
        coming from ``file_name``. With ``picture``, it is also kept in the artist's Pictures
        (see ``add_profile_picture``)."""
        artist = self.get_artist(artist_id)
        with self.transaction():
            name = self._store_image(self.images_dir, artist_id, data, file_name, artist.image)
            if name != artist.image:
                with self.connect() as conn:
                    conn.execute("UPDATE artists SET image = ? WHERE id = ?", (name, artist_id))
            if picture:
                self.add_profile_picture(artist_id, data, file_name)
        if name != artist.image:
            self._remove_files(self.images_dir, [artist.image])
        return self.get_artist(artist_id)

    def add_profile_picture(self, artist_id: int, data: bytes, file_name: str) -> Picture | None:
        """Keep a profile picture in the artist's Pictures, named "Profile picture" and tagged
        "profile", unless one of them is that same image already (``None`` then)."""
        key = hashlib.sha256(data).hexdigest()[:12]
        if any(_content_key(p.file) == key for p in self.list_pictures(artist_id)):
            return None
        return self.add_picture(
            artist_id, PictureIn("Profile picture", tags=["profile"]), data, file_name
        )

    def is_profile_picture(self, picture_id: int) -> bool:
        """Whether the picture is the same image as its artist's profile picture."""
        picture = self.get_picture(picture_id)
        image = self.get_artist(picture.artist_id).image
        return bool(image) and _content_key(image) == _content_key(picture.file)

    def picture_is_square(self, picture_id: int) -> bool:
        """Whether the picture is square (see ``imagesize.is_square``), so it can be the
        artist's profile picture; ``False`` when its file is missing or unreadable."""
        try:
            return is_square(self.picture_path(picture_id).read_bytes())
        except NotFound:
            return False

    def make_profile_picture(self, picture_id: int) -> Artist:
        """Make a square picture its artist's profile picture."""
        picture = self.get_picture(picture_id)
        if not self.picture_is_square(picture_id):
            raise ValueError("Only a square picture can be the profile picture.")
        data = self.picture_path(picture_id).read_bytes()
        return self.set_image_data(picture.artist_id, data, picture.file, picture=False)

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

    # Pictures

    def list_pictures(
        self,
        artist_id: int,
        q: str | None = None,
        tag: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Picture]:
        """The artist's pictures, newest first, that match ``q`` and ``tag`` (see
        ``PICTURES_WHERE``); ``limit`` of them from ``offset`` on, or all of them."""
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT p.* {PICTURES_WHERE} ORDER BY p.created_at DESC, p.id DESC"
                " LIMIT :limit OFFSET :offset",
                {
                    **self._picture_filters(artist_id, q, tag),
                    "limit": -1 if limit is None else limit,
                    "offset": offset,
                },
            ).fetchall()
        return [_picture(row) for row in rows]

    def count_pictures(self, artist_id: int, q: str | None = None, tag: str | None = None) -> int:
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS n {PICTURES_WHERE}", self._picture_filters(artist_id, q, tag)
            ).fetchone()
        return row["n"]

    @staticmethod
    def _picture_filters(artist_id: int, q: str | None, tag: str | None) -> dict:
        return {
            "artist_id": artist_id,
            "q": (q or "").strip() or None,
            "tag": (tag or "").strip() or None,
        }

    def picture_tags(self, artist_id: int) -> list[Point]:
        """The tags of the artist's pictures and how many have each, most used first. Tags that
        differ only in case count as one, written as the first picture that has it does."""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT value FROM pictures p, json_each(p.tags)
                    WHERE p.artist_id = ? ORDER BY p.id""",
                (artist_id,),
            ).fetchall()
        counts: Counter[str] = Counter()
        names: dict[str, str] = {}
        for row in rows:
            key = row["value"].casefold()
            names.setdefault(key, row["value"])
            counts[key] += 1
        ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        return [Point(names[key], count) for key, count in ranked]

    def get_picture(self, picture_id: int) -> Picture:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM pictures WHERE id = ?", (picture_id,)).fetchone()
        if row is None:
            raise NotFound(f"picture {picture_id} not found")
        return _picture(row)

    def add_picture(
        self, artist_id: int, data: PictureIn, content: bytes, file_name: str
    ) -> Picture:
        """Save the image ``content``, uploaded as ``file_name`` (one of ``IMAGE_TYPES``), as a
        picture of the artist. Without a name, it is named after the file."""
        self.get_artist(artist_id)
        file_name = Path(file_name.replace("\\", "/")).name.strip()
        check_image_name(file_name, "picture")
        if not content:
            raise ValueError(f"picture {file_name!r} is empty")
        if not data.name.strip():
            data = replace(data, name=Path(file_name).stem)
        values = self._picture_values(data)
        with self.transaction(), self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO pictures (artist_id, name, description, tags, file_name, file,
                                         created_at)
                   VALUES (:artist_id, :name, :description, :tags, :file_name, '', :now)""",
                {**values, "artist_id": artist_id, "file_name": file_name, "now": _now()},
            )
            picture_id = cur.lastrowid
            name = self._store_image(
                self.pictures_dir, picture_id, content, file_name, None, "picture"
            )
            conn.execute("UPDATE pictures SET file = ? WHERE id = ?", (name, picture_id))
        return self.get_picture(picture_id)

    def update_picture(self, picture_id: int, data: PictureIn) -> Picture:
        self.get_picture(picture_id)
        with self.connect() as conn:
            conn.execute(
                """UPDATE pictures SET name = :name, description = :description, tags = :tags
                    WHERE id = :id""",
                {**self._picture_values(data), "id": picture_id},
            )
        return self.get_picture(picture_id)

    def delete_picture(self, picture_id: int) -> None:
        picture = self.get_picture(picture_id)
        self._delete("pictures", picture_id)
        self._remove_files(self.pictures_dir, [picture.file])

    def picture_path(self, picture_id: int) -> Path:
        path = self.pictures_dir / self.get_picture(picture_id).file
        if not path.is_file():
            raise NotFound(f"picture {picture_id} has no image file")
        return path

    @staticmethod
    def _picture_values(data: PictureIn) -> dict:
        name = data.name.strip()
        if not name:
            raise ValueError("name is required")
        description = data.description.strip() if data.description else None
        return {"name": name, "description": description or None, "tags": _tags(data.tags)}

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
        mains_only: bool = False,
        order: str = "year",
        favorites_first: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[AlbumSummary]:
        """Albums, optionally only those with the given CD or vinyl ownership (see
        ``OWNERSHIP``), the favorites, the ones marked to listen to later, the ones with songs
        still missing their lyrics or the ones without a cover of their own (none, or the same
        image as another album), and only ``limit`` of them after the first ``offset``. They go
        from the oldest to the newest, or the other way with ``order="latest"``; or with
        ``order="plays"`` the most played first, ``order="recent"`` the last played first (the
        never played last) or ``order="rating"`` the highest rated first (the unrated last).
        ``favorites_first`` goes before any of them, and ``mains_only`` leaves out the editions
        merged into a main one."""
        year = "al.year IS NULL, " + ("al.year DESC" if order == "latest" else "al.year")
        order_by = {
            "year": year,
            "latest": year,
            "plays": f"x_plays DESC, {year}",
            "recent": "x_last_played IS NULL, x_last_played DESC",
            "rating": f"al.rating IS NULL, al.rating DESC, {year}",
        }[order]
        sql = f"""
            SELECT al.*, ar.name AS x_artist,
                   (SELECT COUNT(*) FROM songs WHERE album_id = al.id) AS x_songs,
                   (SELECT COALESCE(SUM(duration_seconds), 0) FROM songs
                     WHERE album_id = al.id) AS x_seconds,
                   (SELECT COUNT(*) FROM plays p JOIN songs s ON s.id = p.song_id
                     WHERE s.album_id = al.id) AS x_plays,
                   (SELECT MAX(p.played_at) FROM plays p JOIN songs s ON s.id = p.song_id
                     WHERE s.album_id = al.id) AS x_last_played,
                   (SELECT COUNT(*) FROM songs WHERE album_id = al.id
                       AND lyrics IS NOT NULL AND no_lyrics = 0) AS x_lyrics,
                   (SELECT COUNT(*) FROM songs WHERE album_id = al.id
                       AND no_lyrics = 1) AS x_no_lyrics,
                   EXISTS (SELECT 1 FROM albums o WHERE o.id != al.id
                              AND {SAME_COVER}) AS x_shared_cover
              {ALBUMS_WHERE}
             ORDER BY {"al.favorite DESC," if favorites_first else ""}
                      {order_by}, al.title COLLATE NOCASE, al.id
             LIMIT :limit OFFSET :offset
        """
        params = {
            **self._album_filters(
                artist_id, favorites, q, cd, vinyl, listen_later, missing_lyrics, no_own_cover
            ),
            "mains_only": int(mains_only),
            "limit": -1 if limit is None else limit,
            "offset": offset,
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
                    extra["last_played"],
                )
            )
        return out

    def count_albums(
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
    ) -> int:
        """How many albums ``list_albums`` finds without a ``limit``."""
        params = self._album_filters(
            artist_id, favorites, q, cd, vinyl, listen_later, missing_lyrics, no_own_cover
        )
        with self.connect() as conn:
            return conn.execute(
                f"SELECT COUNT(*) {ALBUMS_WHERE}", {**params, "mains_only": 0}
            ).fetchone()[0]

    @staticmethod
    def _album_filters(
        artist_id: int | None,
        favorites: bool,
        q: str | None,
        cd: str | None,
        vinyl: str | None,
        listen_later: bool,
        missing_lyrics: bool,
        no_own_cover: bool,
    ) -> dict:
        return {
            "artist_id": artist_id,
            "favorites": int(favorites),
            "listen_later": int(listen_later),
            "cd": cd,
            "vinyl": vinyl,
            "missing_lyrics": int(missing_lyrics),
            "no_own_cover": int(no_own_cover),
            "q": q or None,
        }

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

    def create_album(self, data: AlbumIn, uid: str | None = None) -> Album:
        values = self._album_values(data)
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO albums (artist_id, title, year, kind, unreleased, rating, notes,
                                       favorite, cd, vinyl, listen_later, created_at, uid)
                   VALUES (:artist_id, :title, :year, :kind, :unreleased, :rating, :notes,
                           :favorite, :cd, :vinyl, :listen_later, :now, :uid)""",
                {**values, "now": _now(), "uid": uid or new_uid()},
            )
        return self.get_album(cur.lastrowid)

    def copy_album(self, album_id: int, title: str) -> Album:
        """Make a new album of the same artist called ``title`` with everything the album has:
        its details, cover and songs, with their details, lyrics, featured artists and videos.
        Plays and playlist entries stay with the original (copied favorites and listen later marks
        still land in those playlists)."""
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
                self.set_featuring(new.id, [a.id for a in self.list_featuring(song.id)])
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
        self._hand_over_mains("album_id = ?", album_id)
        heir = self._next_main_album(album_id)
        if heir is not None:
            self._swap_main_album(album_id, heir)
        self._delete("albums", album_id)
        self._remove_files(self.covers_dir, [cover])

    def set_cover(self, album_id: int, source: str | Path) -> Album:
        """Copy ``source`` into the covers directory as the album's cover.

        Files are named after their content, so setting the same image again is a no-op and a
        changed image gets a new URL (no stale browser caches).
        """
        source = Path(source)
        check_cover_name(source.name)
        return self.set_cover_data(album_id, source.read_bytes(), source.name)

    def set_cover_data(self, album_id: int, data: bytes, file_name: str) -> Album:
        """Like ``set_cover``, for an image's ``data`` (say, an upload) whose type comes from
        ``file_name``."""
        album = self.get_album(album_id)
        name = self._store_image(
            self.covers_dir, album_id, data, file_name, album.cover, "cover", COVER_TYPES
        )
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

    def _store_image(
        self,
        directory: Path,
        record_id: int,
        data: bytes,
        file_name: str,
        current: str | None,
        what: str = "image",
        types: dict[str, str] = IMAGE_TYPES,
    ) -> str:
        """Write ``data`` into ``directory`` under a name made from its content (with the suffix
        of ``file_name``, one of ``types``), unless it is ``current`` already, and return that
        name."""
        suffix = check_image_name(file_name, what, types)
        if not data:
            raise ValueError(f"{what} {file_name!r} is empty")
        name = f"{record_id}-{hashlib.sha256(data).hexdigest()[:12]}{suffix}"
        if name != current and not self.dry:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / name).write_bytes(data)
        return name

    def _remove_files(self, directory: Path, names: list[str | None]) -> None:
        if self.dry:
            return
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
        featuring: int | None = None,
        mains_only: bool = False,
        order: str = "title",
        favorites_first: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[SongSummary]:
        """Songs matching every filter given (``missing_lyrics``: songs with words but no lyrics
        yet; ``featuring``: songs of other artists that feature this one; ``mains_only``: no
        songs merged into a main version), and only ``limit`` of them after the first
        ``offset``; ``order="playlist"`` keeps the order of the songs in ``playlist_id``, and
        ``favorites_first`` goes before it. Merged songs count as one: each has the plays of
        them all."""
        order_by = {
            "title": "s.title COLLATE NOCASE",
            "track": "s.track_number IS NULL, s.track_number, s.title COLLATE NOCASE",
            "plays": "x_plays DESC, s.title COLLATE NOCASE",
            "recent": "x_last_played IS NULL, x_last_played DESC",
            "rating": "s.rating IS NULL, s.rating DESC, x_plays DESC, s.title COLLATE NOCASE",
            "playlist": "ps.position",
            "album": "al.id IS NULL, al.year IS NULL, al.year, al.title COLLATE NOCASE, al.id, "
            "s.track_number IS NULL, s.track_number, s.title COLLATE NOCASE",
        }[order]
        sql = f"""
            SELECT s.*, ar.name AS x_artist, al.title AS x_album, al.cover AS x_album_cover,
                   (SELECT COUNT(*) FROM plays WHERE song_id IN ({SAME_SONG})) AS x_plays,
                   (SELECT MAX(played_at) FROM plays WHERE song_id IN ({SAME_SONG}))
                       AS x_last_played,
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
                featuring,
                mains_only,
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
        featuring: int | None = None,
        mains_only: bool = False,
    ) -> dict:
        return {
            "artist_id": artist_id,
            "album_id": album_id,
            "playlist_id": playlist_id,
            "song_ids": None if song_ids is None else json.dumps(song_ids),
            "featuring": featuring,
            "favorites": int(favorites),
            "listen_later": int(listen_later),
            "missing_lyrics": int(missing_lyrics),
            "mains_only": int(mains_only),
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

    def create_song(self, data: SongIn, uid: str | None = None) -> Song:
        values = self._song_values(data)
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO songs (artist_id, album_id, title, track_number,
                                      duration_seconds, rating, notes, favorite, no_lyrics,
                                      explicit, mv, listen_later, created_at, uid)
                   VALUES (:artist_id, :album_id, :title, :track_number, :duration_seconds,
                           :rating, :notes, :favorite, :no_lyrics, :explicit, :mv,
                           :listen_later, :now, :uid)""",
                {**values, "now": _now(), "uid": uid or new_uid()},
            )
        self._sync_marks(cur.lastrowid)
        song = self.get_song(cur.lastrowid)
        self.ensure_lyrics_file(song.id)
        return song

    def update_song(self, song_id: int, data: SongIn) -> Song:
        old_path = self.lyrics_path(song_id)
        old_tab = self.tab_path(song_id)
        old_lrc = self.lrc_path(song_id)
        shared = self._shares_lyrics_file(self.get_song(song_id))
        with self.connect() as conn:
            conn.execute(
                """UPDATE songs SET artist_id = :artist_id, album_id = :album_id,
                       title = :title, track_number = :track_number,
                       duration_seconds = :duration_seconds, rating = :rating,
                       notes = :notes, favorite = :favorite, no_lyrics = :no_lyrics,
                       explicit = :explicit, mv = :mv, listen_later = :listen_later
                   WHERE id = :id""",
                {**self._song_values(data), "id": song_id},
            )
            # An artist is not featured on its own song.
            conn.execute(
                "DELETE FROM song_features WHERE song_id = ? AND artist_id = ?",
                (song_id, data.artist_id),
            )
        self._sync_marks(self._spread_marks(song_id))
        # A renamed song takes its lyrics, timed lyrics and tab files along (copies, if another
        # song still uses them).
        new_path = self.lyrics_path(song_id)
        if self.dry:
            return self.get_song(song_id)
        if old_path and new_path and old_path.is_file():
            _move_file(old_path, new_path, copy=shared)
        if old_tab and self.tab_path(song_id) is None:
            _move_file(old_tab, self.new_tab_path(song_id, old_tab.suffix), copy=shared)
        if old_lrc and self.lrc_path(song_id) is None:
            _move_file(old_lrc, new_path.with_suffix(".lrc"), copy=shared)
        self.ensure_lyrics_file(song_id)
        return self.get_song(song_id)

    def delete_song(self, song_id: int) -> None:
        path = self.lyrics_path(song_id)
        shared = self._shares_lyrics_file(self.get_song(song_id))
        self._hand_over_mains("id = ?", song_id)
        self._delete("songs", song_id)
        # Lyrics someone wrote are kept; an empty file only belonged to this song.
        if path and not self.dry and not shared and path.is_file() and path.stat().st_size == 0:
            path.unlink()

    def lyrics_path(self, song_id: int) -> Path | None:
        """The song's lyrics file, whether it exists or not; ``None`` without ``media_dir``.

        Names match ignoring case, like the importer does, so an existing
        ``our lady of sorrows.txt`` is the file of "Our Lady of Sorrows".
        """
        song = self.get_song(song_id)
        if self.media_dir is None:
            return None
        folder = self._song_folder(song, "songs")
        name = lyrics_file_name(song.title)
        return _find_file(folder, [name]) or folder / name

    def lrc_path(self, song_id: int) -> Path | None:
        """The song's timed lyrics, an ``.lrc`` file next to its lyrics file and named like it
        (ignoring case); ``None`` when it has none."""
        song = self.get_song(song_id)
        if self.media_dir is None:
            return None
        stem = lyrics_file_name(song.title).removesuffix(".txt")
        return _find_file(self._song_folder(song, "songs"), [f"{stem}.lrc"])

    def timed_lyrics(self, song_id: int) -> list[TimedLine]:
        """The lines of the song's ``.lrc`` file, read now, so edits show without a new import.

        Raises ``ValueError`` when the file is not UTF-8 text.
        """
        path = self.lrc_path(song_id)
        return read_lrc(path) if path else []

    def tab_path(self, song_id: int) -> Path | None:
        """The song's tab, a Guitar Pro file in ``bands/<artist>/tabs/`` named like its lyrics
        file (any of ``TAB_TYPES``, ignoring case); ``None`` when it has none."""
        song = self.get_song(song_id)
        if self.media_dir is None:
            return None
        stem = lyrics_file_name(song.title).removesuffix(".txt")
        return _find_file(self._song_folder(song, "tabs"), [stem + t for t in TAB_TYPES])

    def new_tab_path(self, song_id: int, suffix: str = ".gp5") -> Path | None:
        """Where a tab for the song goes; ``None`` without ``media_dir``."""
        song = self.get_song(song_id)
        if self.media_dir is None:
            return None
        stem = lyrics_file_name(song.title).removesuffix(".txt")
        return self._song_folder(song, "tabs") / f"{stem}{suffix}"

    def _song_folder(self, song: Song, kind: str) -> Path:
        folder = self.artist_folder(self.get_artist(song.artist_id).name)
        assert folder is not None
        return folder / kind

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
        if path is not None and not path.exists() and not self.dry:
            path.parent.mkdir(parents=True, exist_ok=True)
            lyrics = self.get_song(song_id).lyrics
            path.write_text(f"{lyrics}\n" if lyrics else "", encoding="utf-8")
        return path

    def reload_lyrics(self, song_id: int) -> Song:
        """Read the song's lyrics again from its file, so edits show without a new import. A
        blank file takes the words of the song's ``.lrc`` file, if it has one.

        Raises ``ValueError`` when the file is not UTF-8 text; the stored lyrics stay as they are.
        """
        path = self.ensure_lyrics_file(song_id)
        if path is None:
            return self.get_song(song_id)
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ValueError(f"{path.name} is not UTF-8 text") from None
        return self.set_lyrics(song_id, text.strip() or plain_lyrics(self.timed_lyrics(song_id)))

    def set_lyrics(self, song_id: int, lyrics: str | None, detect_explicit: bool = True) -> Song:
        """Store the song's lyrics; blank text marks them as missing again.

        New lyrics with obvious swearing (see ``implicated.explicit``) mark the song as explicit,
        unless ``detect_explicit`` is false. Only a change does it, so a song unmarked by hand
        stays unmarked while its words stay the same, and nothing ever unmarks a song.
        """
        song = self.get_song(song_id)
        lyrics = (lyrics.strip() if lyrics else None) or None
        explicit = song.explicit or (
            detect_explicit and lyrics != song.lyrics and is_explicit(lyrics)
        )
        with self.connect() as conn:
            conn.execute(
                "UPDATE songs SET lyrics = ?, explicit = ? WHERE id = ?",
                (lyrics, int(explicit), song_id),
            )
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
        values["explicit"] = int(data.explicit)
        values["mv"] = youtube_id(data.mv, "mv")
        values["listen_later"] = int(data.listen_later)
        return values

    # Featured artists

    def list_featuring(self, song_id: int) -> list[Artist]:
        """The other artists the song features, in the order they are credited."""
        self.get_song(song_id)
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT a.* FROM song_features f JOIN artists a ON a.id = f.artist_id
                    WHERE f.song_id = ? ORDER BY f.position""",
                (song_id,),
            ).fetchall()
        return [_artist(row) for row in rows]

    def set_featuring(self, song_id: int, artist_ids: list[int]) -> list[Artist]:
        """Replace the artists the song features with ``artist_ids``, in that order (repeats
        count once). The song's own artist cannot be one of them."""
        song = self.get_song(song_id)
        artist_ids = list(dict.fromkeys(artist_ids))
        for artist_id in artist_ids:
            self.get_artist(artist_id)
            if artist_id == song.artist_id:
                raise ValueError("a song cannot feature its own artist")
        with self.connect() as conn:
            conn.execute("DELETE FROM song_features WHERE song_id = ?", (song_id,))
            conn.executemany(
                "INSERT INTO song_features (song_id, artist_id, position) VALUES (?, ?, ?)",
                [(song_id, artist_id, i) for i, artist_id in enumerate(artist_ids)],
            )
        return self.list_featuring(song_id)

    def featured_songs(self, artist_id: int) -> list[SongSummary]:
        """Songs of other artists that feature this one, A–Z."""
        self.get_artist(artist_id)
        return self.list_songs(featuring=artist_id)

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
        self._sync_marks(self._spread_marks(song_id))
        return Play(cur.lastrowid, song_id, played_at)

    def record_album_play(self, album_id: int) -> list[Play]:
        """Log a listen of every song on the album, in track order; the album is no longer
        waiting to be listened to later."""
        self.get_album(album_id)
        played_at = _now()
        songs = self.list_songs(album_id=album_id, order="track")
        with self.transaction():
            plays = [self.record_play(s.song.id, played_at) for s in songs]
            self.finish_album(album_id)
            return plays

    def finish_album(self, album_id: int) -> None:
        """The album was listened to in full, so it leaves Listen Later, with the editions
        merged with it. (Artists only ever leave it by hand.)"""
        with self.connect() as conn:
            cur = conn.execute("UPDATE albums SET listen_later = 0 WHERE id = ?", (album_id,))
        if cur.rowcount == 0:
            raise NotFound(f"album {album_id} not found")
        self._spread_album_marks(album_id)

    def recent_plays(self, limit: int = 10) -> list[tuple[Play, SongSummary]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM plays ORDER BY played_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [(Play(**dict(row)), self.get_song_summary(row["song_id"])) for row in rows]

    # Budget

    def get_cart(self) -> Cart:
        """The albums in the Budget cart, in the order they were added, with the shipping, the
        discount, the total and the budget it is editing, if any."""
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM cart_items ORDER BY id").fetchall()
            editing = conn.execute("SELECT * FROM budgets WHERE in_cart = 1").fetchone()
            cart = conn.execute("SELECT shipping_cents, discount_cents FROM cart").fetchone()
        shipping, discount = cart["shipping_cents"] / 100, cart["discount_cents"] / 100
        lines = [self._cart_line(_cart_item(row)) for row in rows]
        total = _total(lines, shipping, discount)
        return Cart(lines, total, _budget(editing) if editing else None, shipping, discount)

    def set_shipping(self, shipping: float) -> Cart:
        """Say what shipping the cart costs in all; it adds up to the total."""
        validate_price(shipping, "shipping")
        with self.connect() as conn:
            conn.execute("UPDATE cart SET shipping_cents = ?", (_cents(shipping),))
        return self.get_cart()

    def set_discount(self, discount: float) -> Cart:
        """Say how much is taken off the cart in all; it comes off the total."""
        validate_price(discount, "discount")
        with self.connect() as conn:
            conn.execute("UPDATE cart SET discount_cents = ?", (_cents(discount),))
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
                """SELECT al.*, ar.name AS x_artist, ar.favorite AS x_favorite FROM albums al
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
                    state = getattr(album, each)
                    options.append(
                        CartOption(album, extra["artist"], each, state, bool(extra["favorite"]))
                    )
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
            conn.execute("UPDATE cart SET shipping_cents = 0, discount_cents = 0")
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
                "UPDATE budgets SET shipping_cents = ?, discount_cents = ? WHERE id = ?",
                (_cents(cart.shipping), _cents(cart.discount), budget_id),
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
        return BudgetDetail(budget, lines, _total(lines, budget.shipping, budget.discount))

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
                """UPDATE cart SET (shipping_cents, discount_cents) =
                       (SELECT shipping_cents, discount_cents FROM budgets WHERE id = ?)""",
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

    def create_playlist(
        self, data: PlaylistIn, uid: str | None = None, folder_id: int | None = None
    ) -> Playlist:
        """A new playlist in ``folder_id`` (one of yours, or Default, which it is in without
        one)."""
        name = self._playlist_name(data)
        folder = self._folder_to_fill(folder_id)
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO playlists (name, created_at, uid, folder_id) VALUES (?, ?, ?, ?)",
                (name, _now(), uid or new_uid(), folder.id),
            )
        return self.get_playlist(cur.lastrowid)

    def copy_playlist(
        self, playlist_id: int, data: PlaylistIn, folder_id: int | None = None
    ) -> Playlist:
        """A new playlist named as in ``data`` with the songs of ``playlist_id``, in its order
        (a built-in one's too: the copy is a playlist of your own, which keeps them), in
        ``folder_id`` as in ``create_playlist``."""
        songs = [s.song.id for s in self.get_playlist_detail(playlist_id).songs]
        copy = self.create_playlist(data, folder_id=folder_id)
        self._write_playlist(copy.id, songs)
        return copy

    def update_playlist(self, playlist_id: int, data: PlaylistIn) -> Playlist:
        self._own_playlist(playlist_id, "renamed")
        name = self._playlist_name(data)
        with self.connect() as conn:
            conn.execute("UPDATE playlists SET name = ? WHERE id = ?", (name, playlist_id))
        return self.get_playlist(playlist_id)

    def delete_playlist(self, playlist_id: int) -> None:
        self._own_playlist(playlist_id, "deleted")
        self._delete("playlists", playlist_id)

    def move_playlist(self, playlist_id: int, folder_id: int) -> Playlist:
        """Put one of your playlists in another folder: one of yours, or Default. The Setlists
        folder holds exactly the concerts' setlists (see ``sync_setlists``), so a setlist cannot
        leave it and no other playlist can go in."""
        self._own_playlist(playlist_id, "moved")
        self.sync_setlists()
        playlist = self.get_playlist(playlist_id)
        if playlist.folder_id == self.special_folder("setlists").id:
            raise ValueError(
                f"{playlist.name} is a concert's setlist, so it stays in Setlists while it is one"
            )
        folder = self._folder_to_fill(folder_id)
        with self.connect() as conn:
            conn.execute(
                "UPDATE playlists SET folder_id = ? WHERE id = ?", (folder.id, playlist_id)
            )
        return self.get_playlist(playlist_id)

    def setlist_playlists(self) -> dict[str, Playlist]:
        """The playlist each setlist name stands for, by its name folded to ignore case: the
        oldest of your playlists called that."""
        by_name: dict[str, Playlist] = {}
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM playlists WHERE kind IS NULL ORDER BY id").fetchall()
        for row in rows:
            by_name.setdefault(row["name"].casefold(), Playlist(**dict(row)))
        return by_name

    def sync_setlists(self, concerts: list[Concert] | None = None) -> None:
        """Make the Setlists folder hold exactly the playlists that are the setlist of one of
        ``concerts`` (by default, those in the library folder's ``concerts.csv``, which can be
        edited by hand): they go into it from any folder, and the others in it go back to
        Default. Without a library folder, no playlist is a setlist; when the file cannot be
        read, the folders stay as they are."""
        if concerts is None:
            concerts = []
            if self.media_dir is not None:
                try:
                    concerts, _warnings = read_concerts(self.media_dir / CONCERTS_FILE)
                except (OSError, UnicodeDecodeError):
                    return
        by_name = self.setlist_playlists()
        ids = {
            by_name[c.setlist.casefold()].id
            for c in concerts
            if c.setlist and c.setlist.casefold() in by_name
        }
        self._fill_setlists(ids)

    def _fill_setlists(self, playlist_ids: set[int]) -> None:
        """Make the Setlists folder hold exactly ``playlist_ids``; see ``sync_setlists``."""
        setlists = self.special_folder("setlists").id
        default = self.special_folder("default").id
        ids = sorted(playlist_ids)
        marks = ", ".join("?" * len(ids))
        with self.connect() as conn:
            conn.execute(
                f"""UPDATE playlists SET folder_id = ?
                     WHERE folder_id = ? AND id NOT IN ({marks})""",
                (default, setlists, *ids),
            )
            if ids:
                conn.execute(
                    f"""UPDATE playlists SET folder_id = ?
                         WHERE kind IS NULL AND id IN ({marks})""",
                    (setlists, *ids),
                )

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

    # Folders of playlists

    def list_folders(self) -> list[FolderDetail]:
        """Every folder with its playlists: Default, Setlists, then yours A–Z. The Setlists
        folder is brought up to date with ``concerts.csv`` first."""
        self.sync_setlists()
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM folders
                    ORDER BY kind IS NULL, kind = 'setlists', name COLLATE NOCASE, id"""
            ).fetchall()
        folders = {row["id"]: FolderDetail(Folder(**dict(row))) for row in rows}
        for summary in self.list_playlists():
            folders[summary.playlist.folder_id].playlists.append(summary)
        return list(folders.values())

    def get_folder(self, folder_id: int) -> Folder:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
        if row is None:
            raise NotFound(f"folder {folder_id} not found")
        return Folder(**dict(row))

    def special_folder(self, kind: str) -> Folder:
        """The built-in folder of one of ``SPECIAL_FOLDERS``."""
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM folders WHERE kind = ?", (kind,)).fetchone()
        return Folder(**dict(row))

    def find_folder(self, name: str) -> Folder | None:
        """The folder called ``name``, ignoring case."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM folders WHERE name = ? COLLATE NOCASE ORDER BY id", (name.strip(),)
            ).fetchone()
        return Folder(**dict(row)) if row else None

    def create_folder(self, data: FolderIn) -> Folder:
        name = self._folder_name(data)
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO folders (name, created_at) VALUES (?, ?)", (name, _now())
            )
        return self.get_folder(cur.lastrowid)

    def update_folder(self, folder_id: int, data: FolderIn) -> Folder:
        self._own_folder(folder_id, "renamed")
        name = self._folder_name(data, folder_id)
        with self.connect() as conn:
            conn.execute("UPDATE folders SET name = ? WHERE id = ?", (name, folder_id))
        return self.get_folder(folder_id)

    def delete_folder(self, folder_id: int) -> None:
        """Delete one of your folders; its playlists go to Default."""
        self._own_folder(folder_id, "deleted")
        with self.connect() as conn:
            conn.execute(
                "UPDATE playlists SET folder_id = ? WHERE folder_id = ?",
                (self.special_folder("default").id, folder_id),
            )
            conn.execute("DELETE FROM folders WHERE id = ?", (folder_id,))

    def _own_folder(self, folder_id: int, change: str) -> Folder:
        folder = self.get_folder(folder_id)
        if folder.kind:
            raise ValueError(f"{folder.name} is a built-in folder and cannot be {change}")
        return folder

    def _folder_to_fill(self, folder_id: int | None) -> Folder:
        """The folder a playlist may be put in by hand: Default when ``folder_id`` is ``None``,
        never Setlists."""
        if folder_id is None:
            return self.special_folder("default")
        folder = self.get_folder(folder_id)
        if folder.kind == "setlists":
            raise ValueError(
                "Setlists only holds the concerts' setlists; make the playlist a concert's "
                "setlist on the Concerts page instead"
            )
        return folder

    def _folder_name(self, data: FolderIn, folder_id: int | None = None) -> str:
        name = " ".join(data.name.split())
        if not name:
            raise ValueError("name is required")
        same = self.find_folder(name)
        if same is not None and same.id != folder_id:
            if same.kind:
                raise ValueError(f"{same.name} is the name of a built-in folder")
            raise ValueError(f"there is already a folder called {same.name}")
        return name

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
        self._prune_tiers("playlist", playlist_id)
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
        if self.get_playlist(playlist_id).kind:
            # Built-in playlists hold the main version of merged songs.
            song_ids = list(dict.fromkeys(self.get_song(i).main_id or i for i in song_ids))
        with self.connect() as conn:
            conn.execute("DELETE FROM playlist_songs WHERE playlist_id = ?", (playlist_id,))
            conn.executemany(
                "INSERT INTO playlist_songs (playlist_id, song_id, position) VALUES (?, ?, ?)",
                [(playlist_id, song_id, position) for position, song_id in enumerate(song_ids)],
            )
        self._prune_tiers("playlist", playlist_id)
        self._marks_from_playlist(playlist_id)

    def _marks_from_playlist(self, playlist_id: int) -> None:
        """After a built-in playlist changed, mark exactly the songs in it, and the songs merged
        with them."""
        kind = self.get_playlist(playlist_id).kind
        if kind is None:
            return
        field = SPECIAL_PLAYLISTS[kind][1]
        with self.connect() as conn:
            conn.execute(
                f"""UPDATE songs SET {field} = EXISTS (SELECT 1 FROM playlist_songs
                        WHERE playlist_id = ? AND song_id = COALESCE(songs.main_id, songs.id))""",
                (playlist_id,),
            )

    def _sync_marks(self, song_id: int | None = None) -> None:
        """After songs' marks changed, put the marked songs (all of them, or just ``song_id``)
        at the end of their built-in playlist and take out the others. Songs merged into a main
        version are left out: the main one stands for them."""
        for kind, (_name, field) in SPECIAL_PLAYLISTS.items():
            params = {"playlist_id": self.special_playlist(kind).id, "song_id": song_id}
            with self.connect() as conn:
                conn.execute(
                    f"""DELETE FROM playlist_songs
                         WHERE playlist_id = :playlist_id AND song_id IN (
                               SELECT id FROM songs WHERE ({field} = 0 OR main_id IS NOT NULL)
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
                         WHERE s.{field} = 1 AND s.main_id IS NULL
                           AND (:song_id IS NULL OR s.id = :song_id)
                           AND s.id NOT IN (SELECT song_id FROM playlist_songs
                                             WHERE playlist_id = :playlist_id)""",
                    params,
                )
            self._prune_tiers("playlist", params["playlist_id"])

    # Merged songs: the same song on several albums (a deluxe edition, say), each still on its
    # own album, counted as one where it matters. One of them is the main version (its
    # ``main_id`` is ``None``); the others point at it. They share the plays (see
    # ``list_songs``), the favorite and listen later marks and the rating; the built-in
    # playlists, the Overview lists of marked songs and tier lists show the main one only.

    def song_versions(self, song_id: int) -> list[SongSummary]:
        """The songs merged with this one, itself included, the main one first and then album
        by album; just the song when it is not merged."""
        main = self._main_of(song_id)
        with self.connect() as conn:
            ids = [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM songs WHERE id = ? OR main_id = ?", (main, main)
                )
            ]
        songs = self.list_songs(song_ids=ids, order="album")
        return sorted(songs, key=lambda s: s.song.id != main)

    def merge_suggestions(self, song_id: int) -> list[SongSummary]:
        """The artist's other songs with the same title (ignoring case and kinds of dash) that
        are not merged with this one yet, album by album."""
        song = self.get_song(song_id)
        main = song.main_id or song.id
        with self.connect() as conn:
            ids = [
                row[0]
                for row in conn.execute(
                    f"""SELECT id FROM songs WHERE artist_id = ? AND {_same_title("title")}
                          AND COALESCE(main_id, id) != ?""",
                    (song.artist_id, _dashless(song.title), main),
                )
            ]
        return self.list_songs(song_ids=ids, order="album") if ids else []

    def merge_songs(self, main_id: int, song_ids: list[int]) -> list[SongSummary]:
        """Merge songs with ``main_id``: from now on they count as one, with the main version of
        ``main_id`` (itself, or the one it is merged into) as theirs. A song already merged with
        others brings them along. They are a favorite, or to listen to later, when any of them
        was, and they keep the main version's rating (or the first one any of them has).
        Returns all the songs merged, as ``song_versions``."""
        main = self._main_of(main_id)
        joining = []
        for song_id in dict.fromkeys(song_ids):
            other = self._main_of(song_id)
            if other != main and other not in joining:
                joining.append(other)
        if not joining:
            raise ValueError("those songs are merged already")
        with self.transaction(), self.connect() as conn:
            members = [main]
            for other in joining:
                group = [
                    row[0]
                    for row in conn.execute(
                        "SELECT id FROM songs WHERE id = ? OR main_id = ?", (other, other)
                    )
                ]
                members += group
                conn.execute(
                    "UPDATE songs SET main_id = ? WHERE id = ? OR main_id = ?",
                    (main, other, other),
                )
                # Where the main version now stands for the song, it keeps the joining
                # one's place.
                self._carry_placements(other, main)
                self._carry_playlist_places(other, main)
            rows = conn.execute(
                "SELECT favorite, listen_later, rating FROM songs WHERE id IN "
                "(SELECT value FROM json_each(?))",
                (json.dumps(members),),
            ).fetchall()
            ratings = [self.get_song(main).rating, *(row["rating"] for row in rows)]
            conn.execute(
                "UPDATE songs SET favorite = ?, listen_later = ?, rating = ? WHERE id = ?",
                (
                    int(any(row["favorite"] for row in rows)),
                    int(any(row["listen_later"] for row in rows)),
                    next((r for r in ratings if r is not None), None),
                    main,
                ),
            )
            self._spread_marks(main)
            self._sync_marks()
        return self.song_versions(main)

    def make_main(self, song_id: int) -> list[SongSummary]:
        """Make a merged song the main version of the songs it is merged with."""
        song = self.get_song(song_id)
        if song.main_id is None:
            raise ValueError(f"{song.title} is not merged into another song")
        self._swap_main(song.main_id, song_id)
        return self.song_versions(song_id)

    def unmerge_song(self, song_id: int) -> None:
        """Take a song out of the songs it is merged with; it keeps its marks and rating. When
        it was the main version, the next one becomes the main one."""
        song = self.get_song(song_id)
        if song.main_id is None:
            heir = self._next_main(song_id)
            if heir is None:
                raise ValueError(f"{song.title} is not merged with other songs")
            self._swap_main(song_id, heir)
        with self.connect() as conn:
            conn.execute("UPDATE songs SET main_id = NULL WHERE id = ?", (song_id,))
        self._sync_marks()

    # Merged albums: editions of one album (a deluxe one and the original, say), each with its
    # own songs, counted as one: they share the favorite and listen later marks and the rating,
    # and the Overview lists the main edition only. Merging one into another merges their songs
    # of equal names the same way (see ``merge_albums``).

    def album_editions(self, album_id: int) -> list[Album]:
        """The albums merged with this one, itself included: the main edition first, then by
        year; just the album when it is not merged."""
        main = self._main_album_of(album_id)
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM albums WHERE id = ? OR main_id = ?
                    ORDER BY id != ?, year IS NULL, year, id""",
                (main, main, main),
            ).fetchall()
        return [_album(row) for row in rows]

    def album_merge_choices(self, album_id: int) -> list[tuple[Album, int]]:
        """The artist's albums this one can be merged with (those not merged with it yet), each
        with how many of their songs are named like one of this album's."""
        album = self.get_album(album_id)
        mine = {_title_key(s.song.title) for s in self.list_songs(album_id=album_id)}
        group = {a.id for a in self.album_editions(album_id)}
        return [
            (other.album, len(mine & set(self._by_title(other.album.id))))
            for other in self.list_albums(album.artist_id)
            if other.album.id not in group
        ]

    def merge_albums(self, album_id: int, into_album_id: int) -> int:
        """Merge an album (and the albums merged with it) into ``into_album_id``, whose main
        edition is theirs from now on. Then each song of the album is merged with the song of
        ``into_album_id`` with the same name (ignoring case, outer spaces and kinds of dash),
        whose version is the main one, the same way round. The albums are a favorite, or to
        listen to later, when any of them was, and keep the main edition's rating (or the first
        one any of them has). Returns how many songs were merged."""
        main = self._main_album_of(into_album_id)
        joining = self._main_album_of(album_id)
        if main == joining:
            raise ValueError("those albums are merged already")
        with self.transaction(), self.connect() as conn:
            conn.execute(
                "UPDATE albums SET main_id = ? WHERE id = ? OR main_id = ?",
                (main, joining, joining),
            )
            rows = conn.execute(
                "SELECT favorite, listen_later, rating FROM albums WHERE id = ? OR main_id = ?",
                (main, main),
            ).fetchall()
            ratings = [self.get_album(main).rating, *(row["rating"] for row in rows)]
            conn.execute(
                "UPDATE albums SET favorite = ?, listen_later = ?, rating = ? WHERE id = ?",
                (
                    int(any(row["favorite"] for row in rows)),
                    int(any(row["listen_later"] for row in rows)),
                    next((r for r in ratings if r is not None), None),
                    main,
                ),
            )
            self._spread_album_marks(main)
            return self._merge_album_songs(album_id, into_album_id)

    def make_main_album(self, album_id: int) -> list[Album]:
        """Make a merged album the main edition of the albums it is merged with."""
        album = self.get_album(album_id)
        if album.main_id is None:
            raise ValueError(f"{album.title} is not merged into another album")
        self._swap_main_album(album.main_id, album_id)
        return self.album_editions(album_id)

    def unmerge_album(self, album_id: int) -> None:
        """Take an album out of the albums it is merged with, and its songs out of their merges
        with songs of those albums; it keeps its marks and rating. When it was the main
        edition, the next one becomes the main one."""
        album = self.get_album(album_id)
        others = [a.id for a in self.album_editions(album_id) if a.id != album_id]
        if not others:
            raise ValueError(f"{album.title} is not merged with other albums")
        with self.transaction():
            for s in self.list_songs(album_id=album_id):
                if any(
                    v.song.album_id in others for v in self.song_versions(s.song.id)
                ):  # fmt: skip
                    self.unmerge_song(s.song.id)
            if album.main_id is None:
                self._swap_main_album(album_id, self._next_main_album(album_id))
            with self.connect() as conn:
                conn.execute("UPDATE albums SET main_id = NULL WHERE id = ?", (album_id,))

    def unmerged_songs(self, album_id: int) -> list[tuple[SongSummary, list[SongSummary]]]:
        """The album's songs merged with no song of the albums it is merged with, each with the
        songs of those albums it could be merged with (the ones merged with none of this
        album's), album by album."""
        others = [a.id for a in self.album_editions(album_id) if a.id != album_id]
        if not others:
            return []
        mine = self.list_songs(album_id=album_id, order="track")
        mine_groups = {s.song.main_id or s.song.id for s in mine}
        theirs = [
            s
            for other in others
            for s in self.list_songs(album_id=other, order="track")
        ]  # fmt: skip
        theirs_groups = {s.song.main_id or s.song.id for s in theirs}
        candidates = [s for s in theirs if (s.song.main_id or s.song.id) not in mine_groups]
        return [
            (s, candidates)
            for s in mine
            if (s.song.main_id or s.song.id) not in theirs_groups
        ]  # fmt: skip

    def merge_album_song(self, song_id: int, other_id: int) -> list[SongSummary]:
        """Merge two songs of merged albums, the way round the albums are: the song on the
        main edition is the main version (and when neither is, ``other_id``'s)."""
        song = self.get_song(song_id)
        other = self.get_song(other_id)
        main_album = self._main_album_of(song.album_id) if song.album_id else None
        if main_album is not None and song.album_id == main_album:
            return self.merge_songs(song_id, [other_id])
        return self.merge_songs(other.id, [song_id])

    def _merge_album_songs(self, album_id: int, into_album_id: int) -> int:
        """Merge each song of the album with the song named like it on ``into_album_id``, whose
        version is the main one (or stays merged into its own main version). Returns how many
        songs were merged."""
        theirs = self._by_title(into_album_id)
        merged = 0
        with self.transaction():
            for s in self.list_songs(album_id=album_id, order="track"):
                match = theirs.get(_title_key(s.song.title))
                if match is not None and self._main_of(match) != self._main_of(s.song.id):
                    self.merge_songs(match, [s.song.id])
                    merged += 1
        return merged

    def _main_album_of(self, album_id: int) -> int:
        album = self.get_album(album_id)
        return album.main_id or album.id

    def _spread_album_marks(self, album_id: int) -> None:
        """Give the albums merged with ``album_id`` its marks and rating."""
        album = self.get_album(album_id)
        main = album.main_id or album.id
        with self.connect() as conn:
            conn.execute(
                """UPDATE albums SET favorite = :favorite, listen_later = :later, rating = :rating
                    WHERE (id = :main OR main_id = :main) AND id != :id""",
                {
                    "favorite": int(album.favorite),
                    "later": int(album.listen_later),
                    "rating": album.rating,
                    "main": main,
                    "id": album_id,
                },
            )

    def _spread_all_album_marks(self) -> None:
        """Give every merged album the marks and rating of its main edition."""
        with self.connect() as conn:
            conn.execute(
                """UPDATE albums SET (favorite, listen_later, rating) =
                       (SELECT m.favorite, m.listen_later, m.rating FROM albums m
                         WHERE m.id = albums.main_id)
                    WHERE main_id IS NOT NULL"""
            )

    def _next_main_album(self, main_id: int) -> int | None:
        """The album merged into ``main_id`` that would be the main edition after it."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT id FROM albums WHERE main_id = ? ORDER BY year IS NULL, year, id LIMIT 1",
                (main_id,),
            ).fetchone()
        return row[0] if row else None

    def _swap_main_album(self, old: int, new: int | None) -> None:
        """Make ``new``, merged into ``old``, the main edition instead of it."""
        if new is None:
            return
        with self.connect() as conn:
            conn.execute("UPDATE albums SET main_id = ? WHERE main_id = ?", (new, old))
            conn.execute("UPDATE albums SET main_id = ? WHERE id = ?", (new, old))
            conn.execute("UPDATE albums SET main_id = NULL WHERE id = ?", (new,))

    def _by_title(self, album_id: int) -> dict[str, int]:
        """The album's song ids by ``_title_key``, the first one by track for repeated titles."""
        out: dict[str, int] = {}
        for s in self.list_songs(album_id=album_id, order="track"):
            out.setdefault(_title_key(s.song.title), s.song.id)
        return out

    def _main_of(self, song_id: int) -> int:
        song = self.get_song(song_id)
        return song.main_id or song.id

    def _spread_marks(self, song_id: int) -> int:
        """Give the songs merged with ``song_id`` its favorite and listen later marks and its
        rating; returns the main version's id."""
        song = self.get_song(song_id)
        main = song.main_id or song.id
        with self.connect() as conn:
            conn.execute(
                """UPDATE songs SET favorite = :favorite, listen_later = :later, rating = :rating
                    WHERE (id = :main OR main_id = :main) AND id != :id""",
                {
                    "favorite": int(song.favorite),
                    "later": int(song.listen_later),
                    "rating": song.rating,
                    "main": main,
                    "id": song_id,
                },
            )
        return main

    def _spread_all_marks(self) -> None:
        """Give every merged song the marks and rating of its main version."""
        with self.connect() as conn:
            conn.execute(
                """UPDATE songs SET (favorite, listen_later, rating) =
                       (SELECT m.favorite, m.listen_later, m.rating FROM songs m
                         WHERE m.id = songs.main_id)
                    WHERE main_id IS NOT NULL"""
            )

    def _next_main(self, main_id: int, leaving: str = "0", *params: object) -> int | None:
        """The song merged into ``main_id`` that would be the main one after it: the first
        album by album, leaving out the songs ``s`` matching the SQL ``leaving`` (with
        ``params``)."""
        with self.connect() as conn:
            row = conn.execute(
                f"""SELECT s.id FROM songs s LEFT JOIN albums al ON al.id = s.album_id
                     WHERE s.main_id = ? AND NOT ({leaving})
                     ORDER BY al.id IS NULL, al.year IS NULL, al.year, al.id,
                              s.track_number, s.id LIMIT 1""",
                (main_id, *params),
            ).fetchone()
        return row[0] if row else None

    def _swap_main(self, old: int, new: int) -> None:
        """Make ``new``, merged into ``old``, the main version instead of it."""
        with self.transaction(), self.connect() as conn:
            conn.execute("UPDATE songs SET main_id = ? WHERE main_id = ?", (new, old))
            conn.execute("UPDATE songs SET main_id = ? WHERE id = ?", (new, old))
            conn.execute("UPDATE songs SET main_id = NULL WHERE id = ?", (new,))
            self._carry_placements(old, new)
            self._carry_playlist_places(old, new)
            self._sync_marks()

    def _hand_over_mains(self, where: str, value: int) -> None:
        """Before the songs matching the SQL ``where`` (on ``songs``) are deleted: the main
        versions among them hand over to one of their merged songs that stays."""
        leaving = f"s.id IN (SELECT id FROM songs WHERE {where})"
        with self.connect() as conn:
            mains = [
                row[0]
                for row in conn.execute(
                    f"""SELECT id FROM songs WHERE {where} AND main_id IS NULL
                          AND EXISTS (SELECT 1 FROM songs m WHERE m.main_id = songs.id)""",
                    (value,),
                )
            ]
        for main in mains:
            heir = self._next_main(main, leaving, value)
            if heir is not None:
                self._swap_main(main, heir)

    def _carry_playlist_places(self, old: int, new: int) -> None:
        """``new`` takes the place of ``old`` in the built-in playlists."""
        with self.connect() as conn:
            conn.execute(
                """UPDATE OR IGNORE playlist_songs SET song_id = ?
                    WHERE song_id = ? AND playlist_id IN
                          (SELECT id FROM playlists WHERE kind IS NOT NULL)""",
                (new, old),
            )

    def _carry_placements(self, old: int, new: int) -> None:
        """In the tier lists where ``new`` now stands for ``old`` (they rank both, and it is
        not placed there yet), it takes the level and place of ``old``."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT t.*, ts.position AS x_at FROM tier_songs ts "
                "JOIN tiers t ON t.id = ts.tier_id WHERE ts.song_id = ?",
                (old,),
            ).fetchall()
            for row in rows:
                own, _extra = _split(row, "x_")
                kind, owner_id = Tier(**own).owner
                if new not in {s.song.id for s in self._owner_songs(kind, owner_id)}:
                    continue
                placed = conn.execute(
                    f"""SELECT 1 FROM tier_songs ts JOIN tiers t ON t.id = ts.tier_id
                         WHERE ts.song_id = ? AND t.{kind}_id = ?""",
                    (new, owner_id),
                ).fetchone()
                if placed is None:
                    conn.execute(
                        "UPDATE tier_songs SET song_id = ? WHERE tier_id = ? AND song_id = ?",
                        (new, row["id"], old),
                    )
                    # A lock goes along.
                    conn.execute(
                        f"""UPDATE OR IGNORE tier_locks SET song_id = ? WHERE song_id = ?
                              AND tier_list_id IN (SELECT id FROM tier_lists
                                                    WHERE {kind}_id = ?)""",
                        (new, old, owner_id),
                    )

    # Tier lists

    def tier_songs(self, kind: str, owner_id: int) -> list[SongSummary]:
        """The songs a tier list of ``kind`` (one of ``TIER_OWNERS``) ranks, in the owner's
        order: a playlist's, an album's in track order, or an artist's own songs album by album
        (oldest first, then the songs on no album). Merged songs are ranked once: as their
        main version, or, when the owner does not have it, the first of them it has.
        ``NotFound`` when the owner does not exist."""
        songs = self._owner_songs(kind, owner_id)
        present = {s.song.id for s in songs}
        out, seen = [], set()
        for s in songs:
            main = s.song.main_id or s.song.id
            if main in seen or (main != s.song.id and main in present):
                continue
            seen.add(main)
            out.append(s)
        return out

    def _owner_songs(self, kind: str, owner_id: int) -> list[SongSummary]:
        """All the songs of a playlist, album or artist, in its order (see ``tier_songs``)."""
        if kind == "playlist":
            return self.get_playlist_detail(owner_id).songs
        if kind == "album":
            self.get_album(owner_id)
            return self.list_songs(album_id=owner_id, order="track")
        if kind == "artist":
            self.get_artist(owner_id)
            return self.list_songs(artist_id=owner_id, order="album")
        raise NotFound(f"no tier lists for {kind!r}")

    def tier_owner_name(self, kind: str, owner_id: int) -> str:
        """The name of what the tier list ranks: the playlist, album or artist."""
        if kind == "playlist":
            return self.get_playlist(owner_id).name
        if kind == "album":
            return self.get_album(owner_id).title
        if kind == "artist":
            return self.get_artist(owner_id).name
        raise NotFound(f"no tier lists for {kind!r}")

    def get_tier_list(self, kind: str, owner_id: int) -> TierList:
        """The tier list of a playlist, album or artist; without one, its songs are all
        ``unassigned``."""
        songs = self.tier_songs(kind, owner_id)
        by_id = {s.song.id: s for s in songs}
        rows = {tier.id: TierRow(tier) for tier in self._tiers(kind, owner_id)}
        with self.connect() as conn:
            placed = conn.execute(
                f"""SELECT ts.tier_id, ts.song_id FROM tier_songs ts
                      JOIN tiers t ON t.id = ts.tier_id
                     WHERE t.{kind}_id = ? ORDER BY ts.tier_id, ts.position""",
                (owner_id,),
            ).fetchall()
        for tier_id, song_id in placed:
            song = by_id.pop(song_id, None)
            if song is not None:
                rows[tier_id].songs.append(song)
        locking, locked = self._locks(kind, owner_id)
        return TierList(
            kind,
            owner_id,
            self.tier_owner_name(kind, owner_id),
            list(rows.values()),
            list(by_id.values()),
            locking,
            locked & {s.song.id for s in songs},
        )

    def has_tier_list(self, kind: str, owner_id: int) -> bool:
        return bool(self._tiers(kind, owner_id))

    def create_tier_list(self, kind: str, owner_id: int) -> TierList:
        """Give a playlist, album or artist a tier list with the ``TIER_NAMES`` levels, every
        song unassigned; one that has a tier list keeps it as it is."""
        self.tier_owner_name(kind, owner_id)
        if not self.has_tier_list(kind, owner_id):
            with self.connect() as conn:
                conn.executemany(
                    f"INSERT INTO tiers ({kind}_id, position, name) VALUES (?, ?, ?)",
                    [(owner_id, position, name) for position, name in enumerate(TIER_NAMES)],
                )
                # A new tier list starts in locking mode, with nothing locked.
                conn.execute(f"DELETE FROM tier_lists WHERE {kind}_id = ?", (owner_id,))
                conn.execute(f"INSERT INTO tier_lists ({kind}_id) VALUES (?)", (owner_id,))
        return self.get_tier_list(kind, owner_id)

    def delete_tier_list(self, kind: str, owner_id: int) -> None:
        """Take the tier list away; the songs stay as they are."""
        self.tier_owner_name(kind, owner_id)
        with self.connect() as conn:
            conn.execute(f"DELETE FROM tiers WHERE {kind}_id = ?", (owner_id,))
            conn.execute(f"DELETE FROM tier_lists WHERE {kind}_id = ?", (owner_id,))

    def get_tier(self, tier_id: int) -> Tier:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM tiers WHERE id = ?", (tier_id,)).fetchone()
        if row is None:
            raise NotFound(f"tier {tier_id} not found")
        return Tier(**dict(row))

    def add_tier(self, kind: str, owner_id: int, name: str | None = None) -> Tier:
        """Add a level at the bottom, named ``name`` or else after the first letter of
        ``TIER_LETTERS`` no level has."""
        tiers = self._tier_list_of(kind, owner_id)
        if len(tiers) >= MAX_TIERS:
            raise ValueError(f"a tier list has at most {MAX_TIERS} levels")
        taken = {t.name.casefold() for t in tiers}
        if name is None:
            name = next((c for c in TIER_LETTERS if c.casefold() not in taken), str(len(tiers) + 1))
        name = self._tier_name(name, tiers)
        with self.connect() as conn:
            cur = conn.execute(
                f"INSERT INTO tiers ({kind}_id, position, name) VALUES (?, ?, ?)",
                (owner_id, max((t.position for t in tiers), default=-1) + 1, name),
            )
        return self.get_tier(cur.lastrowid)

    def rename_tier(self, tier_id: int, name: str) -> Tier:
        tier = self.get_tier(tier_id)
        others = [t for t in self._tiers(*tier.owner) if t.id != tier_id]
        name = self._tier_name(name, others)
        with self.connect() as conn:
            conn.execute("UPDATE tiers SET name = ? WHERE id = ?", (name, tier_id))
        return self.get_tier(tier_id)

    def delete_tier(self, tier_id: int) -> None:
        """Delete a level; its songs go back to the unassigned ones."""
        tier = self.get_tier(tier_id)
        if len(self._tiers(*tier.owner)) <= MIN_TIERS:
            raise ValueError(f"a tier list has at least {MIN_TIERS} levels")
        self._delete("tiers", tier_id)

    def place_song(
        self,
        kind: str,
        owner_id: int,
        song_id: int,
        tier_id: int | None,
        position: int | None = None,
    ) -> TierList:
        """``place_songs`` for one song."""
        return self.place_songs(kind, owner_id, [song_id], tier_id, position)

    def place_songs(
        self,
        kind: str,
        owner_id: int,
        song_ids: list[int],
        tier_id: int | None,
        position: int | None = None,
    ) -> TierList:
        """Put songs of the tier list in the level ``tier_id``, in the order given (taking them
        out of the levels they were in), before the song at ``position`` of what the level holds
        besides them, or last; ``tier_id`` ``None`` takes them out of the levels, so they are
        unassigned again."""
        tiers = self._tier_list_of(kind, owner_id)
        song_ids = list(dict.fromkeys(song_ids))
        if not song_ids:
            raise ValueError("pick a song")
        known = {s.song.id for s in self.tier_songs(kind, owner_id)}
        for song_id in song_ids:
            if song_id not in known:
                raise NotFound(f"song {song_id} is not in the tier list of {kind} {owner_id}")
        if tier_id is not None and tier_id not in {t.id for t in tiers}:
            raise NotFound(f"tier {tier_id} is not in the tier list of {kind} {owner_id}")
        locking, locked = self._locks(kind, owner_id)
        if locking and (stuck := [i for i in song_ids if i in locked]):
            title = self.get_song(stuck[0]).title
            raise ValueError(f"{title} is locked: unlock it first, or turn locking mode off")
        self._prune_tiers(kind, owner_id)
        with self.connect() as conn:
            conn.execute(
                f"""DELETE FROM tier_songs WHERE song_id IN (SELECT value FROM json_each(?))
                       AND tier_id IN (SELECT id FROM tiers WHERE {kind}_id = ?)""",
                (json.dumps(song_ids), owner_id),
            )
            if tier_id is not None:
                current = [
                    row[0]
                    for row in conn.execute(
                        "SELECT song_id FROM tier_songs WHERE tier_id = ? ORDER BY position",
                        (tier_id,),
                    )
                ]
                at = len(current) if position is None else max(0, min(position, len(current)))
                self._write_tier(conn, tier_id, current[:at] + song_ids + current[at:])
        return self.get_tier_list(kind, owner_id)

    def auto_tier_levels(self, kind: str, owner_id: int) -> list[list[SongSummary]]:
        """What ``auto_tier`` puts in the first three levels: the favorite songs, then the songs
        on a favorite album, then the other songs played at least once; each level's songs by
        play count, most first (the owner's order breaks ties). In locking mode, the locked
        songs are left out: they stay where they are."""
        locking, locked = self._locks(kind, owner_id)
        songs = [
            s for s in self.tier_songs(kind, owner_id) if not (locking and s.song.id in locked)
        ]
        with self.connect() as conn:
            favorite_albums = {
                row[0] for row in conn.execute("SELECT id FROM albums WHERE favorite = 1")
            }
        levels: list[list[SongSummary]] = [[], [], []]
        for s in songs:
            if s.song.favorite:
                levels[0].append(s)
            elif s.song.album_id in favorite_albums:
                levels[1].append(s)
            elif s.play_count > 0:
                levels[2].append(s)
        return [sorted(level, key=lambda s: -s.play_count) for level in levels]

    def auto_tier(self, kind: str, owner_id: int) -> int:
        """Fill the tier list again from scratch as ``auto_tier_levels`` says, adding levels
        when it has fewer than three; the other levels are emptied, and the songs in none of
        the three left unassigned. In locking mode, the locked songs stay where they are, first
        in their level. Returns how many songs were placed."""
        tiers = self._tier_list_of(kind, owner_id)
        levels = self.auto_tier_levels(kind, owner_id)
        while len(tiers) < len(levels):
            tiers.append(self.add_tier(kind, owner_id))
        tier_list = self.get_tier_list(kind, owner_id)
        kept = {
            row.tier.id: [s.song.id for s in row.songs if s.song.id in tier_list.locked]
            for row in tier_list.tiers
        } if tier_list.locking else {}  # fmt: skip
        with self.connect() as conn:
            for i, tier in enumerate(tiers):
                ids = [s.song.id for s in levels[i]] if i < len(levels) else []
                self._write_tier(conn, tier.id, kept.get(tier.id, []) + ids)
        return sum(len(level) for level in levels)

    def set_locking(self, kind: str, owner_id: int, on: bool) -> TierList:
        """Turn the tier list's locking mode on or off; its locks are kept either way."""
        self._tier_list_of(kind, owner_id)
        list_id = self._tier_list_id(kind, owner_id)
        with self.connect() as conn:
            conn.execute("UPDATE tier_lists SET locking = ? WHERE id = ?", (int(on), list_id))
        return self.get_tier_list(kind, owner_id)

    def set_locked(self, kind: str, owner_id: int, song_ids: list[int], locked: bool) -> TierList:
        """Lock songs of the tier list where they are (or unlock them); only in locking
        mode."""
        self._tier_list_of(kind, owner_id)
        locking, _locked = self._locks(kind, owner_id)
        if not locking:
            raise ValueError("turn locking mode on to lock or unlock songs")
        song_ids = list(dict.fromkeys(song_ids))
        if not song_ids:
            raise ValueError("pick a song")
        known = {s.song.id for s in self.tier_songs(kind, owner_id)}
        for song_id in song_ids:
            if song_id not in known:
                raise NotFound(f"song {song_id} is not in the tier list of {kind} {owner_id}")
        list_id = self._tier_list_id(kind, owner_id)
        with self.connect() as conn:
            if locked:
                conn.executemany(
                    "INSERT OR IGNORE INTO tier_locks (tier_list_id, song_id) VALUES (?, ?)",
                    [(list_id, song_id) for song_id in song_ids],
                )
            else:
                conn.execute(
                    """DELETE FROM tier_locks WHERE tier_list_id = ?
                          AND song_id IN (SELECT value FROM json_each(?))""",
                    (list_id, json.dumps(song_ids)),
                )
        return self.get_tier_list(kind, owner_id)

    def _locks(self, kind: str, owner_id: int) -> tuple[bool, set[int]]:
        """Whether the tier list is in locking mode (it is, unless turned off), and the ids of
        its locked songs."""
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT id, locking FROM tier_lists WHERE {kind}_id = ?", (owner_id,)
            ).fetchone()
            if row is None:
                return True, set()
            locked = {
                r[0]
                for r in conn.execute(
                    "SELECT song_id FROM tier_locks WHERE tier_list_id = ?", (row["id"],)
                )
            }
        return bool(row["locking"]), locked

    def _tier_list_id(self, kind: str, owner_id: int) -> int:
        """The id of the tier list's settings, made (in locking mode) when it has none yet: tier
        lists from before locking mode came without."""
        with self.connect() as conn:
            conn.execute(
                f"""INSERT INTO tier_lists ({kind}_id) SELECT ?
                     WHERE NOT EXISTS (SELECT 1 FROM tier_lists WHERE {kind}_id = ?)""",
                (owner_id, owner_id),
            )
            return conn.execute(
                f"SELECT id FROM tier_lists WHERE {kind}_id = ?", (owner_id,)
            ).fetchone()[0]

    def _tiers(self, kind: str, owner_id: int) -> list[Tier]:
        if kind not in TIER_OWNERS:
            raise NotFound(f"no tier lists for {kind!r}")
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM tiers WHERE {kind}_id = ? ORDER BY position, id", (owner_id,)
            ).fetchall()
        return [Tier(**dict(row)) for row in rows]

    def _tier_list_of(self, kind: str, owner_id: int) -> list[Tier]:
        """The levels of a tier list; ``NotFound`` when its owner, or the tier list, does not
        exist."""
        self.tier_owner_name(kind, owner_id)
        tiers = self._tiers(kind, owner_id)
        if not tiers:
            raise NotFound(f"{kind} {owner_id} has no tier list")
        return tiers

    @staticmethod
    def _write_tier(conn: sqlite3.Connection, tier_id: int, song_ids: list[int]) -> None:
        conn.execute("DELETE FROM tier_songs WHERE tier_id = ?", (tier_id,))
        conn.executemany(
            "INSERT INTO tier_songs (tier_id, song_id, position) VALUES (?, ?, ?)",
            [(tier_id, song_id, position) for position, song_id in enumerate(song_ids)],
        )

    def _prune_tiers(self, kind: str, owner_id: int) -> None:
        """Take the songs the owner no longer has (taken out of the playlist, moved to another
        album or artist) out of its tier list, locks included."""
        keep = [s.song.id for s in self.tier_songs(kind, owner_id)]
        with self.connect() as conn:
            conn.execute(
                f"""DELETE FROM tier_songs
                     WHERE tier_id IN (SELECT id FROM tiers WHERE {kind}_id = ?)
                       AND song_id NOT IN (SELECT value FROM json_each(?))""",
                (owner_id, json.dumps(keep)),
            )
            conn.execute(
                f"""DELETE FROM tier_locks
                     WHERE tier_list_id IN (SELECT id FROM tier_lists WHERE {kind}_id = ?)
                       AND song_id NOT IN (SELECT value FROM json_each(?))""",
                (owner_id, json.dumps(keep)),
            )

    @staticmethod
    def _tier_name(name: str, others: list[Tier]) -> str:
        name = " ".join(name.split())
        if not name:
            raise ValueError("a level needs a name")
        if len(name) > TIER_NAME_LENGTH:
            raise ValueError(f"a level's name has at most {TIER_NAME_LENGTH} characters")
        if name.casefold() in {t.name.casefold() for t in others}:
            raise ValueError(f"there is already a level {name}")
        return name

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
            self._sync_marks(self._spread_marks(record_id))
        elif kind == "album":
            self._spread_album_marks(record_id)
        return value

    def set_rating(self, kind: str, record_id: int, rating: int | None) -> int | None:
        """Rate an artist, album or song from 1 to 5 stars; ``None`` takes the rating away."""
        table = FAVORITE_TABLES[kind]
        validate_rating(rating)
        with self.connect() as conn:
            cur = conn.execute(f"UPDATE {table} SET rating = ? WHERE id = ?", (rating, record_id))
        if cur.rowcount == 0:
            raise NotFound(f"{kind} {record_id} not found")
        if kind == "song":
            self._spread_marks(record_id)
        elif kind == "album":
            self._spread_album_marks(record_id)
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
