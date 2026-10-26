import runpy
import sys

import pytest

from implicated.library import Library
from implicated.seed import seed
from implicated.web import __main__ as web_main


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
    web_main.main()
    assert calls == [
        {"factory": True, "host": "127.0.0.1", "port": 9000, "reload": True},
    ]
