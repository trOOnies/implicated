from __future__ import annotations

import json
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import Response

from implicated.api.deps import Lib
from implicated.userdata import export_data, export_file_name, import_data

router = APIRouter(prefix="/data", tags=["Your data"])


@router.get("/export")
def export(lib: Lib, owner: str | None = None, download: bool = False) -> dict:
    """Your marks, ratings, notes, ownership, videos, playlists and plays, naming artists,
    albums and songs by name. ``owner`` says whose data it is; ``download`` sends it as a file."""
    data = export_data(lib, owner)
    if not download:
        return data
    return Response(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{export_file_name(data)}"'},
    )


@router.post("/import")
def import_(lib: Lib, data: Annotated[dict, Body()]) -> dict:
    """Import an export: records the library has take the file's marks and values, plays are
    added and playlists created or replaced by name. Nothing changes when the file is broken."""
    try:
        return asdict(import_data(lib, data))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
