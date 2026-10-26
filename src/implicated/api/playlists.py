from __future__ import annotations

from fastapi import APIRouter, Response

from implicated.api.deps import Lib, call
from implicated.models import Playlist, PlaylistDetail, PlaylistIn, PlaylistSummary

router = APIRouter(prefix="/playlists", tags=["Playlists"])


@router.get("")
def list_playlists(lib: Lib) -> list[PlaylistSummary]:
    return lib.list_playlists()


@router.post("", status_code=201)
def create_playlist(lib: Lib, data: PlaylistIn) -> Playlist:
    return call(lib.create_playlist, data)


@router.get("/{playlist_id}")
def get_playlist(lib: Lib, playlist_id: int) -> PlaylistDetail:
    return call(lib.get_playlist_detail, playlist_id)


@router.put("/{playlist_id}")
def update_playlist(lib: Lib, playlist_id: int, data: PlaylistIn) -> Playlist:
    return call(lib.update_playlist, playlist_id, data)


@router.delete("/{playlist_id}", status_code=204)
def delete_playlist(lib: Lib, playlist_id: int) -> Response:
    call(lib.delete_playlist, playlist_id)
    return Response(status_code=204)


@router.put("/{playlist_id}/songs")
def set_playlist_songs(lib: Lib, playlist_id: int, song_ids: list[int]) -> PlaylistDetail:
    """Replace the playlist's songs with these song ids, in this order."""
    return call(lib.set_playlist_songs, playlist_id, song_ids)


@router.post("/{playlist_id}/songs/{song_id}")
def add_song(lib: Lib, playlist_id: int, song_id: int) -> PlaylistDetail:
    """Append the song to the playlist, unless it is already in it."""
    call(lib.add_to_playlist, playlist_id, [song_id])
    return lib.get_playlist_detail(playlist_id)


@router.delete("/{playlist_id}/songs/{song_id}")
def remove_song(lib: Lib, playlist_id: int, song_id: int) -> PlaylistDetail:
    call(lib.remove_from_playlist, playlist_id, song_id)
    return lib.get_playlist_detail(playlist_id)
