"""Import artists, albums, songs, covers and lyrics from YAML and text files.

``python -m implicated.importer [info_dir] [--db path]``

The directory holds a ``bands.yaml`` listing the artists, and one folder per artist (named
exactly like the artist) with an ``albums.yaml``, a picture of the artist, the album covers
and the song lyrics::

    info/
      bands.yaml
      bands/<artist name>/albums.yaml
      bands/<artist name>/band.jpg          # any of ``IMAGE_TYPES``
      bands/<artist name>/covers/<image>.jpg
      bands/<artist name>/songs/<song title>.txt

``bands.yaml``::

    bands:
    - name: My Chemical Romance
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
      release_year: 2006
      kind: album                 # optional, see ``ALBUM_KINDS``
      cover: the_black_parade.jpg # optional; otherwise guessed from the file names
      songs:
      - name: The End.
        duration: 1:52
      - name: Intro (instrumental)
        no_lyrics: true           # optional; the song has no words, so no lyrics are missing

Lyrics files are named after the song (see ``lyrics_file_name``): ``/`` becomes ``_`` and the
characters Windows does not allow in file names (``<>:"\\|?*``) are left out, so "S/C/A/R/E/C/R/O/W"
is ``S_C_A_R_E_C_R_O_W.txt`` and "Question...?" is ``Question....txt``. Case does not matter. An
empty file means the lyrics are still missing; songs without a file get an empty one.

Importing again updates what changed and never touches ratings, notes, favorites, CD and vinyl
ownership, music videos, song videos, listen later marks, playlists or plays.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from implicated.library import IMAGE_TYPES, Library
from implicated.models import (
    AlbumIn,
    ArtistIn,
    MemberIn,
    Song,
    SongIn,
    lyrics_file_name,
    validate_wiki,
)

# "country" and "bio" are the older names of "origin" and "description".
BAND_KEYS = {
    "name", "creation_year", "origin", "country", "description", "bio", "wiki", "languages",
    "genres", "members",
}  # fmt: skip
MEMBER_KEYS = {"name", "roles", "from", "to"}
ALBUM_KEYS = {"release_year", "kind", "cover", "songs"}
SONG_KEYS = {"name", "duration", "no_lyrics"}

# Words that say nothing about which album a cover file belongs to.
COVER_STOPWORDS = {
    "a", "an", "and", "the", "of", "for", "to", "with", "i", "me", "my", "you", "your",
    "album", "cover", "front", "art",
}  # fmt: skip

# Footnote markers copied along with titles from Wikipedia, e.g. "Sing[a]".
FOOTNOTE = re.compile(r"\s*\[[a-z0-9]{1,2}\]$")


@dataclass
class Report:
    created: dict[str, int] = field(default_factory=lambda: {"artists": 0, "albums": 0, "songs": 0})
    updated: dict[str, int] = field(default_factory=lambda: {"artists": 0, "albums": 0, "songs": 0})
    covers: int = 0
    images: int = 0
    lyrics: int = 0
    lyrics_files: int = 0
    warnings: list[str] = field(default_factory=list)

    def count(self, kind: str, created: bool) -> None:
        (self.created if created else self.updated)[kind] += 1

    def totals(self) -> str:
        def counts(d: dict[str, int]) -> str:
            return ", ".join(f"{n} {kind}" for kind, n in d.items())

        return (
            f"created {counts(self.created)}; updated {counts(self.updated)}; "
            f"{self.covers} covers, {self.images} band images, {self.lyrics} lyrics, "
            f"{self.lyrics_files} new lyrics files"
        )

    def summary(self) -> str:
        return "\n".join([self.totals(), *(f"warning: {w}" for w in self.warnings)])


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


def import_dir(lib: Library, info_dir: str | Path) -> Report:
    info_dir = Path(info_dir)
    report = Report()
    raw = _load(info_dir / "bands.yaml")
    with lib.transaction():
        _import_bands(lib, info_dir, raw, report)
    return report


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


def _import_artist(lib: Library, data: dict, report: Report) -> int:
    name = str(data["name"]).strip()
    existing = lib.find_artist(name)
    wiki = str(data["wiki"]).strip() if data.get("wiki") else None
    try:
        validate_wiki(wiki)
    except ValueError:
        report.warnings.append(f"{name}: ignored wiki {wiki!r}, it is not a Wikipedia article")
        wiki = None
    artist_in = ArtistIn(
        name=name,
        country=_text(data.get("origin") or data.get("country")) or (existing and existing.country),
        formed_year=_year(data.get("creation_year"), name) or (existing and existing.formed_year),
        genres=_strings(data.get("genres")) or (existing.genres if existing else []),
        languages=_strings(data.get("languages")) or (existing.languages if existing else []),
        bio=data.get("description") or data.get("bio") or (existing and existing.bio),
        wiki=wiki or (existing and existing.wiki),
        favorite=bool(existing and existing.favorite),
        notes=existing and existing.notes,
        listen_later=bool(existing and existing.listen_later),
        rating=existing and existing.rating,
    )
    if existing:
        artist = lib.update_artist(existing.id, artist_in)
    else:
        artist = lib.create_artist(artist_in)
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
        lib.set_members(artist.id, members)
    return artist.id


def _import_image(lib: Library, artist_id: int, band_dir: Path, report: Report) -> None:
    images = sorted(
        p for p in band_dir.glob("band.*") if p.is_file() and p.suffix.lower() in IMAGE_TYPES
    )
    if len(images) > 1:
        names = ", ".join(p.name for p in images)
        report.warnings.append(f"{band_dir.name}: several band images ({names}), using the first")
    if images:
        lib.set_image(artist_id, images[0])
        report.images += 1


def _import_albums(
    lib: Library, artist_id: int, albums: dict, band_dir: Path, report: Report
) -> None:
    artist = lib.get_artist(artist_id)
    covers_dir = band_dir / "covers"
    covers = sorted(
        p for p in covers_dir.glob("*") if p.is_file() and p.suffix.lower() in IMAGE_TYPES
    )
    used_covers: set[Path] = set()
    lyrics = {
        p.name.casefold(): p
        for p in sorted((band_dir / "songs").glob("*"))
        if p.is_file() and p.suffix.lower() == ".txt"
    }
    used_lyrics: set[Path] = set()
    for raw_title, raw in albums.items():
        title = _title(raw_title)
        where = f"{artist.name} / {title}"
        data = _mapping(raw, where, ALBUM_KEYS, report)

        existing = lib.find_album(artist_id, title)
        album_in = AlbumIn(
            artist_id=artist_id,
            title=title,
            year=_year(data.get("release_year"), where) or (existing and existing.year),
            kind=data.get("kind") or (existing.kind if existing else "album"),
            rating=existing and existing.rating,
            notes=existing and existing.notes,
            favorite=bool(existing and existing.favorite),
            cd=existing and existing.cd,
            vinyl=existing and existing.vinyl,
            listen_later=bool(existing and existing.listen_later),
        )
        if existing:
            album = lib.update_album(existing.id, album_in)
        else:
            album = lib.create_album(album_in)
        report.count("albums", existing is None)

        if data.get("cover"):
            cover = covers_dir / str(data["cover"])
            if not cover.is_file():
                report.warnings.append(f"{where}: cover {cover.name!r} not found")
                cover = None
        else:
            cover = match_cover(title, covers)
            if cover is None and covers:
                report.warnings.append(f"{where}: no cover file matches the title")
        if cover is not None:
            lib.set_cover(album.id, cover)
            used_covers.add(cover)
            report.covers += 1

        for number, song in enumerate(data.get("songs") or [], start=1):
            imported = _import_song(lib, artist_id, album.id, number, song, where, report)
            if imported is not None:
                name = lyrics_file_name(imported.title)
                path = lyrics.get(name.casefold())
                if path is None:
                    # The library may have just made it, when its ``info_dir`` is this folder.
                    path = lyrics[name.casefold()] = band_dir / "songs" / name
                    report.lyrics_files += 1
                if not path.exists():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("", encoding="utf-8")
                _import_lyrics(lib, imported.id, path, report)
                used_lyrics.add(path)

    for path in covers:
        if path not in used_covers:
            report.warnings.append(f"{artist.name}: cover {path.name!r} matched no album")
    for path in lyrics.values():
        if path not in used_lyrics:
            report.warnings.append(f"{artist.name}: lyrics {path.name!r} matched no song")


def _import_lyrics(lib: Library, song_id: int, path: Path, report: Report) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        report.warnings.append(f"{path.parent.parent.name}: lyrics {path.name!r} is not UTF-8")
        return
    if lib.set_lyrics(song_id, text).lyrics:
        report.lyrics += 1


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
    try:
        seconds = parse_duration(data.get("duration"))
    except ValueError as exc:
        report.warnings.append(f"{where} / {title}: {exc}")
        seconds = None
    if seconds is None:
        report.warnings.append(f"{where} / {title}: no duration")

    existing = lib.find_song(artist_id, album_id, title)
    # Without the key, a choice made in the app stays.
    no_lyrics = bool(existing and existing.no_lyrics)
    if "no_lyrics" in data:
        if isinstance(data["no_lyrics"], bool):
            no_lyrics = data["no_lyrics"]
        else:
            report.warnings.append(f"{where} / {title}: no_lyrics must be true or false")
    song_in = SongIn(
        artist_id=artist_id,
        title=title,
        album_id=album_id,
        track_number=number,
        duration_seconds=seconds or (existing and existing.duration_seconds),
        rating=existing and existing.rating,
        notes=existing and existing.notes,
        favorite=bool(existing and existing.favorite),
        no_lyrics=no_lyrics,
        mv=existing and existing.mv,
        listen_later=bool(existing and existing.listen_later),
    )
    if existing:
        song = lib.update_song(existing.id, song_in)
    else:
        song = lib.create_song(song_in)
    report.count("songs", existing is None)
    return song


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m implicated.importer")
    parser.add_argument("info_dir", nargs="?", default="info")
    parser.add_argument("--db", default=os.environ.get("IMPLICATED_DB", "local/implicated.db"))
    args = parser.parse_args(argv)
    try:
        report = import_dir(Library(args.db), args.info_dir)
    except (OSError, ValueError) as exc:
        sys.exit(f"import failed: {exc}")
    print(f"Imported {args.info_dir} into {args.db}")
    print(report.summary())


if __name__ == "__main__":
    main()
