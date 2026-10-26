from __future__ import annotations

from fastapi import APIRouter

from implicated.api.deps import Lib, call
from implicated.models import Play

router = APIRouter(tags=["Plays"])


@router.post("/songs/{song_id}/plays", status_code=201)
def record_play(lib: Lib, song_id: int) -> Play:
    return call(lib.record_play, song_id)
