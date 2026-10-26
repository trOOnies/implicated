"""Domain models.

Persisted records (``Artist``, ``Member``, ``Album``, ``Song``, ``Play``) carry their database id.
The ``*In`` variants are the writable fields, used as API request bodies and for form input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlsplit

ALBUM_KINDS = ("album", "ep", "single", "live", "compilation")
# Whether you have an album on a format (CD or vinyl); ``None`` means neither.
OWNERSHIP = ("wanted", "owned")

# YouTube video ids are 11 characters long; links to them come in many shapes.
YOUTUBE_ID = re.compile(r"[A-Za-z0-9_-]{11}")
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
YOUTUBE_PATHS = ("/embed/", "/shorts/", "/live/", "/v/")

# Any language edition, desktop or mobile: en.wikipedia.org, pt-br.m.wikipedia.org, ...
WIKI_HOST = re.compile(r"[a-z]{2,3}(?:-[a-z0-9]{1,8})*(?:\.m)?\.wikipedia\.org")
# Pages under /wiki/ that are not articles (English names; "Image" and "WP" are aliases).
WIKI_NAMESPACES = {
    "special", "media", "talk", "user", "user talk", "wikipedia", "wikipedia talk", "wp",
    "file", "file talk", "image", "mediawiki", "mediawiki talk", "template", "template talk",
    "help", "help talk", "category", "category talk", "portal", "portal talk", "draft",
    "draft talk", "module", "module talk", "timedtext", "timedtext talk",
}  # fmt: skip
# Characters a copied article link never has, but that browsers read in surprising ways.
WIKI_BAD_CHARS = re.compile(r'[\x00-\x20\x7f-\x9f"<>\\^`{|}]')

# Characters that cannot be part of a file name on Windows (or, for "/", anywhere).
SLASHES = re.compile(r"[/\\]")
NOT_IN_FILE_NAMES = re.compile(r'[<>:"|?*\x00-\x1f]')


@dataclass
class ArtistIn:
    name: str
    country: str | None = None
    formed_year: int | None = None
    genres: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    bio: str | None = None
    # Link to the artist's Wikipedia article; see ``validate_wiki``.
    wiki: str | None = None
    favorite: bool = False
    # Your own notes, unlike ``bio``, which the importer keeps up to date.
    notes: str | None = None
    listen_later: bool = False


@dataclass
class Artist(ArtistIn):
    id: int = 0
    created_at: str = ""
    # File name inside the library's artist images directory; set with ``Library.set_image``.
    image: str | None = None


@dataclass
class AlbumIn:
    artist_id: int
    title: str
    year: int | None = None
    kind: str = "album"
    rating: int | None = None
    notes: str | None = None
    favorite: bool = False
    # Whether you own or want the album on CD and on vinyl: one of ``OWNERSHIP``, or ``None``.
    cd: str | None = None
    vinyl: str | None = None
    listen_later: bool = False


@dataclass
class Album(AlbumIn):
    id: int = 0
    created_at: str = ""
    # File name inside the library's covers directory; set with ``Library.set_cover``.
    cover: str | None = None


@dataclass
class MemberIn:
    """A (current or former) member of a band. Open-ended years mean "since the start"
    or "still in the band"."""

    name: str
    roles: list[str] = field(default_factory=list)
    from_year: int | None = None
    to_year: int | None = None


@dataclass
class Member(MemberIn):
    id: int = 0
    artist_id: int = 0


@dataclass
class SongIn:
    artist_id: int
    title: str
    album_id: int | None = None
    track_number: int | None = None
    duration_seconds: int | None = None
    rating: int | None = None
    notes: str | None = None
    favorite: bool = False
    # The song has no words (an instrumental), so its lyrics are not missing.
    no_lyrics: bool = False
    # The song's music video on YouTube: a link to it or its id, stored as the id (see
    # ``youtube_id``).
    mv: str | None = None
    # Turned off when the song is played.
    listen_later: bool = False


@dataclass
class Song(SongIn):
    id: int = 0
    created_at: str = ""
    # Song text; set with ``Library.set_lyrics``. ``None`` means it is still missing.
    lyrics: str | None = None


@dataclass
class Play:
    id: int
    song_id: int
    played_at: str


# Read-side views: records joined with the context a page needs.


@dataclass
class ArtistSummary:
    artist: Artist
    album_count: int = 0
    song_count: int = 0
    play_count: int = 0


@dataclass
class AlbumSummary:
    album: Album
    artist_name: str
    song_count: int = 0
    total_seconds: int = 0
    play_count: int = 0
    # Songs that have lyrics, and songs that have none to have (see ``SongIn.no_lyrics``).
    lyrics_count: int = 0
    no_lyrics_count: int = 0


@dataclass
class SongSummary:
    song: Song
    artist_name: str
    album_title: str | None = None
    play_count: int = 0
    last_played: str | None = None


@dataclass
class Point:
    """One data point of a chart."""

    label: str
    value: float


@dataclass
class LibraryStats:
    artists: int
    albums: int
    songs: int
    plays: int
    total_seconds: int
    plays_per_day: list[Point]
    top_artists: list[Point]
    songs_per_artist: list[Point]
    genres: list[Point]
    albums_per_decade: list[Point]
    ratings: list[Point]


def lyrics_file_name(title: str) -> str:
    """The name of the text file holding a song's lyrics, valid on Linux, macOS and Windows:
    ``/`` becomes ``_`` and the characters Windows refuses are left out."""
    return NOT_IN_FILE_NAMES.sub("", SLASHES.sub("_", title)).strip() + ".txt"


def validate_rating(rating: int | None) -> None:
    if rating is not None and not 0 <= rating <= 5:
        raise ValueError("rating must be between 0 and 5")


def validate_kind(kind: str) -> None:
    if kind not in ALBUM_KINDS:
        raise ValueError(f"kind must be one of {', '.join(ALBUM_KINDS)}")


def validate_ownership(name: str, value: str | None) -> None:
    if value is not None and value not in OWNERSHIP:
        raise ValueError(f"{name} must be one of {', '.join(OWNERSHIP)}, or empty")


def youtube_id(value: str | None, name: str = "video") -> str | None:
    """The video id of a YouTube link (``watch?v=``, ``youtu.be/``, ``/embed/``, ``/shorts/``,
    ``/live/``, ``/v/``, also on ``m.``, ``music.`` and ``youtube-nocookie.com``) or of a bare
    11-character id; ``None`` for no value.

    Only the id is kept, so links built from it always point at YouTube.
    """
    if value is None or not value.strip():
        return None
    value = value.strip()
    error = ValueError(f"{name} must be a YouTube video link or its 11-character id")
    if YOUTUBE_ID.fullmatch(value):
        return value
    if len(value) > 2000 or WIKI_BAD_CHARS.search(value):
        raise error
    if "://" not in value:
        value = f"https://{value}"
    parts = urlsplit(value)
    try:
        port = parts.port
    except ValueError:
        raise error from None
    if parts.scheme not in ("http", "https") or parts.username or port is not None:
        raise error
    host = (parts.hostname or "").lower()
    candidate = ""
    if host in ("youtu.be", "www.youtu.be"):
        candidate = parts.path.removeprefix("/")
    elif host in YOUTUBE_HOSTS and parts.path in ("/watch", "/watch/"):
        values = parse_qs(parts.query).get("v", [])
        candidate = values[0] if len(values) == 1 else ""
    elif host in YOUTUBE_HOSTS | {"youtube-nocookie.com", "www.youtube-nocookie.com"}:
        for prefix in YOUTUBE_PATHS:
            if parts.path.startswith(prefix):
                candidate = parts.path.removeprefix(prefix)
    candidate = candidate.removesuffix("/")
    if not YOUTUBE_ID.fullmatch(candidate):
        raise error
    return candidate


def validate_wiki(url: str | None) -> None:
    """Only accept plain links to Wikipedia articles, such as
    ``https://en.wikipedia.org/wiki/My_Chemical_Romance``.

    The link ends up in an ``href``, so anything else (other sites, look-alike hosts, user
    info, ports, ``javascript:``, edit or special pages, query strings) is rejected.
    """
    if url is None:
        return
    error = ValueError(
        "wiki must be a link to a Wikipedia article, like https://en.wikipedia.org/wiki/Name"
    )
    if len(url) > 2000 or WIKI_BAD_CHARS.search(url):
        raise error
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not WIKI_HOST.fullmatch(parts.netloc)  # also rules out user info and ports
        or not parts.path.startswith("/wiki/")
        or "?" in url.partition("#")[0]
    ):
        raise error
    title = unquote(parts.path.removeprefix("/wiki/"))
    if WIKI_BAD_CHARS.search(title) or not title.strip("_"):
        raise error
    if any(segment in (".", "..") for segment in title.split("/")):
        raise error
    namespace = title.split(":", 1)[0].replace("_", " ").strip().casefold()
    if ":" in title and namespace in WIKI_NAMESPACES:
        raise error
