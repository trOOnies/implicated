from __future__ import annotations

from fastapi import APIRouter, Response

from implicated.api.deps import Lib, call
from implicated.models import Song, SongIn, SongSummary

router = APIRouter(prefix="/songs", tags=["Songs"])


@router.get("")
def list_songs(
    lib: Lib,
    artist_id: int | None = None,
    album_id: int | None = None,
    q: str | None = None,
    favorites: bool = False,
) -> list[SongSummary]:
    return lib.list_songs(artist_id=artist_id, album_id=album_id, q=q, favorites=favorites)


@router.post("", status_code=201)
def create_song(lib: Lib, data: SongIn) -> Song:
    return call(lib.create_song, data)


@router.get("/{song_id}")
def get_song(lib: Lib, song_id: int) -> Song:
    return call(lib.get_song, song_id)


@router.put("/{song_id}")
def update_song(lib: Lib, song_id: int, data: SongIn) -> Song:
    return call(lib.update_song, song_id, data)


@router.delete("/{song_id}", status_code=204)
def delete_song(lib: Lib, song_id: int) -> Response:
    call(lib.delete_song, song_id)
    return Response(status_code=204)
