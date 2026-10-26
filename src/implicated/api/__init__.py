"""JSON API mounted under /api, one router per resource.

Each router carries the tag its routes are grouped under on the API page (``/docs``);
``TAGS`` describes them, in the order the page lists them.
"""

from __future__ import annotations

from fastapi import APIRouter

from implicated.api import albums, artists, members, plays, songs, stats
from implicated.api.deps import get_library

__all__ = ["TAGS", "get_library", "router"]

TAGS = [
    {"name": "Artists", "description": "Bands and solo artists, with their picture."},
    {"name": "Members", "description": "The line-up of an artist, current and former."},
    {"name": "Albums", "description": "Albums, EPs, singles and more, with their cover."},
    {"name": "Songs", "description": "Songs, on an album or as singles."},
    {"name": "Plays", "description": "Listening history."},
    {"name": "Stats", "description": "Totals and chart data for the whole library."},
]

router = APIRouter(prefix="/api")
for module in (artists, members, albums, songs, plays, stats):
    router.include_router(module.router)
