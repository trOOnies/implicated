import os
import runpy
import sys

import pytest

from implicated.library import Library
from implicated.seed import seed
from implicated.web import __main__ as web_main
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


def test_serve_cli_stops_on_missing_info(tmp_path, monkeypatch: pytest.MonkeyPatch):
    calls = []
    monkeypatch.setattr(web_main.uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs))
    monkeypatch.setenv("IMPLICATED_INFO", str(tmp_path / "fresh"))
    monkeypatch.setattr(sys, "argv", ["implicated.web", "--reload"])
    with pytest.raises(SystemExit, match="bands.yaml"):
        web_main.main()
    assert calls == []  # uvicorn, and its reloader, never start
    assert (tmp_path / "fresh" / "bands.yaml").is_file()
