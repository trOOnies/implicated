from __future__ import annotations

from fastapi import APIRouter, Response

from implicated.api.deps import Lib, call
from implicated.models import Folder, FolderDetail, FolderIn

router = APIRouter(prefix="/folders", tags=["Folders"])


@router.get("")
def list_folders(lib: Lib) -> list[FolderDetail]:
    """Every folder with its playlists: Default, Setlists, then yours A–Z."""
    return lib.list_folders()


@router.post("", status_code=201)
def create_folder(lib: Lib, data: FolderIn) -> Folder:
    return call(lib.create_folder, data)


@router.get("/{folder_id}")
def get_folder(lib: Lib, folder_id: int) -> Folder:
    return call(lib.get_folder, folder_id)


@router.put("/{folder_id}")
def update_folder(lib: Lib, folder_id: int, data: FolderIn) -> Folder:
    return call(lib.update_folder, folder_id, data)


@router.delete("/{folder_id}", status_code=204)
def delete_folder(lib: Lib, folder_id: int) -> Response:
    """Delete one of your folders; its playlists go to Default."""
    call(lib.delete_folder, folder_id)
    return Response(status_code=204)
