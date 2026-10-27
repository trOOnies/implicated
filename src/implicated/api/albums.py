from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Response
from fastapi.responses import FileResponse

from implicated.api.deps import Lib, call, image
from implicated.models import Album, AlbumIn, AlbumSummary, PlaylistDetail

router = APIRouter(prefix="/albums", tags=["Albums"])

Ownership = Literal["wanted", "owned"]


@router.get("")
def list_albums(
    lib: Lib,
    artist_id: int | None = None,
    favorites: bool = False,
    q: str | None = None,
    cd: Ownership | None = None,
    vinyl: Ownership | None = None,
    listen_later: bool = False,
    missing_lyrics: bool = False,
    no_own_cover: bool = False,
) -> list[AlbumSummary]:
    """Filter by CD or vinyl ownership with ``cd`` and ``vinyl`` (``wanted`` or ``owned``);
    ``no_own_cover`` keeps the albums with no cover or the same one as another album."""
    return lib.list_albums(
        artist_id,
        favorites,
        q,
        cd=cd,
        vinyl=vinyl,
        listen_later=listen_later,
        missing_lyrics=missing_lyrics,
        no_own_cover=no_own_cover,
    )


@router.put("/{album_id}/formats/{fmt}")
def set_format(
    lib: Lib, album_id: int, fmt: Literal["cd", "vinyl"], state: Ownership | None = None
) -> Album:
    """Say whether you want or own the album on CD or vinyl; leave ``state`` out for neither."""
    return call(lib.set_format, album_id, fmt, state)


@router.post("/{album_id}/playlists/{playlist_id}")
def add_album_to_playlist(lib: Lib, album_id: int, playlist_id: int) -> PlaylistDetail:
    """Append the album's songs to the playlist, in track order; songs already there stay."""
    call(lib.add_album_to_playlist, playlist_id, album_id)
    return lib.get_playlist_detail(playlist_id)


@router.post("", status_code=201)
def create_album(lib: Lib, data: AlbumIn) -> Album:
    return call(lib.create_album, data)


@router.get("/{album_id}")
def get_album(lib: Lib, album_id: int) -> Album:
    return call(lib.get_album, album_id)


@router.put("/{album_id}")
def update_album(lib: Lib, album_id: int, data: AlbumIn) -> Album:
    return call(lib.update_album, album_id, data)


@router.delete("/{album_id}", status_code=204)
def delete_album(lib: Lib, album_id: int) -> Response:
    call(lib.delete_album, album_id)
    return Response(status_code=204)


@router.get("/{album_id}/cover", response_class=FileResponse)
def album_cover(lib: Lib, album_id: int) -> FileResponse:
    return image(call(lib.cover_path, album_id))
