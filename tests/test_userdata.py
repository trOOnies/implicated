import json
from pathlib import Path

import pytest
from conftest import Sample, add_sample

from implicated.library import Library
from implicated.models import AlbumIn, ArtistIn, FolderIn, PlaylistIn, SongIn, SongVideoIn
from implicated.userdata import (
    FORMAT,
    VERSION,
    export_data,
    export_file_name,
    import_data,
    main,
    read_file,
)


def personalize(lib: Library, s: Sample) -> None:
    """Give the sample every kind of personal data."""
    guest = lib.create_artist(ArtistIn("Massive Attack"))
    lib.toggle("artist", s.artist_id, "favorite")
    lib.set_rating("artist", s.artist_id, 4)
    lib.toggle("album", s.album_id, "listen_later")
    lib.set_format(s.album_id, "vinyl", "wanted")
    lib.set_format(s.album_id, "cd", "owned")
    first, second = s.song_ids
    lib.toggle("song", first, "favorite")
    lib.set_rating("song", first, 5)
    lib.set_featuring(first, [guest.id])
    lib.add_video(first, SongVideoIn("https://youtu.be/dQw4w9WgXcQ?t=30", "Live"))
    lib.record_play(first, "2026-10-01T20:00:00+00:00")
    lib.record_play(first, "2026-10-02T20:00:00+00:00")
    lib.toggle("song", second, "listen_later")
    playlist = lib.create_playlist(PlaylistIn("Rainy days"))
    lib.set_playlist_songs(playlist.id, [second, first])


def test_export_names_records_and_leaves_defaults_out(lib, sample):
    personalize(lib, sample)
    data = export_data(lib, owner=" Sol ")
    assert data["format"] == FORMAT and data["version"] == VERSION
    assert data["owner"] == "Sol"
    portishead = next(a for a in data["artists"] if a["name"] == "Portishead")
    assert portishead == {
        "name": "Portishead",
        "origin": "United Kingdom",
        "formed_year": 1991,
        "genres": ["Trip hop"],
        "favorite": True,
        "rating": 4,
    }
    assert data["albums"] == [
        {
            "artist": "Portishead",
            "title": "Dummy",
            "year": 1994,
            "kind": "album",
            "rating": 5,
            "listen_later": True,
            "cd": "owned",
            "vinyl": "wanted",
        }
    ]
    mysterons, sour_times = data["songs"]
    assert mysterons["featuring"] == ["Massive Attack"]
    assert mysterons["videos"] == [{"youtube": "dQw4w9WgXcQ", "title": "Live", "start_seconds": 30}]
    assert mysterons["plays"] == ["2026-10-01T20:00:00+00:00", "2026-10-02T20:00:00+00:00"]
    assert "favorite" not in sour_times and sour_times["listen_later"] is True
    # Built-in playlists follow the marks, so only your own are listed.
    assert data["playlists"] == [
        {
            "name": "Rainy days",
            "songs": [
                {"artist": "Portishead", "album": "Dummy", "title": "Sour Times"},
                {"artist": "Portishead", "album": "Dummy", "title": "Mysterons"},
            ],
        }
    ]
    json.dumps(data)  # it is plain JSON


def test_round_trip_into_a_fresh_library(lib, sample, tmp_path):
    personalize(lib, sample)
    data = export_data(lib)
    other = Library(tmp_path / "other" / "implicated.db")
    s = add_sample(other)
    other.create_artist(ArtistIn("Massive Attack"))

    report = import_data(other, json.loads(json.dumps(data)))

    assert report.matched == {"artists": 2, "albums": 1, "songs": 2}
    assert report.skipped == {"artists": 0, "albums": 0, "songs": 0}
    assert report.plays_added == 2 and report.playlists_created == 1
    assert report.warnings == []
    # Ids differ, but everything personal is the same.
    assert {k: v for k, v in export_data(other).items() if k != "exported_at"} == {
        k: v for k, v in data.items() if k != "exported_at"
    }
    first = other.get_song(s.song_ids[0])
    assert first.favorite and first.rating == 5
    favorites = other.special_playlist("favorites")
    assert other.playlist_song_ids(favorites.id) == [first.id]


def test_importing_twice_adds_plays_once_and_replaces_playlists(lib, sample):
    personalize(lib, sample)
    data = export_data(lib)
    report = import_data(lib, data)
    assert report.plays_added == 0
    assert report.playlists_created == 0 and report.playlists_updated == 1
    assert len(export_data(lib)["songs"][0]["plays"]) == 2
    assert len(lib.list_videos(sample.song_ids[0])) == 1


def test_playlists_keep_their_folder(lib, sample):
    road = lib.create_folder(FolderIn("Road"))
    lib.create_playlist(PlaylistIn("Drive"), folder_id=road.id)
    lib.create_playlist(PlaylistIn("Loose"))
    playlists = export_data(lib)["playlists"]
    assert playlists == [{"name": "Drive", "folder": "Road", "songs": []},
                         {"name": "Loose", "songs": []}]  # fmt: skip

    other = Library(lib.path.with_name("other.db"))
    add_sample(other)
    report = import_data(other, export_data(lib) | {"playlists": playlists + [
        {"name": "Odd", "folder": "Setlists", "songs": []}]})  # fmt: skip
    assert report.warnings == [
        "playlist 'Odd': Setlists is a built-in folder; it stays where it is"
    ]
    folders = {f.folder.name: [p.playlist.name for p in f.playlists] for f in other.list_folders()}
    assert folders["Road"] == ["Drive"] and {"Loose", "Odd"} <= set(folders["Default"])
    with pytest.raises(ValueError, match="folder must be a name"):
        import_data(other, export_data(lib) | {"playlists": [{"name": "X", "folder": 3}]})


def test_import_sets_marks_the_file_leaves_out(lib, sample):
    lib.toggle("song", sample.song_ids[0], "favorite")
    data = export_data(lib)
    lib.toggle("song", sample.song_ids[1], "favorite")  # after the export
    import_data(lib, data)
    assert not lib.get_song(sample.song_ids[1]).favorite
    assert lib.get_song(sample.song_ids[0]).favorite


def test_import_matches_names_loosely_and_reports_what_is_missing(lib, sample):
    data = {
        "format": FORMAT,
        "version": 1,
        "artists": [{"name": "PORTISHEAD ", "favorite": True}, {"name": "Nobody"}],
        "albums": [{"artist": "portishead", "title": "dummy", "rating": 9}],
        "songs": [
            {"artist": "Portishead", "album": "Dummy", "title": "sour times", "favorite": True},
            {"artist": "Portishead", "album": "Third", "title": "Machine Gun"},
            {"artist": "Portishead", "title": "Mysterons"},  # a single: not this song
        ],
        "playlists": [
            {"name": "Mix", "songs": [{"artist": "Portishead", "album": "Dummy", "title": "X"}]},
            {"name": "favorites", "songs": []},
        ],
    }
    report = import_data(lib, data)
    assert report.matched == {"artists": 1, "albums": 1, "songs": 1}
    assert report.skipped == {"artists": 1, "albums": 0, "songs": 2}
    assert lib.get_artist(sample.artist_id).favorite
    assert lib.get_album(sample.album_id).rating is None  # 9 stars is refused
    assert lib.get_song(sample.song_ids[1]).favorite
    text = "\n".join(report.warnings)
    assert "Nobody" in text and "rating" in text and "Third" in text
    assert "built-in" in text and "Mix" in text
    mix = next(p for p in lib.list_playlists() if p.playlist.name == "Mix")
    assert mix.song_count == 0


@pytest.mark.parametrize(
    "data, message",
    [
        ({"format": "something else"}, "not an implicated"),
        ({"format": FORMAT}, "version"),
        ({"format": FORMAT, "version": VERSION + 1}, "newer"),
        ({"format": FORMAT, "version": 1, "songs": {}}, "songs must be a list"),
        ({"format": FORMAT, "version": 1, "artists": [{"favorite": True}]}, "no name"),
    ],
)
def test_broken_files_are_refused(lib, sample, data, message):
    with pytest.raises(ValueError, match=message):
        import_data(lib, data)


def test_a_broken_file_changes_nothing(lib, sample):
    data = {
        "format": FORMAT,
        "version": 1,
        "artists": [{"name": "Portishead", "favorite": True}],
        "songs": [{"artist": "Portishead", "album": "Dummy", "title": "Mysterons",
                   "plays": "not a list"}],
    }  # fmt: skip
    with pytest.raises(ValueError):
        import_data(lib, data)
    assert not lib.get_artist(sample.artist_id).favorite


def test_read_file(tmp_path):
    path = tmp_path / "x.json"
    path.write_text("nope")
    with pytest.raises(ValueError, match="not a JSON"):
        read_file(path)
    assert read_file(b'{"format": "implicated-user-data", "version": 1}')["version"] == 1


def test_export_file_name():
    assert export_file_name({"exported_at": "2026-11-03T10:00:00", "owner": "Sol Ibarra"}) == (
        "implicated-sol-ibarra-2026-11-03.json"
    )
    assert export_file_name({"exported_at": "2026-11-03T10:00:00"}) == (
        "implicated-2026-11-03.json"
    )


def test_command_line(lib, sample, tmp_path, capsys):
    lib.toggle("album", sample.album_id, "favorite")
    out = tmp_path / "me.json"
    main(["--db", str(lib.path), "export", str(out), "--owner", "Me"])
    assert json.loads(out.read_text())["owner"] == "Me"
    other = tmp_path / "other.db"
    add_sample(Library(other))
    main(["--db", str(other), "import", str(out)])
    assert "updated 1 artists, 1 albums, 2 songs" in capsys.readouterr().out
    assert Library(other).list_albums()[0].album.favorite
    (tmp_path / "bad.json").write_text("{}")
    with pytest.raises(SystemExit, match="import failed"):
        main(["--db", str(other), "import", str(tmp_path / "bad.json")])


# API and web


def test_api_export_and_import(client, web_sample, client_lib):
    client_lib.toggle("song", web_sample.song_ids[0], "favorite")
    data = client.get("/api/data/export", params={"owner": "Sol"}).json()
    assert data["owner"] == "Sol"
    download = client.get("/api/data/export", params={"download": True})
    assert "attachment" in download.headers["content-disposition"]
    assert download.json()["songs"] == data["songs"]

    client_lib.toggle("song", web_sample.song_ids[0], "favorite")
    answer = client.post("/api/data/import", json=data)
    assert answer.status_code == 200 and answer.json()["matched"]["songs"] == 2
    assert client_lib.get_song(web_sample.song_ids[0]).favorite
    assert client.post("/api/data/import", json={"format": "x"}).status_code == 422


def test_data_page_imports_an_uploaded_file(client, web_sample, client_lib):
    page = client.get("/duo")
    assert page.status_code == 200 and "Download my data" in page.text
    client_lib.toggle("artist", web_sample.artist_id, "favorite")
    body = json.dumps(export_data(client_lib, "Sol")).encode()
    client_lib.toggle("artist", web_sample.artist_id, "favorite")

    answer = client.post("/duo/import", files={"file": ("me.json", body, "application/json")})
    assert answer.status_code == 200
    assert "(Sol's data)" in answer.text and "library-changed" in answer.headers["hx-trigger"]
    assert client_lib.get_artist(web_sample.artist_id).favorite

    bad = client.post("/duo/import", files={"file": ("x.json", b"[]", "application/json")})
    assert bad.status_code == 422 and "Nothing was imported" in bad.text
    assert client.post("/duo/import", data={}).status_code == 422


def test_album_singles_round_trip(lib, tmp_path: Path):
    """Songs without an album are named without one."""
    artist = lib.create_artist(ArtistIn("Burial"))
    lib.create_album(AlbumIn(artist.id, "Untrue"))
    single = lib.create_song(SongIn(artist.id, "Claustro"))
    lib.toggle("song", single.id, "favorite")
    data = export_data(lib)
    assert data["songs"] == [{"artist": "Burial", "title": "Claustro", "favorite": True}]
    lib.toggle("song", single.id, "favorite")
    assert import_data(lib, data).matched["songs"] == 1
    assert lib.get_song(single.id).favorite
