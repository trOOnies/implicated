from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from implicated import importer
from implicated.importer import import_dir, lyrics_file_name, match_cover, parse_duration
from implicated.library import Library
from implicated.web import create_app, sync_info

BANDS = """\
bands:
- name: My Chemical Romance
  origin: " American "
  creation_year: 2001
  languages: English
  genres: [Alternative Rock, Emo]
  description: An American rock band from New Jersey.
  wiki: https://en.wikipedia.org/wiki/My_Chemical_Romance
  members:
  - name: Gerard Way
    roles: Lead vocals
  - name: Frank Iero
    roles: [Rhythm guitar, Backing vocals]
    from: 2002
  - name: Bob Bryar
    roles: Drums
    from: 2004
    to: 2010
- name: Taylor Swift
  country: United States
  creation_year: 2003
  genres: [Pop]
  bio: An American singer-songwriter.
  wiki: https://evil.example.com/wiki/Taylor_Swift
"""

MCR_ALBUMS = """\
I Brought You My Bullets, You Brought Me Your Love:
  release_year: 2002
  songs:
  - name: Romance (instrumental)
    duration: 1:02
    no_lyrics: true
  - name: Our Lady of Sorrows
    duratioN: 2:05
Danger Days:
  release_year: 2010
  songs:
  - name: Look Alive, Sunshine
    duration: 0:29
  - name: Sing[a]
    duration: 4:30
"""

TS_ALBUMS = """\
"1989":
  release_year: 2014
  songs:
  - name: 22
    duration: 3:52
"""


@pytest.fixture
def info(tmp_path: Path) -> Path:
    root = tmp_path / "info"
    (root / "bands" / "My Chemical Romance" / "covers").mkdir(parents=True)
    (root / "bands" / "Taylor Swift" / "covers").mkdir(parents=True)
    (root / "bands.yaml").write_text(BANDS)
    (root / "bands" / "My Chemical Romance" / "albums.yaml").write_text(MCR_ALBUMS)
    (root / "bands" / "Taylor Swift" / "albums.yaml").write_text(TS_ALBUMS)
    covers = root / "bands" / "My Chemical Romance" / "covers"
    (covers / "bullets.jpg").write_bytes(b"bullets")
    (covers / "danger_days.webp").write_bytes(b"danger")
    (covers / "unrelated.png").write_bytes(b"?")
    (root / "bands" / "My Chemical Romance" / "band.avif").write_bytes(b"band")
    songs = root / "bands" / "My Chemical Romance" / "songs"
    songs.mkdir()
    (songs / "Sing.txt").write_text("Sing it out\nboy, you've got to see what tomorrow brings\n")
    (songs / "our lady of sorrows.txt").write_text("")
    (songs / "Unreleased.txt").write_text("words")
    return root


def test_parse_duration():
    assert parse_duration(62) == 62  # YAML 1.1 already turned 1:02 into seconds
    assert parse_duration("0:57") == 57
    assert parse_duration("1:02:03") == 3723
    assert parse_duration(None) is None
    with pytest.raises(ValueError):
        parse_duration("soon")


@pytest.mark.parametrize(
    "title, name",
    [
        ("Sing", "Sing.txt"),
        ("S/C/A/R/E/C/R/O/W", "S_C_A_R_E_C_R_O_W.txt"),
        ("Question...?", "Question....txt"),
        ("…Ready for It?", "…Ready for It.txt"),
        ('Title: "Quoted" <a|b> *', "Title Quoted ab.txt"),
    ],
)
def test_lyrics_file_name(title, name):
    assert lyrics_file_name(title) == name


def test_match_cover():
    files = [Path(n) for n in ("bullets.jpg", "the_black_parade.jpg", "taylor_swift_album.webp")]
    assert match_cover("I Brought You My Bullets, You Brought Me Your Love", files) == files[0]
    assert match_cover("The Black Parade", files) == files[1]
    assert match_cover("Taylor Swift", files) == files[2]
    assert match_cover("Fearless", files) is None
    assert match_cover("The Cover", [Path("the_cover.jpg")]) is None  # only filler words


def test_import(lib: Library, info: Path):
    report = import_dir(lib, info)

    assert report.created == {"artists": 2, "albums": 3, "songs": 5}
    assert report.covers == 2
    assert report.images == 1
    assert report.lyrics == 1
    assert report.lyrics_files == 3  # Romance, Look Alive, Sunshine and 22 had no file
    assert (info / "bands" / "Taylor Swift" / "songs" / "22.txt").read_text() == ""
    mcr_songs = info / "bands" / "My Chemical Romance" / "songs"
    assert sorted(p.name for p in mcr_songs.iterdir()) == [
        "Look Alive, Sunshine.txt",
        "Romance (instrumental).txt",
        "Sing.txt",
        "Unreleased.txt",
        "our lady of sorrows.txt",
    ]
    assert any("unrelated.png" in w for w in report.warnings)
    assert any("lyrics 'Unreleased.txt' matched no song" in w for w in report.warnings)
    assert any("Taylor Swift: ignored wiki" in w for w in report.warnings)

    mcr = lib.find_artist("my chemical romance")
    assert (mcr.country, mcr.formed_year) == ("American", 2001)
    assert mcr.languages == ["English"]
    assert mcr.genres == ["Alternative Rock", "Emo"]
    assert mcr.bio == "An American rock band from New Jersey."
    assert mcr.wiki == "https://en.wikipedia.org/wiki/My_Chemical_Romance"
    assert lib.image_path(mcr.id).read_bytes() == b"band"
    members = lib.list_members(mcr.id)
    assert [m.name for m in members] == ["Gerard Way", "Frank Iero", "Bob Bryar"]
    assert members[1].roles == ["Rhythm guitar", "Backing vocals"]
    assert (members[2].from_year, members[2].to_year) == (2004, 2010)

    bullets = lib.find_album(mcr.id, "I Brought You My Bullets, You Brought Me Your Love")
    songs = lib.list_songs(album_id=bullets.id, order="track")
    assert [(s.song.track_number, s.song.duration_seconds) for s in songs] == [(1, 62), (2, 125)]
    assert lib.cover_path(bullets.id).read_bytes() == b"bullets"

    danger = lib.find_album(mcr.id, "Danger Days")
    songs = lib.list_songs(album_id=danger.id, order="track")
    assert [s.song.title for s in songs] == ["Look Alive, Sunshine", "Sing"]
    assert [s.song.lyrics for s in songs] == [
        None,
        "Sing it out\nboy, you've got to see what tomorrow brings",
    ]
    assert lib.find_song(mcr.id, bullets.id, "Our Lady of Sorrows").lyrics is None
    assert [s.song.no_lyrics for s in lib.list_songs(album_id=bullets.id, order="track")] == [
        True,
        False,
    ]
    assert lib.list_albums(mcr.id)[0].no_lyrics_count == 1
    assert {a.album.title: a.lyrics_count for a in lib.list_albums(mcr.id)} == {
        "I Brought You My Bullets, You Brought Me Your Love": 0,
        "Danger Days": 1,
    }

    swift = lib.find_artist("Taylor Swift")
    assert lib.list_members(swift.id) == []
    assert swift.country == "United States"
    assert (swift.bio, swift.wiki, swift.image) == ("An American singer-songwriter.", None, None)
    album_1989 = lib.find_album(swift.id, "1989")
    assert [s.song.title for s in lib.list_songs(album_id=album_1989.id)] == ["22"]


YT = "dQw4w9WgXcQ"


def test_reimport_keeps_personal_data(lib: Library, info: Path):
    import_dir(lib, info)
    mcr = lib.find_artist("My Chemical Romance")
    album = lib.find_album(mcr.id, "Danger Days")
    song = lib.find_song(mcr.id, album.id, "Sing")
    lib.toggle_favorite("artist", mcr.id)
    lib.update_artist(mcr.id, replace(lib.get_artist(mcr.id), notes="Mine", listen_later=True))
    lib.update_album(album.id, replace(album, cd="owned", vinyl="wanted", listen_later=True))
    lib.update_song(song.id, replace(song, rating=5))
    lib.record_play(song.id)
    lib.update_song(song.id, replace(lib.get_song(song.id), mv=YT, listen_later=True))

    report = import_dir(lib, info)

    assert report.lyrics_files == 0
    assert report.created == {"artists": 0, "albums": 0, "songs": 0}
    assert report.updated == {"artists": 2, "albums": 3, "songs": 5}
    assert lib.get_artist(mcr.id).favorite
    assert (lib.get_artist(mcr.id).notes, lib.get_artist(mcr.id).listen_later) == ("Mine", True)
    kept = lib.get_album(album.id)
    assert (kept.cd, kept.vinyl, kept.listen_later) == ("owned", "wanted", True)
    assert lib.get_song(song.id).rating == 5
    assert (lib.get_song(song.id).mv, lib.get_song(song.id).listen_later) == (YT, True)
    assert lib.playlist_song_ids(lib.special_playlist("listen_later").id) == [song.id]
    assert lib.get_song_summary(song.id).play_count == 1
    assert len(list(lib.covers_dir.iterdir())) == 2
    assert len(list(lib.images_dir.iterdir())) == 1


def test_reimport_no_lyrics(lib: Library, info: Path):
    import_dir(lib, info)
    mcr = lib.find_artist("My Chemical Romance")
    danger = lib.find_album(mcr.id, "Danger Days")
    look_alive = lib.find_song(mcr.id, danger.id, "Look Alive, Sunshine")
    lib.update_song(look_alive.id, replace(look_alive, no_lyrics=True))  # set in the app
    albums = info / "bands" / "My Chemical Romance" / "albums.yaml"
    albums.write_text(albums.read_text().replace("no_lyrics: true", "no_lyrics: false"))

    import_dir(lib, info)
    bullets = lib.find_album(mcr.id, "I Brought You My Bullets, You Brought Me Your Love")
    assert not lib.find_song(mcr.id, bullets.id, "Romance (instrumental)").no_lyrics
    assert lib.get_song(look_alive.id).no_lyrics  # no key in the file: the app's choice stays

    albums.write_text(albums.read_text().replace("no_lyrics: false", "no_lyrics: maybe"))
    report = import_dir(lib, info)
    assert any("no_lyrics must be true or false" in w for w in report.warnings)


def test_reimport_updates_lyrics(lib: Library, info: Path):
    import_dir(lib, info)
    mcr = lib.find_artist("My Chemical Romance")
    sing = lib.find_song(mcr.id, lib.find_album(mcr.id, "Danger Days").id, "Sing")
    songs = info / "bands" / "My Chemical Romance" / "songs"

    (songs / "Sing.txt").write_text("New words")
    import_dir(lib, info)
    assert lib.get_song(sing.id).lyrics == "New words"

    (songs / "Sing.txt").write_text("")
    import_dir(lib, info)
    assert lib.get_song(sing.id).lyrics is None

    (songs / "Sing.txt").write_bytes(b"\xff\xfe")
    report = import_dir(lib, info)
    assert any("lyrics 'Sing.txt' is not UTF-8" in w for w in report.warnings)


def test_pages_show_members_and_covers(tmp_path: Path, info: Path):
    app = create_app(tmp_path / "local" / "implicated.db")
    lib = app.state.library
    import_dir(lib, info)
    mcr = lib.find_artist("My Chemical Romance")
    album = lib.find_album(mcr.id, "Danger Days")
    client = TestClient(app)

    page = client.get(f"/artists/{mcr.id}").text
    assert "Frank Iero" in page and "2002–" in page and "2004–2010" in page
    assert "Sings in English" in page
    assert "American · since 2001" in page
    assert f"/api/albums/{album.id}/cover" in page
    assert f"/api/artists/{mcr.id}/image" in page
    assert "An American rock band from New Jersey." in page
    assert 'href="https://en.wikipedia.org/wiki/My_Chemical_Romance"' in page

    cover = client.get(f"/api/albums/{album.id}/cover")
    assert cover.headers["content-type"] == "image/webp"
    assert cover.content == b"danger"

    members = client.get(f"/api/artists/{mcr.id}/members").json()
    assert members[0]["name"] == "Gerard Way"
    lineup = [{"name": "Gerard Way", "roles": ["Vocals"]}]
    assert client.put(f"/api/artists/{mcr.id}/members", json=lineup).status_code == 200
    assert [m.roles for m in lib.list_members(mcr.id)] == [["Vocals"]]


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_messy_files_import_with_warnings(lib: Library, tmp_path: Path):
    root = tmp_path / "messy"
    write(
        root / "bands.yaml",
        """\
bands:
- name: Band
  planet: Mars
  members:
  - name: Someone
    instrument: Kazoo
- creation_year: 1999
- name: No Albums
""",
    )
    write(
        root / "bands" / "Band" / "albums.yaml",
        """\
First:
  release_year: 2001
  cover: first.png
  label: Indie
  songs:
  - name: Fine
    duration: 3:00
  - name: Untimed
  - name: Broken
    duration: three minutes
  - duration: 1:00
Second:
  cover: missing.jpg
Third:
  songs:
""",
    )
    covers = root / "bands" / "Band" / "covers"
    covers.mkdir()
    (covers / "first.png").write_bytes(b"png")
    (covers / "readme.txt").write_text("not an image")
    for name in ("band.png", "band.jpg", "band.txt"):
        (root / "bands" / "Band" / name).write_bytes(name.encode())

    report = import_dir(lib, root)
    warnings = "\n".join(report.warnings)

    assert "ignored unknown key 'planet'" in warnings
    assert "ignored unknown key 'instrument'" in warnings
    assert "ignored unknown key 'label'" in warnings
    assert "band #2: skipped, it has no name" in warnings
    assert "No Albums: no bands/No Albums/albums.yaml" in warnings
    assert "Untimed: no duration" in warnings
    assert "Broken: 'three minutes' is not a duration" in warnings
    assert "track 4: skipped, it has no name" in warnings
    assert "Second: cover 'missing.jpg' not found" in warnings
    assert "Third: no cover file matches the title" in warnings
    assert "Band: several band images (band.jpg, band.png), using the first" in warnings
    assert report.covers == 1
    assert report.images == 1
    assert report.created == {"artists": 2, "albums": 3, "songs": 3}

    band = lib.find_artist("Band")
    assert [m.name for m in lib.list_members(band.id)] == ["Someone"]
    first = lib.find_album(band.id, "First")
    assert lib.cover_path(first.id).read_bytes() == b"png"
    assert lib.find_song(band.id, first.id, "Broken").duration_seconds is None
    assert lib.image_path(band.id).read_bytes() == b"band.jpg"


@pytest.mark.parametrize(
    "bands, albums, message",
    [
        ("bands: [\n", None, "bands.yaml is not valid YAML"),
        ("- just a list\n", None, "expected a 'bands' list"),
        ("bands:\n- just a name\n", None, "expected a mapping"),
        ("bands:\n- name: B\n  creation_year: soon\n", None, "'soon' is not a year"),
        ("bands:\n- name: B\n", "- not a mapping\n", "expected album titles"),
        ("bands:\n- name: B\n", "A:\n  kind: tape\n", "kind must be one of"),
    ],
)
def test_bad_files_fail_and_roll_back(lib: Library, tmp_path: Path, bands, albums, message):
    root = tmp_path / "bad"
    write(root / "bands.yaml", bands)
    if albums is not None:
        write(root / "bands" / "B" / "albums.yaml", albums)
    with pytest.raises(ValueError, match=message):
        import_dir(lib, root)
    assert lib.list_artists() == []


def test_report_summary():
    report = importer.Report(warnings=["careful"])
    report.count("songs", created=True)
    assert report.summary() == (
        "created 0 artists, 0 albums, 1 songs; updated 0 artists, 0 albums, 0 songs; 0 covers, "
        "0 band images, 0 lyrics, 0 new lyrics files"
        "\nwarning: careful"
    )


def test_cli(tmp_path: Path, info: Path, capsys: pytest.CaptureFixture):
    db = tmp_path / "cli.db"
    importer.main([str(info), "--db", str(db)])
    out = capsys.readouterr().out
    assert f"Imported {info} into {db}" in out
    assert "created 2 artists" in out
    assert Library(db).find_artist("Taylor Swift")

    with pytest.raises(SystemExit, match="import failed"):
        importer.main([str(tmp_path / "nowhere"), "--db", str(db)])


def test_app_imports_info_on_startup(tmp_path: Path, info: Path, monkeypatch):
    monkeypatch.setenv("IMPLICATED_INFO", str(info))
    app = create_app(tmp_path / "app.db")
    assert app.state.library.find_artist("My Chemical Romance")

    # An explicit folder wins over the environment, and a folder without bands.yaml is skipped.
    other = create_app(tmp_path / "other.db", info_dir=tmp_path / "empty")
    assert other.state.library.list_artists() == []


def test_broken_info_does_not_stop_the_app(
    lib: Library, tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    write(tmp_path / "broken" / "bands.yaml", "bands: [\n")
    sync_info(lib, tmp_path / "broken")
    assert "Could not import" in caplog.text
    assert lib.list_artists() == []


def test_startup_logs_import_warnings(lib: Library, info: Path, caplog: pytest.LogCaptureFixture):
    caplog.set_level("INFO", logger="uvicorn.error")
    sync_info(lib, info)
    assert "created 2 artists" in caplog.text
    assert "unrelated.png" in caplog.text
