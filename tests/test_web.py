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
    client_lib.set_rating("album", third.id, 4)
    assert 'name="rating"' not in client.get(f"/fragments/albums/{third.id}/edit").text
    update = {"artist_id": web_sample.artist_id, "title": "Third", "rating": "1", "notes": "!"}
    response = client.post(url, data=update)
    assert response.status_code == 200 and response.text.count('class="star on"') == 4
    assert client_lib.get_album(third.id).rating == 4  # the stars rate, not the form
    assert "tag unreleased" not in response.text
    response = client.post(url, data={**update, "unreleased": "on"})
    assert client_lib.get_album(third.id).unreleased and "tag unreleased" in response.text
    assert "tag unreleased" in client.get(f"/artists/{web_sample.artist_id}").text
    assert client.post(url, data={**update, "title": " "}).status_code == 422

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

    assert 'name="rating"' not in edit
    client_lib.set_rating("song", song_id, 5)
    form = {"title": "Mysterons", "album_id": web_sample.album_id, "rating": "2", "ctx": "artist"}
    updated = client.post(f"/songs/{song_id}", data=form)
    assert updated.status_code == 200 and updated.text.count('class="star on"') == 5
    assert client.post(f"/songs/{song_id}", data=form | {"title": " "}).status_code == 422
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


def test_song_videos(client: TestClient, client_lib: Library, web_sample: Sample):
    song_id = web_sample.song_ids[0]
    page = client.get(f"/songs/{song_id}").text
    assert "<h2>Videos</h2>" in page and "No videos yet" in page

    form = {"title": "Intro riff lesson", "youtube": "https://youtu.be/aaaaaaaaaaa?t=1m5s"}
    added = client.post(f"/songs/{song_id}/videos", data=form)
    assert added.status_code == 200 and "videos-changed" in added.headers["hx-trigger"]
    assert "Intro riff lesson" in added.text and "from 1:05" in added.text
    assert 'src="https://www.youtube-nocookie.com/embed/aaaaaaaaaaa?start=65"' in added.text
    assert 'href="https://www.youtube.com/watch?v=aaaaaaaaaaa&amp;t=65s"' in added.text
    client.post(f"/songs/{song_id}/videos", data={"youtube": "bbbbbbbbbbb", "start": "0:30"})
    second, first = client_lib.list_videos(song_id)[::-1]
    assert (second.start_seconds, second.title) == (30, None)

    bad = client.post(f"/songs/{song_id}/videos", data={"title": "Solo", "youtube": "nope"})
    assert bad.status_code == 422 and "YouTube" in bad.text and 'value="Solo"' in bad.text
    assert client.post(f"/songs/{song_id}/videos", data={"youtube": ""}).status_code == 422
    assert (
        client.post(
            f"/songs/{song_id}/videos", data={"youtube": "bbbbbbbbbbb", "start": "x"}
        ).status_code
        == 422
    )
    assert client.post("/songs/99/videos", data=form).status_code == 404
    assert "▷2" in client.get(f"/fragments/songs/{song_id}").text

    edit = client.get(f"/fragments/videos/{second.id}/edit").text
    assert f'hx-post="/videos/{second.id}"' in edit and 'value="0:30"' in edit
    saved = client.post(f"/videos/{second.id}", data={"youtube": "bbbbbbbbbbb", "title": "Cover"})
    assert saved.status_code == 200 and "Cover" in saved.text
    assert client_lib.get_video(second.id).start_seconds is None
    failed = client.post(f"/videos/{second.id}", data={"youtube": "nope"})
    assert failed.status_code == 422 and f'hx-post="/videos/{second.id}"' in failed.text

    moved = client.post(f"/videos/{second.id}/move", data={"offset": "-1"})
    assert moved.status_code == 200
    assert [v.id for v in client_lib.list_videos(song_id)] == [second.id, first.id]
    assert client.post(f"/videos/{second.id}/move", data={"offset": "x"}).status_code == 422
    deleted = client.post(f"/videos/{second.id}/delete")
    assert "Cover" not in deleted.text and "Intro riff lesson" in deleted.text
    assert client.post(f"/videos/{second.id}/delete").status_code == 404
    assert "Intro riff lesson" in client.get(f"/fragments/songs/{song_id}/videos").text


def test_playlist_pages(client: TestClient, client_lib: Library, web_sample: Sample):
    first, second = web_sample.song_ids
    assert 'href="/playlists"' in client.get("/").text
    listing = client.get("/playlists").text
    assert "Favorites</a> <span" in listing and "Listen Later</a> <span" in listing
    assert client.post("/playlists", data={"name": " "}).status_code == 422
    assert client.post("/playlists", data={"name": "Favorites"}).status_code == 422
    created = client.post("/playlists", data={"name": "Night drive"})
    assert "playlists-changed" in created.headers["hx-trigger"]
    pid = client_lib.list_playlists()[-1].playlist.id
    assert "Night drive" in client.get("/fragments/playlists").text

    page = client.get(f"/playlists/{pid}").text
    assert "Night drive" in page and "No songs here yet" in page
    assert 'aria-current="page">Playlists' in page

    # Add one song from its page, then the whole album from the album page.
    song_page = client.get(f"/songs/{second}").text
    assert "Add to playlist" in song_page and "Night drive (0 songs)" in song_page
    assert "In no playlist yet." in song_page
    added = client.post(f"/songs/{second}/playlists", data={"playlist_id": pid})
    assert (
        "Added to Night drive." in added.text and "playlists-changed" in added.headers["hx-trigger"]
    )
    assert f'href="/playlists/{pid}">Night drive</a>' in added.text
    member_of = client.get(f"/fragments/songs/{second}/member-of").text
    assert f'href="/playlists/{pid}">Night drive</a>' in member_of
    again = client.post(f"/songs/{second}/playlists", data={"playlist_id": pid}).text
    assert "It is already in Night drive." in again
    assert client.post(f"/songs/{second}/playlists", data={}).status_code == 422
    assert client.post(f"/songs/{second}/playlists", data={"playlist_id": 99}).status_code == 404

    album_url = f"/albums/{web_sample.album_id}/playlists"
    assert "Add the album to a playlist" in client.get(f"/albums/{web_sample.album_id}").text
    added = client.post(album_url, data={"playlist_id": pid}).text
    assert "Added 1 song to Night drive. 1 of its 2 songs were already there." in added
    assert "Added 0 songs" in client.post(album_url, data={"playlist_id": pid}).text
    assert client.post(album_url, data={}).status_code == 422
    assert client_lib.playlist_song_ids(pid) == [second, first]

    songs = client.get(f"/fragments/playlists/{pid}/songs").text
    assert f'"playlist_id": {pid}' in songs and "Remove" in songs and "Edit</button>" not in songs
    moved = client.post(f"/playlists/{pid}/songs/{first}/move", data={"offset": "-1"})
    assert "playlists-changed" in moved.headers["hx-trigger"]
    assert client_lib.playlist_song_ids(pid) == [first, second]
    assert (
        client.post(f"/playlists/{pid}/songs/{first}/move", data={"offset": "x"}).status_code == 422
    )
    row = client.post(f"/songs/{first}/play", data={"ctx": "playlist", "playlist_id": pid}).text
    assert f"/playlists/{pid}/songs/{first}/remove" in row
    client.post(f"/playlists/{pid}/songs/{first}/remove")
    assert client_lib.playlist_song_ids(pid) == [second]
    assert client.post(f"/playlists/{pid}/songs/{first}/remove").status_code == 404

    assert 'value="Night drive"' in client.get(f"/fragments/playlists/{pid}/edit").text
    assert client.post(f"/playlists/{pid}", data={"name": ""}).status_code == 422
    renamed = client.post(f"/playlists/{pid}", data={"name": "Late"}).text
    assert "Late" in renamed and "1 songs" in renamed
    assert "Late" in client.get(f"/fragments/playlists/{pid}/info").text
    deleted = client.post(f"/playlists/{pid}/delete")
    assert deleted.headers["hx-redirect"] == "/playlists"
    assert client.get(f"/playlists/{pid}").status_code == 404
    assert len(client_lib.list_songs()) == 2


def test_built_in_playlist_pages(client: TestClient, client_lib: Library, web_sample: Sample):
    first, second = web_sample.song_ids
    favorites = client_lib.special_playlist("favorites").id
    later = client_lib.special_playlist("listen_later").id
    page = client.get(f"/playlists/{favorites}").text
    assert "Every song marked ♥ is here" in page and "Rename</button>" not in page
    assert "Delete</button>" not in page
    assert client.post(f"/playlists/{favorites}/delete").status_code == 422
    assert client.post(f"/playlists/{later}", data={"name": "Other"}).status_code == 422
    assert client_lib.get_playlist(later).name == "Listen Later"

    # Marks fill the playlists, and they say so to the playlist pages.
    toggled = client.post(f"/favorite/song/{second}")
    assert "playlists-changed" in toggled.headers["hx-trigger"]
    assert (
        "playlists-changed"
        not in client.post(f"/favorite/album/{web_sample.album_id}").headers["hx-trigger"]
    )
    assert "Sour Times" in client.get(f"/fragments/playlists/{favorites}/songs").text
    client.post(f"/later/song/{first}")
    assert "Mysterons" in client.get(f"/fragments/playlists/{later}/songs").text
    played = client.post(f"/songs/{first}/play", data={"ctx": "playlist", "playlist_id": later})
    assert "playlists-changed" in played.headers["hx-trigger"]
    assert "Mysterons" not in client.get(f"/fragments/playlists/{later}/songs").text

    # Adding to and removing from the playlists sets the marks.
    client.post(f"/songs/{first}/playlists", data={"playlist_id": favorites})
    assert client_lib.get_song(first).favorite
    assert 'aria-pressed="true"' in client.get(f"/fragments/songs/{first}/info").text
    removed = client.post(f"/playlists/{favorites}/songs/{second}/remove")
    assert "library-changed" in removed.headers["hx-trigger"]
    assert not client_lib.get_song(second).favorite
    client.post(f"/albums/{web_sample.album_id}/playlists", data={"playlist_id": later})
    assert all(s.song.listen_later for s in client_lib.list_songs())
    assert 'id="song-info-slot"' in client.get(f"/songs/{first}").text


def test_rating_stars(client: TestClient, client_lib: Library, web_sample: Sample):
    records = {
        "artist": (web_sample.artist_id, f"/artists/{web_sample.artist_id}"),
        "album": (web_sample.album_id, f"/albums/{web_sample.album_id}"),
        "song": (web_sample.song_ids[0], f"/songs/{web_sample.song_ids[0]}"),
    }
    for kind, (record_id, page) in records.items():
        assert 'class="rating big"' in client.get(page).text
        url = f"/rating/{kind}/{record_id}"
        rated = client.post(url, data={"rating": "3", "size": "big"})
        assert rated.status_code == 200 and "library-changed" in rated.headers["hx-trigger"]
        assert rated.text.count('class="star on"') == 3 and 'class="rating big"' in rated.text
        assert 'aria-label="Rating: 3 of 5"' in rated.text
        # Clicking the current rating offers to take it away.
        assert '"rating": ""' in rated.text and 'aria-label="Remove the rating"' in rated.text
        cleared = client.post(url, data={"rating": ""})
        assert 'class="star on"' not in cleared.text and 'class="rating"' in cleared.text
        assert (
            client.post(url, data={"rating": "3", "size": "x onclick=1"}).text.count("onclick") == 0
        )
        for bad in ("0", "6", "x"):
            assert client.post(url, data={"rating": bad}).status_code == 422
        assert client.post(f"/rating/{kind}/99", data={"rating": "3"}).status_code == 404
    assert client.post("/rating/playlist/1", data={"rating": "3"}).status_code == 404

    client.post(f"/rating/artist/{web_sample.artist_id}", data={"rating": "5"})
    assert 'class="star on"' in client.get("/artists").text
    edit = client.post(f"/artists/{web_sample.artist_id}", data={"name": "Portishead"})
    assert edit.status_code == 200 and client_lib.get_artist(web_sample.artist_id).rating == 5


def test_song_page_shows_the_album_cover(
    client: TestClient, client_lib: Library, web_sample: Sample, image
):
    song_id = web_sample.song_ids[0]
    page = client.get(f"/songs/{song_id}").text
    cover_link = page.index(f'<a href="/albums/{web_sample.album_id}" title="Dummy">')
    assert page.index('<span class="cover big none"', cover_link) < page.index("</a>", cover_link)
    client_lib.set_cover(web_sample.album_id, image())
    cover = client_lib.get_album(web_sample.album_id).cover
    info = client.get(f"/fragments/songs/{song_id}/info").text
    assert f'src="/api/albums/{web_sample.album_id}/cover?v={cover}"' in info
    single = client_lib.create_song(SongIn(web_sample.artist_id, "Single"))
    single_page = client.get(f"/songs/{single.id}").text
    assert '<span class="cover big none" aria-hidden="true">♪</span>' in single_page
    assert "/cover?v=" not in single_page


def test_artist_filters(client: TestClient, client_lib: Library, web_sample: Sample):
    client_lib.create_artist(ArtistIn("Soda Stereo", "Argentina", None, ["Rock"], ["Spanish"]))
    page = client.get("/artists", params={"genre": "rock"}).text
    assert '<option value="Argentina" >Argentina</option>' in page
    assert '<option value="Rock" selected>Rock</option>' in page
    assert '<option value="Spanish" >Spanish</option>' in page
    assert "Soda Stereo" in page and "Portishead" not in page
    origin = client.get("/fragments/artists", params={"country": "United Kingdom"}).text
    assert "Portishead" in origin and "Soda Stereo" not in origin
    nobody = client.get("/fragments/artists", params={"language": "Spanish", "genre": "Trip hop"})
    assert "No artists" in nobody.text


def test_missing_lyrics_filters(client: TestClient, client_lib: Library, web_sample: Sample):
    first, second = web_sample.song_ids
    client_lib.set_lyrics(first, "Did you really want")
    songs = client.get("/fragments/songs", params={"missing": "1"}).text
    assert "Sour Times" in songs and "Mysterons" not in songs
    assert 'name="missing" value="1" checked' in client.get("/songs?missing=1").text
    assert "Dummy" in client.get("/fragments/albums", params={"missing": "1"}).text
    client_lib.set_lyrics(second, "Nobody loves me")
    assert "No albums here yet" in client.get("/fragments/albums", params={"missing": "1"}).text
    assert 'name="missing" value="1" checked' in client.get("/albums?missing=1").text


def test_song_rows_show_the_album_cover(
    client: TestClient, client_lib: Library, web_sample: Sample, image
):
    rows = client.get("/fragments/songs").text
    assert '<span class="cover mini none" aria-hidden="true">♪</span>\nDummy</a>' in rows
    client_lib.set_cover(web_sample.album_id, image())
    cover = client_lib.get_album(web_sample.album_id).cover
    rows = client.get("/fragments/songs").text
    src = f"/api/albums/{web_sample.album_id}/cover?v={cover}"
    assert f'<img class="cover mini" src="{src}"' in rows


def test_help_mentions_the_license_and_repo(client: TestClient):
    page = client.get("/help").text
    assert "GNU General Public License v3.0" in page
    assert 'href="https://github.com/trOOnies/implicated"' in page


def test_discography_is_full_width(client: TestClient, web_sample: Sample):
    page = client.get(f"/artists/{web_sample.artist_id}").text
    discography = page.index("<h2>Discography</h2>")
    assert discography < page.index('<div class="layout">')
    assert '<div class="table-scroll">' in page[discography:]


def test_bad_numbers_in_edit_forms_are_422(client: TestClient, web_sample: Sample):
    song = client.post(
        f"/songs/{web_sample.song_ids[0]}", data={"title": "Mysterons", "duration": "abc"}
    )
    assert song.status_code == 422 and "duration must look like 3:45" in song.text
    album = client.post(
        f"/albums/{web_sample.album_id}",
        data={"artist_id": web_sample.artist_id, "title": "Dummy", "year": "soon"},
    )
    assert album.status_code == 422 and "year must be a number" in album.text
    artist = client.post(
        f"/artists/{web_sample.artist_id}", data={"name": "Portishead", "formed_year": "x"}
    )
    assert artist.status_code == 422 and "formed year must be a number" in artist.text
