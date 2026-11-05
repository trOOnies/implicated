"""Move an older ``info/`` folder's hand-edited files into the library, once:
``python -m implicated.migrate [info_dir] [--db path] [--yes]``.

Before, ``info/`` was imported on every start and also held the lyrics, timed lyrics, tabs and
concerts the app read and wrote, so it was half inbox, half library. Now the library keeps
those next to its database, and ``info/`` is only an inbox. This:

1. shows what the YAML files would still change if they were imported (they are **not**: the
   database already has everything from the last start, plus what you edited since);
2. moves ``bands/<artist>/songs/`` and ``tabs/`` into the library's ``bands/<artist>/``. A file
   the library already has with other content is kept, and the old one stays in the archive
   with a warning (an empty one in the library is replaced);
3. merges ``concerts.csv`` and ``tours.csv`` into the library's (moved, when it has none);
4. archives whatever is left (``bands.yaml``, ``albums.yaml``, covers, pictures) in
   ``info/archive/<when>-migration/``, so the inbox starts out empty.
"""

from __future__ import annotations

import argparse
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from implicated import inbox
from implicated.concerts import CONCERTS_FILE, TOURS_FILE, merge_concerts, merge_tours
from implicated.importer import import_dir
from implicated.library import Library


@dataclass
class MigrationReport:
    moved: int = 0
    kept: int = 0
    concerts: int = 0
    tours: int = 0
    archive: Path | None = None
    moved_csvs: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"moved {self.moved} lyrics and tab files into the library ({self.kept} already "
            f"there); merged {self.concerts} concerts and {self.tours} tours",
        ]
        if self.moved_csvs:
            lines.append(f"moved {' and '.join(self.moved_csvs)} into the library")
        if self.archive:
            lines.append(f"archived the rest in {self.archive}")
        return "\n".join([*lines, *(f"warning: {w}" for w in self.warnings)])


def migrate(lib: Library, info_dir: str | Path) -> MigrationReport:
    """Do steps 2 to 4 of the module docstring."""
    if lib.media_dir is None:
        raise ValueError("the library needs a folder of its own to move the files into")
    info_dir = Path(info_dir)
    report = MigrationReport()
    for folder in sorted((info_dir / "bands").glob("*/*")):
        if not folder.is_dir() or folder.name not in ("songs", "tabs"):
            continue
        target_dir = lib.media_dir / "bands" / folder.parent.name / folder.name
        for source in sorted(p for p in folder.iterdir() if p.is_file()):
            target = target_dir / source.name
            existing = next(
                (p for p in target_dir.glob("*") if p.name.casefold() == source.name.casefold()),
                None,
            ) if target_dir.is_dir() else None  # fmt: skip
            if existing is not None and existing.read_bytes() == source.read_bytes():
                source.unlink()
                report.kept += 1
            elif existing is not None and existing.stat().st_size:
                report.warnings.append(
                    f"{existing} differs from {source.name} in the inbox; kept the library's, "
                    "the other one is in the archive"
                )
            else:
                if existing is not None:
                    existing.unlink()  # an empty one the library made meanwhile
                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(source, target)
                report.moved += 1
    for name in (CONCERTS_FILE, TOURS_FILE):
        source, target = info_dir / name, lib.media_dir / name
        if not source.is_file():
            continue
        if not target.exists():
            shutil.move(source, target)
            report.moved_csvs.append(name)
            continue
        if name == CONCERTS_FILE:
            report.concerts, warnings = merge_concerts(target, source)
            report.warnings += [f"{name}: {w}" for w in warnings]
        else:
            report.tours = merge_tours(target, source)
    report.archive = inbox.archive(info_dir, label="migration")
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m implicated.migrate")
    parser.add_argument("info_dir", nargs="?", default=os.environ.get("IMPLICATED_INFO", "info"))
    parser.add_argument("--db", default=os.environ.get("IMPLICATED_DB", "local/implicated.db"))
    parser.add_argument("--yes", action="store_true", help="migrate without asking first")
    args = parser.parse_args(argv)
    db = Path(args.db)
    lib = Library(db, db.parent)
    if (Path(args.info_dir) / "bands.yaml").is_file():
        preview = import_dir(lib, args.info_dir, dry_run=True)
        if preview.changes:
            print("The YAML files differ from the library here (the library is kept):")
            print("\n".join(preview.summary().splitlines()[1:]))
        else:
            print("The YAML files hold nothing the library lacks.")
    if not args.yes and input(
        f"Move the lyrics, tabs and concerts of {args.info_dir} into {db.parent} and archive "
        "the rest? [y/N] "
    ).strip().lower() not in ("y", "yes"):
        return
    print(migrate(lib, args.info_dir).summary())


if __name__ == "__main__":
    main()
