from pathlib import Path

import pytest

from implicated import migrate
from implicated.library import Library


def fill(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)


def test_migrate_moves_hand_edited_files_into_the_library(tmp_path: Path):
    info, local = tmp_path / "info", tmp_path / "local"
    fill(
        info,
        {
            "bands.yaml": "bands:\n- name: A\n",
            "bands/A/albums.yaml": "First:\n  songs:\n  - name: One\n",
            "bands/A/covers/first.jpg": "img",
            "bands/A/songs/One.txt": "words",
            "bands/A/songs/One.lrc": "[00:01.00]words",
            "bands/A/songs/Two.txt": "mine",
            "bands/A/songs/Same.txt": "same",
            "bands/A/tabs/One.gp5": "tab",
            "concerts.csv": "date;band;tour;country;city;Want to go;Going\n2026-01-01;A;;;X;;\n",
        },
    )
    fill(
        local,
        {
            "bands/A/songs/One.txt": "",  # made empty by the app meanwhile: replaced
            "bands/A/songs/Two.txt": "edited since",  # kept
            "bands/A/songs/Same.txt": "same",
        },
    )
    lib = Library(local / "implicated.db", local)

    report = migrate.migrate(lib, info)

    songs = local / "bands" / "A" / "songs"
    assert (songs / "One.txt").read_text() == "words"
    assert (songs / "One.lrc").is_file()
    assert (songs / "Two.txt").read_text() == "edited since"
    assert (local / "bands" / "A" / "tabs" / "One.gp5").read_text() == "tab"
    assert "2026-01-01;A" in (local / "concerts.csv").read_text()
    assert (report.moved, report.kept) == (3, 1)
    assert any("Two.txt" in w for w in report.warnings)
    assert sorted(p.name for p in info.iterdir()) == ["archive"]
    archived = report.archive
    assert archived.name.endswith("-migration")
    assert (archived / "bands.yaml").is_file()
    assert (archived / "bands" / "A" / "songs" / "Two.txt").read_text() == "mine"
    assert lib.list_artists() == []  # nothing was imported


def test_migrate_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    info, db = tmp_path / "info", tmp_path / "local" / "implicated.db"
    fill(info, {"bands.yaml": "bands:\n- name: A\n", "bands/A/songs/x.txt": "x"})
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    migrate.main([str(info), "--db", str(db)])
    assert "new artist: A" in capsys.readouterr().out
    assert (info / "bands.yaml").is_file()  # said no

    migrate.main([str(info), "--db", str(db), "--yes"])
    assert "moved 1 lyrics and tab files" in capsys.readouterr().out
    assert (db.parent / "bands" / "A" / "songs" / "x.txt").read_text() == "x"
