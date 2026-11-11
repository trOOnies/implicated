"""HTML pages and htmx fragments.

Mutations answer with the fragment that replaces the element that sent them and an
``HX-Trigger`` header naming the events other sections listen to (``hx-trigger="... from:body"``)
so they refresh themselves:

* ``artists-changed``, ``albums-changed``, ``songs-changed``: a list needs reloading
* ``plays-changed``: listening history changed
* ``playlists-changed``: a playlist, or the songs in it, changed (also when a song's favorite or
  listen later mark changes, since the built-in playlists follow them)
* ``videos-changed``: a song's videos changed
* ``pictures-changed``: an artist's pictures changed
* ``library-changed``: anything changed; the overview (the page the logo opens) listens to it
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from collections.abc import Iterable
from dataclasses import fields, replace
from datetime import date, datetime, timezone
from html import escape
from math import ceil
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from starlette.background import BackgroundTask
from starlette.datastructures import FormData, ImmutableMultiDict

from implicated import backup, duo, inbox
from implicated.api import get_library
from implicated.concerts import (
    CONCERTS_FILE,
    TOURS_FILE,
    add_concerts,
    read_concerts,
    read_tours,
    rename_setlist,
    save_tour,
    set_setlist,
    set_status,
)
from implicated.importer import accept, import_dir
from implicated.library import (
    COVER_TYPES,
    IMAGE_TYPES,
    Library,
    NotFound,
    check_cover_name,
)
from implicated.models import (
    ALBUM_KINDS,
    CONCERT_STATUSES,
    FORMATS,
    MAX_TIERS,
    MIN_TIERS,
    OWNERSHIP,
    TIER_NAME_LENGTH,
    TIER_OWNERS,
    Album,
    AlbumIn,
    ArtistIn,
    Budget,
    BudgetIn,
    BudgetOverlap,
    CartItemIn,
    Concert,
    Member,
    PictureIn,
    Playlist,
    PlaylistIn,
    PlaylistSummary,
    Point,
    Song,
    SongIn,
    SongVideoIn,
    TierList,
)
from implicated.userdata import ImportReport, export_data, import_data, read_file
from implicated.web import editor, shutdown

logger = logging.getLogger("uvicorn.error")  # like the app's own messages
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


def ago(timestamp: str | None) -> str:
    """How long ago ``timestamp`` was, short enough for a tile: ``5m``, ``3h``, ``2d``, ``6w``,
    then the month (``Mar 2025``)."""
    if not timestamp:
        return "never"
    then = datetime.fromisoformat(timestamp)
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    minutes = max(int((datetime.now(timezone.utc) - then).total_seconds() // 60), 0)
    if minutes >= 60 * 24 * 7 * 9:
        return then.strftime("%b %Y")
    for size, unit in ((60 * 24 * 7, "w"), (60 * 24, "d"), (60, "h"), (1, "m")):
        if minutes >= size:
            return f"{minutes // size}{unit} ago"
    return "just now"


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
    ago=ago,
    tenure=tenure,
    percent=percent,
    formats=formats,
    money=money,
)
templates.env.globals.update(
    asset=asset,
    album_kinds=ALBUM_KINDS,
    # What the cover pickers accept.
    cover_accept=",".join(COVER_TYPES),
    # What the picture pickers accept.
    picture_accept=",".join(IMAGE_TYPES),
    ownership=OWNERSHIP,
    format_names=FORMATS,
    # The latest year a band can have formed in.
    this_year=lambda: date.today().year,
    concert_statuses=CONCERT_STATUSES,
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


def picture_from_form(form: FormData) -> PictureIn:
    return PictureIn(
        name=_text(form, "name") or "",
        description=_text(form, "description"),
        tags=_csv(form, "tags"),
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
        explicit="explicit" in form,
        mv=_text(form, "mv"),
        listen_later="listen_later" in form,
    )


def song_in(song: Song) -> SongIn:
    """The writable fields of ``song``, to change some of them."""
    return SongIn(**{f.name: getattr(song, f.name) for f in fields(SongIn)})


# Dashboard


# The Overview's showcase: a grid of artists, albums or songs (its Type) in one of the Orders
# of that type, the first one unless asked for another. Each Order is (label, how the library
# sorts them, the list page with the rest of them or None). "plays", "recent" and "rating" show
# only the played or rated ones.
SHOWCASE_ORDERS = {
    "album": {
        "latest": ("Latest releases", "latest", "/albums?order=latest"),
        "favorites": ("Favorites", "latest", "/albums?favorites=1"),
        "later": ("Listen Later", "latest", "/albums?later=1"),
        "recent": ("Recently played", "recent", None),
        "plays": ("Most played", "plays", None),
        "rating": ("Highest rated", "rating", None),
    },
    "artist": {
        "favorites": ("Favorites", "name", "/artists?favorites=1"),
        "later": ("Listen Later", "name", "/artists?later=1"),
        "plays": ("Most played", "plays", None),
        "rating": ("Highest rated", "rating", None),
    },
    "song": {
        "favorites": ("Favorites", "plays", "/songs?favorites=1&order=plays"),
        "later": ("Listen Later", "title", "/songs?later=1"),
        "recent": ("Recently played", "recent", "/songs?order=recent"),
        "plays": ("Most played", "plays", "/songs?order=plays"),
        "rating": ("Highest rated", "rating", None),
    },
}
SHOWCASE_TYPES = {"artist": "Artist", "album": "Album", "song": "Song"}
# How many records the showcase shows; the rest are a See more... link away, where there is one.
SHOWCASE_SIZE = 30  # 10 columns of 3 at full width, 6 of 5 at half


def _showcase(lib: Library, kind: str | None, order: str | None) -> dict:
    """The showcase of ``kind`` records (an album when not one of ``SHOWCASE_TYPES``) in
    ``order`` (the first of that type's when not one of them), each as a tile: its page, its
    picture or cover (``None`` for the ♪ placeholder), its name and the line under it."""
    kind = kind if kind in SHOWCASE_TYPES else "album"
    order = order if order in SHOWCASE_ORDERS[kind] else next(iter(SHOWCASE_ORDERS[kind]))
    filters = {"favorites": order == "favorites", "listen_later": order == "later"}
    label, sort, more = SHOWCASE_ORDERS[kind][order]
    limit = SHOWCASE_SIZE + 1
    if kind == "artist":
        records = lib.list_artists(**filters, order=sort, limit=limit)
        tiles = [
            {
                "href": f"/artists/{a.artist.id}",
                "image": a.artist.image and f"/api/artists/{a.artist.id}/image?v={a.artist.image}",
                "name": a.artist.name,
                "note": None,
                "kind": "artist",
                "id": a.artist.id,
                "favorite": a.artist.favorite,
                "later": a.artist.listen_later,
                "plays": a.play_count,
                "last_played": None,
                "rating": a.artist.rating,
                "year": None,
            }
            for a in records
        ]
    elif kind == "album":
        records = lib.list_albums(**filters, order=sort, mains_only=True, limit=limit)
        tiles = [
            {
                "href": f"/albums/{a.album.id}",
                "image": a.album.cover and f"/api/albums/{a.album.id}/cover?v={a.album.cover}",
                "name": a.album.title,
                "note": a.artist_name,
                "kind": "album",
                "id": a.album.id,
                "favorite": a.album.favorite,
                "later": a.album.listen_later,
                "plays": a.play_count,
                "last_played": a.last_played,
                "rating": a.album.rating,
                "year": a.album.year,
            }
            for a in records
        ]
    else:
        records = lib.list_songs(**filters, order=sort, mains_only=True, limit=limit)
        tiles = [
            {
                "href": f"/songs/{s.song.id}",
                "image": s.song.album_id
                and s.album_cover
                and f"/api/albums/{s.song.album_id}/cover?v={s.album_cover}",
                "name": s.song.title,
                "note": s.artist_name,
                "kind": "song",
                "id": s.song.id,
                "favorite": s.song.favorite,
                "later": s.song.listen_later,
                "plays": s.play_count,
                "last_played": s.last_played,
                "rating": s.song.rating,
                "year": None,
            }
            for s in records
        ]
    # Sorted, so the unplayed or unrated ones come last: dropping them leaves the right ones.
    if order in ("plays", "recent"):
        tiles = [tile for tile in tiles if tile["plays"]]
    elif order == "rating":
        tiles = [tile for tile in tiles if tile["rating"]]
    return {
        "showcase_types": SHOWCASE_TYPES,
        "showcase_orders": {key: o[0] for key, o in SHOWCASE_ORDERS[kind].items()},
        "showcase_kind": kind,
        "showcase_order": order,
        "tiles": tiles[:SHOWCASE_SIZE],
        "see_more": more if len(tiles) > SHOWCASE_SIZE else None,
    }


def _view_context(lib: Library, kind: str | None = None, order: str | None = None) -> dict:
    return {"stats": lib.stats(), **_showcase(lib, kind, order)}


@router.get("/")
def index(request: Request, lib: Lib, type: str | None = None, order: str | None = None):
    return render(request, "index.html", **_view_context(lib, type, order))


@router.get("/fragments/view")
def view_fragment(request: Request, lib: Lib, type: str | None = None, order: str | None = None):
    return render(request, "_view.html", **_view_context(lib, type, order))


@router.get("/fragments/showcase")
def showcase_fragment(
    request: Request, lib: Lib, type: str | None = None, order: str | None = None
):
    return render(request, "_showcase.html", **_showcase(lib, type, order))


# Help: one page per section, in reading order, each with the index and links to the section
# before and after it. The first one, What is implicated?, is /help itself.
HELP_SECTIONS = tuple(
    {"slug": slug, "emoji": emoji, "title": title, "url": f"/help/{slug}" if slug else "/help"}
    for slug, emoji, title in (
        ("", "🎧", "What is implicated?"),
        ("pages", "🗺️", "Pages"),
        ("actions", "✨", "Everyday actions"),
        ("importing", "📥", "Importing from files"),
        ("backups", "📦", "Backups and exports"),
        ("api", "🔌", "API"),
        ("license", "📦", "Source &amp; license"),
    )
)


def help_url(section: str, anchor: str | None = None) -> str:
    """The address of Help's ``section`` page (one of the slugs of ``HELP_SECTIONS``; "" for the
    first one), at the entry with id ``anchor`` when given. For the Help icon (``help_link`` in
    _icons.html); a section that does not exist is a mistake, so it raises."""
    for each in HELP_SECTIONS:
        if each["slug"] == section:
            return f"{each['url']}#{anchor}" if anchor else each["url"]
    raise ValueError(f"no Help section {section!r}")


templates.env.globals["help_url"] = help_url


def _help_page(request: Request, i: int) -> HTMLResponse:
    return render(
        request,
        f"help/{HELP_SECTIONS[i]['slug'] or 'about'}.html",
        sections=HELP_SECTIONS,
        section=HELP_SECTIONS[i],
        prev=HELP_SECTIONS[i - 1] if i else None,
        next=HELP_SECTIONS[i + 1] if i + 1 < len(HELP_SECTIONS) else None,
    )


@router.get("/help")
def help_page(request: Request):
    return _help_page(request, 0)


@router.get("/help/{slug}")
def help_section(request: Request, slug: str):
    for i, section in enumerate(HELP_SECTIONS):
        if i and section["slug"] == slug:
            return _help_page(request, i)
    raise HTTPException(404, "No such help section")


# Duo: your data, and recommendations from it and a friend's

# What importing your data changes: every list.
IMPORT_TRIGGER = (
    "artists-changed", "albums-changed", "songs-changed", "plays-changed", "playlists-changed",
    "videos-changed", "library-changed",
)  # fmt: skip


def _duo_context(lib: Library, friend: str | None, **extra) -> dict:
    friends = duo.list_friends(lib)
    chosen = next((f for f in friends if f.slug == friend), None)
    if chosen is None and friend is None and friends:
        chosen = friends[0]
    comparison = None
    if chosen is not None:
        comparison = duo.compare(
            export_data(lib), duo.load_friend(lib, chosen.slug), duo.library_ids(lib)
        )
    return {
        "stats": lib.stats(),
        "friends": friends,
        "friend": chosen,
        "duo": comparison,
        "duo_shown": DUO_SHOWN,
        **extra,
    }


# How many records each Duo section shows before the rest is folded away.
DUO_SHOWN = 8


@router.get("/data")
def data_page():
    """The Duo page's old name."""
    return RedirectResponse("/duo", status_code=301)


@router.get("/duo")
def duo_page(request: Request, lib: Lib, friend: str | None = None):
    context = _duo_context(lib, friend)
    if friend and context["friend"] is None:
        context["friend_error"] = "That friend's file is gone; pick another one."
    return render(request, "duo.html", **context)


@router.post("/duo/friends")
async def add_friend(request: Request, lib: Lib):
    """Keep a friend's data file and show what you two have in common."""
    form = await request.form()
    upload = form.get("file")
    try:
        if upload is None or isinstance(upload, str) or not upload.filename:
            raise ValueError("Pick your friend's file first.")
        friend = duo.save_friend(lib, read_file(await upload.read()), _text(form, "name"))
    except ValueError as exc:
        context = _duo_context(lib, None, friend_error=f"{exc} Nothing was added.")
        return render(request, "duo.html", status_code=422, **context)
    return RedirectResponse(f"/duo?friend={friend.slug}", status_code=303)


@router.post("/duo/friends/{slug}/delete")
def delete_friend(lib: Lib, slug: str):
    """Forget a friend's file (your library never had any of it)."""
    try:
        duo.delete_friend(lib, slug)
    except FileNotFoundError as exc:
        raise HTTPException(404, "no such friend") from exc
    return RedirectResponse("/duo", status_code=303)


@router.post("/duo/friends/{slug}/playlists/{section_id}")
def duo_playlist(request: Request, lib: Lib, slug: str, section_id: str):
    """Make (or refill) a playlist of your songs in one of the Duo sections."""
    try:
        data = duo.load_friend(lib, slug)
    except (OSError, ValueError) as exc:
        raise HTTPException(404, "no such friend") from exc
    comparison = duo.compare(export_data(lib), data, duo.library_ids(lib))
    section = next((s for s in comparison.sections if s.id == section_id), None)
    if section is None or not section.song_ids:
        raise HTTPException(404, "no songs to make a playlist of")
    name = f"{comparison.friend} & me: {section.title}"
    existing = next(
        (p.playlist for p in lib.list_playlists()
         if not p.playlist.kind and p.playlist.name.casefold() == name.casefold()),
        None,
    )  # fmt: skip
    playlist = existing or lib.create_playlist(PlaylistIn(name))
    lib.set_playlist_songs(playlist.id, section.song_ids)
    return render(
        request,
        "_duo_playlist.html",
        playlist=playlist,
        count=len(section.song_ids),
        refilled=existing is not None,
        trigger=("playlists-changed", "library-changed"),
    )


@router.post("/duo/import")
async def import_data_file(request: Request, lib: Lib):
    """Import a file downloaded with Export: the result, and every list refreshes."""
    form = await request.form()
    upload = form.get("file")
    if upload is None or isinstance(upload, str) or not upload.filename:
        return render(request, "_data_import.html", status_code=422, error="Pick a file first.")
    try:
        report: ImportReport = import_data(lib, read_file(await upload.read()))
    except ValueError as exc:
        return render(request, "_data_import.html", status_code=422, error=str(exc))
    logger.info("Imported %s:\n%s", upload.filename, report.summary())
    return render(
        request,
        "_data_import.html",
        report=report,
        file_name=upload.filename,
        trigger=IMPORT_TRIGGER,
    )


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


# How many artists, albums and songs their pages show at a time.
ARTISTS_PER_PAGE = 24
PICTURES_PER_PAGE = 24
# Thumbnails of the newest pictures the artist page shows, next to the link to all of them.
ARTIST_PAGE_PICTURES = 6
ALBUMS_PER_PAGE = 48
SONGS_PER_PAGE = 50


def _artists_context(
    request: Request,
    lib: Library,
    q: str | None,
    country: str | None,
    genre: str | None,
    language: str | None,
    favorites: str | None,
    later: str | None,
    page: int,
) -> dict:
    query = {
        "q": q or None,
        "country": country,
        "genre": genre,
        "language": language,
        "favorites": bool(favorites),
        "listen_later": bool(later),
    }
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
    favorites: str | None = None,
    later: str | None = None,
    page: int = 1,
):
    return render(
        request,
        "artists.html",
        **_artists_context(request, lib, q, country, genre, language, favorites, later, page),
        q=q or "",
        country=country or "",
        genre=genre or "",
        language=language or "",
        favorites=bool(favorites),
        later=bool(later),
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
    favorites: str | None = None,
    later: str | None = None,
    page: int = 1,
):
    context = _artists_context(request, lib, q, country, genre, language, favorites, later, page)
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
        "has_songs": bool(lib.list_songs(artist_id=artist_id, limit=1)),
        "has_tier_list": lib.has_tier_list("artist", artist_id),
    }


def _artist_context(lib: Library, artist_id: int) -> dict:
    albums = lib.list_albums(artist_id)
    return {
        **_artist_info_context(lib, artist_id),
        "albums": albums,
        "featured": lib.featured_songs(artist_id),
        "album_plays": [Point(a.album.title, a.play_count) for a in albums if a.play_count],
        "pictures": lib.list_pictures(artist_id, limit=ARTIST_PAGE_PICTURES),
        "picture_count": lib.count_pictures(artist_id),
    }


@router.get("/artists/{artist_id}")
def artist_page(request: Request, lib: Lib, artist_id: int):
    return render(request, "artist.html", **_artist_context(lib, artist_id))


@router.get("/fragments/artists/{artist_id}/info")
def artist_info(request: Request, lib: Lib, artist_id: int):
    return render(request, "_artist_info.html", **_artist_info_context(lib, artist_id))


@router.post("/artists/{artist_id}/image")
async def set_artist_image(request: Request, lib: Lib, artist_id: int):
    """Upload a profile picture from the artist page: it replaces the current one, and is kept
    in the artist's Pictures too."""
    form = await request.form()
    found(lib.get_artist, artist_id)
    upload = form.get("image")
    try:
        # Browsers send an empty, nameless file when nothing was picked.
        if upload is None or isinstance(upload, str) or not upload.filename:
            raise ValueError("Pick an image first.")
        lib.set_image_data(artist_id, await upload.read(), upload.filename)
    except ValueError as exc:
        return render(
            request,
            "_artist_info.html",
            status_code=422,
            image_error=str(exc),
            **_artist_info_context(lib, artist_id),
        )
    return render(
        request,
        "_artist_info.html",
        trigger=("artists-changed", "pictures-changed", "library-changed"),
        **_artist_info_context(lib, artist_id),
    )


@router.get("/fragments/artists/{artist_id}/edit")
def artist_edit(request: Request, lib: Lib, artist_id: int):
    return render(request, "_artist_edit.html", artist=found(lib.get_artist, artist_id))


@router.get("/fragments/artists/{artist_id}/albums")
def artist_albums(
    request: Request, lib: Lib, artist_id: int, kind: str | None = None, mains: str | None = None
):
    """The artist's discography, of one ``kind`` of album when given, and with ``mains`` only
    the albums not merged into another (main editions and albums merged with none)."""
    found(lib.get_artist, artist_id)
    albums = [
        a for a in lib.list_albums(artist_id)
        if (not kind or a.album.kind == kind) and not (mains and a.album.main_id)
    ]  # fmt: skip
    return render(
        request, "_albums.html", albums=albums, show_artist=False, filtered=bool(kind or mains)
    )


@router.get("/fragments/artists/{artist_id}/featured")
def artist_featured(request: Request, lib: Lib, artist_id: int):
    """The Songs featured card, gone again when no song features the artist any more."""
    featured = found(lib.featured_songs, artist_id)
    return render(request, "_featured.html", featured=featured)


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


# Pictures


def _pictures_context(
    request: Request, lib: Library, artist_id: int, q: str | None, tag: str | None, page: int
) -> dict:
    artist = found(lib.get_artist, artist_id)
    base = f"/artists/{artist_id}/pictures"
    pager = _pager(request, base, lib.count_pictures(artist_id, q, tag), page, PICTURES_PER_PAGE)
    pictures = lib.list_pictures(artist_id, q, tag, PICTURES_PER_PAGE, pager["offset"])
    return {
        "artist": artist,
        "pictures": pictures,
        "pager": pager,
        "filtered": bool((q or "").strip() or (tag or "").strip()),
    }


def _tags_context(lib: Library, artist_id: int, tag: str | None) -> dict:
    tags = lib.picture_tags(artist_id)
    tag = (tag or "").strip()
    # A tag no picture has any more stays picked, so the list it filters does not jump.
    if tag and not any(t.label.casefold() == tag.casefold() for t in tags):
        tags.append(Point(tag, 0))
    return {"artist_id": artist_id, "tags": tags, "tag": tag}


@router.get("/artists/{artist_id}/pictures")
def pictures_page(
    request: Request,
    lib: Lib,
    artist_id: int,
    q: str | None = None,
    tag: str | None = None,
    page: int = 1,
):
    return render(
        request,
        "pictures.html",
        **_pictures_context(request, lib, artist_id, q, tag, page),
        **_tags_context(lib, artist_id, tag),
        q=q or "",
        form={},
    )


@router.get("/fragments/artists/{artist_id}/pictures-card")
def artist_pictures_card(request: Request, lib: Lib, artist_id: int):
    """The artist page's Pictures card, with the newest pictures."""
    return render(
        request,
        "_artist_pictures.html",
        artist=found(lib.get_artist, artist_id),
        pictures=lib.list_pictures(artist_id, limit=ARTIST_PAGE_PICTURES),
        picture_count=lib.count_pictures(artist_id),
    )


@router.get("/fragments/artists/{artist_id}/pictures")
def pictures_fragment(
    request: Request,
    lib: Lib,
    artist_id: int,
    q: str | None = None,
    tag: str | None = None,
    page: int = 1,
):
    context = _pictures_context(request, lib, artist_id, q, tag, page)
    return render(request, "_pictures.html", **context)


@router.get("/fragments/artists/{artist_id}/pictures/tags")
def picture_tags(request: Request, lib: Lib, artist_id: int, tag: str | None = None):
    """The tag picker of the Pictures page, reloaded when tags may have changed."""
    found(lib.get_artist, artist_id)
    return render(request, "_picture_tags.html", **_tags_context(lib, artist_id, tag))


@router.post("/artists/{artist_id}/pictures")
async def add_picture(request: Request, lib: Lib, artist_id: int):
    form = await request.form()
    found(lib.get_artist, artist_id)
    upload = form.get("picture")
    try:
        # Browsers send an empty, nameless file when nothing was picked.
        if upload is None or isinstance(upload, str) or not upload.filename:
            raise ValueError("Pick an image first.")
        lib.add_picture(artist_id, picture_from_form(form), await upload.read(), upload.filename)
    except ValueError as exc:
        return render(
            request,
            "_picture_form.html",
            status_code=422,
            error=str(exc),
            form=form,
            artist_id=artist_id,
        )
    return render(
        request,
        "_picture_form.html",
        trigger=("pictures-changed",),
        form={},
        artist_id=artist_id,
    )


def _picture_context(lib: Library, picture_id: int) -> dict:
    picture = found(lib.get_picture, picture_id)
    return {
        "picture": picture,
        "artist": lib.get_artist(picture.artist_id),
        "is_profile": lib.is_profile_picture(picture_id),
        "is_square": lib.picture_is_square(picture_id),
    }


@router.get("/pictures/{picture_id}")
def picture_page(request: Request, lib: Lib, picture_id: int):
    return render(request, "picture.html", **_picture_context(lib, picture_id))


@router.get("/fragments/pictures/{picture_id}/info")
def picture_info(request: Request, lib: Lib, picture_id: int):
    return render(request, "_picture_info.html", **_picture_context(lib, picture_id))


@router.get("/fragments/pictures/{picture_id}/edit")
def picture_edit(request: Request, lib: Lib, picture_id: int):
    return render(request, "_picture_edit.html", **_picture_context(lib, picture_id))


@router.post("/pictures/{picture_id}")
async def update_picture(request: Request, lib: Lib, picture_id: int):
    form = await request.form()
    context = _picture_context(lib, picture_id)
    try:
        lib.update_picture(picture_id, picture_from_form(form))
    except ValueError as exc:
        return render(request, "_picture_edit.html", status_code=422, error=str(exc), **context)
    return render(
        request,
        "_picture_info.html",
        trigger=("pictures-changed",),
        **_picture_context(lib, picture_id),
    )


@router.post("/pictures/{picture_id}/profile")
def make_profile_picture(request: Request, lib: Lib, picture_id: int):
    """Make a square picture its artist's profile picture."""
    context = _picture_context(lib, picture_id)
    try:
        lib.make_profile_picture(picture_id)
    except ValueError as exc:
        return render(request, "_picture_info.html", status_code=422, error=str(exc), **context)
    return render(
        request,
        "_picture_info.html",
        trigger=("artists-changed", "library-changed"),
        **_picture_context(lib, picture_id),
    )


@router.post("/pictures/{picture_id}/delete")
def delete_picture(lib: Lib, picture_id: int):
    picture = found(lib.get_picture, picture_id)
    lib.delete_picture(picture_id)
    return Response(headers={"HX-Redirect": f"/artists/{picture.artist_id}/pictures"})


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


def _albums_context(
    request: Request,
    lib: Library,
    q: str | None,
    own: str | None,
    favorites: str | None,
    later: str | None,
    missing: str | None,
    nocover: str | None,
    order: str | None,
    page: int,
) -> dict:
    query = _albums_query(q, own, favorites, later, missing, nocover)
    pager = _pager(request, "/albums", lib.count_albums(**query), page, ALBUMS_PER_PAGE)
    latest = order == "latest"  # picking it asks for that order alone
    albums = lib.list_albums(
        order="latest" if latest else "year",
        favorites_first=not latest,
        limit=ALBUMS_PER_PAGE,
        offset=pager["offset"],
        **query,
    )
    return {"albums": albums, "pager": pager, "order": "latest" if latest else ""}


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
    order: str | None = None,
    page: int = 1,
):
    return render(
        request,
        "albums.html",
        **_albums_context(request, lib, q, own, favorites, later, missing, nocover, order, page),
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
    order: str | None = None,
    page: int = 1,
):
    context = _albums_context(request, lib, q, own, favorites, later, missing, nocover, order, page)
    return render(request, "_album_pages.html", **context)


async def _cover_upload(form: FormData) -> tuple[bytes, str] | None:
    """The image picked in the form's ``cover`` field and its file name, or ``None`` when none
    was; a ``ValueError`` when it is not an image the library takes."""
    upload = form.get("cover")
    # Browsers send an empty, nameless file when nothing was picked.
    if upload is None or isinstance(upload, str) or not upload.filename:
        return None
    check_cover_name(upload.filename)
    data = await upload.read()
    if not data:
        raise ValueError(f"cover {upload.filename!r} is empty")
    return data, upload.filename


@router.post("/albums")
async def create_album(request: Request, lib: Lib):
    form = await request.form()
    artist_id = _int(form, "artist_id")
    try:
        # Checked first, so a bad cover does not leave an album behind.
        cover = await _cover_upload(form)
        with lib.transaction():
            album = lib.create_album(album_from_form(form))
            if cover is not None:
                lib.set_cover_data(album.id, *cover)
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
        # The album's own listens: its songs' play counts also count their merged versions'.
        "play_count": next(
            (a.play_count for a in lib.list_albums(album.artist_id) if a.album.id == album_id), 0
        ),
        "lyrics_count": sum(1 for s in songs if s.song.lyrics and not s.song.no_lyrics),
        "no_lyrics_count": sum(1 for s in songs if s.song.no_lyrics),
        "has_tier_list": lib.has_tier_list("album", album_id),
        "main_edition": lib.get_album(album.main_id) if album.main_id else None,
        "edition_count": len(lib.album_editions(album_id)),
    }


@router.get("/albums/{album_id}")
def album_page(request: Request, lib: Lib, album_id: int):
    return render(
        request,
        "album.html",
        **_album_context(lib, album_id),
        **_album_merge_context(lib, album_id),
    )


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


@router.post("/albums/{album_id}/cover")
async def set_album_cover(request: Request, lib: Lib, album_id: int):
    """Upload a cover from the album page: it replaces the album's current one."""
    form = await request.form()
    found(lib.get_album, album_id)
    try:
        cover = await _cover_upload(form)
        if cover is None:
            raise ValueError("Pick an image first.")
        lib.set_cover_data(album_id, *cover)
    except ValueError as exc:
        return render(
            request,
            "_album_info.html",
            status_code=422,
            cover_error=str(exc),
            **_album_context(lib, album_id),
        )
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
    """Where a song row is shown; "page" is the song's own page, "playlist" a playlist's and
    "featured" the Songs featured card of an artist the song features."""
    ctx = form_or_query.get("ctx") or "all"
    return ctx if ctx in ("all", "album", "playlist", "page", "featured") else "all"


def _lyrics_context(lib: Library, song_id: int, **extra) -> dict:
    """The song with its lyrics read again from its files, so edits show straight away."""
    timed_lines = []
    try:
        found(lib.reload_lyrics, song_id)
        timed_lines = lib.timed_lyrics(song_id)
    except (OSError, ValueError) as exc:
        extra.setdefault("error", f"Could not read the lyrics file: {exc}")
    return {
        "s": lib.get_song_summary(song_id),
        "lyrics_path": lib.lyrics_path(song_id),
        "lrc_path": lib.lrc_path(song_id),
        "timed_lines": timed_lines,
        **extra,
    }


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
        "featuring": lib.list_featuring(song_id),
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
        **_versions_context(lib, song_id),
        videos=lib.list_videos(song_id),
        lyrics_path=context["lyrics_path"],
        lrc_path=context["lrc_path"],
        error=context.get("error"),
    )


# Merged songs (see ``Library.merge_songs``): the Same song card of a song's page, and an
# album page's Merge with another edition card.

# What merging changes: plays, marks and ratings show on song lists, the song's own page and the
# built-in playlists.
MERGE_TRIGGER = ("songs-changed", "plays-changed", "playlists-changed", "library-changed")


def _versions_context(lib: Library, song_id: int, q: str | None = None, **extra) -> dict:
    """The Same song card of ``song_id``: the songs merged with it, the artist's other songs
    with its title, and the songs matching the search ``q`` (from ``MIN_SEARCH`` characters)."""
    versions = found(lib.song_versions, song_id)
    group = {v.song.id for v in versions}
    too_short = bool(q) and len("".join(q.split())) < MIN_SEARCH
    results = []
    if q and not too_short:
        results = [s for s in lib.list_songs(q=q) if s.song.id not in group][:SONG_RESULTS]
    return {
        "page_song": song_id,
        "versions": versions if len(versions) > 1 else [],
        "suggestions": lib.merge_suggestions(song_id),
        "merge_q": q or "",
        "merge_too_short": too_short,
        "merge_results": results,
        "min_search": MIN_SEARCH,
        **extra,
    }


@router.get("/fragments/songs/{song_id}/versions")
def song_versions(request: Request, lib: Lib, song_id: int, q: str | None = None):
    return render(request, "_versions.html", **_versions_context(lib, song_id, q))


@router.post("/songs/{song_id}/merge")
async def merge_song(request: Request, lib: Lib, song_id: int):
    """Merge another song (``song_id`` of the form) with this one, whose main version stays the
    main one."""
    form = await request.form()
    other = _id(form, "song_id")
    if other is None:
        raise HTTPException(422, "pick a song")
    try:
        found(lib.merge_songs, song_id, [other])
    except ValueError as exc:
        context = _versions_context(lib, song_id, error=str(exc))
        return render(request, "_versions.html", status_code=422, **context)
    return render(
        request, "_versions.html", trigger=MERGE_TRIGGER, **_versions_context(lib, song_id)
    )


@router.post("/songs/{song_id}/main")
async def make_main_song(request: Request, lib: Lib, song_id: int):
    """Make the song the main version; answers with the card of the page it was done from
    (``page``)."""
    form = await request.form()
    try:
        found(lib.make_main, song_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    page = _id(form, "page") or song_id
    return render(request, "_versions.html", trigger=MERGE_TRIGGER, **_versions_context(lib, page))


@router.post("/songs/{song_id}/unmerge")
async def unmerge_song(request: Request, lib: Lib, song_id: int):
    """Take the song out of the songs it is merged with; answers with the card of the page it
    was done from (``page``)."""
    form = await request.form()
    try:
        found(lib.unmerge_song, song_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    page = _id(form, "page") or song_id
    return render(request, "_versions.html", trigger=MERGE_TRIGGER, **_versions_context(lib, page))


def _album_merge_context(lib: Library, album_id: int, **extra) -> dict:
    """An album page's Editions card: the albums merged with it, the artist's albums it can be
    merged with, and its songs merged with none of the other editions'."""
    editions = found(lib.album_editions, album_id)
    return {
        "album_id": album_id,
        "editions": editions if len(editions) > 1 else [],
        "merge_choices": lib.album_merge_choices(album_id),
        "unmerged": lib.unmerged_songs(album_id),
        **extra,
    }


def _album_merge(request: Request, lib: Library, album_id: int, **extra) -> HTMLResponse:
    """The Editions card after a change; the other sections of the page listen for it."""
    return render(
        request,
        "_album_merge.html",
        trigger=(*MERGE_TRIGGER, "albums-changed"),
        **_album_merge_context(lib, album_id, **extra),
    )


@router.get("/fragments/albums/{album_id}/merge")
def album_merge(request: Request, lib: Lib, album_id: int):
    return render(request, "_album_merge.html", **_album_merge_context(lib, album_id))


@router.post("/albums/{album_id}/merge")
async def merge_album(request: Request, lib: Lib, album_id: int):
    """Merge the album with another of the artist's (``other``): into it when ``direction`` is
    ``into`` (the other one is the main edition), else the other one into this one. Songs of
    equal names are merged the same way round."""
    form = await request.form()
    other_id = _id(form, "other")
    if other_id is None:
        context = _album_merge_context(lib, album_id, error="Pick an album to merge with.")
        return render(request, "_album_merge.html", status_code=422, **context)
    other = found(lib.get_album, other_id)
    into = form.get("direction") == "into"
    try:
        merged = found(lib.merge_albums, *((album_id, other_id) if into else (other_id, album_id)))
    except ValueError as exc:
        context = _album_merge_context(lib, album_id, error=str(exc))
        return render(request, "_album_merge.html", status_code=422, **context)
    message = f"Merged into {other.title}." if into else f"Merged {other.title} into this album."
    if merged:
        songs = f"{merged} {'song' if merged == 1 else 'songs'} of the same name"
        message += f" So were {songs}, {'theirs' if into else 'this album’s'} the main ones."
    else:
        message += " No songs of the same name were left to merge."
    return _album_merge(request, lib, album_id, message=message)


@router.post("/albums/{album_id}/main")
async def make_main_album(request: Request, lib: Lib, album_id: int):
    """Make the album the main edition; answers with the card of the page it was done from
    (``page``)."""
    form = await request.form()
    try:
        found(lib.make_main_album, album_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _album_merge(request, lib, _id(form, "page") or album_id)


@router.post("/albums/{album_id}/unmerge")
async def unmerge_album(request: Request, lib: Lib, album_id: int):
    """Take the album out of the albums it is merged with (and its songs out of their merges
    with theirs); answers with the card of the page it was done from (``page``)."""
    form = await request.form()
    try:
        found(lib.unmerge_album, album_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return _album_merge(request, lib, _id(form, "page") or album_id)


@router.post("/albums/{album_id}/merge-song")
async def merge_album_song(request: Request, lib: Lib, album_id: int):
    """Merge a song of the album (``song_id``) left unmerged with one of another edition's
    (``other_id``), the way round the albums are."""
    form = await request.form()
    song_id, other_id = _id(form, "song_id"), _id(form, "other_id")
    if song_id is None or other_id is None:
        context = _album_merge_context(lib, album_id, error="Pick a song to merge with.")
        return render(request, "_album_merge.html", status_code=422, **context)
    try:
        found(lib.merge_album_song, song_id, other_id)
    except ValueError as exc:
        context = _album_merge_context(lib, album_id, error=str(exc))
        return render(request, "_album_merge.html", status_code=422, **context)
    return _album_merge(request, lib, album_id)


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


def _song_details_context(lib: Library, song: Song, form, featuring: list[int]) -> dict:
    """The details form; its search offers every other artist to feature, and the
    ``featuring`` ones show as tags, in their order."""
    others = [a.artist for a in lib.list_artists() if a.artist.id != song.artist_id]
    by_id = {artist.id: artist for artist in others}
    chosen = [by_id[artist_id] for artist_id in dict.fromkeys(featuring) if artist_id in by_id]
    return {
        "song": song,
        "form": form,
        "artists": [{"id": artist.id, "name": artist.name} for artist in others],
        "featuring": chosen,
    }


def _featuring_ids(form: FormData) -> list[int]:
    try:
        return [int(value) for value in form.getlist("featuring")]
    except ValueError:
        raise ValueError("featuring must be artist ids") from None


@router.get("/fragments/songs/{song_id}/details")
def song_details_edit(request: Request, lib: Lib, song_id: int):
    song = found(lib.get_song, song_id)
    form = {"notes": song.notes or "", "mv": song.mv or "", "explicit": song.explicit}
    featuring = [artist.id for artist in lib.list_featuring(song_id)]
    return render(
        request, "_song_details_edit.html", **_song_details_context(lib, song, form, featuring)
    )


@router.post("/songs/{song_id}/details")
async def update_song_details(request: Request, lib: Lib, song_id: int):
    """Change only the notes, the music video, whether it is explicit and the featured artists
    of a song, from its page."""
    form = await request.form()
    song = found(lib.get_song, song_id)
    data = song_in(song)
    data.notes = _text(form, "notes")
    data.mv = _text(form, "mv")
    data.explicit = "explicit" in form
    featuring: list[int] = []
    try:
        featuring = _featuring_ids(form)
        with lib.transaction():
            lib.update_song(song_id, data)
            lib.set_featuring(song_id, featuring)
    except (ValueError, NotFound) as exc:
        message = "That artist is no longer in the library." if isinstance(exc, NotFound) else exc
        return render(
            request,
            "_song_details_edit.html",
            status_code=422,
            error=str(message),
            **_song_details_context(lib, song, form, featuring),
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
def song_lyrics(request: Request, lib: Lib, song_id: int, live: bool = False):
    was_explicit = found(lib.get_song, song_id).explicit
    context = _lyrics_context(lib, song_id, live=live)
    # New words with swearing mark the song as explicit; the title shows its badge then.
    marked = context["s"].song.explicit and not was_explicit
    return render(request, "_lyrics.html", trigger=("song-changed",) if marked else (), **context)


@router.post("/songs/{song_id}/lyrics/open")
def open_lyrics(request: Request, lib: Lib, song_id: int, live: bool = False):
    # Only the page's own buttons may open files: other sites cannot send this header.
    if "hx-request" not in request.headers:
        raise HTTPException(403, "open the lyrics file from the song page")
    path = found(lib.lrc_path, song_id) or found(lib.ensure_lyrics_file, song_id)
    if path is None:
        raise HTTPException(404, "the library has no folder for lyrics files")
    try:
        editor.open_file(path)
    except OSError as exc:
        context = _lyrics_context(lib, song_id, live=live, error=str(exc))
        return render(request, "_lyrics.html", **context)
    return render(request, "_lyrics.html", **_lyrics_context(lib, song_id, live=live, opened=True))


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


def _playlist_summary(lib: Library, playlist_id: int) -> PlaylistSummary:
    found(lib.get_playlist, playlist_id)
    return next(p for p in lib.list_playlists() if p.playlist.id == playlist_id)


@router.get("/fragments/playlists/new")
def new_playlist_form(request: Request):
    return render(request, "_playlist_form.html", form={})


@router.get("/fragments/playlists/{playlist_id}/copy")
def copy_playlist_form(request: Request, lib: Lib, playlist_id: int):
    """New playlist turned into Copy playlist, for ``playlist_id``."""
    copy_of = _playlist_summary(lib, playlist_id)
    return render(request, "_playlist_form.html", copy_of=copy_of, form={})


@router.post("/playlists")
async def create_playlist(request: Request, lib: Lib):
    """A new playlist, or with ``copy_of`` in the form a copy of that one."""
    form = await request.form()
    copy_of = _int(form, "copy_of")
    source = _playlist_summary(lib, copy_of) if copy_of is not None else None
    data = PlaylistIn(_text(form, "name") or "")
    try:
        lib.copy_playlist(copy_of, data) if source else lib.create_playlist(data)
    except ValueError as exc:
        return render(
            request,
            "_playlist_form.html",
            status_code=422,
            error=str(exc),
            form=form,
            copy_of=source,
        )
    return render(request, "_playlist_form.html", trigger=("playlists-changed",), form={})


def _playlist_context(lib: Library, playlist_id: int) -> dict:
    detail = found(lib.get_playlist_detail, playlist_id)
    return {
        "playlist": detail.playlist,
        "setlist_of": _setlist_of(lib, detail.playlist),
        "songs": detail.songs,
        "total_seconds": sum(s.song.duration_seconds or 0 for s in detail.songs),
        "has_tier_list": lib.has_tier_list("playlist", playlist_id),
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
        renamed = lib.update_playlist(playlist_id, PlaylistIn(_text(form, "name") or ""))
    except ValueError as exc:
        return render(
            request, "_playlist_edit.html", status_code=422, error=str(exc), playlist=playlist
        )
    _rename_setlists(lib, playlist.name, renamed.name)
    return render(
        request,
        "_playlist_info.html",
        trigger=("playlists-changed",),
        **_playlist_context(lib, playlist_id),
    )


def _rename_setlists(lib: Library, old: str, new: str) -> None:
    """Keep concerts.csv's setlists on a renamed playlist, unless another playlist still has
    its old name (and so is now the setlist)."""
    if lib.media_dir is None or old.casefold() in _setlist_playlists(lib):
        return
    try:
        rename_setlist(lib.media_dir / CONCERTS_FILE, old, new)
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("could not rename the setlist %r in %s: %s", old, CONCERTS_FILE, exc)


@router.post("/playlists/{playlist_id}/delete")
def delete_playlist(lib: Lib, playlist_id: int):
    try:
        found(lib.delete_playlist, playlist_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return Response(headers={"HX-Redirect": "/playlists"})


# Tier lists: of a playlist, album or artist, at ``/<owners>/<id>/tiers``.

TIER_PATHS = {f"{kind}s": kind for kind in TIER_OWNERS}


def _tier_owner(owners: str) -> str:
    """The kind of tier list owner of a path's ``playlists``, ``albums`` or ``artists``."""
    if owners not in TIER_PATHS:
        raise HTTPException(404)
    return TIER_PATHS[owners]


def _tier_context(lib: Library, kind: str, owner_id: int) -> dict:
    tier_list = found(lib.get_tier_list, kind, owner_id)
    return {
        "tier_list": tier_list,
        # The owner's page, the tier list's under it.
        "owner_url": f"/{kind}s/{owner_id}",
        # How many songs Autoadd would put in each of the first three levels.
        "auto_counts": [len(level) for level in lib.auto_tier_levels(kind, owner_id)],
        "min_tiers": MIN_TIERS,
        "max_tiers": MAX_TIERS,
        "name_length": TIER_NAME_LENGTH,
    }


def _tier_board(
    request: Request,
    lib: Library,
    kind: str,
    owner_id: int,
    *,
    status_code: int = 200,
    editing: int | None = None,
    error: str | None = None,
) -> HTMLResponse:
    """A tier list's board, with the level ``editing`` showing its rename form."""
    context = _tier_context(lib, kind, owner_id)
    if not context["tier_list"].tiers:
        raise HTTPException(404, f"{kind} {owner_id} has no tier list")
    return render(
        request,
        "_tier_board.html",
        status_code=status_code,
        editing=editing,
        error=error,
        **context,
    )


@router.get("/{owners}/{owner_id}/tiers")
def tier_list_page(request: Request, lib: Lib, owners: str, owner_id: int):
    """The tier list, or a button to make one when there is none."""
    kind = _tier_owner(owners)
    context = _tier_context(lib, kind, owner_id)
    # The crumbs above the title, as on the owner's page: an album's are its artist's.
    section = "artists" if kind == "album" else owners
    crumbs = [(f"/{section}", section.capitalize())]
    if kind == "album":
        artist = lib.get_artist(lib.get_album(owner_id).artist_id)
        crumbs.append((f"/artists/{artist.id}", artist.name))
    crumbs.append((context["owner_url"], context["tier_list"].name))
    return render(request, "tiers.html", active=section, crumbs=crumbs, **context)


@router.get("/fragments/{owners}/{owner_id}/tiers")
def tier_board(request: Request, lib: Lib, owners: str, owner_id: int, edit: int | None = None):
    return _tier_board(request, lib, _tier_owner(owners), owner_id, editing=edit)


@router.post("/{owners}/{owner_id}/tiers")
def create_tier_list(lib: Lib, owners: str, owner_id: int):
    """Make the tier list (unless there is one) and open it."""
    found(lib.create_tier_list, _tier_owner(owners), owner_id)
    return Response(headers={"HX-Redirect": f"/{owners}/{owner_id}/tiers"})


@router.post("/{owners}/{owner_id}/tiers/delete")
def delete_tier_list(lib: Lib, owners: str, owner_id: int):
    found(lib.delete_tier_list, _tier_owner(owners), owner_id)
    return Response(headers={"HX-Redirect": f"/{owners}/{owner_id}"})


@router.post("/{owners}/{owner_id}/tiers/levels")
def add_tier(request: Request, lib: Lib, owners: str, owner_id: int):
    kind = _tier_owner(owners)
    try:
        found(lib.add_tier, kind, owner_id)
    except ValueError as exc:
        return _tier_board(request, lib, kind, owner_id, status_code=422, error=str(exc))
    return _tier_board(request, lib, kind, owner_id)


@router.post("/tiers/{tier_id}")
async def rename_tier(request: Request, lib: Lib, tier_id: int):
    form = await request.form()
    tier = found(lib.get_tier, tier_id)
    try:
        lib.rename_tier(tier_id, _text(form, "name") or "")
    except ValueError as exc:
        return _tier_board(
            request, lib, *tier.owner, status_code=422, editing=tier_id, error=str(exc)
        )
    return _tier_board(request, lib, *tier.owner)


@router.post("/tiers/{tier_id}/delete")
def delete_tier(request: Request, lib: Lib, tier_id: int):
    tier = found(lib.get_tier, tier_id)
    try:
        lib.delete_tier(tier_id)
    except ValueError as exc:
        return _tier_board(request, lib, *tier.owner, status_code=422, error=str(exc))
    return _tier_board(request, lib, *tier.owner)


@router.post("/{owners}/{owner_id}/tiers/place")
async def place_in_tier(request: Request, lib: Lib, owners: str, owner_id: int):
    """Move songs (``song_id``, given once per song or as ids split by commas, in the order
    to keep) to a level (``tier_id``, none for the unassigned songs), before the song at
    ``position`` of what is there besides them, or last."""
    kind = _tier_owner(owners)
    form = await request.form()
    song_ids = _song_ids(form)
    if not song_ids:
        raise HTTPException(422, "pick a song")
    try:
        found(
            lib.place_songs, kind, owner_id, song_ids, _id(form, "tier_id"), _id(form, "position")
        )
    except ValueError as exc:  # a locked song
        return _tier_board(request, lib, kind, owner_id, status_code=422, error=str(exc))
    return _tier_board(request, lib, kind, owner_id)


def _song_ids(form: FormData) -> list[int]:
    """The songs of a form's ``song_id``, given once per song or as ids split by commas."""
    try:
        return [
            int(part) for value in form.getlist("song_id")
            for part in str(value).split(",") if part.strip()
        ]  # fmt: skip
    except ValueError:
        raise HTTPException(422, "song ids must be numbers") from None


@router.post("/{owners}/{owner_id}/tiers/locking")
async def tier_locking(request: Request, lib: Lib, owners: str, owner_id: int):
    """Turn locking mode on (``on`` given) or off."""
    kind = _tier_owner(owners)
    form = await request.form()
    found(lib.set_locking, kind, owner_id, bool(form.get("on")))
    return _tier_board(request, lib, kind, owner_id)


@router.post("/{owners}/{owner_id}/tiers/lock")
async def tier_lock(request: Request, lib: Lib, owners: str, owner_id: int):
    """Lock songs (``song_id``) where they are, or unlock them with ``locked`` 0. With
    ``quiet``, a success answers with nothing: the page already shows it (see tiers.js)."""
    kind = _tier_owner(owners)
    form = await request.form()
    song_ids = _song_ids(form)
    try:
        found(lib.set_locked, kind, owner_id, song_ids, form.get("locked") != "0")
    except ValueError as exc:
        return _tier_board(request, lib, kind, owner_id, status_code=422, error=str(exc))
    if form.get("quiet"):
        return Response(status_code=204)
    return _tier_board(request, lib, kind, owner_id)


def _tier_playlist_name(tier_list: TierList, last: int) -> str:
    """The name of the playlist of a tier list's levels from the top one to ``last``."""
    top, bottom = tier_list.tiers[0].tier.name, tier_list.tiers[last].tier.name
    levels = top if last == 0 else f"{top} to {bottom}"
    return f"{tier_list.name} tier list: {levels}"


@router.post("/{owners}/{owner_id}/tiers/playlist")
async def tier_playlist(request: Request, lib: Lib, owners: str, owner_id: int):
    """Make (or refill) a playlist of the tier list's songs, in order, from the top level down
    to the level at position ``last``, that one included."""
    kind = _tier_owner(owners)
    tier_list = found(lib.get_tier_list, kind, owner_id)
    last = _id(await request.form(), "last")
    if last is None or not 0 <= last < len(tier_list.tiers):
        raise HTTPException(422, "pick the last level to take")
    song_ids = [s.song.id for row in tier_list.tiers[: last + 1] for s in row.songs]
    if not song_ids:
        return render(
            request,
            "_tier_playlist.html",
            status_code=422,
            error="Those levels have no songs yet: put some in them first.",
        )
    name = _tier_playlist_name(tier_list, last)
    existing = next(
        (p.playlist for p in lib.list_playlists()
         if not p.playlist.kind and p.playlist.name.casefold() == name.casefold()),
        None,
    )  # fmt: skip
    playlist = existing or lib.create_playlist(PlaylistIn(name))
    lib.set_playlist_songs(playlist.id, song_ids)
    return render(
        request,
        "_tier_playlist.html",
        playlist=playlist,
        count=len(song_ids),
        refilled=existing is not None,
    )


@router.post("/{owners}/{owner_id}/tiers/auto")
def auto_tier(request: Request, lib: Lib, owners: str, owner_id: int):
    kind = _tier_owner(owners)
    found(lib.auto_tier, kind, owner_id)
    return _tier_board(request, lib, kind, owner_id)


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
        context = _lyrics_context(lib, songs[at - 1].song.id, live=True)
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
    number ``at``. Reaching an album's last song takes the album out of Listen Later."""
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
    if kind == "album" and at == len(queue):
        found(lib.finish_album, record_id)  # reaching its last song finishes the album
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


def _album_groups(albums: Iterable[dict]) -> list[tuple[str, list[Album]]]:
    """The Album picker's sections, leaving out the empty ones. An album is Wanted when it is on
    any format the cart can still take, else Owned when you have it on one, else Neither; each
    section goes by year and title."""
    groups: dict[str, list[Album]] = {"Wanted": [], "Neither": [], "Owned": []}
    for album in albums:
        states = album["states"]
        name = "Wanted" if "wanted" in states else "Owned" if "owned" in states else "Neither"
        groups[name].append(album["album"])
    return [(name, sorted(group, key=_album_order)) for name, group in groups.items() if group]


def _cart_picker(lib: Library, form: FormData | dict) -> dict:
    """The Band, Album and Format pickers of the Add to cart form. Each lists what fits the
    picks before it (only albums and formats not in the cart yet) and keeps its own pick while
    that still fits; a picker with a single choice picks it."""
    options = lib.cart_options()
    bands: dict[int, dict] = {}
    for o in options:
        band = bands.setdefault(
            o.album.artist_id,
            {
                "id": o.album.artist_id,
                "name": o.artist_name,
                "favorite": o.favorite_artist,
                "wanted": set(),
            },
        )
        if o.state == "wanted":
            band["wanted"].add(o.album.id)
    artist_id = _id(form, "artist_id")
    if artist_id not in bands:
        artist_id = next(iter(bands)) if len(bands) == 1 else None
    albums: dict[int, dict] = {}
    for o in options:
        if o.album.artist_id == artist_id:
            album = albums.setdefault(o.album.id, {"album": o.album, "states": set()})
            album["states"].add(o.state)
    album_id = _id(form, "album_id")
    if album_id not in albums:
        album_id = next(iter(albums)) if len(albums) == 1 else None
    formats = [o for o in options if o.album.id == album_id]
    formats.sort(key=lambda o: list(FORMATS).index(o.format))
    fmt = form.get("format")
    if fmt not in [o.format for o in formats]:
        fmt = formats[0].format if len(formats) == 1 else None
    return {
        "bands": sorted(bands.values(), key=lambda b: (not b["favorite"], b["name"].casefold())),
        "album_groups": _album_groups(albums.values()),
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


@router.post("/budget/cart/discount")
async def set_discount(request: Request, lib: Lib):
    form = await request.form()
    try:
        lib.set_discount(_price(form, "discount") or 0.0)
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


def _tours(pairs: list[tuple[str, str]], band: str | None) -> list[str]:
    """The tours of the band among (band, tour) ``pairs``."""
    band = " ".join((band or "").split()).casefold()
    tours = {tour.casefold(): tour for name, tour in pairs if name.casefold() == band}
    return sorted(tours.values(), key=str.casefold)


def _own_playlists(lib: Library) -> list[Playlist]:
    """Your own playlists, which can be setlists, A–Z."""
    return [p.playlist for p in lib.list_playlists() if not p.playlist.kind]


def _setlist_playlists(lib: Library) -> dict[str, Playlist]:
    """The playlist each setlist name stands for: the oldest of your playlists called that,
    ignoring case."""
    by_name: dict[str, Playlist] = {}
    for playlist in sorted(_own_playlists(lib), key=lambda p: p.id):
        by_name.setdefault(playlist.name.casefold(), playlist)
    return by_name


def _setlist_warnings(lib: Library, concerts: list[Concert]) -> list[str]:
    names = [p.name.casefold() for p in _own_playlists(lib)]
    warnings = []
    for name in sorted({c.setlist for c in concerts if c.setlist}, key=str.casefold):
        count = names.count(name.casefold())
        if count == 0:
            warnings.append(f"Setlist {name!r}: there is no playlist with that name")
        elif count > 1:
            warnings.append(f"Setlist {name!r}: {count} playlists have that name; it is the oldest")
    return warnings


def _concert_setlist(lib: Library, form: FormData) -> str | None:
    """The name of the playlist picked as a setlist in ``form``; ``None`` for none."""
    playlist_id = _int(form, "playlist_id")
    if playlist_id is None:
        return None
    playlist = found(lib.get_playlist, playlist_id)
    if playlist.kind:
        raise ValueError(f"{playlist.name} cannot be a setlist; pick one of your own playlists")
    return playlist.name


def _concerts_context(lib: Library, **extra) -> dict:
    """The concerts in the library folder's ``concerts.csv``, read again every time."""
    path = lib.media_dir / CONCERTS_FILE if lib.media_dir else None
    concerts, warnings, added_tours = [], [], []
    if path:
        try:
            concerts, warnings = read_concerts(path)
        except (OSError, UnicodeDecodeError) as exc:
            warnings.append(f"Could not read {path}: {exc}")
        try:
            added_tours = read_tours(path.with_name(TOURS_FILE))
        except (OSError, UnicodeDecodeError) as exc:
            warnings.append(f"Could not read {path.with_name(TOURS_FILE)}: {exc}")
    named_tours = [(c.band, c.tour) for c in concerts if c.tour]
    warnings += _setlist_warnings(lib, concerts)
    today = date.today()
    return {
        "path": path,
        "found_file": bool(path and path.is_file()),
        "today": today,
        "upcoming": [c for c in concerts if c.date >= today],
        "past": [c for c in reversed(concerts) if c.date < today],
        "artists": {a.artist.name.casefold(): a.artist for a in lib.list_artists()},
        "playlists": _own_playlists(lib),
        "setlists": _setlist_playlists(lib),
        "warnings": warnings,
        "tours": _tours(added_tours + named_tours, extra.get("concert_form", {}).get("band")),
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
    if lib.media_dir is None:
        raise HTTPException(404, "the library has no media folder")
    return lib.media_dir / CONCERTS_FILE


def _band(form: FormData) -> str:
    band = _text(form, "band")
    if band is None:
        raise ValueError("band is required")
    return band


def _tour(form: FormData) -> str:
    tour = _text(form, "tour")
    if tour is None:
        raise ValueError("tour is required")
    return tour


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
            day,
            band,
            _text(form, "country"),
            _text(form, "city"),
            tour=_tour(form),
            setlist=_concert_setlist(lib, form),
        )
        add_concerts(path, [concert])
    except (OSError, ValueError) as exc:
        context = _concerts_context(lib, concert_error=str(exc), concert_form=form)
        return render(request, "_concerts.html", status_code=422, **context)
    context = _concerts_context(lib, message=f"Added {band} on {day}.")
    return render(request, "_concerts.html", **context)


@router.post("/concerts/tours")
async def add_tour(request: Request, lib: Lib):
    """Keep a tour, for Add concert to offer it. It starts filled in there, for its first date."""
    path = _concerts_file(request, lib).with_name(TOURS_FILE)
    form = await request.form()
    try:
        band = _band(form)
        tour = _tour(form)
        known = _concerts_context(lib, concert_form={"band": band})["tours"]
        if tour.casefold() in map(str.casefold, known):
            raise ValueError(f"{band} already has {tour}")
        save_tour(path, band, tour)
    except (OSError, ValueError) as exc:
        context = _concerts_context(lib, tour_error=str(exc), tour_form=form)
        return render(request, "_concerts.html", status_code=422, **context)
    filled = {"band": band, "tour": tour}
    context = _concerts_context(lib, message=f"Added {tour} for {band}.", concert_form=filled)
    return render(request, "_concerts.html", **context)


@router.get("/fragments/concerts/form")
def copy_concert(request: Request, lib: Lib):
    """Add concert, filled in with what is in the query: the concert being copied."""
    context = _concerts_context(lib, concert_form=request.query_params, copied=True)
    return render(request, "_concert_form.html", **context)


@router.get("/fragments/concerts/tours")
def tour_options(request: Request, lib: Lib):
    """The tours Add concert offers for the band in the query."""
    context = _concerts_context(lib, concert_form=request.query_params)
    return render(request, "_tour_options.html", **context)


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


@router.post("/concerts/{line}/setlist")
async def set_concert_setlist(request: Request, lib: Lib, line: int):
    """Make one of your playlists the concert's setlist in the file, or take it away."""
    path = _concerts_file(request, lib)
    form = await request.form()
    try:
        day = date.fromisoformat(_text(form, "date") or "")
        set_setlist(path, line, day, _text(form, "band") or "", _concert_setlist(lib, form))
    except (OSError, ValueError) as exc:
        context = _concerts_context(lib, error=str(exc))
        return render(request, "_concerts.html", status_code=422, **context)
    return render(request, "_concerts.html", **_concerts_context(lib))


def _setlist_of(lib: Library, playlist: Playlist) -> list[Concert]:
    """The concerts whose setlist is ``playlist``, by date."""
    if lib.media_dir is None or playlist.kind:
        return []
    try:
        concerts, _warnings = read_concerts(lib.media_dir / CONCERTS_FILE)
    except (OSError, UnicodeDecodeError):
        return []
    setlists = _setlist_playlists(lib)
    return [c for c in concerts if c.setlist and setlists.get(c.setlist.casefold()) == playlist]


# Import: the inbox, its preview, and taking the library with you

# Changes listed before the rest is folded away.
IMPORT_SHOWN = 50


def _inbox_dir(request: Request) -> Path:
    return Path(request.app.state.info_dir)


def _fingerprint(info_dir: Path) -> str:
    """What the inbox holds now, so Accept imports what the preview showed, nothing else."""
    digest = hashlib.sha256()
    for path in inbox.list_files(info_dir):
        stat = (info_dir / path).stat()
        digest.update(f"{path}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()[:16]


def _import_page(request: Request, lib: Library, status_code: int = 200, **extra):
    info_dir = _inbox_dir(request)
    return render(
        request,
        "import.html",
        status_code=status_code,
        info_dir=info_dir,
        media_dir=lib.media_dir,
        files=inbox.list_files(info_dir),
        archives=inbox.list_archives(info_dir),
        fingerprint=_fingerprint(info_dir),
        shown=IMPORT_SHOWN,
        **extra,
    )


@router.get("/import")
def import_page(request: Request, lib: Lib, preview: bool = False):
    """The inbox, with a preview of what importing it would change when asked for."""
    if not preview or not inbox.list_files(_inbox_dir(request)):
        return _import_page(request, lib)
    try:
        report = import_dir(lib, _inbox_dir(request), dry_run=True)
    except (OSError, ValueError) as exc:
        return _import_page(request, lib, 422, error=f"{exc} Fix the file and preview again.")
    return _import_page(request, lib, preview=report)


@router.post("/import/upload")
async def upload_to_inbox(request: Request, lib: Lib):
    """Put uploaded files into the inbox, then preview it."""
    form = await request.form()
    uploads = [u for u in form.getlist("file") if not isinstance(u, str) and u.filename]
    if not uploads:
        return _import_page(request, lib, 422, error="Pick a file first.")
    try:
        for upload in uploads:
            inbox.add_upload(_inbox_dir(request), upload.filename, await upload.read())
    except ValueError as exc:
        return _import_page(request, lib, 422, error=str(exc))
    return RedirectResponse("/import?preview=1", status_code=303)


@router.post("/import/accept")
async def accept_import(request: Request, lib: Lib):
    """Import the inbox for real, then archive it."""
    form = await request.form()
    info_dir = _inbox_dir(request)
    if form.get("fingerprint") != _fingerprint(info_dir):
        return _import_page(
            request, lib, 409, error="The inbox changed since the preview. Check it again."
        )
    try:
        report, archive = accept(lib, info_dir)
    except (OSError, ValueError) as exc:
        return _import_page(request, lib, 422, error=f"{exc} Nothing was imported.")
    logger.info("Imported %s:\n%s", info_dir, report.summary())
    return _import_page(request, lib, result=report, archive=archive and archive.name)


@router.post("/import/archives/delete")
def delete_all_archives(request: Request, lib: Lib):
    inbox.delete_all_archives(_inbox_dir(request))
    return RedirectResponse("/import", status_code=303)


@router.post("/import/archives/{name}/delete")
def delete_archive(request: Request, lib: Lib, name: str):
    try:
        inbox.delete_archive(_inbox_dir(request), name)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return RedirectResponse("/import", status_code=303)


def _zip_download(write, kind: str) -> FileResponse:
    """A zip made by ``write(path)`` in a temporary file, removed once it was sent."""
    handle, name = tempfile.mkstemp(suffix=".zip")
    os.close(handle)
    path = Path(name)
    try:
        write(path)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return FileResponse(
        path,
        media_type="application/zip",
        filename=backup.file_name(kind),
        background=BackgroundTask(path.unlink, missing_ok=True),
    )


@router.get("/import/backup")
def download_backup(lib: Lib):
    """The whole library folder, exactly, as a zip (see ``implicated.backup``)."""
    return _zip_download(lambda path: backup.write_backup(lib, path), "backup")


@router.get("/import/export")
def download_export(lib: Lib, owner: str | None = None):
    """The library as an inbox zip, to import here or into another library."""
    return _zip_download(lambda path: backup.write_export(lib, path, owner or None), "export")
