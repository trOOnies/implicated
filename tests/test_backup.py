import json
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from implicated import backup, inbox
from implicated.importer import accept
from implicated.library import Library
from implicated.models import AlbumIn, ArtistIn, MemberIn, PlaylistIn, SongIn


def full_library(root: Path) -> Library:
    lib = Library(root / "implicated.db", root)
    a = lib.create_artist(
        ArtistIn(
            "Portishead",
            "United Kingdom",
            1991,
            ["Trip hop"],
            ["English"],
            "Bristol band",
            "https://en.wikipedia.org/wiki/Portishead_(band)",
            favorite=True,
        )  # fmt: skip
    )
    b = lib.create_artist(ArtistIn("Beth Gibbons"))
    lib.set_members(a.id, [MemberIn("Beth Gibbons", ["Vocals"], 1991)])
    (root.parent / "img").mkdir(exist_ok=True)
    (root.parent / "img" / "p.jpg").write_bytes(b"picture")
    (root.parent / "img" / "dummy.png").write_bytes(b"cover")
    lib.set_image(a.id, root.parent / "img" / "p.jpg")
    dummy = lib.create_album(AlbumIn(a.id, "Dummy", 1994, rating=5, vinyl="owned"))
    lib.set_cover(dummy.id, root.parent / "img" / "dummy.png")
    lib.create_album(AlbumIn(a.id, "Third", 2008, kind="album", unreleased=True))
    s1 = lib.create_song(SongIn(a.id, "Mysterons", dummy.id, 1, 302, favorite=True))
    s2 = lib.create_song(
        SongIn(a.id, "Sour Times", dummy.id, 2, 3725, no_lyrics=True, explicit=True)
    )
    lib.set_featuring(s1.id, [b.id])
    lib.lyrics_path(s1.id).write_text("Did you really want\n")
    lib.reload_lyrics(s1.id)
    lib.new_tab_path(s1.id).parent.mkdir(parents=True, exist_ok=True)
    lib.new_tab_path(s1.id).write_bytes(b"gp5")
    lib.record_play(s2.id, "2026-10-30T21:14:00+00:00")
    road = lib.create_playlist(PlaylistIn("Road trip"))
    lib.add_to_playlist(road.id, [s2.id])
    (root / "concerts.csv").write_text(
        "date;band;tour;country;city;Want to go;Going\n"
        "2026-12-09;Portishead;;UK;Bristol;true;true\n"
    )
    return lib


def test_backup_and_restore(tmp_path: Path):
    lib = full_library(tmp_path / "local")
    target = backup.write_backup(lib, tmp_path / "b.zip")
    with zipfile.ZipFile(target) as z:
        names = set(z.namelist())
    assert {"manifest.json", "implicated.db", "concerts.csv"} <= names
    assert "bands/Portishead/songs/Mysterons.txt" in names
    assert backup.read_manifest(target)["artists"] == 2

    # Something goes wrong; the backup brings it all back.
    lib.delete_artist(lib.find_artist("Portishead").id)
    (tmp_path / "local" / "concerts.csv").unlink()
    saved = backup.restore(target, lib.path)
    assert saved.name.startswith("local-before-restore-") and saved.is_file()
    again = Library(lib.path, lib.media_dir)
    portishead = again.find_artist("Portishead")
    assert portishead.favorite and again.image_path(portishead.id).read_bytes() == b"picture"
    assert (tmp_path / "local" / "concerts.csv").is_file()
    assert "Did you really" in (tmp_path / "local" / "bands" / "Portishead" / "songs" /
                                "Mysterons.txt").read_text()  # fmt: skip

    with pytest.raises(ValueError, match="not an implicated backup"):
        backup.restore(tmp_path / "local" / "concerts.csv", lib.path)
    with pytest.raises(ValueError, match="out of the library folder"):
        backup.write_backup(again, tmp_path / "local" / "inside.zip")
        backup.restore(tmp_path / "local" / "inside.zip", lib.path)


def snapshot(lib: Library, tmp_path: Path, name: str) -> dict[str, object]:
    """What an export holds, minus the moments it was made."""
    path = tmp_path / f"{name}.zip"
    backup.write_export(lib, path, owner="Sol")
    out = {}
    with zipfile.ZipFile(path) as z:
        for entry in z.namelist():
            data = z.read(entry)
            if entry.endswith(".json"):
                data = json.loads(data)
                data.pop("exported_at", None)
                data.pop("created_at", None)
            out[entry] = data
    return out


def test_export_round_trips_through_the_inbox(tmp_path: Path):
    lib = full_library(tmp_path / "local")
    first = snapshot(lib, tmp_path, "first")
    bands = yaml.safe_load(first["bands.yaml"])["bands"]
    portishead = next(b for b in bands if b["name"] == "Portishead")
    assert portishead["uid"] == lib.find_artist("Portishead").uid
    albums = yaml.safe_load(first["bands/Portishead/albums.yaml"])
    assert albums["Dummy"]["cover"] == "Dummy.png"
    assert albums["Dummy"]["songs"][1] == {
        "name": "Sour Times",
        "uid": lib.find_song(
            lib.find_artist("Portishead").id,
            albums and lib.find_album(lib.find_artist("Portishead").id, "Dummy").id,
            "Sour Times",
        ).uid,  # fmt: skip
        "duration": "1:02:05",
        "no_lyrics": True,
        "explicit": True,
    }
    assert "bands/Portishead/tabs/Mysterons.gp5" in first

    # A fresh library, rebuilt from the export through the inbox.
    fresh = Library(tmp_path / "fresh" / "implicated.db", tmp_path / "fresh")
    info = tmp_path / "info"
    with open(tmp_path / "first.zip", "rb") as f:
        inbox.add_upload(info, "first.zip", f.read())
    report, _archive = accept(fresh, info)
    assert report.warnings == []
    assert snapshot(fresh, tmp_path, "second") == first

    # Renamed since, then the same export imported again: updated, not duplicated.
    p = fresh.find_artist("Portishead")
    fresh.update_artist(p.id, replace(p, name="Portishead!"))
    with open(tmp_path / "first.zip", "rb") as f:
        inbox.add_upload(info, "first.zip", f.read())
    accept(fresh, info)
    assert sorted(a.artist.name for a in fresh.list_artists()) == ["Beth Gibbons", "Portishead"]


def test_export_warns_about_songs_without_an_album(tmp_path: Path):
    lib = Library(tmp_path / "local" / "implicated.db", tmp_path / "local")
    a = lib.create_artist(ArtistIn("Solo"))
    lib.create_song(SongIn(a.id, "Single"))
    report = backup.write_export(lib, tmp_path / "x.zip")
    assert report.warnings == ["Solo / Single: left out, it has no album"]


def test_cli(tmp_path: Path, capsys, monkeypatch):
    lib = full_library(tmp_path / "local")
    monkeypatch.chdir(tmp_path)
    backup.main(["backup", "b.zip", "--db", str(lib.path)])
    backup.main(["export", "--db", str(lib.path), "--owner", "Sol"])
    out = capsys.readouterr().out
    assert "Backed up" in out and "exported 2 artists, 2 albums, 2 songs" in out
    assert list(tmp_path.glob("implicated-export-*.zip"))
    backup.main(["restore", "b.zip", "--db", str(lib.path)])
    assert "Restored b.zip" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="restore failed"):
        backup.main(["restore", "nope.zip", "--db", str(lib.path)])
