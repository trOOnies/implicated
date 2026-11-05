import io
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from implicated import inbox


def make_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buffer.getvalue()


def fill(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)


def test_archive_moves_everything_and_empties_the_inbox(tmp_path: Path):
    info = tmp_path / "info"
    fill(info, {"bands.yaml": "bands: []", "bands/A/albums.yaml": "{}", "bands/A/songs/x.txt": ""})
    assert inbox.list_files(info) == [
        Path("bands/A/albums.yaml"), Path("bands/A/songs/x.txt"), Path("bands.yaml"),
    ]  # fmt: skip
    when = datetime(2026, 11, 4, 15, 30)

    first = inbox.archive(info, now=when)

    assert first == info / "archive" / "2026-11-04T153000"
    assert (first / "bands" / "A" / "songs" / "x.txt").is_file()
    assert sorted(p.name for p in info.iterdir()) == ["archive"]
    assert inbox.list_files(info) == []
    assert inbox.archive(info) is None  # nothing left to archive

    fill(info, {"tours.csv": "band;tour\n"})
    second = inbox.archive(info, now=when)  # same second: a name of its own
    assert second.name == "2026-11-04T153000-2"
    archives = inbox.list_archives(info)
    assert [(a.name, a.files) for a in archives] == [
        ("2026-11-04T153000-2", 1),
        ("2026-11-04T153000", 3),
    ]
    assert archives[0].size == len("band;tour\n")


def test_delete_archives(tmp_path: Path):
    info = tmp_path / "info"
    fill(info, {"bands.yaml": ""})
    one = inbox.archive(info, now=datetime(2026, 1, 1))
    fill(info, {"bands.yaml": ""})
    inbox.archive(info, now=datetime(2026, 1, 2))
    fill(tmp_path, {"keep/me.txt": "precious"})

    for name in ("..", "../keep", "/etc", "nope", ""):
        with pytest.raises(ValueError, match="no archive"):
            inbox.delete_archive(info, name)
    assert (tmp_path / "keep" / "me.txt").is_file()

    inbox.delete_archive(info, one.name)
    assert [a.name for a in inbox.list_archives(info)] == ["2026-01-02T000000"]
    assert inbox.delete_all_archives(info) == 1
    assert inbox.list_archives(info) == []


def test_upload_single_files(tmp_path: Path):
    info = tmp_path / "info"
    assert inbox.add_upload(info, "Bands.YAML", b"bands: []") == [Path("bands.yaml")]
    assert inbox.add_upload(info, "concerts.csv", b"date;band\n") == [Path("concerts.csv")]
    export = b'{"format": "implicated-user-data", "version": 1}'
    assert inbox.add_upload(info, "sol.json", export) == [Path("userdata.json")]
    with pytest.raises(ValueError, match="not an implicated data export"):
        inbox.add_upload(info, "other.json", b"{}")
    with pytest.raises(ValueError, match="zip an albums.yaml"):
        inbox.add_upload(info, "albums.yaml", b"{}")
    with pytest.raises(ValueError, match="upload a .zip"):
        inbox.add_upload(info, "notes.txt", b"hi")


def test_upload_a_zip(tmp_path: Path):
    info = tmp_path / "info"
    data = make_zip(
        {
            "info/bands.yaml": b"bands: []",
            "info/bands/A/albums.yaml": b"{}",
            "info/archive/old/bands.yaml": b"old",  # imported already
        }
    )
    assert inbox.add_upload(info, "backup.zip", data) == [
        Path("bands/A/albums.yaml"),
        Path("bands.yaml"),
    ]
    assert not (info / "archive").exists()

    flat = make_zip({"bands.yaml": b"bands: [x]", "tours.csv": b"band;tour\n"})
    assert inbox.add_upload(info, "export.zip", flat) == [Path("bands.yaml"), Path("tours.csv")]
    assert (info / "bands.yaml").read_text() == "bands: [x]"  # replaced


@pytest.mark.parametrize("name", ["../evil.txt", "/abs.txt", "a/../../evil.txt", "C:/x.txt"])
def test_upload_refuses_paths_outside_the_inbox(tmp_path: Path, name: str):
    with pytest.raises(ValueError, match="outside its folder"):
        inbox.add_upload(tmp_path / "info", "bad.zip", make_zip({name: b"x", "ok.txt": b"y"}))
    assert not (tmp_path / "evil.txt").exists()
    assert inbox.list_files(tmp_path / "info") == []


def test_upload_refuses_broken_zips(tmp_path: Path):
    with pytest.raises(ValueError, match="not a zip"):
        inbox.add_upload(tmp_path, "x.zip", b"nope")
    with pytest.raises(ValueError, match="no files"):
        inbox.add_upload(tmp_path, "x.zip", make_zip({}))
