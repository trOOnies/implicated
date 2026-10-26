import pytest
from conftest import Sample
from fastapi.testclient import TestClient

from implicated.library import Library
from implicated.models import AlbumIn, ArtistIn, Member, MemberIn
from implicated.web.views import duration, stars, tenure, when

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

    error = client.post("/artists", data={"name": "Air", "formed_year": "soon"})
    assert error.status_code == 422 and "formed year must be a number" in error.text
    assert client.post("/artists", data={"name": " "}).status_code == 422


def test_update_and_delete_artist(client: TestClient, client_lib: Library, web_sample: Sample):
    url = f"/artists/{web_sample.artist_id}"
    response = client.post(url, data={"name": "Portishead", "languages": "English"})
    assert response.status_code == 200 and "Sings in English" in response.text
    assert client.post(url, data={"name": ""}).status_code == 422
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
