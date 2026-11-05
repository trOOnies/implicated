from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from implicated import importer
from implicated.importer import (
    accept,
    import_dir,
    lyrics_file_name,
    match_cover,
    parse_duration,
)
from implicated.library import Library
from implicated.models import SongVideoIn
from implicated.web import create_app

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


@pytest.fixture
def media_lib(tmp_path: Path) -> Library:
    """A library that keeps lyrics and tabs in its own folder, like the app's."""
    return Library(tmp_path / "local" / "implicated.db", tmp_path / "local")


def test_import(media_lib: Library, info: Path):
    lib = media_lib
    inbox_before = sorted(p for p in info.rglob("*"))
    report = import_dir(lib, info)

    assert report.created == {"artists": 2, "albums": 3, "songs": 5}
    assert report.covers == 2
    assert report.images == 1
    assert report.lyrics == 1
    assert report.lyrics_files == 1  # Sing.txt has words; the empty one changes nothing
    assert sorted(p for p in info.rglob("*")) == inbox_before  # the inbox is only read
    # Every song has a lyrics file in the library's own folder.
    assert (lib.media_dir / "bands" / "Taylor Swift" / "songs" / "22.txt").read_text() == ""
    mcr_songs = lib.media_dir / "bands" / "My Chemical Romance" / "songs"
    assert sorted(p.name for p in mcr_songs.iterdir()) == [
        "Look Alive, Sunshine.txt",
        "Our Lady of Sorrows.txt",
        "Romance (instrumental).txt",
        "Sing.txt",
    ]
    assert (mcr_songs / "Sing.txt").read_text().startswith("Sing it out")
    assert [c.name for c in report.new][:2] == [
        "My Chemical Romance",
        "My Chemical Romance / I Brought You My Bullets, You Brought Me Your Love",
    ]
    assert report.updates == []
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
    lib.set_rating("artist", mcr.id, 5)
    lib.update_artist(mcr.id, replace(lib.get_artist(mcr.id), notes="Mine", listen_later=True))
    lib.update_album(album.id, replace(album, cd="owned", vinyl="wanted", listen_later=True))
    lib.update_song(song.id, replace(song, rating=5))
    lib.record_play(song.id)
    lib.update_song(song.id, replace(lib.get_song(song.id), mv=YT, listen_later=True))
    video = lib.add_video(song.id, SongVideoIn(YT, "Lesson"))

    report = import_dir(lib, info)

    assert report.lyrics_files == 0
    assert report.created == {"artists": 0, "albums": 0, "songs": 0}
    assert report.updated == {"artists": 2, "albums": 3, "songs": 5}
    assert lib.get_artist(mcr.id).favorite
    assert lib.get_artist(mcr.id).rating == 5
    assert (lib.get_artist(mcr.id).notes, lib.get_artist(mcr.id).listen_later) == ("Mine", True)
    kept = lib.get_album(album.id)
    assert (kept.cd, kept.vinyl, kept.listen_later) == ("owned", "wanted", True)
    assert lib.get_song(song.id).rating == 5
    assert (lib.get_song(song.id).mv, lib.get_song(song.id).listen_later) == (YT, True)
    assert lib.list_videos(song.id) == [video]
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


def test_import_featuring(lib: Library, info: Path):
    albums = info / "bands" / "My Chemical Romance" / "albums.yaml"
    albums.write_text(
        MCR_ALBUMS.replace(
            "    duration: 0:29\n",
            "    duration: 0:29\n    featuring: [taylor swift, Nobody, My Chemical Romance]\n",
        )
    )
    # Taylor Swift comes after My Chemical Romance in bands.yaml, and is still found.
    report = import_dir(lib, info)
    mcr, taylor = lib.find_artist("My Chemical Romance"), lib.find_artist("Taylor Swift")
    look_alive = lib.find_song(
        mcr.id, lib.find_album(mcr.id, "Danger Days").id, "Look Alive, Sunshine"
    )
    assert [a.id for a in lib.list_featuring(look_alive.id)] == [taylor.id]
    assert [s.song.id for s in lib.featured_songs(taylor.id)] == [look_alive.id]
    assert any("featured artist 'Nobody' is not in bands.yaml" in w for w in report.warnings)
    assert any("cannot feature its own artist" in w for w in report.warnings)

    # Without the key, what the app picked stays; an empty list takes them away.
    sing = lib.find_song(mcr.id, lib.find_album(mcr.id, "Danger Days").id, "Sing")
    lib.set_featuring(sing.id, [taylor.id])
    albums.write_text(
        MCR_ALBUMS.replace("    duration: 0:29\n", "    duration: 0:29\n    featuring: []\n")
    )
    import_dir(lib, info)
    assert lib.list_featuring(look_alive.id) == []
    assert [a.id for a in lib.list_featuring(sing.id)] == [taylor.id]


def test_reimport_unreleased(lib: Library, info: Path):
    albums = info / "bands" / "My Chemical Romance" / "albums.yaml"
    albums.write_text(
        albums.read_text()
        + "The Paper Kingdom:\n  release_year: 2012\n  unreleased: true\n  songs: []\n"
    )
    import_dir(lib, info)
    mcr = lib.find_artist("My Chemical Romance")
    kingdom = lib.find_album(mcr.id, "The Paper Kingdom")
    danger = lib.find_album(mcr.id, "Danger Days")
    assert (kingdom.unreleased, danger.unreleased) == (True, False)
    lib.update_album(danger.id, replace(danger, unreleased=True))  # set in the app
    albums.write_text(albums.read_text().replace("unreleased: true", "unreleased: false"))

    import_dir(lib, info)
    assert not lib.get_album(kingdom.id).unreleased
    assert lib.get_album(danger.id).unreleased  # no key in the file: the app's choice stays

    albums.write_text(albums.read_text().replace("unreleased: false", "unreleased: soon"))
    report = import_dir(lib, info)
    assert any("unreleased must be true or false" in w for w in report.warnings)


def test_reimport_with_other_dashes_renames(lib: Library, info: Path):
    albums = info / "bands" / "My Chemical Romance" / "albums.yaml"
    albums.write_text(
        albums.read_text()
        + "Conventional Weapons \u2013 Part 1:\n  songs:\n  - name: Boy Division \u2014 Demo\n"
    )
    import_dir(lib, info)
    mcr = lib.find_artist("My Chemical Romance")
    weapons = lib.find_album(mcr.id, "Conventional Weapons - Part 1")
    song = lib.find_song(mcr.id, weapons.id, "boy division - demo")
    lib.set_rating("album", weapons.id, 4)
    albums.write_text(albums.read_text().replace("\u2013", "-").replace("\u2014", "-"))

    report = import_dir(lib, info)
    assert report.created == {"artists": 0, "albums": 0, "songs": 0}
    renamed = lib.get_album(weapons.id)
    assert (renamed.title, renamed.rating) == ("Conventional Weapons - Part 1", 4)
    assert lib.get_song(song.id).title == "Boy Division - Demo"
    assert len(lib.list_albums(mcr.id)) == 3


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


def test_import_timed_lyrics(lib: Library, info: Path):
    import_dir(lib, info)
    mcr = lib.find_artist("My Chemical Romance")
    sing = lib.find_song(mcr.id, lib.find_album(mcr.id, "Danger Days").id, "Sing")
    songs = info / "bands" / "My Chemical Romance" / "songs"
    (songs / "Sing.txt").write_text("")
    (songs / "sing.lrc").write_text("[00:01.00]Timed words")

    report = import_dir(lib, info)
    assert lib.get_song(sing.id).lyrics == "Timed words"
    assert not any("sing.lrc" in w for w in report.warnings)

    (songs / "Sing.txt").write_text("Written words")
    import_dir(lib, info)
    assert lib.get_song(sing.id).lyrics == "Written words"

    (songs / "sing.lrc").write_bytes(b"\xff\xfe")
    (songs / "Nobody.lrc").write_text("[00:01.00]Words")
    report = import_dir(lib, info)
    assert lib.get_song(sing.id).lyrics == "Written words"
    assert any("lyrics 'sing.lrc' is not UTF-8" in w for w in report.warnings)
    assert any("lyrics 'Nobody.lrc' matched no song" in w for w in report.warnings)


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


def test_gif_covers_are_skipped(lib: Library, tmp_path: Path):
    root = tmp_path / "gifs"
    write(root / "bands.yaml", "bands:\n- name: Band\n")
    write(
        root / "bands" / "Band" / "albums.yaml",
        "Named:\n  cover: named.gif\nGuessed:\n  release_year: 2001\n",
    )
    covers = root / "bands" / "Band" / "covers"
    covers.mkdir()
    (covers / "named.gif").write_bytes(b"gif")
    (covers / "guessed.GIF").write_bytes(b"gif")
    (covers / "band.gif").write_bytes(b"gif")
    (root / "bands" / "Band" / "band.gif").write_bytes(b"artist gif")

    report = import_dir(lib, root)  # a GIF warns instead of stopping the import
    warnings = "\n".join(report.warnings)

    assert "Band / Named: cover 'named.gif' skipped, it is not one of .avif" in warnings
    assert "Band: cover 'guessed.GIF' skipped, covers cannot be GIFs" in warnings
    assert "Guessed: no cover file matches the title" not in warnings
    assert "matched no album" not in warnings
    assert report.covers == 0 and report.created["albums"] == 2
    band = lib.find_artist("Band")
    assert all(a.album.cover is None for a in lib.list_albums(band.id))
    assert lib.image_path(band.id).read_bytes() == b"artist gif"  # artist images still can be


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
    report.changes.append(importer.Change("song", "A / B / C"))
    report.changes.append(importer.Change("album", "A / B", "year", None, 2001))
    assert report.summary() == (
        "created 0 artists, 0 albums, 1 songs; updated 0 artists, 0 albums, 0 songs; 0 covers, "
        "0 band images, 0 lyrics, 0 lyrics files, 0 tabs, 0 concerts, 0 tours"
        "\nnew song: A / B / C"
        "\nA / B: year (none) -> 2001"
        "\nwarning: careful"
    )


def test_cli(tmp_path: Path, info: Path, capsys: pytest.CaptureFixture, monkeypatch):
    db = tmp_path / "cli" / "implicated.db"
    importer.main([str(info), "--db", str(db), "--preview"])
    out = capsys.readouterr().out
    assert "would do this" in out and "new artist: Taylor Swift" in out
    assert Library(db).list_artists() == []  # only a preview

    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    importer.main([str(info), "--db", str(db)])
    assert Library(db).list_artists() == []

    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    importer.main([str(info), "--db", str(db)])
    out = capsys.readouterr().out
    assert f"Imported {info} into {db}" in out
    assert "created 2 artists" in out
    assert "The inbox's files are now in" in out
    assert Library(db).find_artist("Taylor Swift")
    assert not (info / "bands.yaml").exists()  # archived
    assert (db.parent / "bands" / "My Chemical Romance" / "songs" / "Sing.txt").is_file()

    importer.main([str(info), "--db", str(db), "--yes"])
    assert "is empty: nothing to import" in capsys.readouterr().out

    write(tmp_path / "broken" / "bands.yaml", "bands: [\n")
    with pytest.raises(SystemExit, match="import failed"):
        importer.main([str(tmp_path / "broken"), "--db", str(db), "--yes"])
    assert (tmp_path / "broken" / "bands.yaml").is_file()  # a failed import archives nothing


def test_preview_changes_nothing(media_lib: Library, info: Path):
    lib = media_lib
    import_dir(lib, info)
    mcr = lib.find_artist("My Chemical Romance")
    lib.update_artist(mcr.id, replace(mcr, formed_year=2000))
    files_before = {p: p.read_bytes() for p in lib.media_dir.rglob("*") if p.is_file()}
    write(info / "bands" / "My Chemical Romance" / "songs" / "Sing.txt", "Changed words")
    write(info / "bands" / "New" / "albums.yaml", "First:\n  songs:\n  - name: One\n")
    (info / "bands.yaml").write_text(BANDS + "- name: New\n")

    report = import_dir(lib, info, dry_run=True)

    assert report.created == {"artists": 1, "albums": 1, "songs": 1}
    assert [c.name for c in report.new] == ["New", "New / First", "New / First / One"]
    changes = {(c.name, c.field): (c.old, c.new) for c in report.updates}
    assert changes[("My Chemical Romance", "formed_year")] == (2000, 2001)
    assert ("My Chemical Romance / Danger Days / Sing", "lyrics") in changes
    assert lib.find_artist("New") is None
    assert lib.get_artist(mcr.id).formed_year == 2000
    files_after = {p: p.read_bytes() for p in lib.media_dir.rglob("*") if p.is_file()}
    assert files_after == files_before


def test_keys_left_out_keep_the_apps_edits(lib: Library, info: Path):
    import_dir(lib, info)
    mcr = lib.find_artist("My Chemical Romance")
    danger = lib.find_album(mcr.id, "Danger Days")
    lib.update_artist(mcr.id, replace(mcr, genres=["Emo"], bio="Edited in the app"))
    lib.update_album(danger.id, replace(danger, year=2011))
    (info / "bands.yaml").write_text("bands:\n- name: My Chemical Romance\n  genres: [Rock]\n")
    (info / "bands" / "My Chemical Romance" / "albums.yaml").write_text("Danger Days: {}\n")

    report = import_dir(lib, info)

    kept = lib.get_artist(mcr.id)
    assert kept.genres == ["Rock"]  # in the file: the file wins
    assert kept.bio == "Edited in the app"  # left out: the app's stays
    assert kept.formed_year == 2001 and kept.wiki
    assert lib.get_album(danger.id).year == 2011
    assert [(c.field, c.old, c.new) for c in report.updates] == [("genres", ["Emo"], ["Rock"])]


def test_uids_follow_renames(lib: Library, info: Path):
    import_dir(lib, info)
    mcr = lib.find_artist("My Chemical Romance")
    danger = lib.find_album(mcr.id, "Danger Days")
    sing = lib.find_song(mcr.id, danger.id, "Sing")
    lib.update_artist(mcr.id, replace(mcr, name="MCR"))  # renamed in the app
    (info / "bands.yaml").write_text(
        f"bands:\n- name: My Chemical Romance\n  uid: {mcr.uid}\n  creation_year: 2001\n"
    )
    (info / "bands" / "My Chemical Romance" / "albums.yaml").write_text(
        f"Danger Days!:\n  uid: {danger.uid}\n  songs:\n  - name: Sing it\n    uid: {sing.uid}\n"
    )

    report = import_dir(lib, info)

    assert report.created == {"artists": 0, "albums": 0, "songs": 0}
    assert lib.get_artist(mcr.id).name == "My Chemical Romance"
    assert lib.get_album(danger.id).title == "Danger Days!"
    assert lib.get_song(sing.id).title == "Sing it"
    assert len(lib.list_albums(mcr.id)) == 2

    # A uid nobody has yet is kept by the new record, as an Export restores it.
    (info / "bands.yaml").write_text("bands:\n- name: Elsewhere\n  uid: 1234-abcd\n")
    import_dir(lib, info)
    assert lib.find_artist("Elsewhere").uid == "1234-abcd"


def test_lyrics_and_tabs_go_into_the_library(media_lib: Library, info: Path):
    lib = media_lib
    import_dir(lib, info)
    mcr = lib.find_artist("My Chemical Romance")
    sing = lib.find_song(mcr.id, lib.find_album(mcr.id, "Danger Days").id, "Sing")
    own = lib.lyrics_path(sing.id)
    own.write_text("Edited in my editor")
    songs = info / "bands" / "My Chemical Romance" / "songs"
    (songs / "Sing.txt").write_text("")  # empty: changes nothing
    write(songs / "Sing.lrc", "[00:01.00]Timed")
    tabs = info / "bands" / "My Chemical Romance" / "tabs"
    tabs.mkdir()
    (tabs / "sing.gp5").write_bytes(b"tab")
    (tabs / "Nobody.gp5").write_bytes(b"tab")

    report = import_dir(lib, info)

    assert own.read_text() == "Edited in my editor"
    assert lib.get_song(sing.id).lyrics == "Edited in my editor"
    assert lib.lrc_path(sing.id).read_text() == "[00:01.00]Timed"
    assert lib.tab_path(sing.id).read_bytes() == b"tab"
    assert (report.lyrics_files, report.tabs) == (1, 1)
    assert any("tab 'Nobody.gp5' matched no song" in w for w in report.warnings)

    (songs / "Sing.txt").write_text("New words from the inbox")
    import_dir(lib, info)
    assert own.read_text() == "New words from the inbox"  # words in the inbox win


def test_accept_archives_the_inbox(media_lib: Library, info: Path):
    report, archive = accept(media_lib, info)
    assert report.created["artists"] == 2
    assert archive.parent == info / "archive"
    assert (archive / "bands.yaml").read_text() == BANDS
    assert (archive / "bands" / "My Chemical Romance" / "covers" / "bullets.jpg").is_file()
    assert sorted(p.name for p in info.iterdir()) == ["archive"]
    # The library has what it needs: the covers and pictures were copied in.
    mcr = media_lib.find_artist("My Chemical Romance")
    assert media_lib.image_path(mcr.id).read_bytes() == b"band"


def test_inbox_csvs_and_userdata(media_lib: Library, tmp_path: Path):
    lib = media_lib
    root = tmp_path / "csvs"
    (lib.media_dir / "concerts.csv").write_text(
        "date;band;tour;country;city;Want to go;Going\n2026-12-09;A;T;AR;Rosario;true;false\n"
    )
    write(
        root / "concerts.csv",
        "date;band;city;Going\n2026-12-09;A;Rosario;true\n2027-01-02;B;Lima;true\n",
    )
    write(root / "tours.csv", "band;tour\nA;T\nB;New Tour\n")

    preview = import_dir(lib, root, dry_run=True)
    assert (preview.concerts, preview.tours) == (1, 2)
    assert "Lima" not in (lib.media_dir / "concerts.csv").read_text()

    report = import_dir(lib, root)
    assert (report.concerts, report.tours) == (1, 2)
    text = (lib.media_dir / "concerts.csv").read_text()
    assert text.count("Rosario") == 1 and "2027-01-02;B;;;Lima;false;true" in text
    assert "B;New Tour" in (lib.media_dir / "tours.csv").read_text()

    artist = lib.create_artist(importer.ArtistIn("Solo"))
    write(
        root / "userdata.json",
        '{"format": "implicated-user-data", "version": 1,'
        ' "artists": [{"name": "Solo", "favorite": true}]}',
    )
    report = import_dir(lib, root)
    assert lib.get_artist(artist.id).favorite
    assert "your data:" in report.totals()


def test_future_creation_year_is_ignored(lib: Library, tmp_path: Path):
    root = tmp_path / "future"
    next_year = date.today().year + 1
    write(root / "bands.yaml", f"bands:\n- name: Soon\n  creation_year: {next_year}\n")
    report = import_dir(lib, root)
    assert f"Soon: ignored creation_year {next_year}, it is in the future" in report.warnings
    assert lib.find_artist("Soon").formed_year is None


def test_the_same_title_twice_in_an_album_warns(lib: Library, tmp_path: Path):
    root = tmp_path / "twice"
    write(root / "bands.yaml", "bands:\n- name: Band\n")
    write(
        root / "bands" / "Band" / "albums.yaml",
        "A:\n  songs:\n  - name: Song\n  - name: Other\n  - name: song\n",
    )
    report = import_dir(lib, root)
    assert "Band / A / song: listed as tracks 1 and 3, which are one song" in "\n".join(
        report.warnings
    )
