import os
import runpy
import sys

import pytest

from implicated.library import Library
from implicated.models import AlbumIn, ArtistIn
from implicated.seed import seed
from implicated.web import __main__ as web_main
from implicated.web import create_app
from implicated.web.shutdown import SERVER_PID


def test_seed(lib: Library):
    seed(lib, plays=20)
    stats = lib.stats()
    assert (stats.artists, stats.albums, stats.songs, stats.plays) == (3, 4, 11, 20)
    assert lib.find_artist("Massive Attack").favorite


def test_seed_cli(tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    db = tmp_path / "seeded.db"
    monkeypatch.setattr(sys, "argv", ["implicated.seed", str(db)])
    monkeypatch.delitem(sys.modules, "implicated.seed")  # run it fresh, as `python -m` would
    runpy.run_module("implicated.seed", run_name="__main__")
    assert f"Seeded demo data into {db}" in capsys.readouterr().out
    assert Library(db).stats().artists == 3


def test_serve_cli(monkeypatch: pytest.MonkeyPatch):
    calls = []
    monkeypatch.setattr(web_main.uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs))
    monkeypatch.setattr(sys, "argv", ["implicated.web", "--port", "9000", "--reload"])
    monkeypatch.setenv(SERVER_PID, "")  # undone after the test
    web_main.main()
    assert calls == [
        {"factory": True, "host": "127.0.0.1", "port": 9000, "reload": True},
    ]
    assert os.environ[SERVER_PID] == str(os.getpid())  # what the exit button stops


def test_serve_cli_exits_quietly_once_stopped(monkeypatch: pytest.MonkeyPatch):
    def stopped(*args, **kwargs):
        raise KeyboardInterrupt  # uvicorn raising the exit button's signal again

    monkeypatch.setattr(web_main.uvicorn, "run", stopped)
    monkeypatch.setattr(sys, "argv", ["implicated.web"])
    monkeypatch.setenv(SERVER_PID, "")
    web_main.main()


def test_app_creates_an_empty_inbox_and_imports_nothing(tmp_path, monkeypatch):
    info = tmp_path / "fresh"
    monkeypatch.setenv("IMPLICATED_INFO", str(info))
    app = create_app(tmp_path / "local" / "implicated.db")
    assert info.is_dir() and not any(info.iterdir())
    assert app.state.info_dir == info
    assert app.state.library.media_dir == tmp_path / "local"


def test_app_edits_survive_a_restart(tmp_path, monkeypatch):
    """The inbox is never imported on startup, so what the app changed stays."""
    info = tmp_path / "info"
    (info / "bands" / "Portishead").mkdir(parents=True)
    (info / "bands.yaml").write_text("bands:\n- name: Portishead\n  creation_year: 1991\n")
    (info / "bands" / "Portishead" / "albums.yaml").write_text("Dummy:\n  release_year: 1994\n")
    monkeypatch.setenv("IMPLICATED_INFO", str(info))
    db = tmp_path / "local" / "implicated.db"
    lib = create_app(db).state.library
    assert lib.stats().artists == 0  # nothing imported
    artist = lib.create_artist(ArtistIn("Portishead", formed_year=1990))
    lib.create_album(AlbumIn(artist.id, "Dummy (Remastered)", 1995))
    lib = create_app(db).state.library
    assert lib.get_artist(artist.id).formed_year == 1990
    assert [a.album.title for a in lib.list_albums(artist.id)] == ["Dummy (Remastered)"]
