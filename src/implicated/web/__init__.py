"""Server-rendered web front end (FastAPI + Jinja2 + htmx)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from implicated import __version__
from implicated.api import router as api_router
from implicated.importer import import_dir
from implicated.library import Library
from implicated.web.views import router as views_router

HERE = Path(__file__).parent
DEFAULT_DB = "local/implicated.db"
DEFAULT_INFO = "info"

# uvicorn configures its own loggers only; this one shows up next to its startup lines.
log = logging.getLogger("uvicorn.error")


def create_app(db_path: str | Path | None = None, info_dir: str | Path | None = None) -> FastAPI:
    """Build the app. When ``info_dir`` (default ``$IMPLICATED_INFO`` or ``info/``) holds a
    ``bands.yaml``, it is imported first so the library always reflects it."""
    app = FastAPI(title="implicated", version=__version__)
    app.state.library = Library(db_path or os.environ.get("IMPLICATED_DB", DEFAULT_DB))
    sync_info(app.state.library, info_dir or os.environ.get("IMPLICATED_INFO", DEFAULT_INFO))
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.include_router(api_router)
    app.include_router(views_router)
    return app


def sync_info(lib: Library, info_dir: str | Path) -> None:
    if not (Path(info_dir) / "bands.yaml").is_file():
        return
    try:
        report = import_dir(lib, info_dir)
    except (OSError, ValueError) as exc:
        # A broken file should not keep the app from starting; the import rolled back.
        log.error("Could not import %s: %s", info_dir, exc)
        return
    log.info("Imported %s: %s", info_dir, report.totals())
    for warning in report.warnings:
        log.warning("Import: %s", warning)
