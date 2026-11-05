from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, Response, UploadFile
from fastapi.responses import FileResponse

from implicated.api.deps import Lib, call, image
from implicated.models import Picture, PictureIn

router = APIRouter(tags=["Pictures"])


@router.get("/artists/{artist_id}/pictures")
def list_pictures(
    lib: Lib, artist_id: int, q: str | None = None, tag: str | None = None
) -> list[Picture]:
    """The artist's pictures, newest first. ``q`` is looked for in their name, file name,
    description and tags; ``tag`` keeps those with that tag."""
    call(lib.get_artist, artist_id)
    return lib.list_pictures(artist_id, q, tag)


@router.post("/artists/{artist_id}/pictures", status_code=201)
async def add_picture(
    lib: Lib,
    artist_id: int,
    file: Annotated[UploadFile, File(description="The image: AVIF, GIF, JPEG, PNG or WebP")],
    name: Annotated[str, Form(description="Named after the file when left out")] = "",
    description: Annotated[str | None, Form()] = None,
    tags: Annotated[str, Form(description="Separated by commas")] = "",
) -> Picture:
    data = PictureIn(name, description, tags.split(","))
    return call(lib.add_picture, artist_id, data, await file.read(), file.filename or "")


@router.get("/pictures/{picture_id}")
def get_picture(lib: Lib, picture_id: int) -> Picture:
    return call(lib.get_picture, picture_id)


@router.put("/pictures/{picture_id}")
def update_picture(lib: Lib, picture_id: int, data: PictureIn) -> Picture:
    return call(lib.update_picture, picture_id, data)


@router.delete("/pictures/{picture_id}", status_code=204)
def delete_picture(lib: Lib, picture_id: int) -> Response:
    call(lib.delete_picture, picture_id)
    return Response(status_code=204)


@router.get("/pictures/{picture_id}/image", response_class=FileResponse)
def picture_image(lib: Lib, picture_id: int) -> FileResponse:
    return image(call(lib.picture_path, picture_id))
