from __future__ import annotations

from fastapi import APIRouter

from implicated.api.deps import Lib, call
from implicated.models import Play

router = APIRouter(tags=["Plays"])


@router.post("/songs/{song_id}/plays", status_code=201)
def record_play(lib: Lib, song_id: int) -> Play:
    return call(lib.record_play, song_id)


@router.post("/albums/{album_id}/plays", status_code=201)
def record_album_play(lib: Lib, album_id: int) -> list[Play]:
    """Log a listen of every song on the album."""
    return call(lib.record_album_play, album_id)
