"""Domain models.

Persisted records (``Artist``, ``Member``, ``Album``, ``Song``, ``Play``) carry their database id.
The ``*In`` variants are the writable fields, used as API request bodies and for form input.
"""

from __future__ import annotations

from dataclasses import dataclass, field

ALBUM_KINDS = ("album", "ep", "single", "live", "compilation")


@dataclass
class ArtistIn:
    name: str
    country: str | None = None
    formed_year: int | None = None
    genres: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    bio: str | None = None
    favorite: bool = False


@dataclass
class Artist(ArtistIn):
    id: int = 0
    created_at: str = ""


@dataclass
class AlbumIn:
    artist_id: int
    title: str
    year: int | None = None
    kind: str = "album"
    rating: int | None = None
    notes: str | None = None
    favorite: bool = False


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


@dataclass
class Song(SongIn):
    id: int = 0
    created_at: str = ""


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


def validate_rating(rating: int | None) -> None:
    if rating is not None and not 0 <= rating <= 5:
        raise ValueError("rating must be between 0 and 5")


def validate_kind(kind: str) -> None:
    if kind not in ALBUM_KINDS:
        raise ValueError(f"kind must be one of {', '.join(ALBUM_KINDS)}")
