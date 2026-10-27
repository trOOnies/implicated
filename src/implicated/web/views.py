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

from dataclasses import fields
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.datastructures import FormData

from implicated.api import get_library
from implicated.library import Library, NotFound
from implicated.models import (
    ALBUM_KINDS,
    OWNERSHIP,
    Album,
    AlbumIn,
    ArtistIn,
    Member,
    PlaylistIn,
    Point,
    Song,
    SongIn,
    SongVideoIn,
)
from implicated.web import editor

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


templates.env.filters.update(
    duration=duration, stars=stars, when=when, tenure=tenure, percent=percent, formats=formats
)
templates.env.globals.update(album_kinds=ALBUM_KINDS, ownership=OWNERSHIP)


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
        notes=_text(form, "notes"),
        listen_later="listen_later" in form,
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


# Artists


def _artist_options(lib: Library) -> dict[str, list[str]]:
    """The origins, genres and languages of every artist, for the Artists page filters."""
    artists = [a.artist for a in lib.list_artists()]

    def sorted_set(values) -> list[str]:
        unique = {v.casefold(): v for v in values if v}
        return sorted(unique.values(), key=str.casefold)

    return {
        "countries": sorted_set(a.country for a in artists),
        "genres": sorted_set(g for a in artists for g in a.genres),
        "languages": sorted_set(lang for a in artists for lang in a.languages),
    }


def _artists_query(
    q: str | None, country: str | None, genre: str | None, language: str | None
) -> dict:
    return {"q": q or None, "country": country, "genre": genre, "language": language}


@router.get("/artists")
def artists_page(
    request: Request,
    lib: Lib,
    q: str | None = None,
    country: str | None = None,
    genre: str | None = None,
    language: str | None = None,
):
    return render(
        request,
        "artists.html",
        artists=lib.list_artists(**_artists_query(q, country, genre, language)),
        q=q or "",
        country=country or "",
        genre=genre or "",
        language=language or "",
        **_artist_options(lib),
    )


@router.get("/fragments/artists")
def artists_fragment(
    request: Request,
    lib: Lib,
    q: str | None = None,
    country: str | None = None,
    genre: str | None = None,
    language: str | None = None,
):
    artists = lib.list_artists(**_artists_query(q, country, genre, language))
    return render(request, "_artists.html", artists=artists)


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
        albums=lib.list_albums(**_albums_query(q, own, favorites, later, missing, nocover)),
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
    return render(request, "_album_grid.html", albums=albums)


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
        "playlists": lib.list_playlists(),
        "album": album,
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


def _songs_query(
    q: str | None, favorites: str | None, later: str | None, missing: str | None
) -> dict:
    return {
        "q": q or None,
        "favorites": bool(favorites),
        "listen_later": bool(later),
        "missing_lyrics": bool(missing),
    }


@router.get("/songs")
def songs_page(
    request: Request,
    lib: Lib,
    q: str | None = None,
    favorites: str | None = None,
    later: str | None = None,
    missing: str | None = None,
    order: str = "title",
):
    order = order if order in ("title", "plays", "recent") else "title"
    songs = lib.list_songs(order=order, **_songs_query(q, favorites, later, missing))
    return render(
        request,
        "songs.html",
        songs=songs,
        q=q or "",
        favorites=bool(favorites),
        later=bool(later),
        missing=bool(missing),
        order=order,
        ctx="all",
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
):
    order = order if order in ("title", "plays", "recent") else "title"
    songs = lib.list_songs(order=order, **_songs_query(q, favorites, later, missing))
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
    """Where a song row is shown; "page" is the song's own page, "playlist" a playlist's."""
    ctx = form_or_query.get("ctx") or "all"
    return ctx if ctx in ("all", "artist", "album", "playlist", "page") else "all"


def _lyrics_context(lib: Library, song_id: int, **extra) -> dict:
    """The song with its lyrics read again from its file, so edits show straight away."""
    try:
        found(lib.reload_lyrics, song_id)
    except (OSError, ValueError) as exc:
        extra.setdefault("error", f"Could not read the lyrics file: {exc}")
    return {"s": lib.get_song_summary(song_id), "lyrics_path": lib.lyrics_path(song_id), **extra}


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
    trigger = ("plays-changed", "playlists-changed", "library-changed")
    ctx = _ctx(form)
    if ctx == "page":
        return render(
            request, "_song_info.html", trigger=trigger, **_song_info_context(lib, song_id)
        )
    return render(
        request,
        "_song_row.html",
        trigger=trigger,
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
