from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from implicated.library import Library
from implicated.models import AlbumIn, ArtistIn, SongIn
from implicated.web import create_app


@pytest.fixture(autouse=True)
def no_info_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep ``create_app`` from importing the developer's own ``info/`` folder: give it an empty
    one that passes the startup check."""
    info = tmp_path / "app-info"
    (info / "bands").mkdir(parents=True)
    (info / "bands.yaml").write_text("bands: []\n")
    monkeypatch.setenv("IMPLICATED_INFO", str(info))


@pytest.fixture
def lib(tmp_path: Path) -> Library:
    return Library(tmp_path / "local" / "implicated.db")


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "local" / "implicated.db"))


@pytest.fixture
def client_lib(client: TestClient) -> Library:
    return client.app.state.library


@dataclass
class Sample:
    artist_id: int
    album_id: int
    song_ids: list[int]


def add_sample(lib: Library) -> Sample:
    """One artist with one album of two songs."""
    artist = lib.create_artist(ArtistIn("Portishead", "United Kingdom", 1991, ["Trip hop"]))
    album = lib.create_album(AlbumIn(artist.id, "Dummy", 1994, rating=5))
    songs = [
        lib.create_song(SongIn(artist.id, "Mysterons", album.id, 1, 302)),
        lib.create_song(SongIn(artist.id, "Sour Times", album.id, 2, 251)),
    ]
    return Sample(artist.id, album.id, [s.id for s in songs])


@pytest.fixture
def sample(lib: Library) -> Sample:
    return add_sample(lib)


@pytest.fixture
def web_sample(client_lib: Library) -> Sample:
    return add_sample(client_lib)


@pytest.fixture
def image(tmp_path: Path):
    """Write a fake image file and return its path."""

    def make(name: str = "cover.jpg", data: bytes = b"image") -> Path:
        path = tmp_path / "images" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    return make
