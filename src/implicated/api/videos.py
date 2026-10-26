from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response

from implicated.api.deps import Lib, call
from implicated.models import SongVideo, SongVideoIn

router = APIRouter(prefix="/songs/{song_id}/videos", tags=["Song videos"])


def _video(lib, song_id: int, video_id: int) -> SongVideo:
    video = call(lib.get_video, video_id)
    if video.song_id != song_id:
        raise HTTPException(404, f"song {song_id} has no video {video_id}")
    return video


@router.get("")
def list_videos(lib: Lib, song_id: int) -> list[SongVideo]:
    return call(lib.list_videos, song_id)


@router.post("", status_code=201)
def add_video(lib: Lib, song_id: int, data: SongVideoIn) -> SongVideo:
    """Add a YouTube video (a link or its id) at the end of the song's videos. A ``t=`` in the
    link becomes ``start_seconds`` unless that is given."""
    return call(lib.add_video, song_id, data)


@router.put("/{video_id}")
def update_video(lib: Lib, song_id: int, video_id: int, data: SongVideoIn) -> SongVideo:
    _video(lib, song_id, video_id)
    return call(lib.update_video, video_id, data)


@router.delete("/{video_id}", status_code=204)
def delete_video(lib: Lib, song_id: int, video_id: int) -> Response:
    _video(lib, song_id, video_id)
    lib.delete_video(video_id)
    return Response(status_code=204)
