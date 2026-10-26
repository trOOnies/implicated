"""HTML pages and htmx fragments.

Mutations answer with the fragment that replaces the element that sent them and an
``HX-Trigger`` header naming the events other sections listen to (``hx-trigger="... from:body"``)
so they refresh themselves:

* ``artists-changed``, ``albums-changed``, ``songs-changed``: a list needs reloading
* ``plays-changed``: listening history changed
* ``library-changed``: anything changed; the dashboard overview listens to it
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.datastructures import FormData

from implicated.api import get_library
from implicated.library import Library, NotFound
from implicated.models import ALBUM_KINDS, AlbumIn, ArtistIn, Member, Point, SongIn

router = APIRouter(default_response_class=HTMLResponse, include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

Lib = Annotated[Library, Depends(get_library)]


# Template helpers


def duration(seconds: int | None) -> str:
    if not seconds:
        return "–"
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"


def stars(rating: int | None) -> str:
    return "–" if rating is None else "★" * rating + "☆" * (5 - rating)


def when(timestamp: str | None) -> str:
    if not timestamp:
        return "never"
    return datetime.fromisoformat(timestamp).strftime("%Y-%m-%d %H:%M")


def tenure(member: Member) -> str:
    """``2002–``, ``2001–2004`` or ``–2017``; empty when the years are unknown."""
    if member.from_year is None and member.to_year is None:
        return ""
    return f"{member.from_year or ''}–{member.to_year or ''}"


templates.env.filters.update(duration=duration, stars=stars, when=when, tenure=tenure)
templates.env.globals.update(album_kinds=ALBUM_KINDS)


def render(
    request: Request,
    name: str,
    *,
    trigger: tuple[str, ...] = (),
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    **context,
) -> HTMLResponse:
    headers = dict(headers or {})
    if trigger:
        headers["HX-Trigger"] = ", ".join(trigger)
    return templates.TemplateResponse(
        request, name, context, status_code=status_code, headers=headers
    )


def found(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc


# Form parsing


def _text(form: FormData, key: str) -> str | None:
    value = str(form.get(key) or "").strip()
    return value or None


def _int(form: FormData, key: str) -> int | None:
    value = _text(form, key)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        raise ValueError(f"{key.replace('_', ' ')} must be a number") from None


def _csv(form: FormData, key: str) -> list[str]:
    return [v for v in (_text(form, key) or "").split(",") if v.strip()]


def _duration(form: FormData, key: str) -> int | None:
    """Accept ``m:ss`` or plain seconds."""
    value = _text(form, key)
    if value is None:
        return None
    try:
        if ":" in value:
            minutes, seconds = value.split(":", 1)
            return int(minutes) * 60 + int(seconds)
        return int(value)
    except ValueError:
        raise ValueError("duration must look like 3:45") from None


def artist_from_form(form: FormData) -> ArtistIn:
    return ArtistIn(
        name=_text(form, "name") or "",
        country=_text(form, "country"),
        formed_year=_int(form, "formed_year"),
        genres=_csv(form, "genres"),
        languages=_csv(form, "languages"),
        bio=_text(form, "bio"),
        wiki=_text(form, "wiki"),
        favorite="favorite" in form,
    )


def album_from_form(form: FormData) -> AlbumIn:
    return AlbumIn(
        artist_id=_int(form, "artist_id") or 0,
        title=_text(form, "title") or "",
        year=_int(form, "year"),
        kind=_text(form, "kind") or "album",
        rating=_int(form, "rating"),
        notes=_text(form, "notes"),
        favorite="favorite" in form,
    )


def song_from_form(form: FormData) -> SongIn:
    return SongIn(
        artist_id=_int(form, "artist_id") or 0,
        album_id=_int(form, "album_id"),
        title=_text(form, "title") or "",
        track_number=_int(form, "track_number"),
        duration_seconds=_duration(form, "duration"),
        rating=_int(form, "rating"),
        notes=_text(form, "notes"),
        favorite="favorite" in form,
    )


# Dashboard


def _view_context(lib: Library) -> dict:
    albums = lib.list_albums()
    return {
        "stats": lib.stats(),
        "latest_albums": sorted(albums, key=lambda a: a.album.year or 0, reverse=True)[:12],
        "favorite_artists": [a for a in lib.list_artists() if a.artist.favorite],
        "favorite_albums": lib.list_albums(favorites=True),
        "favorite_songs": lib.list_songs(favorites=True, order="plays"),
        "recent": lib.recent_plays(8),
    }


@router.get("/")
def index(request: Request, lib: Lib):
    return render(request, "index.html", **_view_context(lib))


@router.get("/fragments/view")
def view_fragment(request: Request, lib: Lib):
    return render(request, "_view.html", **_view_context(lib))


@router.get("/help")
def help_page(request: Request):
    return render(request, "help.html")


# Artists


@router.get("/artists")
def artists_page(request: Request, lib: Lib, q: str | None = None):
    return render(request, "artists.html", artists=lib.list_artists(q), q=q or "")


@router.get("/fragments/artists")
def artists_fragment(request: Request, lib: Lib, q: str | None = None):
    return render(request, "_artists.html", artists=lib.list_artists(q))


@router.post("/artists")
async def create_artist(request: Request, lib: Lib):
    form = await request.form()
    try:
        lib.create_artist(artist_from_form(form))
    except ValueError as exc:
        return render(request, "_artist_form.html", status_code=422, error=str(exc), form=form)
    return render(
        request, "_artist_form.html", trigger=("artists-changed", "library-changed"), form={}
    )


def _artist_info_context(lib: Library, artist_id: int) -> dict:
    return {
        "artist": found(lib.get_artist, artist_id),
        "members": lib.list_members(artist_id),
    }


def _artist_context(lib: Library, artist_id: int) -> dict:
    albums = lib.list_albums(artist_id)
    return {
        **_artist_info_context(lib, artist_id),
        "albums": albums,
        "songs": lib.list_songs(artist_id=artist_id, order="plays"),
        "album_plays": [Point(a.album.title, a.play_count) for a in albums if a.play_count],
        "album_minutes": [
            Point(a.album.title, round(a.total_seconds / 60)) for a in albums if a.total_seconds
        ],
    }


@router.get("/artists/{artist_id}")
def artist_page(request: Request, lib: Lib, artist_id: int):
    return render(request, "artist.html", **_artist_context(lib, artist_id))


@router.get("/fragments/artists/{artist_id}/info")
def artist_info(request: Request, lib: Lib, artist_id: int):
    return render(request, "_artist_info.html", **_artist_info_context(lib, artist_id))


@router.get("/fragments/artists/{artist_id}/edit")
def artist_edit(request: Request, lib: Lib, artist_id: int):
    return render(request, "_artist_edit.html", artist=found(lib.get_artist, artist_id))


@router.get("/fragments/artists/{artist_id}/albums")
def artist_albums(request: Request, lib: Lib, artist_id: int):
    return render(request, "_albums.html", albums=lib.list_albums(artist_id), show_artist=False)


@router.get("/fragments/artists/{artist_id}/songs")
def artist_songs(request: Request, lib: Lib, artist_id: int):
    return render(
        request,
        "_songs.html",
        songs=lib.list_songs(artist_id=artist_id, order="plays"),
        ctx="artist",
    )


@router.post("/artists/{artist_id}")
async def update_artist(request: Request, lib: Lib, artist_id: int):
    form = await request.form()
    artist = found(lib.get_artist, artist_id)
    try:
        found(lib.update_artist, artist_id, artist_from_form(form))
    except ValueError as exc:
        return render(request, "_artist_edit.html", status_code=422, error=str(exc), artist=artist)
    return render(
        request,
        "_artist_info.html",
        trigger=("artists-changed", "library-changed"),
        **_artist_info_context(lib, artist_id),
    )


@router.post("/artists/{artist_id}/delete")
def delete_artist(lib: Lib, artist_id: int):
    found(lib.delete_artist, artist_id)
    return Response(headers={"HX-Redirect": "/artists"})


# Albums


@router.get("/albums")
def albums_page(request: Request, lib: Lib, q: str | None = None):
    return render(request, "albums.html", albums=lib.list_albums(q=q), q=q or "")


@router.get("/fragments/albums")
def albums_fragment(request: Request, lib: Lib, q: str | None = None):
    return render(request, "_album_grid.html", albums=lib.list_albums(q=q))


@router.post("/albums")
async def create_album(request: Request, lib: Lib):
    form = await request.form()
    artist_id = _int(form, "artist_id")
    try:
        lib.create_album(album_from_form(form))
    except (ValueError, NotFound) as exc:
        return render(
            request,
            "_album_form.html",
            status_code=422,
            error=str(exc),
            form=form,
            artist_id=artist_id,
        )
    return render(
        request,
        "_album_form.html",
        trigger=("albums-changed", "library-changed"),
        form={},
        artist_id=artist_id,
    )


def _album_context(lib: Library, album_id: int) -> dict:
    album = found(lib.get_album, album_id)
    songs = lib.list_songs(album_id=album_id, order="track")
    return {
        "album": album,
        "artist": lib.get_artist(album.artist_id),
        "songs": songs,
        "total_seconds": sum(s.song.duration_seconds or 0 for s in songs),
        "play_count": sum(s.play_count for s in songs),
    }


@router.get("/albums/{album_id}")
def album_page(request: Request, lib: Lib, album_id: int):
    return render(request, "album.html", **_album_context(lib, album_id))


@router.get("/fragments/albums/{album_id}/info")
def album_info(request: Request, lib: Lib, album_id: int):
    return render(request, "_album_info.html", **_album_context(lib, album_id))


@router.get("/fragments/albums/{album_id}/edit")
def album_edit(request: Request, lib: Lib, album_id: int):
    return render(request, "_album_edit.html", album=found(lib.get_album, album_id))


@router.get("/fragments/albums/{album_id}/songs")
def album_songs(request: Request, lib: Lib, album_id: int):
    return render(
        request,
        "_songs.html",
        songs=lib.list_songs(album_id=album_id, order="track"),
        ctx="album",
    )


@router.post("/albums/{album_id}")
async def update_album(request: Request, lib: Lib, album_id: int):
    form = await request.form()
    album = found(lib.get_album, album_id)
    try:
        found(lib.update_album, album_id, album_from_form(form))
    except ValueError as exc:
        return render(request, "_album_edit.html", status_code=422, error=str(exc), album=album)
    return render(
        request,
        "_album_info.html",
        trigger=("albums-changed", "library-changed"),
        **_album_context(lib, album_id),
    )


@router.post("/albums/{album_id}/delete")
def delete_album(lib: Lib, album_id: int):
    album = found(lib.get_album, album_id)
    lib.delete_album(album_id)
    return Response(headers={"HX-Redirect": f"/artists/{album.artist_id}"})


# Songs


def _songs_query(q: str | None, favorites: str | None) -> dict:
    return {"q": q or None, "favorites": bool(favorites)}


@router.get("/songs")
def songs_page(
    request: Request,
    lib: Lib,
    q: str | None = None,
    favorites: str | None = None,
    order: str = "title",
):
    order = order if order in ("title", "plays", "recent") else "title"
    songs = lib.list_songs(order=order, **_songs_query(q, favorites))
    return render(
        request,
        "songs.html",
        songs=songs,
        q=q or "",
        favorites=bool(favorites),
        order=order,
        ctx="all",
    )


@router.get("/fragments/songs")
def songs_fragment(
    request: Request,
    lib: Lib,
    q: str | None = None,
    favorites: str | None = None,
    order: str = "title",
):
    order = order if order in ("title", "plays", "recent") else "title"
    songs = lib.list_songs(order=order, **_songs_query(q, favorites))
    return render(request, "_songs.html", songs=songs, ctx="all")


@router.post("/songs")
async def create_song(request: Request, lib: Lib):
    form = await request.form()
    context = {
        "artist_id": _int(form, "artist_id"),
        "album_id": _int(form, "album_id"),
        "fixed_album": "fixed_album" in form,
    }
    if context["artist_id"] and not context["fixed_album"]:
        context["albums"] = lib.list_albums(context["artist_id"])
    try:
        lib.create_song(song_from_form(form))
    except (ValueError, NotFound) as exc:
        return render(
            request, "_song_form.html", status_code=422, error=str(exc), form=form, **context
        )
    next_track = (_int(form, "track_number") or 0) + 1 if context["fixed_album"] else None
    return render(
        request,
        "_song_form.html",
        trigger=("songs-changed", "library-changed"),
        form={"track_number": next_track} if next_track else {},
        **context,
    )


def _ctx(form_or_query) -> str:
    ctx = form_or_query.get("ctx") or "all"
    return ctx if ctx in ("all", "artist", "album") else "all"


@router.get("/fragments/songs/{song_id}")
def song_row(request: Request, lib: Lib, song_id: int, ctx: str = "all"):
    summary = found(lib.get_song_summary, song_id)
    return render(request, "_song_row.html", s=summary, ctx=_ctx({"ctx": ctx}))


@router.get("/fragments/songs/{song_id}/edit")
def song_edit(request: Request, lib: Lib, song_id: int, ctx: str = "all"):
    summary = found(lib.get_song_summary, song_id)
    return render(
        request,
        "_song_edit_row.html",
        s=summary,
        ctx=_ctx({"ctx": ctx}),
        albums=lib.list_albums(summary.song.artist_id),
    )


@router.post("/songs/{song_id}")
async def update_song(request: Request, lib: Lib, song_id: int):
    form = await request.form()
    song = found(lib.get_song, song_id)
    data = song_from_form(form)
    data.artist_id = song.artist_id
    try:
        found(lib.update_song, song_id, data)
    except ValueError as exc:
        return render(
            request,
            "_song_edit_row.html",
            status_code=422,
            error=str(exc),
            s=lib.get_song_summary(song_id),
            ctx=_ctx(form),
            albums=lib.list_albums(song.artist_id),
        )
    return render(
        request,
        "_song_row.html",
        trigger=("library-changed",),
        s=lib.get_song_summary(song_id),
        ctx=_ctx(form),
    )


@router.post("/songs/{song_id}/play")
async def play_song(request: Request, lib: Lib, song_id: int):
    form = await request.form()
    found(lib.record_play, song_id)
    return render(
        request,
        "_song_row.html",
        trigger=("plays-changed", "library-changed"),
        s=lib.get_song_summary(song_id),
        ctx=_ctx(form),
    )


@router.post("/songs/{song_id}/delete")
def delete_song(lib: Lib, song_id: int):
    found(lib.delete_song, song_id)
    return Response(headers={"HX-Trigger": "songs-changed, library-changed"})


# Favorites


@router.post("/favorite/{kind}/{record_id}")
def toggle_favorite(request: Request, lib: Lib, kind: str, record_id: int):
    if kind not in ("artist", "album", "song"):
        raise HTTPException(404)
    favorite = found(lib.toggle_favorite, kind, record_id)
    return render(
        request,
        "_fav.html",
        trigger=("library-changed",),
        kind=kind,
        record_id=record_id,
        favorite=favorite,
    )
