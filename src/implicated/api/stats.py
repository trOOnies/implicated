from __future__ import annotations

from fastapi import APIRouter

from implicated.api.deps import Lib
from implicated.models import LibraryStats

router = APIRouter(prefix="/stats", tags=["Stats"])


@router.get("")
def stats(lib: Lib, days: int = 30) -> LibraryStats:
    return lib.stats(days=max(1, min(days, 365)))
