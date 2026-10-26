from dataclasses import replace

import pytest
from conftest import Sample
from fastapi.testclient import TestClient

from implicated.library import Library
from implicated.models import AlbumIn, ArtistIn, Member, MemberIn, SongIn
from implicated.web import editor
from implicated.web.views import duration, percent, stars, tenure, when

# Template helpers


@pytest.mark.parametrize(
    "seconds, text", [(None, "–"), (0, "–"), (59, "0:59"), (306, "5:06"), (3723, "1:02:03")]
)
def test_duration(seconds, text):
    assert duration(seconds) == text


def test_stars_and_when():
    assert stars(None) == "–"
    assert stars(3) == "★★★☆☆"
    assert when(None) == "never"
    assert when("2024-05-01T13:45:10+00:00") == "2024-05-01 13:45"


def test_percent():
    cases = [(0, 0), (0, 3), (2, 3), (3, 3)]
    assert [percent(part, whole) for part, whole in cases] == ["–", "0%", "66%", "100%"]


@pytest.mark.parametrize(
    "years, text",
    [
        ((None, None), ""),
        ((2002, None), "2002–"),
        ((2001, 2004), "2001–2004"),
        ((None, 2017), "–2017"),
    ],
)
def test_tenure(years, text):
    assert tenure(Member("X", from_year=years[0], to_year=years[1])) == text


# Pages


def test_empty_library_overview(client: TestClient):
    page = client.get("/")
    assert page.status_code == 200
    assert "Your library is empty" in page.text
    assert "Plays, last 30 days" not in page.text


def test_overview_needs_no_plays(client: TestClient, client_lib: Library, web_sample: Sample):
    page = client.get("/").text
    assert "Latest releases" in page and "Dummy" in page
    assert "Songs per artist" in page and "Genres" in page and "Trip hop" in page
    assert "Album ratings" in page  # Dummy is rated
    assert "Plays, last 30 days" not in page and "Recently played" not in page
    assert "Favorite artists" not in page

    client_lib.record_play(web_sample.song_ids[0])
    client_lib.toggle_favorite("artist", web_sample.artist_id)
    page = client.get("/fragments/view").text
    assert "Plays, last 30 days" in page and "Recently played" in page and "Mysterons" in page
    assert "Favorite artists" in page and "No favorite albums yet" in page


def test_albums_page(client: TestClient, client_lib: Library, web_sample: Sample, image):
    other = client_lib.create_artist(ArtistIn("Air"))
    client_lib.create_album(AlbumIn(other.id, "Moon Safari", 1998))
    client_lib.set_cover(web_sample.album_id, image())

    page = client.get("/albums").text
    assert "Dummy" in page and "Moon Safari" in page
    assert f"/api/albums/{web_sample.album_id}/cover" in page
    filtered = client.get("/albums", params={"q": "air"}).text
    assert "Moon Safari" in filtered and "Dummy" not in filtered
    assert "No albums here yet" in client.get("/fragments/albums", params={"q": "zzz"}).text


def test_artist_pages(client: TestClient, client_lib: Library, web_sample: Sample):
    client_lib.set_members(web_sample.artist_id, [MemberIn("Beth Gibbons", ["Vocals"], 1991)])
    assert "No artists yet" in client.get("/artists", params={"q": "zzz"}).text
    assert "Portishead" in client.get("/fragments/artists").text

    page = client.get(f"/artists/{web_sample.artist_id}").text
    assert "Beth Gibbons" in page and "1991–" in page
    assert "Album length (minutes)" in page and "Plays per album" not in page

    client_lib.record_play(web_sample.song_ids[0])
    assert "Plays per album" in client.get(f"/artists/{web_sample.artist_id}").text
    assert client.get("/artists/99").status_code == 404

    base = f"/fragments/artists/{web_sample.artist_id}"
    assert "Beth Gibbons" in client.get(f"{base}/info").text
    assert 'name="languages"' in client.get(f"{base}/edit").text
    assert "Dummy" in client.get(f"{base}/albums").text
    assert "Sour Times" in client.get(f"{base}/songs").text


def test_create_artist_form(client: TestClient, client_lib: Library):
    form = {"name": "Air", "genres": "electronic, downtempo", "languages": "French, English"}
    response = client.post("/artists", data=form | {"formed_year": "1995", "favorite": "on"})
    assert response.status_code == 200
    assert "artists-changed" in response.headers["hx-trigger"]
    artist = client_lib.find_artist("Air")
    assert (artist.genres, artist.languages) == (["electronic", "downtempo"], ["French", "English"])
    assert artist.favorite

    wiki = "https://en.wikipedia.org/wiki/Zero_7"
    client.post("/artists", data={"name": "Zero 7", "bio": "A duo.", "wiki": wiki})
    zero7 = client_lib.find_artist("Zero 7")
    assert (zero7.bio, zero7.wiki) == ("A duo.", wiki)
    page = client.get(f"/artists/{zero7.id}").text
    assert f'href="{wiki}"' in page and 'rel="noopener noreferrer nofollow"' in page

    error = client.post("/artists", data={"name": "Bad", "wiki": "https://evil.example.com/"})
    assert error.status_code == 422 and "Wikipedia article" in error.text
    assert client_lib.find_artist("Bad") is None

    error = client.post("/artists", data={"name": "Air", "formed_year": "soon"})
    assert error.status_code == 422 and "formed year must be a number" in error.text
    assert client.post("/artists", data={"name": " "}).status_code == 422


def test_update_and_delete_artist(client: TestClient, client_lib: Library, web_sample: Sample):
    url = f"/artists/{web_sample.artist_id}"
    response = client.post(url, data={"name": "Portishead", "languages": "English"})
    assert response.status_code == 200 and "Sings in English" in response.text
    assert client.post(url, data={"name": ""}).status_code == 422
    assert client.post(url, data={"name": "P", "wiki": "javascript:alert(1)"}).status_code == 422
    assert client.post("/artists/99", data={"name": "X"}).status_code == 404

    deleted = client.post(f"{url}/delete")
    assert deleted.headers["hx-redirect"] == "/artists"
    assert client_lib.find_artist("Portishead") is None


def test_album_forms(client: TestClient, client_lib: Library, web_sample: Sample):
    created = client.post(
        "/albums", data={"artist_id": web_sample.artist_id, "title": "Third", "year": "2008"}
    )
    assert created.status_code == 200 and "albums-changed" in created.headers["hx-trigger"]
    third = client_lib.find_album(web_sample.artist_id, "Third")
    assert third.year == 2008

    assert client.post("/albums", data={"artist_id": "99", "title": "X"}).status_code == 422
    bad_kind = {"artist_id": web_sample.artist_id, "title": "X", "kind": "tape"}
    assert client.post("/albums", data=bad_kind).status_code == 422

    url = f"/albums/{third.id}"
    update = {"artist_id": web_sample.artist_id, "title": "Third", "rating": "4", "notes": "!"}
    response = client.post(url, data=update)
    assert response.status_code == 200 and "★★★★☆" in response.text
    assert client.post(url, data={**update, "rating": "8"}).status_code == 422

    deleted = client.post(f"{url}/delete")
    assert deleted.headers["hx-redirect"] == f"/artists/{web_sample.artist_id}"
    assert client.post(f"{url}/delete").status_code == 404


def test_album_pages(client: TestClient, client_lib: Library, web_sample: Sample, image):
    page = client.get(f"/albums/{web_sample.album_id}").text
    assert "Mysterons" in page and "cover big none" in page
    client_lib.set_cover(web_sample.album_id, image())
    base = f"/fragments/albums/{web_sample.album_id}"
    assert "cover big" in client.get(f"{base}/info").text
    assert 'name="kind"' in client.get(f"{base}/edit").text
    assert "Sour Times" in client.get(f"{base}/songs").text
    assert client.get("/albums/99").status_code == 404


def test_lyrics_on_album_pages(client: TestClient, client_lib: Library, web_sample: Sample):
    client_lib.lyrics_path(web_sample.song_ids[0]).write_text("Take a ride")
    client_lib.reload_lyrics(web_sample.song_ids[0])
    page = client.get(f"/albums/{web_sample.album_id}").text
    assert "50% with lyrics" in page
    assert page.count("missing lyrics</span>") == 1  # only Sour Times
    assert f'href="/songs/{web_sample.song_ids[0]}"' in page
    assert ">50%</td>" in client.get(f"/fragments/artists/{web_sample.artist_id}/albums").text
    assert "50% lyrics" in client.get("/albums").text
    # The artist's song list does not carry the mark; the Songs page has a column for it.
    assert "missing lyrics</span>" not in client.get(f"/artists/{web_sample.artist_id}").text
    songs = client.get("/songs").text
    assert "Missing lyrics</th>" in songs
    assert songs.count('<td data-sort="2"><span class="tag missing"') == 1
    assert songs.count('<td data-sort="0"></td>') == 1


def test_songs_with_no_lyrics(client: TestClient, client_lib: Library, web_sample: Sample):
    sour_times = client_lib.get_song(web_sample.song_ids[1])
    client_lib.update_song(sour_times.id, replace(sour_times, no_lyrics=True))
    # Mysterons has no lyrics yet; Sour Times has none to have, so it does not count.
    page = client.get(f"/albums/{web_sample.album_id}").text
    assert "0% with lyrics" in page and "1 have none to have" in page
    assert page.count("missing lyrics</span>") == 1 and page.count("no lyrics</span>") == 1
    songs = client.get("/songs").text
    assert songs.count('<td data-sort="1"><span class="tag none"') == 1
    assert "This song has no lyrics." in client.get(f"/songs/{sour_times.id}").text

    client_lib.lyrics_path(web_sample.song_ids[0]).write_text("Take a ride")
    client_lib.reload_lyrics(web_sample.song_ids[0])
    assert "100% with lyrics" in client.get(f"/albums/{web_sample.album_id}").text
    assert ">100%</td>" in client.get(f"/fragments/artists/{web_sample.artist_id}/albums").text

    # The edit row keeps the choice, and unticking it clears it.
    edit = client.get(f"/fragments/songs/{sour_times.id}/edit").text
    assert 'name="no_lyrics" checked' in edit
    form = {"title": "Sour Times", "album_id": web_sample.album_id}
    client.post(f"/songs/{sour_times.id}", data=form | {"no_lyrics": "on"})
    assert client_lib.get_song(sour_times.id).no_lyrics
    client.post(f"/songs/{sour_times.id}", data=form)
    assert not client_lib.get_song(sour_times.id).no_lyrics

    added = {"artist_id": web_sample.artist_id, "title": "Interlude", "no_lyrics": "on"}
    assert client.post("/songs", data=added).status_code == 200
    assert client_lib.find_song(web_sample.artist_id, None, "Interlude").no_lyrics


def test_tables_are_sortable(client: TestClient, web_sample: Sample):
    page = client.get(f"/artists/{web_sample.artist_id}").text
    assert page.count('<table class="sortable">') == 1  # discography
    assert page.count('<table class="songs sortable">') == 1
    assert "/static/sort.js" in page
    assert '<th data-type="number">Plays</th>' in page
    assert client.get("/static/sort.js").status_code == 200


def test_song_page(client: TestClient, client_lib: Library, web_sample: Sample):
    song_id = web_sample.song_ids[0]
    page = client.get(f"/songs/{song_id}").text
    assert "Mysterons" in page and "track 1 of" in page and "Dummy" in page
    assert "No lyrics yet" in page and "bands/Portishead/songs/Mysterons.txt" in page

    lyrics_file = client_lib.lyrics_path(song_id)
    assert lyrics_file.read_text() == ""  # made along with the song
    lyrics_file.write_text("Take a ride\nup the hill\n")
    page = client.get(f"/songs/{song_id}").text
    assert "Take a ride\nup the hill" in page and "No lyrics yet" not in page
    assert client_lib.get_song(song_id).lyrics == "Take a ride\nup the hill"

    single = client_lib.create_song(SongIn(web_sample.artist_id, "Wandering Star / Live"))
    page = client.get(f"/songs/{single.id}").text
    assert "single" in page and "Wandering Star _ Live.txt" in page
    assert client.get("/songs/99").status_code == 404


def test_songs_page(client: TestClient, client_lib: Library, web_sample: Sample):
    client_lib.toggle_favorite("song", web_sample.song_ids[1])
    page = client.get("/songs", params={"order": "bogus"}).text
    assert page.index("Mysterons") < page.index("Sour Times")

    favorites = client.get("/fragments/songs", params={"favorites": "1", "order": "plays"}).text
    assert "Sour Times" in favorites and "Mysterons" not in favorites
    assert "No songs here yet" in client.get("/fragments/songs", params={"q": "zzz"}).text


def test_song_forms(client: TestClient, client_lib: Library, web_sample: Sample):
    on_album = {
        "artist_id": web_sample.artist_id,
        "album_id": web_sample.album_id,
        "fixed_album": "1",
        "title": "Roads",
        "track_number": "3",
        "duration": "5:05",
    }
    response = client.post("/songs", data=on_album)
    assert response.status_code == 200
    assert 'value="4"' in response.text  # the form moves on to the next track
    roads = client_lib.find_song(web_sample.artist_id, web_sample.album_id, "Roads")
    assert roads.duration_seconds == 305

    single = {"artist_id": web_sample.artist_id, "title": "Wandering Star", "duration": "293"}
    assert client.post("/songs", data=single).status_code == 200
    assert (
        client_lib.find_song(web_sample.artist_id, None, "Wandering Star").duration_seconds == 293
    )

    bad = client.post("/songs", data=single | {"duration": "long"})
    assert bad.status_code == 422 and "duration must look like 3:45" in bad.text
    assert client.post("/songs", data=single | {"track_number": "x"}).status_code == 422


def test_song_rows(client: TestClient, client_lib: Library, web_sample: Sample):
    song_id = web_sample.song_ids[0]
    assert "Mysterons" in client.get(f"/fragments/songs/{song_id}", params={"ctx": "album"}).text
    edit = client.get(f"/fragments/songs/{song_id}/edit", params={"ctx": "nonsense"}).text
    assert 'value="all"' in edit and "Dummy" in edit

    form = {"title": "Mysterons", "album_id": web_sample.album_id, "rating": "5", "ctx": "artist"}
    updated = client.post(f"/songs/{song_id}", data=form)
    assert updated.status_code == 200 and "★★★★★" in updated.text
    assert client.post(f"/songs/{song_id}", data=form | {"rating": "7"}).status_code == 422
    assert client.post("/songs/99", data=form).status_code == 404

    played = client.post(f"/songs/{song_id}/play", data={"ctx": "all"})
    assert "plays-changed" in played.headers["hx-trigger"]
    assert client_lib.get_song_summary(song_id).play_count == 1

    deleted = client.post(f"/songs/{song_id}/delete")
    assert "songs-changed" in deleted.headers["hx-trigger"]
    assert client.post(f"/songs/{song_id}/delete").status_code == 404


def test_favorite_toggle(client: TestClient, web_sample: Sample):
    url = f"/favorite/album/{web_sample.album_id}"
    assert "♥" in client.post(url).text
    assert "♡" in client.post(url).text
    assert client.post("/favorite/playlist/1").status_code == 404
    assert client.post("/favorite/song/99").status_code == 404


def test_help_page_and_layout(client: TestClient):
    page = client.get("/help")
    assert page.status_code == 200
    assert "Importing from files" in page.text
    assert 'href="/help" aria-current="page"' in page.text
    assert 'href="/docs">API</a>' in page.text
    assert "© 2026 Implicated by trOOnies" in page.text
    assert "Made with Claude Code" in page.text
    assert client.get("/docs").status_code == 200


def test_open_lyrics_file(client: TestClient, client_lib: Library, web_sample: Sample, monkeypatch):
    opened = []
    monkeypatch.setattr(editor, "open_file", opened.append)
    song_id = web_sample.song_ids[0]
    url = f"/songs/{song_id}/lyrics/open"
    page = client.get(f"/songs/{song_id}").text
    assert f'hx-post="{url}"' in page and "Reload lyrics" in page

    assert client.post(url).status_code == 403  # not from the page
    path = client_lib.lyrics_path(song_id)
    path.unlink()
    response = client.post(url, headers={"HX-Request": "true"})
    assert response.status_code == 200 and "Opened <code>Mysterons.txt</code>" in response.text
    assert opened == [path] and path.exists()
    assert client.post("/songs/99/lyrics/open", headers={"HX-Request": "true"}).status_code == 404

    def fail(path):
        raise OSError("no program to open files with")

    monkeypatch.setattr(editor, "open_file", fail)
    failed = client.post(url, headers={"HX-Request": "true"})
    assert "no program to open files with" in failed.text

    path.write_text("Take a ride\n")
    assert "Take a ride" in client.get(f"/fragments/songs/{song_id}/lyrics").text
    path.write_bytes(b"\xff")
    broken = client.get(f"/fragments/songs/{song_id}/lyrics").text
    assert "Could not read the lyrics file" in broken and "Take a ride" in broken


def test_open_command(monkeypatch, tmp_path):
    path = tmp_path / "Song.txt"
    monkeypatch.setattr(editor.sys, "platform", "linux")
    monkeypatch.setattr(editor, "_is_wsl", lambda: False)
    monkeypatch.setattr(editor.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert editor.open_command(path) == ["xdg-open", str(path)]

    monkeypatch.setattr(editor, "_is_wsl", lambda: True)
    assert editor.open_command(path) == ["wslview", str(path)]

    monkeypatch.setattr(editor.shutil, "which", lambda name: None if name == "wslview" else name)
    monkeypatch.setattr(
        editor.subprocess,
        "run",
        lambda *a, **k: editor.subprocess.CompletedProcess(a, 0, "\\\\wsl$\\Song.txt\n"),
    )
    assert editor.open_command(path) == ["explorer.exe", "\\\\wsl$\\Song.txt"]

    monkeypatch.setattr(editor.sys, "platform", "darwin")
    assert editor.open_command(path) == ["open", str(path)]

    monkeypatch.setattr(editor.sys, "platform", "linux")
    monkeypatch.setattr(editor.shutil, "which", lambda name: None)
    with pytest.raises(OSError, match="no program"):
        editor.open_command(path)


def test_listen_later_toggle(client: TestClient, client_lib: Library, web_sample: Sample):
    for kind, record_id in [
        ("artist", web_sample.artist_id),
        ("album", web_sample.album_id),
        ("song", web_sample.song_ids[0]),
    ]:
        on = client.post(f"/later/{kind}/{record_id}")
        assert "⧗" in on.text and 'aria-pressed="true"' in on.text
        assert "library-changed" in on.headers["hx-trigger"]
    assert client_lib.get_album(web_sample.album_id).listen_later
    overview = client.get("/").text
    assert "Artists to listen to" in overview and "Songs to listen to" in overview
    assert f'href="/songs/{web_sample.song_ids[0]}">Mysterons' in overview
    assert "⧖" in client.post(f"/later/album/{web_sample.album_id}").text
    assert client.post("/later/playlist/1").status_code == 404
    assert client.post("/later/song/99").status_code == 404


def test_playing_clears_listen_later(client: TestClient, client_lib: Library, web_sample: Sample):
    song_id = web_sample.song_ids[0]
    client.post(f"/later/song/{song_id}")
    assert "Mysterons" in client.get("/fragments/songs", params={"later": "1"}).text
    row = client.post(f"/songs/{song_id}/play", data={"ctx": "album"}).text
    assert 'class="later"' in row and "⧖" in row
    assert not client_lib.get_song(song_id).listen_later
    assert "Mysterons" not in client.get("/fragments/songs", params={"later": "1"}).text

    client.post(f"/later/song/{song_id}")
    info = client.post(f"/songs/{song_id}/play", data={"ctx": "page"}).text
    assert 'id="song-info"' in info and "2 plays" in info and "⧖" in info


def test_album_ownership_forms_and_filters(
    client: TestClient, client_lib: Library, web_sample: Sample
):
    album_id = web_sample.album_id
    page = client.get(f"/albums/{album_id}").text
    assert 'role="group" aria-label="CD"' in page and 'role="group" aria-label="Vinyl"' in page
    assert 'name="cd"' not in client.get(f"/fragments/albums/{album_id}/edit").text

    picked = client.post(f"/albums/{album_id}/formats/cd", data={"state": "owned"})
    assert picked.status_code == 200 and "library-changed" in picked.headers["hx-trigger"]
    assert 'class="owned on"' in picked.text and "compact" not in picked.text
    assert picked.text.count('aria-pressed="true"') == 1
    compact = client.post(
        f"/albums/{album_id}/formats/vinyl", data={"state": "wanted", "compact": "1"}
    )
    assert 'class="picker compact"' in compact.text and '"compact": "1"' in compact.text
    assert client.post(f"/albums/{album_id}/formats/cd", data={"state": "lost"}).status_code == 422
    assert (
        client.post(f"/albums/{album_id}/formats/tape", data={"state": "owned"}).status_code == 404
    )
    assert client.post("/albums/99/formats/cd", data={"state": "owned"}).status_code == 404
    info = client.get(f"/fragments/albums/{album_id}/info").text
    assert 'class="owned on"' in info and 'class="wanted on"' in info

    # Saving the edit form keeps the formats.
    form = {"artist_id": web_sample.artist_id, "title": "Dummy", "listen_later": "on"}
    assert client.post(f"/albums/{album_id}", data=form | {"cd": "wanted"}).status_code == 200
    album = client_lib.get_album(album_id)
    assert (album.cd, album.vinyl, album.listen_later) == ("owned", "wanted", True)
    new = {"artist_id": web_sample.artist_id, "title": "Third", "cd": "wanted", "vinyl": "wanted"}
    created = client.post("/albums", data=new)
    assert created.status_code == 200
    assert client_lib.find_album(web_sample.artist_id, "Third").cd == "wanted"

    page = client.get("/albums").text
    for label in ("Wanted (CD)", "Owned (CD)", "Wanted (Vinyl)", "Owned (Vinyl)"):
        assert f">{label}</option>" in page

    def shown(**params):
        text = client.get("/fragments/albums", params=params).text
        return [title for title in ("Dummy", "Third") if f"<strong>{title}</strong>" in text]

    assert shown() == ["Dummy", "Third"]
    assert shown(own="cd-owned") == ["Dummy"]
    assert shown(own="cd-wanted") == ["Third"]
    assert shown(own="vinyl-wanted") == ["Dummy", "Third"]
    assert shown(own="vinyl-owned") == []
    assert shown(own="nonsense") == ["Dummy", "Third"]
    assert shown(later="1") == ["Dummy"]
    assert shown(later="1", own="cd-wanted") == []
    assert 'value="cd-owned" selected' in client.get("/albums", params={"own": "cd-owned"}).text
    artist_page = client.get(f"/artists/{web_sample.artist_id}").text
    assert "<th>Formats</th>" in artist_page and 'class="picker compact"' in artist_page


def test_artist_notes(client: TestClient, client_lib: Library, web_sample: Sample):
    artist_id = web_sample.artist_id
    assert 'name="notes"' in client.get(f"/fragments/artists/{artist_id}/edit").text
    form = {"name": "Portishead", "notes": "Saw them in 2022", "listen_later": "on"}
    info = client.post(f"/artists/{artist_id}", data=form).text
    assert "Notes" in info and "Saw them in 2022" in info
    artist = client_lib.get_artist(artist_id)
    assert (artist.notes, artist.listen_later) == ("Saw them in 2022", True)


def test_song_notes_and_music_video(client: TestClient, client_lib: Library, web_sample: Sample):
    song_id = web_sample.song_ids[0]
    page = client.get(f"/songs/{song_id}").text
    assert "Edit notes &amp; MV" in page and "Music video" not in page

    edit = client.get(f"/fragments/songs/{song_id}/details").text
    assert 'name="notes"' in edit and 'name="mv"' in edit
    form = {"notes": "Best on headphones", "mv": "https://youtu.be/dQw4w9WgXcQ?t=3"}
    info = client.post(f"/songs/{song_id}/details", data=form)
    assert info.status_code == 200 and "Best on headphones" in info.text
    assert "Music video" in info.text
    assert 'src="https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"' in info.text
    assert 'href="https://www.youtube.com/watch?v=dQw4w9WgXcQ"' in info.text
    song = client_lib.get_song(song_id)
    assert (song.notes, song.mv, song.rating) == ("Best on headphones", "dQw4w9WgXcQ", None)

    bad = client.post(f"/songs/{song_id}/details", data={"mv": "https://evil.com/x"})
    assert bad.status_code == 422 and "YouTube" in bad.text and "https://evil.com/x" in bad.text
    assert client_lib.get_song(song_id).mv == "dQw4w9WgXcQ"
    assert client.post("/songs/99/details", data=form).status_code == 404
    assert ">MV</small>" in client.get(f"/fragments/songs/{song_id}").text

    # The edit row and the add form take it too, also as a bare id.
    row = {"title": "Mysterons", "mv": "a1B2c3D4e5F", "ctx": "album"}
    assert client.post(f"/songs/{song_id}", data=row).status_code == 200
    assert client_lib.get_song(song_id).mv == "a1B2c3D4e5F"
    assert client.post(f"/songs/{song_id}", data=row | {"mv": "short"}).status_code == 422
    new = {"artist_id": web_sample.artist_id, "title": "Roads", "mv": "a1B2c3D4e5F"}
    assert client.post("/songs", data=new).status_code == 200
    bad_new = client.post("/songs", data=new | {"title": "Glory Box", "mv": "nope"})
    assert bad_new.status_code == 422 and "YouTube" in bad_new.text
