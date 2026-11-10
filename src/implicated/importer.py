"""Import artists, albums, songs, covers, lyrics and tabs from the inbox: YAML, text and CSV files.

``python -m implicated.importer [info_dir] [--db path] [--preview | --yes]``

The library (the database, and the lyrics, tabs and concerts next to it) is the one source of
truth: small edits happen in the app. The inbox (``info/``) is where bigger batches are prepared
by hand and then imported **on purpose**, from the app's Import page or this command, never on
startup. A preview (``dry_run``) shows what would change first; accepting it imports everything
together, then moves the inbox's files to ``info/archive/<when>/`` (see ``implicated.inbox``),
so the inbox is empty again and the same files are never imported twice.

The inbox may hold any of::

    info/
      bands.yaml
      bands/<artist name>/albums.yaml
      bands/<artist name>/band.jpg          # any of ``IMAGE_TYPES``
      bands/<artist name>/covers/<image>.jpg  # any of ``COVER_TYPES`` (no GIFs)
      bands/<artist name>/songs/<song title>.txt
      bands/<artist name>/songs/<song title>.lrc  # optional; timed lyrics
      bands/<artist name>/tabs/<song title>.gp5   # optional; any of ``TAB_TYPES``
      concerts.csv, tours.csv               # merged into the library's (see ``concerts``)
      userdata.json                         # an Export file (see ``userdata``), applied last

``bands.yaml``::

    bands:
    - name: My Chemical Romance
      uid: 0b6f...                # optional; set by Export, matches the artist even renamed
      origin: American            # where the artist comes from
      creation_year: 2001
      languages: English          # a single value or a list
      genres: [Alternative Rock, Emo]
      description: My Chemical Romance is an American rock band from New Jersey.
      wiki: https://en.wikipedia.org/wiki/My_Chemical_Romance  # only Wikipedia articles
      members:
      - name: Frank Iero
        roles: Rhythm guitar      # a single value or a list
        from: 2002                # optional; "to" too

``albums.yaml`` maps album titles to their data::

    The Black Parade:
      uid: 5d1c...                # optional, like the artist's
      release_year: 2006
      kind: album                 # optional, see ``ALBUM_KINDS``
      unreleased: true            # optional; scrapped or leaked, release_year is when it was made
      cover: the_black_parade.jpg # optional; otherwise guessed from the file names (null: none)
      songs:
      - name: The End.
        uid: 9a2e...              # optional, like the artist's
        duration: 1:52
      - name: Intro (instrumental)
        no_lyrics: true           # optional; the song has no words, so no lyrics are missing
      - name: Teenagers
        explicit: true            # optional; the song has explicit content
      - name: Everything Has Changed
        featuring: Ed Sheeran     # optional; a single name or a list, of artists in bands.yaml

Records are matched by ``uid`` first, then by name (ignoring case and kinds of dash), and a
record nobody has yet is created. **A key in the file wins; a key left out keeps what the
library has**, so a file only needs what it means to change. Ratings, notes, favorites, CD and
vinyl ownership, music videos, song videos, listen later marks, playlists and plays are never
touched (``userdata.json`` is the way to bring those).

Lyrics files are named after the song (see ``lyrics_file_name``): ``/`` becomes ``_`` and the
characters Windows does not allow in file names (``<>:"\\|?*``) are left out, so "S/C/A/R/E/C/R/O/W"
is ``S_C_A_R_E_C_R_O_W.txt`` and "Question...?" is ``Question....txt``. Case does not matter.
They are copied into the library's own ``bands/<artist>/songs/``, replacing the file there when
they have words that differ; an empty one changes nothing.

Timed lyrics are ``.lrc`` files named like the lyrics file (see ``implicated.lrc``), which Live Play
follows line by line. An empty lyrics file takes its words from the ``.lrc`` file, so the song's
lyrics are not missing. They are copied along like the lyrics files.

Tabs are Guitar Pro files named like the lyrics file (``Sing.gp5``), shown and played on the song
page. TuxGuitar saves them with File > Save As, choosing Guitar Pro 5 (``.gp5``); its own ``.tg``
files cannot be read. They are copied into the library's ``bands/<artist>/tabs/``.

A song's ``featuring`` replaces the artists it features (``featuring: []`` takes them all away);
without the key, the ones picked in the app stay. Names that are not an artist of ``bands.yaml``
(or already in the library) are left out with a warning.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

import yaml

from implicated import inbox
from implicated.concerts import CONCERTS_FILE, TOURS_FILE, merge_concerts, merge_tours
from implicated.library import COVER_TYPES, IMAGE_TYPES, TAB_TYPES, Library
from implicated.lrc import plain_lyrics, read_lrc
from implicated.models import (
    AlbumIn,
    ArtistIn,
    MemberIn,
    Song,
    SongIn,
    lyrics_file_name,
    validate_formed_year,
    validate_wiki,
)

# "country" and "bio" are the older names of "origin" and "description".
BAND_KEYS = {
    "name", "uid", "creation_year", "origin", "country", "description", "bio", "wiki",
    "languages", "genres", "members",
}  # fmt: skip
MEMBER_KEYS = {"name", "roles", "from", "to"}
ALBUM_KEYS = {"uid", "release_year", "kind", "unreleased", "cover", "songs"}
SONG_KEYS = {"name", "uid", "duration", "no_lyrics", "explicit", "featuring"}
USERDATA_FILE = "userdata.json"

# The catalog fields the files describe, compared to say what an import changes. Your own data
# (ratings, notes, marks, ownership) is never among them.
ARTIST_FIELDS = ("name", "country", "formed_year", "genres", "languages", "bio", "wiki")
ALBUM_FIELDS = ("title", "year", "kind", "unreleased")
SONG_FIELDS = ("title", "track_number", "duration_seconds", "no_lyrics", "explicit")

# Words that say nothing about which album a cover file belongs to.
COVER_STOPWORDS = {
    "a", "an", "and", "the", "of", "for", "to", "with", "i", "me", "my", "you", "your",
    "album", "cover", "front", "art",
}  # fmt: skip

# Footnote markers copied along with titles from Wikipedia, e.g. "Sing[a]".
FOOTNOTE = re.compile(r"\s*\[[a-z0-9]{1,2}\]$")


@dataclass
class Change:
    """One thing an import changes: a record it creates (``field`` is empty) or one field of a
    record it updates, from ``old`` to ``new``."""

    kind: str  # "artist", "album" or "song"
    name: str  # "Artist", "Artist / Album" or "Artist / Album / Song"
    field: str = ""
    old: object = None
    new: object = None


@dataclass
class Report:
    created: dict[str, int] = field(default_factory=lambda: {"artists": 0, "albums": 0, "songs": 0})
    updated: dict[str, int] = field(default_factory=lambda: {"artists": 0, "albums": 0, "songs": 0})
    covers: int = 0
    images: int = 0
    lyrics: int = 0
    # Lyrics, timed lyrics and tab files copied into the library's folder.
    lyrics_files: int = 0
    tabs: int = 0
    concerts: int = 0
    tours: int = 0
    # What ``userdata.json`` did, when the inbox had one (an ``implicated.userdata.ImportReport``).
    userdata: object = None
    changes: list[Change] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Songs whose ``featuring`` names artists, linked once every band is in: (song id, names,
    # where the song is in the files).
    featuring: list[tuple[int, list[str], str]] = field(default_factory=list, repr=False)
    # Files to copy into the library once the database work is in: (source, target).
    copies: list[tuple[Path, Path]] = field(default_factory=list, repr=False)

    def count(self, kind: str, created: bool) -> None:
        (self.created if created else self.updated)[kind] += 1

    @property
    def updates(self) -> list[Change]:
        return [c for c in self.changes if c.field]

    @property
    def new(self) -> list[Change]:
        return [c for c in self.changes if not c.field]

    def totals(self) -> str:
        def counts(d: dict[str, int]) -> str:
            return ", ".join(f"{n} {kind}" for kind, n in d.items())

        text = (
            f"created {counts(self.created)}; updated {counts(self.updated)}; "
            f"{self.covers} covers, {self.images} band images, {self.lyrics} lyrics, "
            f"{self.lyrics_files} lyrics files, {self.tabs} tabs, {self.concerts} concerts, "
            f"{self.tours} tours"
        )
        if self.userdata is not None:
            text += f"; your data: {self.userdata.summary().splitlines()[0]}"
        return text

    def summary(self) -> str:
        lines = [self.totals()]
        lines += [f"new {c.kind}: {c.name}" for c in self.new]
        lines += [f"{c.name}: {c.field} {_show(c.old)} -> {_show(c.new)}" for c in self.updates]
        return "\n".join([*lines, *(f"warning: {w}" for w in self.warnings)])


def _show(value: object) -> str:
    if value is None or value == []:
        return "(none)"
    if isinstance(value, list):
        return ", ".join(map(str, value))
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "..."


def _diff(report: Report, kind: str, name: str, old: object, new: object, names) -> None:
    for attr in names:
        before, after = getattr(old, attr), getattr(new, attr)
        if before != after:
            report.changes.append(Change(kind, name, attr, before, after))


# Parsing


def _mapping(value: object, where: str, allowed: set[str], report: Report) -> dict:
    """Lower-case the keys (``duratioN`` is ``duration``) and warn about unknown ones."""
    if not isinstance(value, dict):
        raise ValueError(f"{where}: expected a mapping, got {type(value).__name__}")
    data = {str(k).lower(): v for k, v in value.items()}
    for key in sorted(data.keys() - allowed):
        report.warnings.append(f"{where}: ignored unknown key {key!r}")
    return data


def _strings(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()]


def _year(value: object, where: str) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{where}: {value!r} is not a year") from None


def _text(value: object) -> str | None:
    if value is None:
        return None
    return str(value).strip() or None


def _title(value: object) -> str:
    # Titles such as 22 or 1989 come back from YAML as numbers.
    return FOOTNOTE.sub("", str(value).strip())


def parse_duration(value: object) -> int | None:
    """Seconds from ``m:ss`` or ``h:mm:ss``.

    YAML 1.1 reads unquoted ``3:22`` as the base-60 integer 202, which already is the number of
    seconds, but keeps ``0:57`` as a string; both forms land here.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    seconds = 0
    for part in str(value).strip().split(":"):
        if not part.isdigit():
            raise ValueError(f"{value!r} is not a duration like 3:45")
        seconds = seconds * 60 + int(part)
    return seconds


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in COVER_STOPWORDS}


def match_cover(title: str, files: list[Path]) -> Path | None:
    """Pick the image whose file name best matches the album title.

    A file matches when at least half of its meaningful words appear in the title
    (``bullets.jpg`` for "I Brought You My Bullets, You Brought Me Your Love"); ties go to the
    file that also covers more of the title.
    """
    title_words = _words(title)
    best, best_score = None, (0.0, 0.0)
    for path in files:
        words = _words(path.stem)
        if not words:
            continue
        common = len(title_words & words)
        score = (common / len(words), common / len(title_words | words))
        if score > best_score:
            best, best_score = path, score
    return best if best_score[0] >= 0.5 else None


# Importing


def _load(path: Path) -> object:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"{path} is not valid YAML: {exc}") from None


def import_dir(lib: Library, info_dir: str | Path, dry_run: bool = False) -> Report:
    """Import the inbox at ``info_dir`` into the library, all together or (on an error)
    nothing. With ``dry_run``, only say what it would do: the library and its files stay as
    they are. The inbox itself is left as it is either way (see ``accept``)."""
    info_dir = Path(info_dir)
    report = Report()
    bands_file = info_dir / "bands.yaml"
    raw = _load(bands_file) if bands_file.is_file() else None
    userdata_file = info_dir / USERDATA_FILE
    with lib.dry_run() if dry_run else lib.transaction():
        if raw is not None:
            _import_bands(lib, info_dir, raw, report)
            _import_featuring(lib, report)
        if userdata_file.is_file():
            from implicated.userdata import import_data, read_file

            report.userdata = import_data(lib, read_file(userdata_file))
    if not dry_run:
        for source, target in report.copies:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    _import_csvs(lib, info_dir, report, dry_run)
    return report


def accept(lib: Library, info_dir: str | Path) -> tuple[Report, Path | None]:
    """Import the inbox for real, then move its files to an archive (``None`` when it was
    empty). On an error nothing is imported and the inbox stays."""
    report = import_dir(lib, info_dir)
    return report, inbox.archive(info_dir)


def _import_csvs(lib: Library, info_dir: Path, report: Report, dry_run: bool) -> None:
    for name in (CONCERTS_FILE, TOURS_FILE):
        source = info_dir / name
        if not source.is_file():
            continue
        if lib.media_dir is None:
            report.warnings.append(f"{name}: skipped, the library has no folder for it")
            continue
        target = lib.media_dir / name
        try:
            if name == CONCERTS_FILE:
                report.concerts, warnings = merge_concerts(target, source, dry=dry_run)
                report.warnings += [f"{name}: {w}" for w in warnings]
            else:
                report.tours = merge_tours(target, source, dry=dry_run)
        except (OSError, ValueError) as exc:
            report.warnings.append(f"{name}: skipped, {exc}")


def _import_featuring(lib: Library, report: Report) -> None:
    """Link songs to the artists they feature, now that every artist of ``bands.yaml`` exists
    (one further down the file may be featured on an earlier one's song)."""
    for song_id, names, where in report.featuring:
        own = lib.get_song(song_id).artist_id
        artist_ids = []
        for name in names:
            artist = lib.find_artist(name)
            if artist is None:
                report.warnings.append(f"{where}: featured artist {name!r} is not in bands.yaml")
            elif artist.id == own:
                report.warnings.append(f"{where}: a song cannot feature its own artist")
            else:
                artist_ids.append(artist.id)
        before = [a.id for a in lib.list_featuring(song_id)]
        if before != artist_ids:
            names_of = {a: lib.get_artist(a).name for a in {*before, *artist_ids}}
            report.changes.append(
                Change(
                    "song",
                    where,
                    "featuring",
                    [names_of[a] for a in before],
                    [names_of[a] for a in artist_ids],
                )
            )
        lib.set_featuring(song_id, artist_ids)


def _import_bands(lib: Library, info_dir: Path, raw: object, report: Report) -> None:
    if not isinstance(raw, dict):
        raise ValueError(f"{info_dir / 'bands.yaml'}: expected a 'bands' list")
    for i, band in enumerate(raw.get("bands") or []):
        data = _mapping(band, f"bands.yaml band #{i + 1}", BAND_KEYS, report)
        name = str(data.get("name") or "").strip()
        if not name:
            report.warnings.append(f"bands.yaml band #{i + 1}: skipped, it has no name")
            continue
        artist_id = _import_artist(lib, data, report)
        band_dir = info_dir / "bands" / name
        _import_image(lib, artist_id, band_dir, report)
        albums_file = band_dir / "albums.yaml"
        if albums_file.is_file():
            albums = _load(albums_file)
            if not isinstance(albums, dict):
                raise ValueError(f"{albums_file}: expected album titles mapped to their data")
            _import_albums(lib, artist_id, albums, band_dir, report)
        else:
            report.warnings.append(f"{name}: no {albums_file.relative_to(info_dir)}")


def _uid(data: dict) -> str | None:
    return _text(data.get("uid"))


def _import_artist(lib: Library, data: dict, report: Report) -> int:
    name = str(data["name"]).strip()
    found = lib.find_uid("artists", _uid(data))
    existing = lib.get_artist(found) if found else lib.find_artist(name)
    values: dict = {"name": name}
    if "wiki" in data:
        wiki = str(data["wiki"]).strip() if data["wiki"] else None
        try:
            validate_wiki(wiki)
            values["wiki"] = wiki
        except ValueError:
            report.warnings.append(f"{name}: ignored wiki {wiki!r}, it is not a Wikipedia article")
    if "creation_year" in data:
        formed_year = _year(data["creation_year"], name)
        try:
            validate_formed_year(formed_year)
            values["formed_year"] = formed_year
        except ValueError:
            report.warnings.append(
                f"{name}: ignored creation_year {formed_year}, it is in the future"
            )
    for key in ("origin", "country"):
        if key in data:
            values["country"] = _text(data[key])
            break
    for key in ("description", "bio"):
        if key in data:
            values["bio"] = _text(data[key])
            break
    for key in ("genres", "languages"):
        if key in data:
            values[key] = _strings(data[key])

    if existing:
        current = ArtistIn(**{f.name: getattr(existing, f.name) for f in fields(ArtistIn)})
        artist_in = replace(current, **values)
        _diff(report, "artist", existing.name, current, artist_in, ARTIST_FIELDS)
        artist = lib.update_artist(existing.id, artist_in)
    else:
        artist = lib.create_artist(ArtistIn(**values), uid=_uid(data))
        report.changes.append(Change("artist", name))
    report.count("artists", existing is None)

    if "members" in data:
        members = []
        for j, member in enumerate(data["members"] or []):
            where = f"{name} member #{j + 1}"
            m = _mapping(member, where, MEMBER_KEYS, report)
            members.append(
                MemberIn(
                    name=str(m.get("name") or ""),
                    roles=_strings(m.get("roles")),
                    from_year=_year(m.get("from"), where),
                    to_year=_year(m.get("to"), where),
                )
            )
        before = [
            MemberIn(m.name, m.roles, m.from_year, m.to_year) for m in lib.list_members(artist.id)
        ]
        after = [
            MemberIn(m.name, m.roles, m.from_year, m.to_year)
            for m in lib.set_members(artist.id, members)
        ]
        if existing and before != after:
            report.changes.append(
                Change("artist", name, "members", [m.name for m in before], [m.name for m in after])
            )
    return artist.id


def _import_image(lib: Library, artist_id: int, band_dir: Path, report: Report) -> None:
    images = sorted(
        p for p in band_dir.glob("band.*") if p.is_file() and p.suffix.lower() in IMAGE_TYPES
    )
    if len(images) > 1:
        names = ", ".join(p.name for p in images)
        report.warnings.append(f"{band_dir.name}: several band images ({names}), using the first")
    if images:
        before = lib.get_artist(artist_id)
        after = lib.set_image(artist_id, images[0])
        if before.image and before.image != after.image:
            report.changes.append(Change("artist", after.name, "picture", None, images[0].name))
        report.images += 1


def _files(folder: Path, suffixes: tuple[str, ...]) -> dict[str, Path]:
    """The files of ``folder`` with one of ``suffixes``, by their lower-case names."""
    return {
        p.name.casefold(): p
        for p in sorted(folder.glob("*"))
        if p.is_file() and p.suffix.lower() in suffixes
    }


def _import_albums(
    lib: Library, artist_id: int, albums: dict, band_dir: Path, report: Report
) -> None:
    artist = lib.get_artist(artist_id)
    covers_dir = band_dir / "covers"
    images = sorted(
        p for p in covers_dir.glob("*") if p.is_file() and p.suffix.lower() in IMAGE_TYPES
    )
    covers = [p for p in images if p.suffix.lower() in COVER_TYPES]
    for path in images:
        if path not in covers:
            report.warnings.append(
                f"{artist.name}: cover {path.name!r} skipped, covers cannot be GIFs"
            )
    used_covers: set[Path] = set()
    inbox_files = {
        "lyrics": _files(band_dir / "songs", (".txt",)),
        "timed": _files(band_dir / "songs", (".lrc",)),
        "tabs": _files(band_dir / "tabs", TAB_TYPES),
    }
    used: set[Path] = set()
    for raw_title, raw in albums.items():
        title = _title(raw_title)
        where = f"{artist.name} / {title}"
        data = _mapping(raw, where, ALBUM_KEYS, report)

        found = lib.find_uid("albums", _uid(data))
        existing = lib.get_album(found) if found else lib.find_album(artist_id, title)
        values: dict = {"artist_id": artist_id, "title": title}
        if "release_year" in data:
            values["year"] = _year(data["release_year"], where)
        if data.get("kind"):
            values["kind"] = data["kind"]
        if "unreleased" in data:
            if isinstance(data["unreleased"], bool):
                values["unreleased"] = data["unreleased"]
            else:
                report.warnings.append(f"{where}: unreleased must be true or false")
        if existing:
            current = AlbumIn(**{f.name: getattr(existing, f.name) for f in fields(AlbumIn)})
            album_in = replace(current, **values)
            _diff(report, "album", f"{artist.name} / {existing.title}", current, album_in,
                  ALBUM_FIELDS)  # fmt: skip
            album = lib.update_album(existing.id, album_in)
        else:
            album = lib.create_album(AlbumIn(**values), uid=_uid(data))
            report.changes.append(Change("album", where))
        report.count("albums", existing is None)

        if data.get("cover"):
            cover = covers_dir / str(data["cover"])
            if not cover.is_file():
                report.warnings.append(f"{where}: cover {cover.name!r} not found")
                cover = None
            elif cover.suffix.lower() not in COVER_TYPES:
                report.warnings.append(
                    f"{where}: cover {cover.name!r} skipped, "
                    f"it is not one of {', '.join(COVER_TYPES)}"
                )
                cover = None
        elif "cover" in data:
            cover = None  # ``cover: null``: no cover file, so none is guessed
        else:
            cover = match_cover(title, covers)
            if cover is None and covers:
                report.warnings.append(f"{where}: no cover file matches the title")
        if cover is not None:
            after = lib.set_cover(album.id, cover)
            if existing and existing.cover != after.cover:
                report.changes.append(Change("album", where, "cover", None, cover.name))
            used_covers.add(cover)
            report.covers += 1

        seen: dict[int, int] = {}
        for number, song in enumerate(data.get("songs") or [], start=1):
            imported = _import_song(lib, artist_id, album.id, number, song, where, report)
            if imported is not None and imported.id in seen:
                report.warnings.append(
                    f"{where} / {imported.title}: listed as tracks {seen[imported.id]} and "
                    f"{number}, which are one song in the library; give them different names"
                )
            if imported is not None:
                seen.setdefault(imported.id, number)
                used.update(
                    _import_song_files(
                        lib, imported, inbox_files, f"{where} / {imported.title}", report
                    )
                )

    for path in covers:
        if path not in used_covers:
            report.warnings.append(f"{artist.name}: cover {path.name!r} matched no album")
    for kind in ("lyrics", "timed"):
        for path in inbox_files[kind].values():
            if path not in used:
                report.warnings.append(f"{artist.name}: lyrics {path.name!r} matched no song")
    for path in inbox_files["tabs"].values():
        if path not in used:
            report.warnings.append(f"{artist.name}: tab {path.name!r} matched no song")


def _read_text(path: Path, report: Report) -> str | None:
    """The text of a lyrics file; ``None`` (and a warning) when it is not UTF-8."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        report.warnings.append(f"{path.parent.parent.name}: lyrics {path.name!r} is not UTF-8")
        return None


def _copy(source: Path, target: Path | None, report: Report) -> bool:
    """Plan copying ``source`` over ``target`` (in the library) when they differ."""
    if target is None:
        return False
    if target.is_file() and target.read_bytes() == source.read_bytes():
        return False
    report.copies.append((source, target))
    return True


def _import_song_files(
    lib: Library, song: Song, files: dict[str, dict[str, Path]], where: str, report: Report
) -> set[Path]:
    """Bring the song's lyrics, timed lyrics and tab from the inbox into the library, and set
    the lyrics it shows. Returns the inbox files used."""
    stem = lyrics_file_name(song.title).removesuffix(".txt")
    source = files["lyrics"].get(f"{stem}.txt".casefold())
    timed = files["timed"].get(f"{stem}.lrc".casefold())
    tab = next(
        (files["tabs"][k] for t in TAB_TYPES if (k := f"{stem}{t}".casefold()) in files["tabs"]),
        None,
    )
    target = lib.lyrics_path(song.id)  # ``None`` when the library keeps no files

    text = _read_text(source, report) if source else None
    if text is not None and text.strip():
        if _copy(source, target, report):
            report.lyrics_files += 1
    elif target is not None and target.is_file():
        text = _read_text(target, report)  # no words in the inbox: keep the library's
    words = ""
    lrc = timed or lib.lrc_path(song.id)
    if lrc is not None:
        try:
            words = plain_lyrics(read_lrc(lrc))
        except ValueError:
            report.warnings.append(f"{lrc.parent.parent.name}: lyrics {lrc.name!r} is not UTF-8")
    if timed is not None and target is not None:
        if _copy(timed, lib.lrc_path(song.id) or target.with_suffix(".lrc"), report):
            report.lyrics_files += 1
    if tab is not None:
        current = lib.tab_path(song.id)
        if current is not None and current.suffix.lower() != tab.suffix.lower():
            current = None  # a new kind of file; the first of ``TAB_TYPES`` found wins
        if _copy(tab, current or lib.new_tab_path(song.id, tab.suffix.lower()), report):
            report.tabs += 1
    if source is not None or timed is not None:
        before = song.lyrics
        after = lib.set_lyrics(song.id, (text or "").strip() or words).lyrics
        if after:
            report.lyrics += 1
        if before != after and before is not None:
            report.changes.append(Change("song", where, "lyrics", before, after))
    return {p for p in (source, timed, tab) if p}


def _import_song(
    lib: Library,
    artist_id: int,
    album_id: int,
    number: int,
    raw: object,
    where: str,
    report: Report,
) -> Song | None:
    data = _mapping(raw, f"{where} track {number}", SONG_KEYS, report)
    title = _title(data.get("name") or "")
    if not title:
        report.warnings.append(f"{where} track {number}: skipped, it has no name")
        return None
    found = lib.find_uid("songs", _uid(data))
    existing = lib.get_song(found) if found else lib.find_song(artist_id, album_id, title)
    values: dict = {
        "artist_id": artist_id,
        "album_id": album_id,
        "title": title,
        "track_number": number,
    }
    if "duration" in data:
        try:
            values["duration_seconds"] = parse_duration(data["duration"])
        except ValueError as exc:
            report.warnings.append(f"{where} / {title}: {exc}")
    if values.get("duration_seconds") is None and not (existing and existing.duration_seconds):
        report.warnings.append(f"{where} / {title}: no duration")
    for flag in ("no_lyrics", "explicit"):
        if flag in data:
            if isinstance(data[flag], bool):
                values[flag] = data[flag]
            else:
                report.warnings.append(f"{where} / {title}: {flag} must be true or false")
    if existing:
        current = SongIn(**{f.name: getattr(existing, f.name) for f in fields(SongIn)})
        song_in = replace(current, **values)
        _diff(report, "song", f"{where} / {existing.title}", current, song_in, SONG_FIELDS)
        song = lib.update_song(existing.id, song_in)
    else:
        song = lib.create_song(SongIn(**values), uid=_uid(data))
        report.changes.append(Change("song", f"{where} / {title}"))
    report.count("songs", existing is None)
    if "featuring" in data:
        report.featuring.append((song.id, _strings(data["featuring"]), f"{where} / {title}"))
    return song


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m implicated.importer")
    parser.add_argument("info_dir", nargs="?", default=os.environ.get("IMPLICATED_INFO", "info"))
    parser.add_argument("--db", default=os.environ.get("IMPLICATED_DB", "local/implicated.db"))
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--preview", action="store_true", help="only show what would change")
    group.add_argument("--yes", action="store_true", help="import without asking first")
    args = parser.parse_args(argv)
    db = Path(args.db)
    lib = Library(db, db.parent)
    if not inbox.list_files(args.info_dir):
        print(f"{args.info_dir} is empty: nothing to import")
        return
    try:
        if not args.yes:
            print(f"Importing {args.info_dir} into {db} would do this:")
            print(import_dir(lib, args.info_dir, dry_run=True).summary())
            if args.preview or input("Import it? [y/N] ").strip().lower() not in ("y", "yes"):
                return
        report, archive = accept(lib, args.info_dir)
    except (OSError, ValueError) as exc:
        sys.exit(f"import failed: {exc}")
    print(f"Imported {args.info_dir} into {db}")
    print(report.summary())
    if archive:
        print(f"The inbox's files are now in {archive}")


if __name__ == "__main__":
    main()
