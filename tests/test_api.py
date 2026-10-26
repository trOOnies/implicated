from conftest import Sample
from fastapi.testclient import TestClient

from implicated.library import Library


def test_artist_routes(client: TestClient):
    created = client.post(
        "/api/artists", json={"name": "Air", "genres": ["electronic"], "languages": ["French"]}
    )
    assert created.status_code == 201
    artist = created.json()
    assert artist["languages"] == ["French"]

    assert client.get(f"/api/artists/{artist['id']}").json()["name"] == "Air"
    assert client.get("/api/artists", params={"q": "ai"}).json()[0]["artist"]["id"] == artist["id"]

    updated = client.put(f"/api/artists/{artist['id']}", json={"name": "Air", "country": "France"})
    assert updated.json()["country"] == "France"

    assert client.delete(f"/api/artists/{artist['id']}").status_code == 204
    assert client.get(f"/api/artists/{artist['id']}").status_code == 404
    assert client.delete(f"/api/artists/{artist['id']}").status_code == 404


def test_validation_errors_are_422(client: TestClient, web_sample: Sample):
    assert client.post("/api/artists", json={"name": " "}).status_code == 422
    bad_wiki = {"name": "X", "wiki": "javascript:alert(1)"}
    assert client.post("/api/artists", json=bad_wiki).status_code == 422
    bad_album = {"artist_id": web_sample.artist_id, "title": "X", "rating": 9}
    assert client.post("/api/albums", json=bad_album).status_code == 422
    assert client.post("/api/albums", json={"artist_id": 99, "title": "X"}).status_code == 404


def test_member_routes(client: TestClient, web_sample: Sample):
    url = f"/api/artists/{web_sample.artist_id}/members"
    assert client.get(url).json() == []

    lineup = [
        {"name": "Beth Gibbons", "roles": ["Vocals"]},
        {"name": "Geoff Barrow", "from_year": 1991},
    ]
    saved = client.put(url, json=lineup)
    assert saved.status_code == 200
    assert [m["name"] for m in client.get(url).json()] == ["Beth Gibbons", "Geoff Barrow"]
    assert saved.json()[1]["from_year"] == 1991

    assert client.put(url, json=[{"name": "X", "from_year": 2, "to_year": 1}]).status_code == 422
    assert client.get("/api/artists/99/members").status_code == 404
    assert client.put("/api/artists/99/members", json=[]).status_code == 404


def test_album_routes(client: TestClient, web_sample: Sample):
    body = {"artist_id": web_sample.artist_id, "title": "Third", "year": 2008}
    album = client.post("/api/albums", json=body).json()
    assert album["cover"] is None

    listed = client.get("/api/albums", params={"artist_id": web_sample.artist_id}).json()
    assert [a["album"]["title"] for a in listed] == ["Dummy", "Third"]
    assert client.get("/api/albums", params={"q": "thi"}).json()[0]["album"]["id"] == album["id"]
    assert client.get("/api/albums", params={"favorites": True}).json() == []

    assert client.get(f"/api/albums/{album['id']}").json()["year"] == 2008
    body["kind"] = "live"
    assert client.put(f"/api/albums/{album['id']}", json=body).json()["kind"] == "live"
    assert client.delete(f"/api/albums/{album['id']}").status_code == 204
    assert client.get(f"/api/albums/{album['id']}").status_code == 404


def test_artist_image_route(client: TestClient, client_lib: Library, web_sample: Sample, image):
    url = f"/api/artists/{web_sample.artist_id}/image"
    assert client.get(url).status_code == 404
    assert client.get("/api/artists/99/image").status_code == 404

    client_lib.set_image(web_sample.artist_id, image("band.avif", b"avif!"))
    response = client.get(url)
    assert response.headers["content-type"] == "image/avif"
    assert "immutable" in response.headers["cache-control"]
    assert response.content == b"avif!"
    assert client.get(f"/api/artists/{web_sample.artist_id}").json()["image"].endswith(".avif")


def test_album_cover_route(client: TestClient, client_lib: Library, web_sample: Sample, image):
    url = f"/api/albums/{web_sample.album_id}/cover"
    assert client.get(url).status_code == 404
    assert client.get("/api/albums/99/cover").status_code == 404

    client_lib.set_cover(web_sample.album_id, image("dummy.png", b"png!"))
    response = client.get(url)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert "immutable" in response.headers["cache-control"]
    assert response.content == b"png!"


def test_song_routes(client: TestClient, web_sample: Sample):
    body = {"artist_id": web_sample.artist_id, "title": "Roads", "album_id": web_sample.album_id}
    song = client.post("/api/songs", json=body).json()

    listed = client.get("/api/songs", params={"album_id": web_sample.album_id, "q": "road"})
    assert [s["song"]["id"] for s in listed.json()] == [song["id"]]
    assert len(client.get("/api/songs", params={"artist_id": web_sample.artist_id}).json()) == 3
    assert client.get("/api/songs", params={"favorites": True}).json() == []

    assert client.get(f"/api/songs/{song['id']}").json()["title"] == "Roads"
    body["duration_seconds"] = 305
    assert client.put(f"/api/songs/{song['id']}", json=body).json()["duration_seconds"] == 305

    play = client.post(f"/api/songs/{song['id']}/plays")
    assert play.status_code == 201 and play.json()["song_id"] == song["id"]
    assert client.post("/api/songs/99/plays").status_code == 404

    assert client.delete(f"/api/songs/{song['id']}").status_code == 204
    assert client.get(f"/api/songs/{song['id']}").status_code == 404


def test_stats_route_clamps_days(client: TestClient, web_sample: Sample):
    assert len(client.get("/api/stats", params={"days": 0}).json()["plays_per_day"]) == 1
    assert len(client.get("/api/stats", params={"days": 9999}).json()["plays_per_day"]) == 365
    stats = client.get("/api/stats").json()
    assert (stats["songs"], len(stats["plays_per_day"])) == (2, 30)
    assert stats["songs_per_artist"] == [{"label": "Portishead", "value": 2}]


def test_routes_are_grouped_by_tag(client: TestClient):
    schema = client.get("/openapi.json").json()
    assert [t["name"] for t in schema["tags"]] == [
        "Artists", "Members", "Albums", "Songs", "Plays", "Stats"
    ]  # fmt: skip
    tags = {
        (method, path): op["tags"]
        for path, ops in schema["paths"].items()
        for method, op in ops.items()
    }
    assert tags[("get", "/api/artists")] == ["Artists"]
    assert tags[("put", "/api/artists/{artist_id}/members")] == ["Members"]
    assert tags[("get", "/api/albums/{album_id}/cover")] == ["Albums"]
    assert tags[("post", "/api/songs/{song_id}/plays")] == ["Plays"]
    assert tags[("get", "/api/stats")] == ["Stats"]
    assert all(len(t) == 1 for t in tags.values())
    assert not any(path.startswith("/fragments") for path in schema["paths"])


def test_song_no_lyrics(client: TestClient, web_sample: Sample):
    body = {"artist_id": web_sample.artist_id, "title": "Intro", "no_lyrics": True}
    created = client.post("/api/songs", json=body).json()
    assert created["no_lyrics"] is True
    assert (
        client.post("/api/songs", json=body | {"title": "Outro"} | {"no_lyrics": False}).json()[
            "no_lyrics"
        ]
        is False
    )
    albums = client.get("/api/albums").json()
    assert albums[0]["no_lyrics_count"] == 0
