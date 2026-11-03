"""JSON API mounted under /api, one router per resource.

Each router carries the tag its routes are grouped under on the API page (``/docs``);
``TAGS`` describes them, in the order the page lists them.
"""

from __future__ import annotations

from fastapi import APIRouter

from implicated.api import (
    albums,
    artists,
    budget,
    data,
    members,
    playlists,
    plays,
    songs,
    stats,
    videos,
)
from implicated.api.deps import get_library

__all__ = ["TAGS", "get_library", "router"]

TAGS = [
    {"name": "Artists", "description": "Bands and solo artists, with their picture."},
    {"name": "Members", "description": "The line-up of an artist, current and former."},
    {"name": "Albums", "description": "Albums, EPs, singles and more, with their cover."},
    {"name": "Songs", "description": "Songs, on an album or as singles, and their lyrics."},
    {
        "name": "Song videos",
        "description": "YouTube videos about a song: lessons, covers, live takes.",
    },
    {
        "name": "Playlists",
        "description": "Lists of songs in your own order. Favorites and Listen Later are built in: "
        "they hold exactly the songs with that mark, and adding or removing songs sets the mark.",
    },
    {
        "name": "Plays",
        "description": "Listening history; a play clears the song's listen later mark.",
    },
    {
        "name": "Budget",
        "description": "A cart of albums to buy on CD or vinyl, with their prices, quantities, "
        "where to buy them and the total, and the budgets saved from it.",
    },
    {"name": "Stats", "description": "Totals and chart data for the whole library."},
    {
        "name": "Your data",
        "description": "Download your marks, ratings, notes, playlists and plays as a JSON file, "
        "and import such a file back.",
    },
]

router = APIRouter(prefix="/api")
for module in (artists, members, albums, songs, videos, playlists, plays, budget, stats, data):
    router.include_router(module.router)
