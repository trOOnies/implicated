from __future__ import annotations

from fastapi import APIRouter

from implicated.api.deps import Lib, call
from implicated.models import Member, MemberIn

router = APIRouter(prefix="/artists/{artist_id}/members", tags=["Members"])


@router.get("")
def list_members(lib: Lib, artist_id: int) -> list[Member]:
    call(lib.get_artist, artist_id)
    return lib.list_members(artist_id)


@router.put("")
def set_members(lib: Lib, artist_id: int, data: list[MemberIn]) -> list[Member]:
    """Replace the whole line-up, in the given order."""
    return call(lib.set_members, artist_id, data)
