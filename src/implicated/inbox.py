"""The inbox (``info/``): files waiting to be imported, and the archives of past imports.

Accepting an import (see ``importer.accept``) moves every file of the inbox into
``info/archive/<when>/``, keeping their folders, so the inbox is empty again and the same files
are never imported twice, while nothing is lost until you delete the archive. Files can also
arrive by upload (``add_upload``): a ``.zip`` of an inbox (an Export, or a zipped ``info/``) is
unpacked into it.
"""

from __future__ import annotations

import io
import shutil
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

ARCHIVE = "archive"
# Archive names are when the import was accepted, without the colons Windows refuses.
STAMP = "%Y-%m-%dT%H%M%S"
# Files a single upload may be, and where each lands in the inbox; anything else comes in a zip
# that keeps its folders (an ``albums.yaml`` needs its ``bands/<artist>/``).
SINGLE_FILES = {
    "bands.yaml": "bands.yaml",
    "concerts.csv": "concerts.csv",
    "tours.csv": "tours.csv",
}
# What a zip may unpack to at most, so a broken (or hostile) one cannot fill the disk.
MAX_UNPACKED = 2 * 1024**3


@dataclass
class Archive:
    name: str
    files: int
    size: int  # bytes


def list_files(info_dir: str | Path) -> list[Path]:
    """The files waiting in the inbox (its archives aside), relative to it, in order."""
    info_dir = Path(info_dir)
    if not info_dir.is_dir():
        return []
    return sorted(
        path.relative_to(info_dir)
        for path in info_dir.rglob("*")
        if path.is_file() and path.relative_to(info_dir).parts[0] != ARCHIVE
    )


def archive(
    info_dir: str | Path, now: datetime | None = None, label: str | None = None
) -> Path | None:
    """Move every file of the inbox into a new archive (named after ``now``, and ``label``
    when given) and return it; ``None`` when the inbox had no files."""
    info_dir = Path(info_dir)
    files = list_files(info_dir)
    if not files:
        return None
    name = (now or datetime.now()).strftime(STAMP) + (f"-{label}" if label else "")
    target = info_dir / ARCHIVE / name
    n = 1
    while target.exists():
        n += 1
        target = info_dir / ARCHIVE / f"{name}-{n}"
    for path in files:
        (target / path).parent.mkdir(parents=True, exist_ok=True)
        (info_dir / path).rename(target / path)
    # The folders left empty go too.
    for path in sorted(info_dir.rglob("*"), reverse=True):
        if path.is_dir() and path.relative_to(info_dir).parts[0] != ARCHIVE:
            if not any(path.iterdir()):
                path.rmdir()
    return target


def list_archives(info_dir: str | Path) -> list[Archive]:
    """The archives of past imports, newest first."""
    root = Path(info_dir) / ARCHIVE
    if not root.is_dir():
        return []
    archives = []
    for folder in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
        files = [p for p in folder.rglob("*") if p.is_file()]
        archives.append(Archive(folder.name, len(files), sum(p.stat().st_size for p in files)))
    return archives


def _archive_folder(info_dir: str | Path, name: str) -> Path:
    root = (Path(info_dir) / ARCHIVE).resolve()
    folder = (root / name).resolve()
    if folder.parent != root or not folder.is_dir():
        raise ValueError(f"there is no archive called {name!r}")
    return folder


def delete_archive(info_dir: str | Path, name: str) -> None:
    """Delete one archive for good. Only folders right inside ``info/archive/`` can go."""
    shutil.rmtree(_archive_folder(info_dir, name))


def delete_all_archives(info_dir: str | Path) -> int:
    """Delete every archive for good; how many there were."""
    archives = list_archives(info_dir)
    for item in archives:
        delete_archive(info_dir, item.name)
    return len(archives)


def _safe_parts(name: str) -> tuple[str, ...] | None:
    """The folders and file name of a zip entry, or ``None`` for one that would land outside
    the inbox (absolute, ``..``, a drive) or is a folder."""
    if name.endswith("/") or "\\" in name:
        return None
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ("", ".", "..") or ":" in part for part in path.parts):
        return None
    return path.parts


def add_upload(info_dir: str | Path, file_name: str, data: bytes) -> list[Path]:
    """Put an uploaded file into the inbox and return what it added (relative to the inbox).

    A ``.zip`` is unpacked, keeping its folders; when everything in it sits in one folder (say
    a zipped ``info/``), that folder is left out. A single file can be ``bands.yaml``,
    ``concerts.csv``, ``tours.csv`` or an Export ``.json``. Files already in the inbox at the same
    place are replaced. Raises ``ValueError`` for anything else."""
    info_dir = Path(info_dir)
    name = Path(file_name).name
    lower = name.casefold()
    if lower.endswith(".zip"):
        return _unzip(info_dir, data)
    if lower.endswith(".json"):
        from implicated.importer import USERDATA_FILE
        from implicated.userdata import read_file

        read_file(data)  # an Export file, or a ValueError saying why not
        return [_write(info_dir, Path(USERDATA_FILE), data)]
    if lower in SINGLE_FILES:
        return [_write(info_dir, Path(SINGLE_FILES[lower]), data)]
    if lower.endswith((".yaml", ".yml")):
        raise ValueError(
            f"{name}: only bands.yaml can come on its own; zip an albums.yaml with its "
            "bands/<artist>/ folders"
        )
    raise ValueError(f"{name}: upload a .zip, bands.yaml, concerts.csv, tours.csv or a .json")


def _write(info_dir: Path, relative: Path, data: bytes) -> Path:
    target = info_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return relative


def _unzip(info_dir: Path, data: bytes) -> list[Path]:
    try:
        archive_file = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("not a zip file") from None
    with archive_file:
        entries = []
        for info in archive_file.infolist():
            if info.is_dir():
                continue
            parts = _safe_parts(info.filename)
            if parts is None:
                raise ValueError(f"the zip has a file outside its folder: {info.filename!r}")
            if parts[0] == ARCHIVE:
                continue  # a zipped info/ with its archives: those were imported already
            entries.append((info, parts))
        if not entries:
            raise ValueError("the zip has no files")
        if sum(info.file_size for info, _ in entries) > MAX_UNPACKED:
            raise ValueError("the zip is too big to unpack")
        # Everything in one folder (a zipped info/, say): leave that folder out.
        tops = {parts[0] for _, parts in entries}
        if len(tops) == 1 and all(len(parts) > 1 for _, parts in entries):
            entries = [(info, parts[1:]) for info, parts in entries if parts[1] != ARCHIVE]
        added = []
        for info, parts in entries:
            added.append(_write(info_dir, Path(*parts), archive_file.read(info)))
    return sorted(added)
