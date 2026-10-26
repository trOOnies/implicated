"""JSON API mounted under /api."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse

from implicated.library import IMAGE_TYPES, Library, NotFound
from implicated.models import (
    Album,
    AlbumIn,
    AlbumSummary,
    Artist,
    ArtistIn,
    ArtistSummary,
    LibraryStats,
    Member,
    MemberIn,
    Play,
    Song,
    SongIn,
    SongSummary,
)

router = APIRouter(prefix="/api", tags=["api"])


def get_library(request: Request) -> Library:
    return request.app.state.library


Lib = Annotated[Library, Depends(get_library)]


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _image(path: Path) -> FileResponse:
    # Image file names change with their content, so browsers may keep them forever.
    return FileResponse(
        path,
        media_type=IMAGE_TYPES[path.suffix],
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


# Artists


@router.get("/artists")
def list_artists(lib: Lib, q: str | None = None) -> list[ArtistSummary]:
    return lib.list_artists(q)


@router.post("/artists", status_code=201)
def create_artist(lib: Lib, data: ArtistIn) -> Artist:
    return _call(lib.create_artist, data)


@router.get("/artists/{artist_id}")
def get_artist(lib: Lib, artist_id: int) -> Artist:
    return _call(lib.get_artist, artist_id)


@router.put("/artists/{artist_id}")
def update_artist(lib: Lib, artist_id: int, data: ArtistIn) -> Artist:
    return _call(lib.update_artist, artist_id, data)


@router.delete("/artists/{artist_id}", status_code=204)
def delete_artist(lib: Lib, artist_id: int) -> Response:
    _call(lib.delete_artist, artist_id)
    return Response(status_code=204)


@router.get("/artists/{artist_id}/image", response_class=FileResponse)
def artist_image(lib: Lib, artist_id: int) -> FileResponse:
    return _image(_call(lib.image_path, artist_id))


@router.get("/artists/{artist_id}/members")
def list_members(lib: Lib, artist_id: int) -> list[Member]:
    _call(lib.get_artist, artist_id)
    return lib.list_members(artist_id)


@router.put("/artists/{artist_id}/members")
def set_members(lib: Lib, artist_id: int, data: list[MemberIn]) -> list[Member]:
    """Replace the whole line-up, in the given order."""
    return _call(lib.set_members, artist_id, data)


# Albums


@router.get("/albums")
def list_albums(
    lib: Lib, artist_id: int | None = None, favorites: bool = False, q: str | None = None
) -> list[AlbumSummary]:
    return lib.list_albums(artist_id, favorites, q)


@router.post("/albums", status_code=201)
def create_album(lib: Lib, data: AlbumIn) -> Album:
    return _call(lib.create_album, data)


@router.get("/albums/{album_id}")
def get_album(lib: Lib, album_id: int) -> Album:
    return _call(lib.get_album, album_id)


@router.put("/albums/{album_id}")
def update_album(lib: Lib, album_id: int, data: AlbumIn) -> Album:
    return _call(lib.update_album, album_id, data)


@router.delete("/albums/{album_id}", status_code=204)
def delete_album(lib: Lib, album_id: int) -> Response:
    _call(lib.delete_album, album_id)
    return Response(status_code=204)


@router.get("/albums/{album_id}/cover", response_class=FileResponse)
def album_cover(lib: Lib, album_id: int) -> FileResponse:
    return _image(_call(lib.cover_path, album_id))


# Songs


@router.get("/songs")
def list_songs(
    lib: Lib,
    artist_id: int | None = None,
    album_id: int | None = None,
    q: str | None = None,
    favorites: bool = False,
) -> list[SongSummary]:
    return lib.list_songs(artist_id=artist_id, album_id=album_id, q=q, favorites=favorites)


@router.post("/songs", status_code=201)
def create_song(lib: Lib, data: SongIn) -> Song:
    return _call(lib.create_song, data)


@router.get("/songs/{song_id}")
def get_song(lib: Lib, song_id: int) -> Song:
    return _call(lib.get_song, song_id)


@router.put("/songs/{song_id}")
def update_song(lib: Lib, song_id: int, data: SongIn) -> Song:
    return _call(lib.update_song, song_id, data)


@router.delete("/songs/{song_id}", status_code=204)
def delete_song(lib: Lib, song_id: int) -> Response:
    _call(lib.delete_song, song_id)
    return Response(status_code=204)


@router.post("/songs/{song_id}/plays", status_code=201)
def record_play(lib: Lib, song_id: int) -> Play:
    return _call(lib.record_play, song_id)


# Stats


@router.get("/stats")
def stats(lib: Lib, days: int = 30) -> LibraryStats:
    return lib.stats(days=max(1, min(days, 365)))
