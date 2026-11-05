"""Server-rendered web front end (FastAPI + Jinja2 + htmx)."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from implicated import __version__
from implicated.api import TAGS as api_tags
from implicated.api import router as api_router
from implicated.library import Library
from implicated.web.views import router as views_router

HERE = Path(__file__).parent
DEFAULT_DB = "local/implicated.db"
DEFAULT_INFO = "info"


def create_app(db_path: str | Path | None = None, info_dir: str | Path | None = None) -> FastAPI:
    """Build the app on the library at ``db_path`` (default ``$IMPLICATED_DB``), which keeps its
    lyrics, tabs and concerts next to the database.

    ``info_dir`` (default ``$IMPLICATED_INFO`` or ``info/``) is the inbox: files dropped there are
    imported only when you ask, from the Import page or ``make import``, never on startup, so the
    edits you make in the app always stay. It is created, empty, when missing."""
    db_path = Path(db_path or os.environ.get("IMPLICATED_DB", DEFAULT_DB))
    info_dir = Path(info_dir or os.environ.get("IMPLICATED_INFO", DEFAULT_INFO))
    info_dir.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="implicated", version=__version__, openapi_tags=api_tags)
    app.state.library = Library(db_path, db_path.parent)
    app.state.info_dir = info_dir
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.include_router(api_router)
    app.include_router(views_router)
    return app
