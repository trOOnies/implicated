"""Export your own data (marks, ratings, notes, ownership, videos, playlists and listening
history) to a JSON file, and import such a file back.

``python -m implicated.userdata export [file] [--owner NAME]`` and
``python -m implicated.userdata import <file>``, both with ``--db path``.

The file refers to artists, albums and songs **by name**, never by database id, so it can be
imported into another library built from the same ``info/`` folder (another computer, a fresh
database), and two people's files can be compared with each other. With that in mind it also
carries the catalog facts that describe each record (genres, languages, origin, year, kind,
duration), so a file makes sense on its own, without the library it came from, and ``owner``
says whose taste it is::

    {
      "format": "implicated-user-data",
      "version": 1,
      "exported_at": "2026-11-03T12:00:00+00:00",
      "owner": "Sol",
      "artists": [{"name": "Lunes Rotos", "origin": "Argentine", "genres": ["Post-Punk"],
                   "favorite": true, "rating": 5}],
      "albums": [{"artist": "Lunes Rotos", "title": "Lunes", "kind": "ep", "year": 2017,
                  "vinyl": "wanted", "listen_later": true}],
      "songs": [{"artist": "Lunes Rotos", "album": "Lunes", "title": "Ruido Blanco",
                 "duration": 217, "favorite": true, "featuring": ["Glass Harbor"],
                 "videos": [{"youtube": "dQw4w9WgXcQ", "title": "Live", "start_seconds": 30}],
                 "plays": ["2026-10-30T21:14:00+00:00"]},
                {"artist": "Lunes Rotos", "album": "Lunes (Deluxe)", "title": "Ruido Blanco",
                 "favorite": true,
                 "same_as": {"artist": "Lunes Rotos", "album": "Lunes", "title": "Ruido Blanco"}}],
      "playlists": [{"name": "Road trip",
                     "songs": [{"artist": "Lunes Rotos", "album": "Lunes", "title": "Lunes"}]}]
    }

Every artist, album and song of the library is in the file, marked or not: having it in the
library says something about your taste too. Keys left at their default (``false``, ``null``,
empty lists) are left out to keep the file small. A song's ``album`` is ``null`` (or missing) for
a single without an album. ``same_as`` names the main version of the songs a song is merged with
(they count as one: see ``Library.merge_songs``), and of an album the main edition of the albums
it is merged with (see ``Library.merge_albums``). The built-in Favorites and Listen Later
playlists are not listed: they follow the songs' marks.

Importing a file treats each record in it as the full picture of that record: its favorite and
listen later marks, rating, notes, CD and vinyl ownership, music video, featured artists and
videos become the file's (a mark missing from the file is taken away), and so does what it is
merged with (merged songs then take their main version's marks and rating). Plays are added to the
history, except those already in it at the same moment, and playlists are created, or have
their songs replaced, by name. Records of the file the library does not have are skipped and
reported, and records of the library the file does not mention stay as they are. Everything is
imported together, or nothing is when the file is broken.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from implicated.library import Library, NotFound, _dashless
from implicated.models import (
    OWNERSHIP,
    SPECIAL_PLAYLISTS,
    PlaylistIn,
    SongVideoIn,
    validate_ownership,
    validate_rating,
    youtube_id,
)

FORMAT = "implicated-user-data"
VERSION = 1
# The kinds of record of the file, in the order they are imported.
KINDS = ("artists", "albums", "songs", "playlists")


def key(*names: str | None) -> tuple[str, ...]:
    """How names are compared, here and between two people's files: ignoring case, outer
    spaces and kinds of dash, like the library looks titles up."""
    return tuple(_dashless(n).casefold() if n else "" for n in names)


def _compact(record: dict) -> dict:
    """``record`` without the values that are only the default."""
    return {k: v for k, v in record.items() if v not in (None, False, [], "")}


# Export


def export_data(lib: Library, owner: str | None = None) -> dict:
    """Your data in the library, as described in the module docstring."""
    with lib.connect() as conn:
        artists = conn.execute("SELECT * FROM artists ORDER BY name COLLATE NOCASE, id").fetchall()
        albums = conn.execute(
            """SELECT al.*, ar.name AS artist FROM albums al JOIN artists ar ON ar.id = al.artist_id
                ORDER BY ar.name COLLATE NOCASE, al.year, al.title COLLATE NOCASE, al.id"""
        ).fetchall()
        songs = conn.execute(
            """SELECT s.*, ar.name AS artist, al.title AS album
                 FROM songs s JOIN artists ar ON ar.id = s.artist_id
                 LEFT JOIN albums al ON al.id = s.album_id
                ORDER BY ar.name COLLATE NOCASE, al.year, al.title COLLATE NOCASE,
                         s.track_number, s.title COLLATE NOCASE, s.id"""
        ).fetchall()
        features: dict[int, list[str]] = {}
        for row in conn.execute(
            """SELECT f.song_id, a.name FROM song_features f JOIN artists a ON a.id = f.artist_id
                ORDER BY f.song_id, f.position"""
        ):
            features.setdefault(row["song_id"], []).append(row["name"])
        videos: dict[int, list[dict]] = {}
        for row in conn.execute("SELECT * FROM song_videos ORDER BY song_id, position, id"):
            videos.setdefault(row["song_id"], []).append(
                _compact(
                    {
                        "youtube": row["youtube"],
                        "title": row["title"],
                        "start_seconds": row["start_seconds"],
                    }
                )
            )
        album_refs = {
            al["id"]: {"artist": al["artist"], "title": al["title"]}
            for al in albums
            if al["id"] in {a["main_id"] for a in albums}
        }
        main_refs = {
            row["id"]: _compact(
                {"artist": row["artist"], "album": row["album"], "title": row["title"]}
            )
            for row in conn.execute(
                """SELECT s.id, ar.name AS artist, al.title AS album, s.title
                     FROM songs s JOIN artists ar ON ar.id = s.artist_id
                     LEFT JOIN albums al ON al.id = s.album_id
                    WHERE s.id IN (SELECT main_id FROM songs)"""
            )
        }
        plays: dict[int, list[str]] = {}
        for row in conn.execute("SELECT song_id, played_at FROM plays ORDER BY played_at, id"):
            plays.setdefault(row["song_id"], []).append(row["played_at"])
        playlists = conn.execute(
            "SELECT * FROM playlists WHERE kind IS NULL ORDER BY name COLLATE NOCASE, id"
        ).fetchall()
        playlist_songs: dict[int, list[dict]] = {}
        for row in conn.execute(
            """SELECT ps.playlist_id, ar.name AS artist, al.title AS album, s.title
                 FROM playlist_songs ps JOIN songs s ON s.id = ps.song_id
                 JOIN artists ar ON ar.id = s.artist_id
                 LEFT JOIN albums al ON al.id = s.album_id
                ORDER BY ps.playlist_id, ps.position"""
        ):
            playlist_songs.setdefault(row["playlist_id"], []).append(
                _compact({"artist": row["artist"], "album": row["album"], "title": row["title"]})
            )

    return {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "owner": owner.strip() if owner and owner.strip() else None,
        "artists": [
            _compact(
                {
                    "name": a["name"],
                    "origin": a["country"],
                    "formed_year": a["formed_year"],
                    "genres": json.loads(a["genres"]),
                    "languages": json.loads(a["languages"]),
                    "favorite": bool(a["favorite"]),
                    "listen_later": bool(a["listen_later"]),
                    "rating": a["rating"],
                    "notes": a["notes"],
                }
            )
            for a in artists
        ],
        "albums": [
            _compact(
                {
                    "artist": al["artist"],
                    "title": al["title"],
                    "year": al["year"],
                    "kind": al["kind"],
                    "unreleased": bool(al["unreleased"]),
                    "favorite": bool(al["favorite"]),
                    "listen_later": bool(al["listen_later"]),
                    "rating": al["rating"],
                    "notes": al["notes"],
                    "cd": al["cd"],
                    "vinyl": al["vinyl"],
                    "same_as": album_refs.get(al["main_id"]),
                }
            )
            for al in albums
        ],
        "songs": [
            _compact(
                {
                    "artist": s["artist"],
                    "album": s["album"],
                    "title": s["title"],
                    "track_number": s["track_number"],
                    "duration": s["duration_seconds"],
                    "no_lyrics": bool(s["no_lyrics"]),
                    "explicit": bool(s["explicit"]),
                    "favorite": bool(s["favorite"]),
                    "listen_later": bool(s["listen_later"]),
                    "rating": s["rating"],
                    "notes": s["notes"],
                    "mv": s["mv"],
                    "featuring": features.get(s["id"], []),
                    "videos": videos.get(s["id"], []),
                    "plays": plays.get(s["id"], []),
                    "same_as": main_refs.get(s["main_id"]),
                }
            )
            for s in songs
        ],
        "playlists": [
            {"name": p["name"], "songs": playlist_songs.get(p["id"], [])} for p in playlists
        ],
    }


def export_file_name(data: dict) -> str:
    """``implicated-<owner>-<date>.json``, the owner left out when there is none."""
    day = str(data.get("exported_at") or "")[:10] or datetime.now(timezone.utc).date().isoformat()
    owner = "".join(c if c.isalnum() else "-" for c in (data.get("owner") or "")).strip("-")
    return f"implicated-{owner + '-' if owner else ''}{day}.json".lower()


# Import


@dataclass
class ImportReport:
    # Records of the file found in the library, and the ones it does not have.
    matched: dict[str, int] = field(default_factory=lambda: dict.fromkeys(KINDS[:3], 0))
    skipped: dict[str, int] = field(default_factory=lambda: dict.fromkeys(KINDS[:3], 0))
    plays_added: int = 0
    playlists_created: int = 0
    playlists_updated: int = 0
    owner: str | None = None
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        def counts(d: dict[str, int]) -> str:
            return ", ".join(f"{n} {kind}" for kind, n in d.items())

        lines = [
            f"updated {counts(self.matched)}; skipped {counts(self.skipped)} not in the library",
            f"{self.plays_added} plays added; {self.playlists_created} playlists created, "
            f"{self.playlists_updated} updated",
        ]
        return "\n".join([*lines, *(f"warning: {w}" for w in self.warnings)])


def read_file(source: str | Path | bytes) -> dict:
    """The data of an export file (its path or contents); ``ValueError`` when it is not one."""
    raw = Path(source).read_bytes() if isinstance(source, (str, Path)) else source
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"not a JSON file: {exc}") from exc
    check(data)
    return data


def check(data: object) -> None:
    """Raise ``ValueError`` unless ``data`` looks like an export this version can read."""
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise ValueError("not an implicated data export (its format must be implicated-user-data)")
    version = data.get("version")
    if not isinstance(version, int) or version < 1:
        raise ValueError("the export has no valid version")
    if version > VERSION:
        raise ValueError(
            f"the export is version {version}, made by a newer implicated (this one reads "
            f"up to {VERSION})"
        )
    for kind in KINDS:
        if not isinstance(data.get(kind, []), list):
            raise ValueError(f"{kind} must be a list")


def import_data(lib: Library, data: dict) -> ImportReport:
    """Import an export into the library, as described in the module docstring."""
    check(data)
    report = ImportReport(owner=data.get("owner"))
    with lib.transaction():
        for i, record in enumerate(data.get("artists", [])):
            _import_artist(lib, _record(record, f"artist #{i + 1}"), report)
        album_merges: dict[int, object] = {}
        for i, record in enumerate(data.get("albums", [])):
            _import_album(lib, _record(record, f"album #{i + 1}"), report, album_merges)
        _import_album_merges(lib, album_merges, report)
        merges: dict[int, object] = {}
        for i, record in enumerate(data.get("songs", [])):
            _import_song(lib, _record(record, f"song #{i + 1}"), report, merges)
        _import_merges(lib, merges, report)
        for i, record in enumerate(data.get("playlists", [])):
            _import_playlist(lib, _record(record, f"playlist #{i + 1}"), report)
        # The marks were written straight to the songs: merged songs take their main version's,
        # and the built-in playlists are put in line.
        lib._spread_all_marks()
        lib._spread_all_album_marks()
        lib._sync_marks()
    return report


def _record(record: object, where: str) -> dict:
    if not isinstance(record, dict):
        raise ValueError(f"{where} must be an object")
    return record


def _text(record: dict, name: str) -> str | None:
    value = record.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text, not {value!r}")
    return value.strip() or None


def _required(record: dict, name: str, where: str) -> str:
    value = _text(record, name)
    if value is None:
        raise ValueError(f"{where} has no {name}")
    return value


def _rating(record: dict, where: str, report: ImportReport) -> int | None:
    rating = record.get("rating")
    try:
        if rating is not None and (isinstance(rating, bool) or not isinstance(rating, int)):
            raise ValueError("rating must be a whole number")
        validate_rating(rating)
    except ValueError as exc:
        report.warnings.append(f"{where}: {exc}; rating left out")
        return None
    return rating


def _marks(record: dict) -> dict:
    return {
        "favorite": int(record.get("favorite") is True),
        "listen_later": int(record.get("listen_later") is True),
    }


def _find_artist(lib: Library, name: str, where: str, report: ImportReport, kind: str):
    artist = lib.find_artist(name)
    if artist is None:
        report.skipped[kind] += 1
        report.warnings.append(f"{where}: no artist {name!r} in the library")
    return artist


def _import_artist(lib: Library, record: dict, report: ImportReport) -> None:
    name = _required(record, "name", "an artist")
    artist = _find_artist(lib, name, name, report, "artists")
    if artist is None:
        return
    with lib.connect() as conn:
        conn.execute(
            """UPDATE artists SET favorite = :favorite, listen_later = :listen_later,
                   rating = :rating, notes = :notes WHERE id = :id""",
            {
                **_marks(record),
                "rating": _rating(record, name, report),
                "notes": _text(record, "notes"),
                "id": artist.id,
            },
        )
    report.matched["artists"] += 1


def _find_album(lib: Library, record: dict, report: ImportReport, kind: str):
    """The album ``record`` names (by ``artist`` and ``title``), or ``None``, reported."""
    artist_name = _required(record, "artist", f"an {kind[:-1]}")
    title = _required(record, "title" if kind == "albums" else "album", f"an {kind[:-1]}")
    where = f"{artist_name} / {title}"
    artist = _find_artist(lib, artist_name, where, report, kind)
    if artist is None:
        return None, where
    album = lib.find_album(artist.id, title)
    if album is None:
        report.skipped[kind] += 1
        report.warnings.append(f"{where}: no such album in the library")
    return album, where


def _import_album(lib: Library, record: dict, report: ImportReport, merges: dict) -> None:
    album, where = _find_album(lib, record, report, "albums")
    if album is None:
        return
    merges[album.id] = (record.get("same_as"), where)
    formats = {}
    for fmt in ("cd", "vinyl"):
        value = _text(record, fmt)
        try:
            validate_ownership(fmt, value)
        except ValueError:
            report.warnings.append(
                f"{where}: {fmt} must be one of {', '.join(OWNERSHIP)}; left out"
            )
            value = None
        formats[fmt] = value
    with lib.connect() as conn:
        conn.execute(
            """UPDATE albums SET favorite = :favorite, listen_later = :listen_later,
                   rating = :rating, notes = :notes, cd = :cd, vinyl = :vinyl WHERE id = :id""",
            {
                **_marks(record),
                **formats,
                "rating": _rating(record, where, report),
                "notes": _text(record, "notes"),
                "id": album.id,
            },
        )
    report.matched["albums"] += 1


def _find_song(lib: Library, record: dict, report: ImportReport | None, kind: str = "songs"):
    """The song ``record`` names (``artist``, ``album``, ``title``), or ``None``. Unless
    ``report`` is ``None``, a song not found is reported as skipped."""
    artist_name = _required(record, "artist", "a song")
    title = _required(record, "title", "a song")
    album_title = _text(record, "album")
    where = " / ".join(n for n in (artist_name, album_title, title) if n)
    artist = lib.find_artist(artist_name)
    album = None
    if artist is not None and album_title is not None:
        album = lib.find_album(artist.id, album_title)
    song = None
    if artist is not None and (album_title is None or album is not None):
        song = lib.find_song(artist.id, album.id if album else None, title)
    if song is None and report is not None:
        report.skipped[kind] += 1
        report.warnings.append(f"{where}: no such song in the library")
    return song, where


def _strings(record: dict, name: str, where: str) -> list[str]:
    values = record.get(name) or []
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        raise ValueError(f"{where}: {name} must be a list of text")
    return values


def _import_song(lib: Library, record: dict, report: ImportReport, merges: dict) -> None:
    song, where = _find_song(lib, record, report)
    if song is None:
        return
    merges[song.id] = (record.get("same_as"), where)
    try:
        mv = youtube_id(_text(record, "mv"), "mv")
    except ValueError as exc:
        report.warnings.append(f"{where}: {exc}; music video left out")
        mv = None
    with lib.connect() as conn:
        conn.execute(
            """UPDATE songs SET favorite = :favorite, listen_later = :listen_later,
                   rating = :rating, notes = :notes, mv = :mv WHERE id = :id""",
            {
                **_marks(record),
                "rating": _rating(record, where, report),
                "notes": _text(record, "notes"),
                "mv": mv,
                "id": song.id,
            },
        )

    featuring = []
    for name in _strings(record, "featuring", where):
        artist = lib.find_artist(name)
        if artist is None or artist.id == song.artist_id:
            report.warnings.append(f"{where}: featured artist {name!r} left out")
        else:
            featuring.append(artist.id)
    lib.set_featuring(song.id, featuring)

    videos = record.get("videos") or []
    if not isinstance(videos, list):
        raise ValueError(f"{where}: videos must be a list")
    with lib.connect() as conn:
        conn.execute("DELETE FROM song_videos WHERE song_id = ?", (song.id,))
    for video in videos:
        try:
            video = _record(video, f"{where}: a video")
            start = video.get("start_seconds")
            lib.add_video(
                song.id,
                SongVideoIn(
                    _required(video, "youtube", "a video"),
                    _text(video, "title"),
                    start if isinstance(start, int) and not isinstance(start, bool) else None,
                ),
            )
        except ValueError as exc:
            report.warnings.append(f"{where}: video left out: {exc}")

    plays = _strings(record, "plays", where)
    with lib.connect() as conn:
        known = {
            played_at
            for (played_at,) in conn.execute(
                "SELECT played_at FROM plays WHERE song_id = ?", (song.id,)
            )
        }
        new = []
        for played_at in dict.fromkeys(plays):
            try:
                datetime.fromisoformat(played_at)
            except ValueError:
                report.warnings.append(f"{where}: play {played_at!r} is not a date and time")
                continue
            if played_at not in known:
                new.append((song.id, played_at))
        conn.executemany("INSERT INTO plays (song_id, played_at) VALUES (?, ?)", new)
    report.plays_added += len(new)
    report.matched["songs"] += 1


def _import_album_merges(lib: Library, merges: dict, report: ImportReport) -> None:
    """Merge the albums of the file as it says (``same_as``, collected in ``merges`` by album
    id), and take out of their merges the ones it does not."""
    mains: dict[int, int | None] = {}
    for album_id, (ref, where) in merges.items():
        mains[album_id] = None
        if ref is None:
            continue
        ref = _record(ref, f"{where}: same_as")
        artist = lib.find_artist(_required(ref, "artist", f"{where}: same_as"))
        title = _required(ref, "title", f"{where}: same_as")
        main = lib.find_album(artist.id, title) if artist else None
        if main is None:
            report.warnings.append(f"{where}: no album {title!r} in the library; not merged")
        elif main.id != album_id:
            mains[album_id] = main.id
    _write_mains(lib, "albums", mains)


def _write_mains(lib: Library, table: str, mains: dict[int, int | None]) -> None:
    """Set the ``main_id`` of records of ``table``, keeping every main record unmerged itself."""
    with lib.connect() as conn:
        conn.executemany(
            f"UPDATE {table} SET main_id = ? WHERE id = ?", [(m, r) for r, m in mains.items()]
        )
        # A main record is never merged into another: point at the end of the chain (and break
        # a loop).
        for _ in range(len(mains) + 1):
            cur = conn.execute(
                f"""UPDATE {table} SET main_id = (SELECT m.main_id FROM {table} m
                                                   WHERE m.id = {table}.main_id)
                     WHERE main_id IN (SELECT id FROM {table} WHERE main_id IS NOT NULL)"""
            )
            if cur.rowcount == 0:
                break
        conn.execute(f"UPDATE {table} SET main_id = NULL WHERE main_id = id")


def _import_merges(lib: Library, merges: dict, report: ImportReport) -> None:
    """Merge the songs of the file as it says (``same_as``, collected in ``merges`` by song
    id), and take out of their merges the ones it does not."""
    mains: dict[int, int | None] = {}
    for song_id, (ref, where) in merges.items():
        mains[song_id] = None
        if ref is None:
            continue
        main, main_where = _find_song(lib, _record(ref, f"{where}: same_as"), None)
        if main is None:
            report.warnings.append(f"{where}: {main_where} is not in the library; not merged")
        elif main.id != song_id:
            mains[song_id] = main.id
    _write_mains(lib, "songs", mains)


def _import_playlist(lib: Library, record: dict, report: ImportReport) -> None:
    name = _required(record, "name", "a playlist")
    if key(name) in {key(n) for n, _field in SPECIAL_PLAYLISTS.values()}:
        report.warnings.append(f"playlist {name!r}: built-in playlists follow the marks; skipped")
        return
    songs = record.get("songs") or []
    if not isinstance(songs, list):
        raise ValueError(f"playlist {name!r}: songs must be a list")
    song_ids = []
    for ref in songs:
        song, where = _find_song(lib, _record(ref, f"playlist {name!r}: a song"), None)
        if song is None:
            report.warnings.append(f"playlist {name!r}: {where} is not in the library; left out")
        elif song.id not in song_ids:
            song_ids.append(song.id)
    existing = next(
        (p.playlist for p in lib.list_playlists() if not p.playlist.kind
         and key(p.playlist.name) == key(name)),
        None,
    )  # fmt: skip
    if existing is None:
        existing = lib.create_playlist(PlaylistIn(name))
        report.playlists_created += 1
    else:
        report.playlists_updated += 1
    lib.set_playlist_songs(existing.id, song_ids)


# Command line


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m implicated.userdata")
    parser.add_argument("--db", default=os.environ.get("IMPLICATED_DB", "local/implicated.db"))
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="write your data to a JSON file")
    export.add_argument("file", nargs="?", help="where to write it (default: a dated name)")
    export.add_argument("--owner", help="whose data it is, say your name")
    load = commands.add_parser("import", help="import a JSON file exported before")
    load.add_argument("file")
    args = parser.parse_args(argv)
    lib = Library(args.db)
    if args.command == "export":
        data = export_data(lib, args.owner)
        path = Path(args.file or export_file_name(data))
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Exported {args.db} to {path}")
        return
    try:
        report = import_data(lib, read_file(args.file))
    except (OSError, ValueError, NotFound) as exc:
        sys.exit(f"import failed: {exc}")
    print(f"Imported {args.file} into {args.db}")
    print(report.summary())


if __name__ == "__main__":
    main()
