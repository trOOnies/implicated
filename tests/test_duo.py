import json
from pathlib import Path

import pytest
from conftest import Sample, add_sample

from implicated import duo
from implicated.library import Library
from implicated.models import AlbumIn, ArtistIn, SongIn
from implicated.userdata import FORMAT, export_data


def two_libraries(tmp_path: Path) -> tuple[Library, Sample, Library, Sample]:
    """Your library and a friend's, with the same sample in both."""
    me = Library(tmp_path / "me" / "implicated.db")
    friend = Library(tmp_path / "friend" / "implicated.db")
    return me, add_sample(me), friend, add_sample(friend)


def sections(d: duo.Duo) -> dict[str, list[str]]:
    return {s.id: [i.title for i in s.items] for s in d.sections if s.items}


def test_compare_sorts_both_tastes_into_sections(tmp_path):
    me, m, friend, f = two_libraries(tmp_path)
    mysterons, sour_times = 0, 1
    # Both love the artist; both have the album on listen later.
    for lib, s in ((me, m), (friend, f)):
        lib.toggle("artist", s.artist_id, "favorite")
        lib.toggle("album", s.album_id, "listen_later")
    # Mysterons: on repeat for both.
    for _ in range(3):
        me.record_play(m.song_ids[mysterons])
        friend.record_play(f.song_ids[mysterons])
    # Sour Times: on your listen later, a favorite of your friend's.
    me.toggle("song", m.song_ids[sour_times], "listen_later")
    friend.toggle("song", f.song_ids[sour_times], "favorite")
    # The friend owns the vinyl you want, and you own the CD they want.
    me.set_format(m.album_id, "vinyl", "wanted")
    friend.set_format(f.album_id, "vinyl", "owned")
    me.set_format(m.album_id, "cd", "owned")
    friend.set_format(f.album_id, "cd", "wanted")
    # An artist only the friend has, that they love.
    friend.create_artist(ArtistIn("Burial", genres=["Dubstep"], favorite=True))
    # Your favorite artist the friend does not have.
    me.create_artist(ArtistIn("Björk", favorite=True))

    d = duo.compare(export_data(me), export_data(friend, "Sol"), duo.library_ids(me))

    assert d.friend == "Sol" and d.you == "You"
    got = sections(d)
    assert got["love"] == ["Portishead"]
    assert got["later"] == ["Dummy"]
    assert got["repeat"] == ["Mysterons"]
    # They rated Dummy 5 stars (and played it 3 times); Sour Times is their favorite.
    assert got["queue"] == ["Dummy", "Sour Times"]
    assert got["discover"] == ["Burial"]
    assert got["share"] == ["Björk"]
    swap = next(s for s in d.sections if s.id == "swap")
    assert [i.note for i in swap.items] == ["borrow Sol's vinyl", "lend Sol your CD"]
    assert "recommends" not in got and "rotation" not in got

    discover = next(s for s in d.sections if s.id == "discover").items[0]
    assert discover.id is None and discover.genres == ["Dubstep"]
    queue = next(s for s in d.sections if s.id == "queue")
    assert queue.items[1].id == m.song_ids[sour_times]
    assert queue.items[1].note == "Sol: favorite"
    assert queue.song_ids == [m.song_ids[sour_times]]
    # Portishead is the only artist both like (Burial and Björk are on one side each).
    assert d.match == 33
    assert d.genres == [("Trip hop", 1)]
    assert d.shared_artists == 1 and d.shared_songs == 2


def test_friend_recommends_and_heavy_rotation(tmp_path):
    me, m, friend, f = two_libraries(tmp_path)
    me.set_rating("album", m.album_id, None)  # the sample's is 5 stars
    for _ in range(4):
        friend.record_play(f.song_ids[1])
    d = duo.compare(export_data(me), export_data(friend, "Sol"), duo.library_ids(me))
    got = sections(d)
    assert got["rotation"] == ["Sour Times"]
    # Dummy is liked by them and unplayed by you.
    assert got["recommends"] == ["Dummy"]
    # Once you played it, it's no longer a recommendation.
    me.record_play(m.song_ids[0])
    d = duo.compare(export_data(me), export_data(friend, "Sol"), duo.library_ids(me))
    assert "recommends" not in sections(d)


def test_names_match_loosely_and_singles_too(tmp_path):
    me = Library(tmp_path / "me.db")
    artist = me.create_artist(ArtistIn("Burial"))
    single = me.create_song(SongIn(artist.id, "Archangel – Remix"))
    me.toggle("song", single.id, "favorite")
    theirs = {
        "format": FORMAT,
        "version": 1,
        "owner": "Sol",
        "songs": [{"artist": "BURIAL", "title": "archangel - remix", "favorite": True}],
    }
    d = duo.compare(export_data(me), theirs, duo.library_ids(me))
    love = next(s for s in d.sections if s.id == "love")
    assert [(i.title, i.id) for i in love.items] == [("Archangel – Remix", single.id)]


def test_nothing_in_common(lib, sample):
    empty = {"format": FORMAT, "version": 1}
    d = duo.compare(export_data(lib), empty)
    assert all(not s.items for s in d.sections)
    assert d.match == 0 and d.friend == "?"


def test_friends_are_kept_apart_from_the_library(lib, sample):
    data = export_data(lib, "Sol Ibarra")
    friend = duo.save_friend(lib, data)
    assert friend.slug == "sol-ibarra" and friend.name == "Sol Ibarra"
    assert (lib.path.parent / "friends" / "sol-ibarra.json").is_file()
    renamed = duo.save_friend(lib, data, "  Sol ")
    assert renamed.slug == "sol" and duo.load_friend(lib, "sol")["owner"] == "Sol"
    assert duo.save_friend(lib, {**data, "owner": None}).slug == "friend"
    assert [f.name for f in duo.list_friends(lib)] == ["Friend", "Sol", "Sol Ibarra"]
    (lib.path.parent / "friends" / "broken.json").write_text("{}")
    assert len(duo.list_friends(lib)) == 3  # unreadable files are left out
    duo.delete_friend(lib, "sol")
    with pytest.raises(FileNotFoundError):
        duo.load_friend(lib, "sol")
    with pytest.raises(FileNotFoundError):
        duo.load_friend(lib, "../implicated")
    with pytest.raises(ValueError):
        duo.save_friend(lib, {"format": "nope"})


# Web


def friend_file(tmp_path: Path, owner: str = "Sol") -> bytes:
    other = Library(tmp_path / "other" / "implicated.db")
    s = add_sample(other)
    other.toggle("song", s.song_ids[0], "favorite")
    other.create_album(AlbumIn(other.create_artist(ArtistIn("Burial", favorite=True)).id, "Untrue"))
    return json.dumps(export_data(other, owner)).encode()


def test_duo_page_compares_with_an_uploaded_friend(client, web_sample, client_lib, tmp_path):
    client_lib.toggle("song", web_sample.song_ids[0], "favorite")
    page = client.get("/duo")
    assert page.status_code == 200 and "Add friend" in page.text
    assert "taste match" not in page.text
    assert client.get("/data", follow_redirects=False).headers["location"] == "/duo"

    added = client.post(
        "/duo/friends",
        files={"file": ("sol.json", friend_file(tmp_path), "application/json")},
        follow_redirects=False,
    )
    assert added.status_code == 303 and added.headers["location"] == "/duo?friend=sol"
    page = client.get("/duo?friend=sol")
    assert "taste match" in page.text and "You both love" in page.text
    assert f'href="/songs/{web_sample.song_ids[0]}"' in page.text
    assert "Burial" in page.text  # Discover
    # The first friend is picked when none is asked for.
    assert "You both love" in client.get("/duo").text
    # Nothing of the friend's went into your library.
    assert client_lib.find_artist("Burial") is None

    made = client.post("/duo/friends/sol/playlists/love")
    assert made.status_code == 200 and "Made" in made.text
    assert "playlists-changed" in made.headers["hx-trigger"]
    playlist = next(p for p in client_lib.list_playlists() if p.playlist.name.startswith("Sol"))
    assert playlist.playlist.name == "Sol & me: You both love" and playlist.song_count == 1
    assert "Refilled" in client.post("/duo/friends/sol/playlists/love").text
    assert client.post("/duo/friends/sol/playlists/discover").status_code == 404
    assert client.post("/duo/friends/nobody/playlists/love").status_code == 404

    gone = client.get("/duo?friend=nobody")
    assert "That friend" in gone.text
    deleted = client.post("/duo/friends/sol/delete", follow_redirects=False)
    assert deleted.status_code == 303
    assert "taste match" not in client.get("/duo").text
    assert client.post("/duo/friends/sol/delete").status_code == 404


def test_duo_refuses_files_that_are_not_exports(client):
    answer = client.post("/duo/friends", files={"file": ("x.json", b"[]", "application/json")})
    assert answer.status_code == 422 and "Nothing was added" in answer.text
    answer = client.post("/duo/friends", data={"name": "Sol"})
    assert answer.status_code == 422 and "Pick your friend" in answer.text


def test_duo_names_a_friend_on_upload(client, tmp_path):
    client.post(
        "/duo/friends",
        data={"name": "Juli"},
        files={"file": ("x.json", friend_file(tmp_path, owner=None), "application/json")},
    )
    assert "Juli" in client.get("/duo?friend=juli").text
