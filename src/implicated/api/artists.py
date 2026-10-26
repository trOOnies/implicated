from __future__ import annotations

from fastapi import APIRouter, Response
from fastapi.responses import FileResponse

from implicated.api.deps import Lib, call, image
from implicated.models import Artist, ArtistIn, ArtistSummary

router = APIRouter(prefix="/artists", tags=["Artists"])


@router.get("")
def list_artists(lib: Lib, q: str | None = None) -> list[ArtistSummary]:
    return lib.list_artists(q)


@router.post("", status_code=201)
def create_artist(lib: Lib, data: ArtistIn) -> Artist:
    return call(lib.create_artist, data)


@router.get("/{artist_id}")
def get_artist(lib: Lib, artist_id: int) -> Artist:
    return call(lib.get_artist, artist_id)


@router.put("/{artist_id}")
def update_artist(lib: Lib, artist_id: int, data: ArtistIn) -> Artist:
    return call(lib.update_artist, artist_id, data)


@router.delete("/{artist_id}", status_code=204)
def delete_artist(lib: Lib, artist_id: int) -> Response:
    call(lib.delete_artist, artist_id)
    return Response(status_code=204)


@router.get("/{artist_id}/image", response_class=FileResponse)
def artist_image(lib: Lib, artist_id: int) -> FileResponse:
    return image(call(lib.image_path, artist_id))
