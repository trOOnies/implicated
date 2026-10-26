"""Dependencies and helpers shared by the API routers."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse

from implicated.library import IMAGE_TYPES, Library, NotFound


def get_library(request: Request) -> Library:
    return request.app.state.library


Lib = Annotated[Library, Depends(get_library)]


def call(fn, *args, **kwargs):
    """Run a library call, turning its errors into 404 and 422 responses."""
    try:
        return fn(*args, **kwargs)
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def image(path: Path) -> FileResponse:
    # Image file names change with their content, so browsers may keep them forever.
    return FileResponse(
        path,
        media_type=IMAGE_TYPES[path.suffix],
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
