"""HTML pages and htmx fragments.

Mutations answer with the fragment that replaces the element that sent them and an
``HX-Trigger`` header naming the events other sections listen to (``hx-trigger="... from:body"``)
so they refresh themselves:

* ``artists-changed``, ``albums-changed``, ``songs-changed``: a list needs reloading
* ``plays-changed``: listening history changed
* ``playlists-changed``: a playlist, or the songs in it, changed (also when a song's favorite or
  listen later mark changes, since the built-in playlists follow them)
* ``videos-changed``: a song's videos changed
* ``library-changed``: anything changed; the dashboard overview listens to it
"""

from __future__ import annotations

from dataclasses import fields, replace
from datetime import date, datetime
from html import escape
from math import ceil
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from starlette.background import BackgroundTask
from starlette.datastructures import FormData, ImmutableMultiDict

from implicated.api import get_library
from implicated.concerts import (
    CONCERTS_FILE,
    add_concerts,
    parse_tour_dates,
    read_concerts,
    set_status,
)
from implicated.library import Library, NotFound
from implicated.models import (
    ALBUM_KINDS,
    CONCERT_STATUSES,
    FORMATS,
    OWNERSHIP,
    PAST_CONCERT_STATUSES,
    Album,
    AlbumIn,
    AlbumSummary,
    ArtistIn,
    Budget,
    BudgetIn,
    BudgetOverlap,
    CartItemIn,
    Concert,
    Member,
    PlaylistIn,
    Point,
    Song,
    SongIn,
    SongVideoIn,
)
from implicated.web import editor, shutdown

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


def percent(part: int, whole: int) -> str:
    """Rounded down, so 100% only shows once every song is covered."""
    return f"{part * 100 // whole}%" if whole else "–"


# The Albums page filter: value -> (format, ownership, label).
FORMAT_FILTERS = {
    f"{fmt}-{state}": (fmt, state, f"{state.capitalize()} ({label})")
    for fmt, label in (("cd", "CD"), ("vinyl", "Vinyl"))
    for state in OWNERSHIP
}


def formats(album: Album) -> list[tuple[str, str]]:
    """``[("owned", "Owned (CD)"), ("wanted", "Wanted (Vinyl)")]`` and the like; empty when
    neither is set."""
    return [
        (state, label)
        for fmt, state, label in FORMAT_FILTERS.values()
        if getattr(album, fmt) == state
    ]


def money(amount: float) -> str:
    """``1299.5`` -> ``$1,299.50``: the generic dollar sign, whatever the shops' currency."""
    return f"${amount:,.2f}"


@pass_context
def asset(context: dict, path: str) -> str:
    """A file of ours in ``static``, its URL changing with the file. Browsers keep these without
    asking whether they changed, so a page could otherwise show with an old stylesheet."""
    version = (Path(__file__).parent / "static" / path).stat().st_mtime_ns
    return f"{context['request'].url_for('static', path=path)}?v={version}"


templates.env.filters.update(
    duration=duration,
    stars=stars,
    when=when,
    tenure=tenure,
    percent=percent,
    formats=formats,
    money=money,
)
templates.env.globals.update(
    asset=asset,
    album_kinds=ALBUM_KINDS,
    ownership=OWNERSHIP,
    format_names=FORMATS,
    # The latest year a band can have formed in.
    this_year=lambda: date.today().year,
    concert_statuses=CONCERT_STATUSES,
    past_concert_statuses=PAST_CONCERT_STATUSES,
)


# What a logged listen changes; it also takes the song out of Listen Later.
PLAY_TRIGGER = ("plays-changed", "playlists-changed", "library-changed")


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


def _pager(request: Request, base: str, total: int, page: int, per_page: int) -> dict:
    """Which of the pages of ``total`` records to show (the last one when ``page`` is past the
    end), for ``_pager.html``. ``base`` is the page's path, ``query`` the filters to keep."""
    pages = max(ceil(total / per_page), 1)
    page = min(max(page, 1), pages)
    params = request.query_params.multi_items()
    return {
        "base": base,
        "page": page,
        "pages": pages,
        "total": total,
        "offset": (page - 1) * per_page,
        "numbers": sorted({1, pages} | set(range(max(page - 2, 1), min(page + 2, pages) + 1))),
        "query": urlencode([(key, value) for key, value in params if value and key != "page"]),
    }


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
        notes=_text(form, "notes"),
        listen_later="listen_later" in form,
        rating=_int(form, "rating"),
    )


def album_from_form(form: FormData) -> AlbumIn:
    return AlbumIn(
        artist_id=_int(form, "artist_id") or 0,
        title=_text(form, "title") or "",
        year=_int(form, "year"),
        kind=_text(form, "kind") or "album",
        unreleased="unreleased" in form,
        rating=_int(form, "rating"),
        notes=_text(form, "notes"),
        favorite="favorite" in form,
        cd=_text(form, "cd"),
        vinyl=_text(form, "vinyl"),
        listen_later="listen_later" in form,
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
        no_lyrics="no_lyrics" in form,
        mv=_text(form, "mv"),
        listen_later="listen_later" in form,
    )


def song_in(song: Song) -> SongIn:
    """The writable fields of ``song``, to change some of them."""
    return SongIn(**{f.name: getattr(song, f.name) for f in fields(SongIn)})


# Dashboard


def _view_context(lib: Library) -> dict:
    albums = lib.list_albums()
    artists = lib.list_artists()
    return {
        "stats": lib.stats(),
        "latest_albums": sorted(albums, key=lambda a: a.album.year or 0, reverse=True)[:12],
        "favorite_artists": [a for a in artists if a.artist.favorite],
        "favorite_albums": [a for a in albums if a.album.favorite],
        "favorite_songs": lib.list_songs(favorites=True, order="plays"),
        "later_artists": [a for a in artists if a.artist.listen_later],
        "later_albums": [a for a in albums if a.album.listen_later],
        "later_songs": lib.list_songs(listen_later=True),
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


@router.post("/exit")
def exit_app(request: Request):
    """The header's exit button: say goodbye, close the window, then stop the server."""
    # Only the page's own button may stop the server: other sites cannot send this header.
    if "hx-request" not in request.headers:
        raise HTTPException(403, "stop the server from the exit button")
    response = render(request, "_goodbye.html")
    response.background = BackgroundTask(shutdown.stop)  # once the page has its answer
    return response


# Artists


# How many artists and songs their pages show at a time.
ARTISTS_PER_PAGE = 24
SONGS_PER_PAGE = 50


def _artists_context(
    request: Request,
    lib: Library,
    q: str | None,
    country: str | None,
    genre: str | None,
    language: str | None,
    page: int,
) -> dict:
    query = {"q": q or None, "country": country, "genre": genre, "language": language}
    pager = _pager(request, "/artists", lib.count_artists(**query), page, ARTISTS_PER_PAGE)
    artists = lib.list_artists(**query, limit=ARTISTS_PER_PAGE, offset=pager["offset"])
    return {"artists": artists, "pager": pager}


@router.get("/artists")
def artists_page(
    request: Request,
    lib: Lib,
    q: str | None = None,
    country: str | None = None,
    genre: str | None = None,
    language: str | None = None,
    page: int = 1,
):
    return render(
        request,
        "artists.html",
        **_artists_context(request, lib, q, country, genre, language, page),
        q=q or "",
        country=country or "",
        genre=genre or "",
        language=language or "",
        **lib.artist_options(),
    )


@router.get("/fragments/artists")
def artists_fragment(
    request: Request,
    lib: Lib,
    q: str | None = None,
    country: str | None = None,
    genre: str | None = None,
    language: str | None = None,
    page: int = 1,
):
    context = _artists_context(request, lib, q, country, genre, language, page)
    return render(request, "_artists.html", **context)


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
        "album_plays": [Point(a.album.title, a.play_count) for a in albums if a.play_count],
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


@router.post("/artists/{artist_id}")
async def update_artist(request: Request, lib: Lib, artist_id: int):
    form = await request.form()
    artist = found(lib.get_artist, artist_id)
    try:
        data = artist_from_form(form)
        data.rating = artist.rating  # set with the stars (see ``set_rating``)
        found(lib.update_artist, artist_id, data)
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


def _albums_query(
    q: str | None,
    own: str | None,
    favorites: str | None,
    later: str | None,
    missing: str | None,
    nocover: str | None,
) -> dict:
    query = {
        "q": q or None,
        "favorites": bool(favorites),
        "listen_later": bool(later),
        "missing_lyrics": bool(missing),
        "no_own_cover": bool(nocover),
    }
    if own in FORMAT_FILTERS:
        fmt, state, _label = FORMAT_FILTERS[own]
        query[fmt] = state
    return query


def _favorites_first(albums: list[AlbumSummary]) -> list[AlbumSummary]:
    return sorted(albums, key=lambda a: not a.album.favorite)  # stable: keeps the order


@router.get("/albums")
def albums_page(
    request: Request,
    lib: Lib,
    q: str | None = None,
    own: str | None = None,
    favorites: str | None = None,
    later: str | None = None,
    missing: str | None = None,
    nocover: str | None = None,
):
    return render(
        request,
        "albums.html",
        albums=_favorites_first(
            lib.list_albums(**_albums_query(q, own, favorites, later, missing, nocover))
        ),
        q=q or "",
        own=own if own in FORMAT_FILTERS else "",
        favorites=bool(favorites),
        later=bool(later),
        missing=bool(missing),
        nocover=bool(nocover),
        format_filters=FORMAT_FILTERS,
    )


@router.get("/fragments/albums")
def albums_fragment(
    request: Request,
    lib: Lib,
    q: str | None = None,
    own: str | None = None,
    favorites: str | None = None,
    later: str | None = None,
    missing: str | None = None,
    nocover: str | None = None,
):
    albums = lib.list_albums(**_albums_query(q, own, favorites, later, missing, nocover))
    return render(request, "_album_grid.html", albums=_favorites_first(albums))


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
    previous, following = lib.album_neighbors(album_id)
    return {
        "playlists": lib.list_playlists(),
        "album": album,
        "previous": previous,
        "following": following,
        "same_cover": lib.same_cover_albums(album_id),
        "artist": lib.get_artist(album.artist_id),
        "songs": songs,
        "total_seconds": sum(s.song.duration_seconds or 0 for s in songs),
        "play_count": sum(s.play_count for s in songs),
        "lyrics_count": sum(1 for s in songs if s.song.lyrics and not s.song.no_lyrics),
        "no_lyrics_count": sum(1 for s in songs if s.song.no_lyrics),
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
        data = album_from_form(form)
        # The rating, CD and vinyl have their own controls (see ``set_rating`` and
        # ``set_album_format``), not edit form fields.
        data.rating, data.cd, data.vinyl = album.rating, album.cd, album.vinyl
        found(lib.update_album, album_id, data)
    except ValueError as exc:
        return render(request, "_album_edit.html", status_code=422, error=str(exc), album=album)
    return render(
        request,
        "_album_info.html",
        trigger=("albums-changed", "library-changed"),
        **_album_context(lib, album_id),
    )


@router.post("/albums/{album_id}/formats/{fmt}")
async def set_album_format(request: Request, lib: Lib, album_id: int, fmt: str):
    if fmt not in ("cd", "vinyl"):
        raise HTTPException(404)
    form = await request.form()
    try:
        album = found(lib.set_format, album_id, fmt, _text(form, "state"))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return render(
        request,
        "_format_picker.html",
        trigger=("library-changed",),
        album=album,
        fmt=fmt,
        compact="compact" in form,
    )


@router.post("/albums/{album_id}/play")
def play_album(lib: Lib, album_id: int):
    """Log a listen of every song on the album."""
    found(lib.record_album_play, album_id)
    return Response(headers={"HX-Trigger": ", ".join(("songs-changed", *PLAY_TRIGGER))})


@router.post("/albums/{album_id}/playlists")
async def add_album_to_playlist(request: Request, lib: Lib, album_id: int):
    form = await request.form()
    found(lib.get_album, album_id)
    context = {"action": f"/albums/{album_id}/playlists", "album_wide": True}
    playlist_id = _int(form, "playlist_id")
    if playlist_id is None:
        context["playlists"] = lib.list_playlists()
        return render(
            request, "_playlist_add.html", status_code=422, error="Pick a playlist.", **context
        )
    playlist = found(lib.get_playlist, playlist_id)
    added = lib.add_album_to_playlist(playlist_id, album_id)
    total = len(lib.list_songs(album_id=album_id))
    message = f"Added {added} {'song' if added == 1 else 'songs'} to {playlist.name}."
    if added < total:
        message += f" {total - added} of its {total} songs were already there."
    return render(
        request,
        "_playlist_add.html",
        trigger=("playlists-changed",),
        message=message,
        selected=playlist_id,
        playlists=lib.list_playlists(),
        **context,
    )


@router.post("/albums/{album_id}/copy")
def copy_album(request: Request, lib: Lib, album_id: int):
    # The new title comes from the button's ``hx-prompt``.
    title = request.headers.get("HX-Prompt", "")
    try:
        copy = lib.copy_album(album_id, title)
    except ValueError as exc:
        return HTMLResponse(f'<p class="error">{escape(str(exc))}</p>', status_code=422)
    except NotFound:
        raise HTTPException(404) from None
    return Response(headers={"HX-Redirect": f"/albums/{copy.id}"})


@router.post("/albums/{album_id}/delete")
def delete_album(lib: Lib, album_id: int):
    album = found(lib.get_album, album_id)
    lib.delete_album(album_id)
    return Response(headers={"HX-Redirect": f"/artists/{album.artist_id}"})


# Songs


def _songs_context(
    request: Request,
    lib: Library,
    q: str | None,
    favorites: str | None,
    later: str | None,
    missing: str | None,
    order: str,
    page: int,
) -> dict:
    query = {
        "q": q or None,
        "favorites": bool(favorites),
        "listen_later": bool(later),
        "missing_lyrics": bool(missing),
    }
    order = order if order in ("title", "plays", "recent") else "title"
    pager = _pager(request, "/songs", lib.count_songs(**query), page, SONGS_PER_PAGE)
    songs = lib.list_songs(
        order=order,
        favorites_first=order == "title",  # picking another order asks for that one
        limit=SONGS_PER_PAGE,
        offset=pager["offset"],
        **query,
    )
    return {"songs": songs, "order": order, "pager": pager, "ctx": "all"}


@router.get("/songs")
def songs_page(
    request: Request,
    lib: Lib,
    q: str | None = None,
    favorites: str | None = None,
    later: str | None = None,
    missing: str | None = None,
    order: str = "title",
    page: int = 1,
):
    return render(
        request,
        "songs.html",
        **_songs_context(request, lib, q, favorites, later, missing, order, page),
        q=q or "",
        favorites=bool(favorites),
        later=bool(later),
        missing=bool(missing),
    )


@router.get("/fragments/songs")
def songs_fragment(
    request: Request,
    lib: Lib,
    q: str | None = None,
    favorites: str | None = None,
    later: str | None = None,
    missing: str | None = None,
    order: str = "title",
    page: int = 1,
):
    context = _songs_context(request, lib, q, favorites, later, missing, order, page)
    return render(request, "_songs.html", **context)


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
    """Where a song row is shown; "page" is the song's own page, "playlist" a playlist's."""
    ctx = form_or_query.get("ctx") or "all"
    return ctx if ctx in ("all", "album", "playlist", "page") else "all"


def _lyrics_context(lib: Library, song_id: int, **extra) -> dict:
    """The song with its lyrics read again from its file, so edits show straight away."""
    try:
        found(lib.reload_lyrics, song_id)
    except (OSError, ValueError) as exc:
        extra.setdefault("error", f"Could not read the lyrics file: {exc}")
    return {"s": lib.get_song_summary(song_id), "lyrics_path": lib.lyrics_path(song_id), **extra}


def _tab_context(lib: Library, song_id: int, **extra) -> dict:
    """The song's tab file, looked up again so a newly saved one shows straight away."""
    tab_path = found(lib.tab_path, song_id)
    return {
        "song_id": song_id,
        "tab_path": tab_path,
        "new_tab_path": lib.new_tab_path(song_id),
        # Changes when the file is saved again, so the browser fetches the new version.
        "tab_version": tab_path.stat().st_mtime_ns if tab_path else None,
        **extra,
    }


def _song_info_context(lib: Library, song_id: int) -> dict:
    summary = found(lib.get_song_summary, song_id)
    album_id = summary.song.album_id
    previous, following = lib.song_neighbors(song_id)
    return {
        "s": summary,
        "artist": lib.get_artist(summary.song.artist_id),
        "album": lib.get_album(album_id) if album_id else None,
        "previous": previous,
        "following": following,
    }


def _song_playlists_context(lib: Library, song_id: int, **extra) -> dict:
    return {
        "action": f"/songs/{song_id}/playlists",
        "playlists": lib.list_playlists(),
        "member_of": lib.playlists_with_song(song_id),
        **extra,
    }


@router.get("/songs/{song_id}")
def song_page(request: Request, lib: Lib, song_id: int):
    context = _lyrics_context(lib, song_id)
    return render(
        request,
        "song.html",
        **_song_info_context(lib, song_id),
        **_song_playlists_context(lib, song_id),
        **_tab_context(lib, song_id),
        videos=lib.list_videos(song_id),
        lyrics_path=context["lyrics_path"],
        error=context.get("error"),
    )


@router.get("/fragments/songs/{song_id}/member-of")
def song_member_of(request: Request, lib: Lib, song_id: int):
    found(lib.get_song, song_id)
    return render(
        request,
        "_member_of.html",
        song_id=song_id,
        member_of=lib.playlists_with_song(song_id),
    )


# Song videos


def _videos(
    request: Request,
    lib: Library,
    song_id: int,
    *,
    status_code: int = 200,
    changed: bool = False,
    form: FormData | None = None,
    **extra,
) -> HTMLResponse:
    """The song's videos card; ``extra`` can say which video is being edited (``editing``) and
    what went wrong (``error`` for the add form, ``edit_error`` for the edit one)."""
    return render(
        request,
        "_videos.html",
        status_code=status_code,
        trigger=("videos-changed",) if changed else (),
        song_id=song_id,
        videos=lib.list_videos(song_id),
        form=form or {},
        **extra,
    )


def video_from_form(form: FormData) -> SongVideoIn:
    start = _duration(form, "start")
    return SongVideoIn(
        youtube=_text(form, "youtube") or "", title=_text(form, "title"), start_seconds=start
    )


@router.get("/fragments/songs/{song_id}/videos")
def song_videos(request: Request, lib: Lib, song_id: int):
    found(lib.get_song, song_id)
    return _videos(request, lib, song_id)


@router.post("/songs/{song_id}/videos")
async def add_song_video(request: Request, lib: Lib, song_id: int):
    form = await request.form()
    found(lib.get_song, song_id)
    try:
        lib.add_video(song_id, video_from_form(form))
    except ValueError as exc:
        return _videos(request, lib, song_id, status_code=422, error=str(exc), form=form)
    return _videos(request, lib, song_id, changed=True)


@router.get("/fragments/videos/{video_id}/edit")
def video_edit(request: Request, lib: Lib, video_id: int):
    video = found(lib.get_video, video_id)
    return _videos(request, lib, video.song_id, editing=video_id)


@router.post("/videos/{video_id}")
async def update_video(request: Request, lib: Lib, video_id: int):
    form = await request.form()
    video = found(lib.get_video, video_id)
    try:
        lib.update_video(video_id, video_from_form(form))
    except ValueError as exc:
        return _videos(
            request, lib, video.song_id, status_code=422, editing=video_id, edit_error=str(exc)
        )
    return _videos(request, lib, video.song_id, changed=True)


@router.post("/videos/{video_id}/move")
async def move_video(request: Request, lib: Lib, video_id: int):
    form = await request.form()
    video = found(lib.get_video, video_id)
    try:
        offset = _int(form, "offset") or 0
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    lib.move_video(video_id, offset)
    return _videos(request, lib, video.song_id, changed=True)


@router.post("/videos/{video_id}/delete")
def delete_video(request: Request, lib: Lib, video_id: int):
    video = found(lib.get_video, video_id)
    lib.delete_video(video_id)
    return _videos(request, lib, video.song_id, changed=True)


@router.get("/fragments/songs/{song_id}/info")
def song_info(request: Request, lib: Lib, song_id: int):
    return render(request, "_song_info.html", **_song_info_context(lib, song_id))


@router.get("/fragments/songs/{song_id}/details")
def song_details_edit(request: Request, lib: Lib, song_id: int):
    song = found(lib.get_song, song_id)
    return render(
        request,
        "_song_details_edit.html",
        song=song,
        form={"notes": song.notes or "", "mv": song.mv or ""},
    )


@router.post("/songs/{song_id}/details")
async def update_song_details(request: Request, lib: Lib, song_id: int):
    """Change only the notes and the music video of a song, from its page."""
    form = await request.form()
    song = found(lib.get_song, song_id)
    data = song_in(song)
    data.notes = _text(form, "notes")
    data.mv = _text(form, "mv")
    try:
        lib.update_song(song_id, data)
    except ValueError as exc:
        return render(
            request,
            "_song_details_edit.html",
            status_code=422,
            error=str(exc),
            song=song,
            form=form,
        )
    return render(
        request,
        "_song_info.html",
        trigger=("library-changed",),
        **_song_info_context(lib, song_id),
    )


@router.post("/songs/{song_id}/playlists")
async def add_song_to_playlist(request: Request, lib: Lib, song_id: int):
    form = await request.form()
    found(lib.get_song, song_id)
    playlist_id = _int(form, "playlist_id")
    if playlist_id is None:
        context = _song_playlists_context(lib, song_id, error="Pick a playlist.")
        return render(request, "_playlist_add.html", status_code=422, **context)
    playlist = found(lib.get_playlist, playlist_id)
    added = lib.add_to_playlist(playlist_id, [song_id])
    message = f"Added to {playlist.name}." if added else f"It is already in {playlist.name}."
    return render(
        request,
        "_playlist_add.html",
        trigger=("playlists-changed", "library-changed"),
        **_song_playlists_context(lib, song_id, message=message, selected=playlist_id),
    )


@router.get("/fragments/songs/{song_id}/lyrics")
def song_lyrics(request: Request, lib: Lib, song_id: int):
    return render(request, "_lyrics.html", **_lyrics_context(lib, song_id))


@router.post("/songs/{song_id}/lyrics/open")
def open_lyrics(request: Request, lib: Lib, song_id: int):
    # Only the page's own buttons may open files: other sites cannot send this header.
    if "hx-request" not in request.headers:
        raise HTTPException(403, "open the lyrics file from the song page")
    path = found(lib.ensure_lyrics_file, song_id)
    if path is None:
        raise HTTPException(404, "the library has no folder for lyrics files")
    try:
        editor.open_file(path)
    except OSError as exc:
        return render(request, "_lyrics.html", **_lyrics_context(lib, song_id, error=str(exc)))
    return render(request, "_lyrics.html", **_lyrics_context(lib, song_id, opened=True))


@router.get("/fragments/songs/{song_id}/tab")
def song_tab(request: Request, lib: Lib, song_id: int):
    return render(request, "_tab.html", **_tab_context(lib, song_id))


@router.post("/songs/{song_id}/tab/open")
def open_tab(request: Request, lib: Lib, song_id: int):
    # Only the page's own buttons may open files: other sites cannot send this header.
    if "hx-request" not in request.headers:
        raise HTTPException(403, "open the tab from the song page")
    path = found(lib.tab_path, song_id)
    if path is None:
        raise HTTPException(404, "the song has no tab")
    try:
        editor.open_file(path)
    except OSError as exc:
        return render(request, "_tab.html", **_tab_context(lib, song_id, error=str(exc)))
    return render(request, "_tab.html", **_tab_context(lib, song_id, opened=True))


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
    try:
        data = song_from_form(form)
        data.artist_id = song.artist_id
        data.rating = song.rating  # set with the stars (see ``set_rating``)
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
        trigger=("playlists-changed", "library-changed"),
        s=lib.get_song_summary(song_id),
        ctx=_ctx(form),
    )


@router.post("/songs/{song_id}/play")
async def play_song(request: Request, lib: Lib, song_id: int):
    """Log a listen; this also clears the song's listen later mark."""
    form = await request.form()
    found(lib.record_play, song_id)
    ctx = _ctx(form)
    if ctx == "page":
        return render(
            request, "_song_info.html", trigger=PLAY_TRIGGER, **_song_info_context(lib, song_id)
        )
    return render(
        request,
        "_song_row.html",
        trigger=PLAY_TRIGGER,
        s=lib.get_song_summary(song_id),
        ctx=ctx,
        playlist_id=_int(form, "playlist_id"),
    )


@router.post("/songs/{song_id}/delete")
def delete_song(lib: Lib, song_id: int):
    found(lib.delete_song, song_id)
    return Response(headers={"HX-Trigger": "songs-changed, library-changed"})


# Ratings


@router.post("/rating/{kind}/{record_id}")
async def set_rating(request: Request, lib: Lib, kind: str, record_id: int):
    """Rate with the stars; an empty ``rating`` takes the rating away."""
    if kind not in ("artist", "album", "song"):
        raise HTTPException(404)
    form = await request.form()
    try:
        rating = found(lib.set_rating, kind, record_id, _int(form, "rating"))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return render(
        request,
        "_rating.html",
        trigger=("library-changed",),
        kind=kind,
        record_id=record_id,
        rating=rating,
        size="big" if form.get("size") == "big" else None,
    )


# Favorites and listen later


def _mark_trigger(kind: str) -> tuple[str, ...]:
    # Songs' marks decide what the built-in playlists hold.
    return ("playlists-changed", "library-changed") if kind == "song" else ("library-changed",)


@router.post("/later/{kind}/{record_id}")
def toggle_listen_later(request: Request, lib: Lib, kind: str, record_id: int):
    if kind not in ("artist", "album", "song"):
        raise HTTPException(404)
    later = found(lib.toggle, kind, record_id, "listen_later")
    return render(
        request,
        "_later.html",
        trigger=_mark_trigger(kind),
        kind=kind,
        record_id=record_id,
        later=later,
    )


@router.post("/favorite/{kind}/{record_id}")
def toggle_favorite(request: Request, lib: Lib, kind: str, record_id: int):
    if kind not in ("artist", "album", "song"):
        raise HTTPException(404)
    favorite = found(lib.toggle_favorite, kind, record_id)
    return render(
        request,
        "_fav.html",
        trigger=_mark_trigger(kind),
        kind=kind,
        record_id=record_id,
        favorite=favorite,
    )


# Playlists


@router.get("/playlists")
def playlists_page(request: Request, lib: Lib):
    return render(request, "playlists.html", playlists=lib.list_playlists(), form={})


@router.get("/fragments/playlists")
def playlists_fragment(request: Request, lib: Lib):
    return render(request, "_playlists.html", playlists=lib.list_playlists())


@router.post("/playlists")
async def create_playlist(request: Request, lib: Lib):
    form = await request.form()
    try:
        lib.create_playlist(PlaylistIn(_text(form, "name") or ""))
    except ValueError as exc:
        return render(request, "_playlist_form.html", status_code=422, error=str(exc), form=form)
    return render(request, "_playlist_form.html", trigger=("playlists-changed",), form={})


def _playlist_context(lib: Library, playlist_id: int) -> dict:
    detail = found(lib.get_playlist_detail, playlist_id)
    return {
        "playlist": detail.playlist,
        "songs": detail.songs,
        "total_seconds": sum(s.song.duration_seconds or 0 for s in detail.songs),
    }


@router.get("/playlists/{playlist_id}")
def playlist_page(request: Request, lib: Lib, playlist_id: int):
    return render(request, "playlist.html", ctx="playlist", **_playlist_context(lib, playlist_id))


@router.get("/fragments/playlists/{playlist_id}/info")
def playlist_info(request: Request, lib: Lib, playlist_id: int):
    return render(request, "_playlist_info.html", **_playlist_context(lib, playlist_id))


@router.get("/fragments/playlists/{playlist_id}/edit")
def playlist_edit(request: Request, lib: Lib, playlist_id: int):
    return render(request, "_playlist_edit.html", playlist=found(lib.get_playlist, playlist_id))


@router.get("/fragments/playlists/{playlist_id}/songs")
def playlist_songs(request: Request, lib: Lib, playlist_id: int):
    return render(
        request,
        "_songs.html",
        ctx="playlist",
        playlist_id=playlist_id,
        songs=found(lib.get_playlist_detail, playlist_id).songs,
    )


@router.post("/playlists/{playlist_id}")
async def update_playlist(request: Request, lib: Lib, playlist_id: int):
    form = await request.form()
    playlist = found(lib.get_playlist, playlist_id)
    try:
        lib.update_playlist(playlist_id, PlaylistIn(_text(form, "name") or ""))
    except ValueError as exc:
        return render(
            request, "_playlist_edit.html", status_code=422, error=str(exc), playlist=playlist
        )
    return render(
        request,
        "_playlist_info.html",
        trigger=("playlists-changed",),
        **_playlist_context(lib, playlist_id),
    )


@router.post("/playlists/{playlist_id}/delete")
def delete_playlist(lib: Lib, playlist_id: int):
    try:
        found(lib.delete_playlist, playlist_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return Response(headers={"HX-Redirect": "/playlists"})


# How many songs a search in a playlist's Add songs form shows, and how many characters it needs.
SONG_RESULTS = 15
MIN_SEARCH = 3


def _add_songs_context(lib: Library, playlist_id: int, params: ImmutableMultiDict, **extra) -> dict:
    """A playlist's Add songs form: the songs listed to add (``song_id``, with ``add`` put on
    the list and ``drop`` taken off it) and the songs matching the search ``q`` that are neither
    on that list nor in the playlist yet (none when ``q`` has under ``MIN_SEARCH`` characters
    besides spaces)."""
    playlist = found(lib.get_playlist, playlist_id)
    ids = []
    for value in [*params.getlist("song_id"), params.get("add")]:
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            pass
    drop = _id(params, "drop")
    pending = []
    for song_id in dict.fromkeys(ids):
        if song_id != drop:
            try:
                pending.append(lib.get_song_summary(song_id))
            except NotFound:
                pass  # deleted since it was listed
    q = _text(params, "q")
    # Spaces do not count: "a b" is too short.
    too_short = bool(q) and len("".join(q.split())) < MIN_SEARCH
    results = []
    if q and not too_short:
        skip = {s.song.id for s in pending} | set(lib.playlist_song_ids(playlist_id))
        results = [s for s in lib.list_songs(q=q) if s.song.id not in skip][:SONG_RESULTS]
    return {
        "playlist": playlist,
        "pending": pending,
        "q": q or "",
        "too_short": too_short,
        "min_search": MIN_SEARCH,
        "results": results,
        **extra,
    }


@router.get("/fragments/playlists/{playlist_id}/add-songs")
def playlist_add_songs(request: Request, lib: Lib, playlist_id: int):
    """The Add songs form after a song was put on its list or taken off, or the list dropped."""
    context = _add_songs_context(lib, playlist_id, request.query_params)
    return render(request, "_playlist_songs_add.html", **context)


@router.get("/fragments/playlists/{playlist_id}/add-songs/results")
def playlist_song_results(request: Request, lib: Lib, playlist_id: int):
    context = _add_songs_context(lib, playlist_id, request.query_params)
    return render(request, "_playlist_song_results.html", **context)


@router.post("/playlists/{playlist_id}/songs")
async def add_songs_to_playlist(request: Request, lib: Lib, playlist_id: int):
    """Add the songs listed in the Add songs form, in order, and empty the list."""
    form = await request.form()
    context = _add_songs_context(lib, playlist_id, form)
    if not context["pending"]:
        context["error"] = "List some songs to add first."
        return render(request, "_playlist_songs_add.html", status_code=422, **context)
    pending = context["pending"]
    added = lib.add_to_playlist(playlist_id, [s.song.id for s in pending])
    message = f"Added {added} {'song' if added == 1 else 'songs'} to {context['playlist'].name}."
    if added < len(pending):
        message += f" {len(pending) - added} of them were already there."
    return render(
        request,
        "_playlist_songs_add.html",
        trigger=("playlists-changed", "library-changed"),
        **_add_songs_context(lib, playlist_id, ImmutableMultiDict(), message=message),
    )


@router.post("/playlists/{playlist_id}/songs/{song_id}/remove")
def remove_from_playlist(lib: Lib, playlist_id: int, song_id: int):
    found(lib.remove_from_playlist, playlist_id, song_id)
    # Taking a song out of Favorites or Listen Later also clears that mark.
    return Response(headers={"HX-Trigger": "playlists-changed, library-changed"})


@router.post("/playlists/{playlist_id}/songs/{song_id}/move")
async def move_in_playlist(request: Request, lib: Lib, playlist_id: int, song_id: int):
    form = await request.form()
    try:
        offset = _int(form, "offset") or 0
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    found(lib.move_in_playlist, playlist_id, song_id, offset)
    return Response(headers={"HX-Trigger": "playlists-changed"})


# Live Play


def _live_songs(lib: Library, kind: str, record_id: int) -> tuple[str, str, list[int]]:
    """The title, link and song ids of what is played live: an album or a playlist."""
    if kind == "album":
        album = found(lib.get_album, record_id)
        songs = lib.list_songs(album_id=record_id, order="track")
        return album.title, f"/albums/{record_id}", [s.song.id for s in songs]
    if kind == "playlist":
        playlist = found(lib.get_playlist, record_id)
        return playlist.name, f"/playlists/{record_id}", lib.playlist_song_ids(record_id)
    raise HTTPException(404)


def _queue(value: str | None) -> list[int]:
    """The song ids in a ``queue`` value; anything else in it is left out."""
    return [int(part) for part in (value or "").split(",") if part.isdecimal()]


def _live_url(kind: str, record_id: int, queue: list[int], at: int) -> str:
    return f"/live/{kind}/{record_id}?{urlencode({'queue': _queue_text(queue), 'at': at})}"


def _queue_text(queue: list[int]) -> str:
    return ",".join(str(song_id) for song_id in queue)


def _live_context(lib: Library, kind: str, record_id: int, queue: list[int], at: int) -> dict:
    """Song number ``at`` (from 1, kept within the list) of the ``queue``.

    The queue is fixed when Live Play starts and travels with the page: playing a song takes it
    out of Listen Later, and a playlist that shrinks under it would skip songs."""
    title, link, current = _live_songs(lib, kind, record_id)
    queue = queue or current
    summaries = {s.song.id: s for s in lib.list_songs(song_ids=queue)}
    songs = [summaries[song_id] for song_id in queue if song_id in summaries]  # some may be gone
    at = min(max(at, 1), max(len(songs), 1))
    context = {"s": None, "lyrics_path": None}
    if songs:
        context = _lyrics_context(lib, songs[at - 1].song.id)
    return {
        **context,
        "kind": kind,
        "record_id": record_id,
        "title": title,
        "link": link,
        "songs": songs,
        "at": at,
        "queue": _queue_text([s.song.id for s in songs]),
    }


@router.get("/live/{kind}/{record_id}")
def live_page(request: Request, lib: Lib, kind: str, record_id: int, queue: str = "", at: int = 1):
    """Shows where Live Play is; reloading this page logs nothing."""
    return render(request, "live.html", **_live_context(lib, kind, record_id, _queue(queue), at))


@router.post("/live/{kind}/{record_id}")
async def live_play(request: Request, lib: Lib, kind: str, record_id: int):
    """Put a song in Live Play and log a listen of it. Without a ``queue`` this starts Live Play
    from the album or playlist's page, opening it on its first song; with one it steps to song
    number ``at``."""
    form = await request.form()
    queue = _queue(_text(form, "queue"))
    start = not queue
    if start:
        queue = _live_songs(lib, kind, record_id)[2]
    if not queue:
        raise HTTPException(422, "there are no songs to play")
    at = min(max(_id(form, "at") or 1, 1), len(queue))
    url = _live_url(kind, record_id, queue, at)
    found(lib.record_play, queue[at - 1])
    if start:
        return Response(headers={"HX-Redirect": url})
    return render(
        request,
        "_live.html",
        trigger=PLAY_TRIGGER,
        headers={"HX-Push-Url": url},
        **_live_context(lib, kind, record_id, queue, at),
    )


# Budget


def _price(form: FormData, key: str) -> float | None:
    """Accept ``12.99``, ``12,99``, ``$ 1,299.00``, ``1.299,00`` and the like: the last ``.`` or
    ``,`` is the decimal point, and there are at most two decimals."""
    value = _text(form, key)
    if value is None:
        return None
    value = value.lstrip("$€£¥ ").replace(" ", "")
    whole, point, decimals = value.replace(",", ".").rpartition(".")
    if not point:
        whole, decimals = decimals, ""
    whole = whole.replace(".", "")
    if not (whole or decimals).isdigit() or not (decimals == "" or decimals.isdigit()):
        raise ValueError(f"{key} must be a number, like 12.99")
    if len(decimals) > 2:
        raise ValueError(f"{key} can have at most two decimals, like 12.99")
    return float(f"{whole or 0}.{decimals or 0}")


def _quantity(form: FormData) -> int:
    quantity = _int(form, "quantity")
    return 1 if quantity is None else quantity


def _id(form: FormData | dict, key: str) -> int | None:
    """An id picked in a form, or ``None`` when there is none (or it is not a number)."""
    try:
        return _int(form, key)
    except ValueError:
        return None


def _album_order(album: Album) -> tuple:
    return album.year is None, album.year or 0, album.title.casefold(), album.id


def _cart_picker(lib: Library, form: FormData | dict) -> dict:
    """The Band, Album and Format pickers of the Add to cart form. Each lists what fits the
    picks before it (only albums and formats not in the cart yet) and keeps its own pick while
    that still fits; a picker with a single choice picks it."""
    options = lib.cart_options()
    bands: dict[int, dict] = {}
    for o in options:
        band = bands.setdefault(
            o.album.artist_id, {"id": o.album.artist_id, "name": o.artist_name, "wanted": set()}
        )
        if o.state == "wanted":
            band["wanted"].add(o.album.id)
    artist_id = _id(form, "artist_id")
    if artist_id not in bands:
        artist_id = next(iter(bands)) if len(bands) == 1 else None
    albums: dict[int, dict] = {}
    for o in options:
        if o.album.artist_id == artist_id:
            album = albums.setdefault(o.album.id, {"album": o.album, "wanted": False})
            album["wanted"] |= o.state == "wanted"
    album_id = _id(form, "album_id")
    if album_id not in albums:
        album_id = next(iter(albums)) if len(albums) == 1 else None
    formats = [o for o in options if o.album.id == album_id]
    formats.sort(key=lambda o: list(FORMATS).index(o.format))
    fmt = form.get("format")
    if fmt not in [o.format for o in formats]:
        fmt = formats[0].format if len(formats) == 1 else None
    return {
        "bands": sorted(bands.values(), key=lambda b: b["name"].casefold()),
        # ``cart_options`` puts the wanted ones first; here they go by year and title.
        "albums": sorted(albums.values(), key=lambda a: _album_order(a["album"])),
        "formats": formats,
        "picked": {"artist_id": artist_id, "album_id": album_id, "format": fmt},
    }


def _budget(
    request: Request,
    lib: Library,
    *,
    status_code: int = 200,
    form: FormData | None = None,
    **extra,
) -> HTMLResponse:
    """The cart, its Add form and the saved budgets. ``extra`` can say which cart item is being
    edited (``editing``) and what went wrong: ``error`` for the Add form, ``edit_error`` for the
    item being edited, ``cart_error`` for the cart's buttons, ``budget_error`` for the budgets."""
    return render(
        request, "_budget.html", status_code=status_code, **_budget_context(lib, form), **extra
    )


def _budget_context(lib: Library, form: FormData | dict | None = None) -> dict:
    form = form or {}
    cart = lib.get_cart()
    return {
        "cart": cart,
        "budgets": lib.list_budgets(),
        "form": form,
        **_cart_picker(lib, form),
        # What Make Budget and Buy name the budget: the one being edited keeps its name.
        "budget_name": cart.editing.name if cart.editing else f"Budget {date.today()}",
    }


@router.get("/budget")
def budget_page(request: Request, lib: Lib):
    return render(request, "budget.html", **_budget_context(lib))


@router.get("/fragments/budget")
def budget_fragment(request: Request, lib: Lib):
    return _budget(request, lib)


@router.get("/fragments/budget/picker")
def cart_picker(request: Request, lib: Lib):
    """The Band, Album and Format pickers of the Add form, after one of them changed."""
    return render(request, "_cart_picker.html", **_cart_picker(lib, request.query_params))


@router.post("/budget/items")
async def add_to_cart(request: Request, lib: Lib):
    form = await request.form()
    try:
        album_id = _int(form, "album_id")
        if album_id is None:
            raise ValueError("Pick an album.")
        fmt = _text(form, "format")
        if fmt not in FORMATS:
            raise ValueError("Pick a format.")
        price = _price(form, "price")
        if price is None:
            raise ValueError("price is required")
        data = CartItemIn(album_id, fmt, price, _text(form, "link"), _quantity(form))
        found(lib.add_to_cart, data)
    except ValueError as exc:
        return _budget(request, lib, status_code=422, error=str(exc), form=form)
    # Keep the band picked, to add more of its albums.
    return _budget(request, lib, form={"artist_id": _text(form, "artist_id")})


@router.get("/fragments/budget/items/{item_id}/edit")
def cart_item_edit(request: Request, lib: Lib, item_id: int):
    found(lib.get_cart_item, item_id)
    return _budget(request, lib, editing=item_id)


@router.post("/budget/items/{item_id}")
async def update_cart_item(request: Request, lib: Lib, item_id: int):
    form = await request.form()
    item = found(lib.get_cart_item, item_id)
    try:
        price = _price(form, "price")
        if price is None:
            raise ValueError("price is required")
        data = replace(item, price=price, link=_text(form, "link"), quantity=_quantity(form))
        lib.update_cart_item(item_id, data)
    except ValueError as exc:
        return _budget(request, lib, status_code=422, editing=item_id, edit_error=str(exc))
    return _budget(request, lib)


@router.post("/budget/items/{item_id}/delete")
def remove_from_cart(request: Request, lib: Lib, item_id: int):
    found(lib.remove_from_cart, item_id)
    return _budget(request, lib)


@router.post("/budget/items/{item_id}/quantity")
async def change_cart_quantity(request: Request, lib: Lib, item_id: int):
    """The cart's + and - buttons; the page asks before - takes the last copy out."""
    form = await request.form()
    offset = 1 if _text(form, "offset") == "1" else -1
    found(lib.change_cart_quantity, item_id, offset)
    return _budget(request, lib)


@router.post("/budget/cart/shipping")
async def set_shipping(request: Request, lib: Lib):
    form = await request.form()
    try:
        lib.set_shipping(_price(form, "shipping") or 0.0)
    except ValueError as exc:
        return _budget(request, lib, status_code=422, cart_error=str(exc))
    return _budget(request, lib)


@router.post("/budget/cart/save")
async def save_cart(request: Request, lib: Lib):
    form = await request.form()
    try:
        budget = lib.make_budget(BudgetIn(_text(form, "name") or ""))
    except ValueError as exc:
        return _budget(request, lib, status_code=422, cart_error=str(exc))
    return _budget(request, lib, message=f"Saved {budget.name}.")


def _bought(request: Request, lib: Library, budget: Budget, overlaps: list[BudgetOverlap]):
    """The page after buying ``budget``, saying what was dropped from ``overlaps``."""
    message = f"Bought {budget.name}: its albums are now owned."
    if kept := [o.budget.name for o in overlaps if not o.emptied]:
        message += f" Took them out of {', '.join(kept)}."
    if emptied := [o.budget.name for o in overlaps if o.emptied]:
        message += f" Deleted {', '.join(emptied)}, which held nothing else."
    return _budget(request, lib, trigger=("albums-changed", "library-changed"), message=message)


def _others(form: FormData) -> str | None:
    """What to do with the albums being bought that other budgets hold too: "drop" them from
    those budgets, "keep" them, or ``None`` when the user was not asked yet."""
    others = _text(form, "others")
    return others if others in ("drop", "keep") else None


@router.post("/budget/cart/buy")
async def buy_cart(request: Request, lib: Lib):
    form = await request.form()
    name = _text(form, "name") or ""
    overlaps = lib.budget_overlaps()
    if overlaps and _others(form) is None and name:
        prompt = {"action": "/budget/cart/buy", "name": name, "overlaps": overlaps}
        return _budget(request, lib, buy_prompt=prompt)
    drop = _others(form) == "drop"
    try:
        budget = lib.buy_cart(BudgetIn(name), drop_from_others=drop)
    except ValueError as exc:
        return _budget(request, lib, status_code=422, cart_error=str(exc))
    return _bought(request, lib, budget, overlaps if drop else [])


@router.post("/budget/cart/delete")
def delete_cart(request: Request, lib: Lib):
    lib.clear_cart()
    return _budget(request, lib)


@router.post("/budget/budgets/{budget_id}/buy")
async def buy_budget(request: Request, lib: Lib, budget_id: int):
    form = await request.form()
    found(lib.get_budget, budget_id)
    overlaps = lib.budget_overlaps(budget_id)
    if overlaps and _others(form) is None:
        prompt = {"action": f"/budget/budgets/{budget_id}/buy", "overlaps": overlaps}
        return _budget(request, lib, buy_prompt=prompt)
    drop = _others(form) == "drop"
    try:
        budget = lib.buy_budget(budget_id, drop_from_others=drop)
    except ValueError as exc:
        return _budget(request, lib, status_code=422, budget_error=str(exc))
    return _bought(request, lib, budget, overlaps if drop else [])


@router.post("/budget/budgets/{budget_id}/{action}")
def budget_action(request: Request, lib: Lib, budget_id: int, action: str):
    actions = {"edit": lib.edit_budget, "delete": lib.delete_budget}
    if action not in actions:
        raise HTTPException(404)
    found(lib.get_budget, budget_id)
    try:
        actions[action](budget_id)
    except ValueError as exc:
        return _budget(request, lib, status_code=422, budget_error=str(exc))
    return _budget(request, lib)


# Concerts


def days_until(day: date) -> str:
    days = (day - date.today()).days
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    return f"in {days} days" if days > 0 else f"{-days} days ago"


templates.env.filters.update(days_until=days_until)


def _concerts_context(lib: Library, **extra) -> dict:
    """The concerts in the info folder's ``concerts.csv``, read again every time."""
    path = lib.info_dir / CONCERTS_FILE if lib.info_dir else None
    concerts, warnings = [], []
    if path:
        try:
            concerts, warnings = read_concerts(path)
        except (OSError, UnicodeDecodeError) as exc:
            warnings.append(f"Could not read {path}: {exc}")
    today = date.today()
    return {
        "path": path,
        "found_file": bool(path and path.is_file()),
        "today": today,
        "upcoming": [c for c in concerts if c.date >= today],
        "past": [c for c in reversed(concerts) if c.date < today],
        "artists": {a.artist.name.casefold(): a.artist for a in lib.list_artists()},
        "warnings": warnings,
        "concert_form": {},
        "tour_form": {},
        **extra,
    }


@router.get("/concerts")
def concerts_page(request: Request, lib: Lib):
    return render(request, "concerts.html", **_concerts_context(lib))


def _concerts_file(request: Request, lib: Library) -> Path:
    # Only the page's own forms may write the file: other sites cannot send this header.
    if "hx-request" not in request.headers:
        raise HTTPException(403, "change concerts from the Concerts page")
    if lib.info_dir is None:
        raise HTTPException(404, "the library has no info folder")
    return lib.info_dir / CONCERTS_FILE


def _band(form: FormData) -> str:
    band = _text(form, "band")
    if band is None:
        raise ValueError("band is required")
    return band


@router.post("/concerts")
async def add_concert(request: Request, lib: Lib):
    path = _concerts_file(request, lib)
    form = await request.form()
    try:
        try:
            day = date.fromisoformat(_text(form, "date") or "")
        except ValueError:
            raise ValueError("date must look like 2026-12-09") from None
        band = _band(form)
        concert = Concert(
            day, band, _text(form, "country"), _text(form, "city"), tour=_text(form, "tour")
        )
        add_concerts(path, [concert])
    except (OSError, ValueError) as exc:
        context = _concerts_context(lib, concert_error=str(exc), concert_form=form)
        return render(request, "_concerts.html", status_code=422, **context)
    context = _concerts_context(lib, message=f"Added {band} on {day}.")
    return render(request, "_concerts.html", **context)


@router.post("/concerts/tours")
async def add_tour(request: Request, lib: Lib):
    """Add a concert for each date of a tour."""
    path = _concerts_file(request, lib)
    form = await request.form()
    try:
        band = _band(form)
        tour = _text(form, "tour")
        if tour is None:
            raise ValueError("tour is required")
        dates = parse_tour_dates(_text(form, "dates") or "")
        add_concerts(
            path,
            [Concert(day, band, country, city, tour=tour) for day, city, country in dates],
        )
    except (OSError, ValueError) as exc:
        context = _concerts_context(lib, tour_error=str(exc), tour_form=form)
        return render(request, "_concerts.html", status_code=422, **context)
    noun = "date" if len(dates) == 1 else "dates"
    context = _concerts_context(lib, message=f"Added {tour} with {len(dates)} {noun}.")
    return render(request, "_concerts.html", **context)


@router.post("/concerts/{line}/status")
async def set_concert_status(request: Request, lib: Lib, line: int):
    """Mark a concert as nothing, want to go or going (went, once it is over) in the file."""
    path = _concerts_file(request, lib)
    form = await request.form()
    try:
        day = date.fromisoformat(_text(form, "date") or "")
        status = _text(form, "status") or ""
        set_status(path, line, day, _text(form, "band") or "", status)
    except (OSError, ValueError) as exc:
        context = _concerts_context(lib, error=str(exc))
        return render(request, "_concerts.html", status_code=422, **context)
    return render(request, "_concerts.html", **_concerts_context(lib))
