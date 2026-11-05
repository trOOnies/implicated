"""Take the library with you as a zip file, two ways:

``python -m implicated.backup backup [file]``, ``... restore <file>`` and ``... export [file]``,
all with ``--db path``.

* **Backup**: an exact copy of the library folder (``local/``): a consistent snapshot of the
  database, the covers, artist pictures, lyrics, tabs, concerts and friends' files, plus a
  ``manifest.json``. ``restore`` puts it back as it was, after backing up what is there now.
  Use it to move to another computer or to undo a mistake.
* **Export**: the library written out as an **inbox** (see ``implicated.importer``):
  ``bands.yaml`` and every ``albums.yaml`` with a ``uid`` on each record, the covers, pictures,
  lyrics and tabs, ``concerts.csv``, ``tours.csv`` and your data as ``userdata.json``. Unzipped
  into ``info/`` (or uploaded on the Import page) and imported, it rebuilds the library, also in
  a fresh one, and its files can be read and edited by hand. Songs without an album have no
  place in ``albums.yaml``, so it leaves them out, with a warning.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from implicated import __version__
from implicated.concerts import CONCERTS_FILE, TOURS_FILE
from implicated.importer import USERDATA_FILE
from implicated.inbox import _safe_parts
from implicated.library import Library
from implicated.models import lyrics_file_name
from implicated.userdata import export_data

BACKUP_FORMAT = "implicated-backup"
EXPORT_FORMAT = "implicated-export"
VERSION = 1
MANIFEST = "manifest.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _counts(lib: Library) -> dict[str, int]:
    stats = lib.stats()
    return {"artists": stats.artists, "albums": stats.albums, "songs": stats.songs}


def file_name(kind: str, when: datetime | None = None) -> str:
    """``implicated-backup-2026-11-04.zip`` or ``implicated-export-2026-11-04.zip``."""
    return f"implicated-{kind}-{(when or datetime.now()).date().isoformat()}.zip"


# Backup and restore


def _is_database(path: Path, db: Path) -> bool:
    """The database or one of SQLite's files beside it, which the snapshot replaces."""
    return path.name in {db.name, f"{db.name}-journal", f"{db.name}-wal", f"{db.name}-shm"}


def write_backup(lib: Library, target: str | Path) -> Path:
    """Zip the library folder into ``target``, with a snapshot of the database taken while it
    may be in use."""
    target = Path(target)
    root = lib.path.parent
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / lib.path.name
        source = sqlite3.connect(lib.path)
        copy = sqlite3.connect(snapshot)
        try:
            source.backup(copy)
        finally:
            copy.close()
            source.close()
        manifest = {
            "format": BACKUP_FORMAT,
            "version": VERSION,
            "app_version": __version__,
            "created_at": _now(),
            "database": lib.path.name,
            **_counts(lib),
        }
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(MANIFEST, json.dumps(manifest, indent=2))
            z.write(snapshot, lib.path.name)
            for path in sorted(root.rglob("*")):
                if path.is_file() and not _is_database(path, lib.path) and path != target:
                    z.write(path, path.relative_to(root).as_posix())
    return target


def read_manifest(source: str | Path, kind: str = BACKUP_FORMAT) -> dict:
    """The manifest of a backup (or export) zip; ``ValueError`` when it is not one."""
    try:
        with zipfile.ZipFile(source) as z:
            manifest = json.loads(z.read(MANIFEST))
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError(f"{Path(source).name} is not an implicated {kind[11:]}") from None
    if manifest.get("format") != kind:
        raise ValueError(f"{Path(source).name} is not an implicated {kind[11:]}")
    if not isinstance(manifest.get("version"), int) or manifest["version"] > VERSION:
        raise ValueError(f"{Path(source).name} comes from a newer version of implicated")
    return manifest


def restore(source: str | Path, db: str | Path) -> Path | None:
    """Replace the library folder of ``db`` with the backup at ``source``. What was there is
    first backed up beside the folder (``<folder>-before-restore-<when>.zip``), which is
    returned (``None`` when there was no library yet). Stop the app first."""
    source, db = Path(source), Path(db)
    manifest = read_manifest(source)
    root = db.parent
    if root.resolve() in source.resolve().parents:
        raise ValueError("move the backup out of the library folder first: it is emptied")
    saved = None
    if db.is_file():
        stamp = datetime.now().strftime("%Y-%m-%dT%H%M%S")
        saved = root.parent / f"{root.name}-before-restore-{stamp}.zip"
        write_backup(Library(db), saved)
    with zipfile.ZipFile(source) as z:
        entries = []
        for info in z.infolist():
            if info.is_dir() or info.filename == MANIFEST:
                continue
            parts = _safe_parts(info.filename)
            if parts is None:
                raise ValueError(f"the backup has a file outside its folder: {info.filename!r}")
            entries.append((info, parts))
        if root.exists():
            for path in root.iterdir():
                shutil.rmtree(path) if path.is_dir() else path.unlink()
        root.mkdir(parents=True, exist_ok=True)
        for info, parts in entries:
            name = db.name if parts == (manifest.get("database"),) else None
            target = root / (name or Path(*parts))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(info))
    return saved


# Export


@dataclass
class ExportReport:
    artists: int = 0
    albums: int = 0
    songs: int = 0
    files: int = 0
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return "\n".join(
            [
                f"exported {self.artists} artists, {self.albums} albums, {self.songs} songs "
                f"and {self.files} files",
                *(f"warning: {w}" for w in self.warnings),
            ]
        )


def _duration(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


def _compact(record: dict) -> dict:
    return {k: v for k, v in record.items() if v not in (None, False, [], "")}


def _dump(data: object) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100)


def write_export(lib: Library, target: str | Path, owner: str | None = None) -> ExportReport:
    """Write the library into ``target`` as an inbox zip, as the module docstring says."""
    report = ExportReport()
    with lib.connect() as conn:
        artists = conn.execute("SELECT * FROM artists ORDER BY name COLLATE NOCASE, id").fetchall()
        albums = conn.execute(
            "SELECT * FROM albums ORDER BY artist_id, year, title COLLATE NOCASE, id"
        ).fetchall()
        songs = conn.execute(
            "SELECT * FROM songs ORDER BY album_id, track_number, title COLLATE NOCASE, id"
        ).fetchall()
        features: dict[int, list[str]] = {}
        for row in conn.execute(
            """SELECT f.song_id, a.name FROM song_features f JOIN artists a ON a.id = f.artist_id
                ORDER BY f.song_id, f.position"""
        ):
            features.setdefault(row["song_id"], []).append(row["name"])
    albums_of: dict[int, list] = {}
    for album in albums:
        albums_of.setdefault(album["artist_id"], []).append(album)
    songs_of: dict[int, list] = {}
    for song in songs:
        if song["album_id"] is None:
            report.warnings.append(
                f"{lib.get_artist(song['artist_id']).name} / {song['title']}: left out, it has "
                "no album"
            )
            continue
        songs_of.setdefault(song["album_id"], []).append(song)

    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        added: set[str] = set()

        def add(name: str, path: Path | None = None, text: str | None = None) -> None:
            # Songs of the same title (a deluxe edition's) share their lyrics file.
            if name in added:
                return
            added.add(name)
            if path is not None:
                z.write(path, name)
            else:
                z.writestr(name, text or "")
            report.files += 1

        bands = []
        for artist in artists:
            name = artist["name"]
            members = [
                _compact(
                    {
                        "name": m.name,
                        "roles": m.roles,
                        "from": m.from_year,
                        "to": m.to_year,
                    }
                )
                for m in lib.list_members(artist["id"])
            ]
            bands.append(
                _compact(
                    {
                        "name": name,
                        "uid": artist["uid"],
                        "origin": artist["country"],
                        "creation_year": artist["formed_year"],
                        "languages": json.loads(artist["languages"]),
                        "genres": json.loads(artist["genres"]),
                        "description": artist["bio"],
                        "wiki": artist["wiki"],
                    }
                )
                | ({"members": members} if members else {})
            )
            report.artists += 1
            if "/" in name or name in (".", ".."):
                report.warnings.append(f"{name}: its albums and files are left out, the name "
                                       "cannot be a folder")  # fmt: skip
                continue
            folder = f"bands/{name}"
            if artist["image"] and (lib.images_dir / artist["image"]).is_file():
                add(
                    f"{folder}/band{Path(artist['image']).suffix}", lib.images_dir / artist["image"]
                )
            mapping: dict[str, dict] = {}
            for album in albums_of.get(artist["id"], []):
                title = album["title"]
                if title.casefold() in {t.casefold() for t in mapping}:
                    report.warnings.append(f"{name} / {title}: left out, another album has the "
                                           "same title")  # fmt: skip
                    continue
                cover = None
                if album["cover"] and (lib.covers_dir / album["cover"]).is_file():
                    stem = lyrics_file_name(title).removesuffix(".txt")
                    cover = f"{stem}{Path(album['cover']).suffix}"
                    add(f"{folder}/covers/{cover}", lib.covers_dir / album["cover"])
                entries = []
                for song in songs_of.get(album["id"], []):
                    entries.append(
                        _compact(
                            {
                                "name": song["title"],
                                "uid": song["uid"],
                                "duration": _duration(song["duration_seconds"]),
                                "no_lyrics": bool(song["no_lyrics"]),
                            }
                        )
                        | ({"featuring": features[song["id"]]} if song["id"] in features else {})
                    )
                    _add_song_files(lib, song, folder, add)
                    report.songs += 1
                mapping[title] = _compact(
                    {
                        "uid": album["uid"],
                        "release_year": album["year"],
                        "kind": album["kind"],
                        "unreleased": bool(album["unreleased"]),
                    }
                ) | {"cover": cover, "songs": entries}
                report.albums += 1
            add(f"{folder}/albums.yaml", text=_dump(mapping) if mapping else "{}\n")
        add("bands.yaml", text=_dump({"bands": bands}))
        if lib.media_dir is not None:
            for csv_name in (CONCERTS_FILE, TOURS_FILE):
                if (lib.media_dir / csv_name).is_file():
                    add(csv_name, lib.media_dir / csv_name)
        add(USERDATA_FILE, text=json.dumps(export_data(lib, owner), indent=2, ensure_ascii=False))
        manifest = {
            "format": EXPORT_FORMAT,
            "version": VERSION,
            "app_version": __version__,
            "created_at": _now(),
            **_counts(lib),
        }
        z.writestr(MANIFEST, json.dumps(manifest, indent=2))
    return report


def _add_song_files(lib: Library, song, folder: str, add) -> None:
    """The song's lyrics (from its file, or the stored words without one), timed lyrics and
    tab."""
    stem = lyrics_file_name(song["title"]).removesuffix(".txt")
    lyrics = lib.lyrics_path(song["id"])
    if lyrics is not None and lyrics.is_file():
        add(f"{folder}/songs/{stem}.txt", lyrics)
    elif song["lyrics"]:
        add(f"{folder}/songs/{stem}.txt", text=f"{song['lyrics']}\n")
    if (lrc := lib.lrc_path(song["id"])) is not None:
        add(f"{folder}/songs/{stem}.lrc", lrc)
    if (tab := lib.tab_path(song["id"])) is not None:
        add(f"{folder}/tabs/{stem}{tab.suffix.lower()}", tab)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m implicated.backup")
    parser.add_argument("action", choices=("backup", "restore", "export"))
    parser.add_argument("file", nargs="?")
    parser.add_argument("--db", default=os.environ.get("IMPLICATED_DB", "local/implicated.db"))
    parser.add_argument("--owner", help="export: whose data it is")
    args = parser.parse_args(argv)
    db = Path(args.db)
    try:
        if args.action == "restore":
            if not args.file:
                sys.exit("restore needs the backup file: make restore FILE=...")
            saved = restore(args.file, db)
            print(f"Restored {args.file} into {db.parent}")
            if saved:
                print(f"What was there before is in {saved}")
            return
        lib = Library(db, db.parent)
        target = Path(args.file or file_name(args.action))
        if args.action == "backup":
            write_backup(lib, target)
            print(f"Backed up {db.parent} to {target}")
        else:
            print(write_export(lib, target, args.owner).summary())
            print(f"Exported {db} to {target}; unzip it into info/ and import it to rebuild")
    except (OSError, ValueError) as exc:
        sys.exit(f"{args.action} failed: {exc}")


if __name__ == "__main__":
    main()
