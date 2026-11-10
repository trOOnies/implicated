"""Merged songs: the same song on several albums counted as one."""

import json

import pytest
from conftest import Sample, add_sample

from implicated import duo
from implicated.library import Library, NotFound
from implicated.models import AlbumIn, PlaylistIn, SongIn
from implicated.userdata import export_data, import_data


def deluxe(lib: Library, sample: Sample) -> tuple[int, list[int]]:
    """A deluxe edition of the sample's album: the same two songs, and a bonus track."""
    album = lib.create_album(AlbumIn(sample.artist_id, "Dummy (Deluxe)", 1995)).id
    songs = [
        lib.create_song(SongIn(sample.artist_id, "Mysterons", album, 1, 302)).id,
        lib.create_song(SongIn(sample.artist_id, "Sour Times", album, 2, 251)).id,
        lib.create_song(SongIn(sample.artist_id, "Bonus", album, 3)).id,
    ]
    return album, songs


def ids(songs) -> list[int]:
    return [s.song.id for s in songs]


def test_merge_and_unmerge(lib: Library, sample: Sample):
    first, second = sample.song_ids
    album, (d_first, d_second, bonus) = deluxe(lib, sample)
    assert ids(lib.song_versions(first)) == [first]
    assert ids(lib.merge_suggestions(first)) == [d_first]
    with pytest.raises(ValueError, match="merged already"):
        lib.merge_songs(first, [first])

    versions = lib.merge_songs(first, [d_first])
    assert ids(versions) == [first, d_first]  # the main one first
    assert lib.get_song(d_first).main_id == first and lib.get_song(first).main_id is None
    assert ids(lib.song_versions(d_first)) == [first, d_first]
    assert lib.merge_suggestions(first) == []

    # Merging with a merged song merges with its main version; a main brings its songs along.
    third = lib.create_song(SongIn(sample.artist_id, "Mysterons (Live)")).id
    lib.merge_songs(d_first, [third])
    assert lib.get_song(third).main_id == first
    lib.merge_songs(bonus, [first])
    assert {lib.get_song(i).main_id for i in (first, d_first, third)} == {bonus}

    versions = lib.make_main(first)
    assert versions[0].song.id == first and set(ids(versions)) == {first, d_first, bonus, third}
    assert lib.get_song(bonus).main_id == first
    with pytest.raises(ValueError, match="not merged into"):
        lib.make_main(first)

    lib.unmerge_song(bonus)
    assert lib.get_song(bonus).main_id is None and len(lib.song_versions(first)) == 3
    # Unmerging the main one hands over to the next version, album by album.
    lib.unmerge_song(first)
    assert lib.get_song(first).main_id is None
    assert lib.get_song(d_first).main_id is None and lib.get_song(third).main_id == d_first
    with pytest.raises(ValueError, match="not merged with"):
        lib.unmerge_song(second)
    with pytest.raises(NotFound):
        lib.merge_songs(first, [9999])


def test_merged_songs_share_plays_marks_and_rating(lib: Library, sample: Sample):
    first, second = sample.song_ids
    _album, (d_first, *_rest) = deluxe(lib, sample)
    lib.record_play(first)
    lib.record_play(d_first)
    lib.record_play(d_first)
    lib.toggle("song", d_first, "favorite")
    lib.set_rating("song", d_first, 4)
    lib.set_rating("song", first, 3)

    lib.merge_songs(first, [d_first])
    for song_id in (first, d_first):
        summary = lib.get_song_summary(song_id)
        assert summary.play_count == 3 and summary.last_played is not None
        # A favorite when either was; the main version's rating.
        assert summary.song.favorite and summary.song.rating == 3
    assert lib.get_song_summary(second).play_count == 0

    # Marking or rating any of them marks or rates them all.
    lib.toggle("song", d_first, "listen_later")
    lib.set_rating("song", d_first, 5)
    assert lib.get_song(first).listen_later and lib.get_song(first).rating == 5
    lib.record_play(first)  # listened: off Listen Later, both
    assert not lib.get_song(d_first).listen_later
    lib.toggle_favorite("song", first)
    assert not lib.get_song(d_first).favorite


def test_only_main_versions_are_in_the_built_in_playlists(lib: Library, sample: Sample):
    first, _second = sample.song_ids
    _album, (d_first, *_rest) = deluxe(lib, sample)
    favorites = lib.special_playlist("favorites").id
    lib.toggle_favorite("song", d_first)
    assert lib.playlist_song_ids(favorites) == [d_first]
    lib.merge_songs(first, [d_first])
    # The main version took its place.
    assert lib.playlist_song_ids(favorites) == [first]
    assert ids(lib.list_songs(favorites=True)) == [first, d_first]
    assert ids(lib.list_songs(favorites=True, mains_only=True)) == [first]

    # Adding a merged song to a built-in playlist adds its main one; taking it out unmarks all.
    lib.toggle_favorite("song", first)
    lib.add_to_playlist(favorites, [d_first])
    assert lib.playlist_song_ids(favorites) == [first] and lib.get_song(d_first).favorite
    lib.set_playlist_songs(favorites, [])
    assert not lib.get_song(d_first).favorite and not lib.get_song(first).favorite

    lib.toggle_favorite("song", d_first)
    lib.make_main(d_first)
    assert lib.playlist_song_ids(favorites) == [d_first]
    # Your own playlists keep whichever versions you put in them.
    mix = lib.create_playlist(PlaylistIn("Mix")).id
    lib.add_to_playlist(mix, [first, d_first])
    assert lib.playlist_song_ids(mix) == [first, d_first]


def test_tier_lists_rank_merged_songs_once(lib: Library, sample: Sample):
    first, second = sample.song_ids
    album, (d_first, d_second, bonus) = deluxe(lib, sample)
    artist = lib.create_tier_list("artist", sample.artist_id)
    top = artist.tiers[0].tier.id
    lib.place_song("artist", sample.artist_id, d_first, top)
    deluxe_list = lib.create_tier_list("album", album)

    lib.merge_songs(first, [d_first])
    # The main version stands for both, and took the deluxe one's place.
    artist = lib.get_tier_list("artist", sample.artist_id)
    assert ids(artist.tiers[0].songs) == [first]
    assert ids(artist.unassigned) == [second, d_second, bonus]
    # The deluxe album has no main version: its own one stands for it there.
    deluxe_list = lib.get_tier_list("album", album)
    assert ids(deluxe_list.unassigned) == [d_first, d_second, bonus]
    with pytest.raises(NotFound):
        lib.place_song("artist", sample.artist_id, d_first, top)

    # Changing the main one keeps the place.
    lib.make_main(d_first)
    assert ids(lib.get_tier_list("artist", sample.artist_id).tiers[0].songs) == [d_first]

    # Autoadd counts the plays of them all.
    lib.record_play(first)
    lib.record_play(first)
    lib.record_play(second)
    lib.auto_tier("artist", sample.artist_id)
    tiers = lib.get_tier_list("artist", sample.artist_id).tiers
    assert ids(tiers[0].songs) == [] and ids(tiers[2].songs) == [d_first, second]


def test_deleting_a_main_version_hands_over(lib: Library, sample: Sample):
    first, second = sample.song_ids
    album, (d_first, d_second, _bonus) = deluxe(lib, sample)
    third = lib.create_song(SongIn(sample.artist_id, "Mysterons", None)).id
    lib.merge_songs(first, [d_first, third])
    lib.merge_songs(second, [d_second])
    lib.toggle_favorite("song", first)

    lib.delete_song(first)
    assert lib.get_song(d_first).main_id is None and lib.get_song(third).main_id == d_first
    assert lib.playlist_song_ids(lib.special_playlist("favorites").id) == [d_first]
    # Deleting an album: its main versions hand over to versions on other albums.
    lib.make_main(d_second)
    lib.delete_album(album)
    assert lib.get_song(third).main_id is None and lib.get_song(second).main_id is None
    lib.delete_artist(sample.artist_id)


def test_merges_round_trip_through_your_data(lib: Library, sample: Sample, tmp_path):
    first, second = sample.song_ids
    _album, (d_first, *_rest) = deluxe(lib, sample)
    lib.merge_songs(first, [d_first])
    lib.toggle_favorite("song", first)
    data = export_data(lib)
    by_album = {(s.get("album"), s["title"]): s for s in data["songs"]}
    assert by_album[("Dummy (Deluxe)", "Mysterons")]["same_as"] == {
        "artist": "Portishead",
        "album": "Dummy",
        "title": "Mysterons",
    }
    assert "same_as" not in by_album[("Dummy", "Mysterons")]

    other = Library(tmp_path / "other.db")
    s = add_sample(other)
    _a, (o_first, o_second, _bonus) = deluxe(other, s)
    other.merge_songs(s.song_ids[1], [o_second])  # the file says these are not merged
    report = import_data(other, json.loads(json.dumps(data)))
    assert report.warnings == []
    assert other.get_song(o_first).main_id == s.song_ids[0]
    assert other.get_song(o_second).main_id is None
    assert other.get_song(o_first).favorite
    assert other.playlist_song_ids(other.special_playlist("favorites").id) == [s.song_ids[0]]
    clean = lambda d: {k: v for k, v in d.items() if k != "exported_at"}  # noqa: E731
    assert clean(export_data(other)) == clean(data)

    # Merged into a song the library does not have: left unmerged, and reported.
    data["songs"][0]["same_as"] = {"artist": "Portishead", "title": "Nowhere"}
    report = import_data(other, data)
    assert any("Nowhere" in w and "not merged" in w for w in report.warnings)


def test_duo_counts_merged_songs_once(tmp_path):
    me = Library(tmp_path / "me.db")
    m = add_sample(me)
    _album, (d_first, *_rest) = deluxe(me, m)
    me.merge_songs(m.song_ids[0], [d_first])
    for song_id in (m.song_ids[0], d_first, d_first):
        me.record_play(song_id)
    taste = duo._Taste(export_data(me))
    songs = {k[2:]: r.plays for k, r in taste.records.items() if r.kind == "song"}
    assert songs[("dummy", "mysterons")] == 3
    assert ("dummy (deluxe)", "mysterons") not in songs


def test_same_song_card(client, client_lib: Library, web_sample: Sample):
    first, second = web_sample.song_ids
    album, (d_first, _d_second, _bonus) = deluxe(client_lib, web_sample)
    page = client.get(f"/songs/{first}").text
    assert 'id="song-versions"' in page and "Same title" in page
    assert f'hx-vals=\'{{"song_id": "{d_first}"}}\'' in page

    merged = client.post(f"/songs/{first}/merge", data={"song_id": d_first})
    assert merged.status_code == 200
    assert "songs-changed" in merged.headers["hx-trigger"]
    assert ">main</span>" in merged.text and "Make main" in merged.text
    again = client.post(f"/songs/{first}/merge", data={"song_id": d_first})
    assert again.status_code == 422 and "merged already" in again.text
    assert client.post(f"/songs/{first}/merge", data={}).status_code == 422
    assert client.post(f"/songs/{first}/merge", data={"song_id": 9999}).status_code == 404

    # The merged one is tagged in song lists; the Overview lists the main one only.
    client_lib.toggle_favorite("song", first)
    album_songs = client.get(f"/fragments/albums/{album}/songs").text
    assert album_songs.count(">merged</span>") == 1
    view = client.get("/fragments/view").text
    assert view.count(f'href="/songs/{first}"') >= 1 and f'href="/songs/{d_first}"' not in view

    # Search, then make main and unmerge from the page of another version.
    found_ = client.get(f"/fragments/songs/{first}/versions", params={"q": "Bon"}).text
    assert "Bonus" in found_
    assert "at least 3" in client.get(f"/fragments/songs/{first}/versions", params={"q": "B"}).text
    made = client.post(f"/songs/{d_first}/main", data={"page": first})
    assert (
        client_lib.get_song(first).main_id == d_first and f"/songs/{d_first}/unmerge" in made.text
    )
    assert client.post(f"/songs/{d_first}/main").status_code == 422
    client.post(f"/songs/{first}/unmerge", data={"page": first})
    assert client_lib.get_song(first).main_id is None
    assert client.post(f"/songs/{second}/unmerge").status_code == 422


def test_merge_albums(lib: Library, sample: Sample):
    first, second = sample.song_ids
    album, (d_first, d_second, bonus) = deluxe(lib, sample)
    other = lib.create_album(AlbumIn(sample.artist_id, "Unrelated", 2000)).id
    # Any of the artist's albums, whatever its name, with its songs of the same name.
    choices = {a.id: n for a, n in lib.album_merge_choices(album)}
    assert choices == {sample.album_id: 2, other: 0}
    lib.toggle("album", album, "favorite")
    lib.set_rating("album", sample.album_id, None)

    # The deluxe edition into the original: the original's songs are the main ones.
    assert lib.merge_albums(album, sample.album_id) == 2
    assert lib.get_album(album).main_id == sample.album_id
    assert lib.get_song(d_first).main_id == first and lib.get_song(d_second).main_id == second
    assert lib.get_song(bonus).main_id is None
    assert [a.id for a in lib.album_editions(album)] == [sample.album_id, album]
    assert [a.id for a, _n in lib.album_merge_choices(album)] == [other]
    with pytest.raises(ValueError, match="merged already"):
        lib.merge_albums(sample.album_id, album)
    # They share the marks and rating.
    assert lib.get_album(sample.album_id).favorite
    lib.set_rating("album", album, 4)
    lib.toggle("album", sample.album_id, "listen_later")
    assert lib.get_album(sample.album_id).rating == 4 and lib.get_album(album).listen_later

    # The other way round: the deluxe edition is the main one, and so are its songs.
    lib.unmerge_album(album)
    assert lib.get_album(album).main_id is None and lib.get_song(d_first).main_id is None
    assert lib.merge_albums(sample.album_id, album) == 2
    assert lib.get_album(sample.album_id).main_id == album
    assert lib.get_song(first).main_id == d_first

    lib.make_main_album(sample.album_id)
    assert lib.get_album(album).main_id == sample.album_id
    with pytest.raises(ValueError, match="not merged into"):
        lib.make_main_album(sample.album_id)
    # Deleting the main edition hands over.
    lib.merge_albums(other, sample.album_id)
    lib.delete_album(sample.album_id)
    assert lib.get_album(album).main_id is None and lib.get_album(other).main_id == album
    with pytest.raises(ValueError, match="not merged with"):
        lib.unmerge_album(lib.create_album(AlbumIn(sample.artist_id, "Alone")).id)


def test_songs_left_unmerged_can_be_merged_by_hand(lib: Library, sample: Sample):
    first, second = sample.song_ids
    tv = lib.create_album(AlbumIn(sample.artist_id, "Dummy (Taylor's Version)", 2021)).id
    tv_first = lib.create_song(SongIn(sample.artist_id, "Mysterons (Taylor's Version)", tv, 1)).id
    tv_second = lib.create_song(SongIn(sample.artist_id, "Sour Times", tv, 2)).id
    vault = lib.create_song(SongIn(sample.artist_id, "From the Vault", tv, 3)).id
    assert lib.unmerged_songs(tv) == []

    assert lib.merge_albums(tv, sample.album_id) == 1
    unmerged = lib.unmerged_songs(tv)
    assert [(s.song.id, ids(c)) for s, c in unmerged] == [(tv_first, [first]), (vault, [first])]
    assert [(s.song.id, ids(c)) for s, c in lib.unmerged_songs(sample.album_id)] == [
        (first, [tv_first, vault])
    ]
    # Merged the way round the albums are, from either page.
    lib.merge_album_song(tv_first, first)
    assert lib.get_song(tv_first).main_id == first
    assert lib.get_song(tv_second).main_id == second
    assert [s.song.id for s, _c in lib.unmerged_songs(tv)] == [vault]
    assert lib.unmerged_songs(tv)[0][1] == []
    lib.unmerge_song(tv_first)
    lib.merge_album_song(first, tv_first)
    assert lib.get_song(tv_first).main_id == first


def test_album_merges_round_trip_and_the_overview(client, client_lib: Library, web_sample):
    album, _songs = deluxe(client_lib, web_sample)
    client_lib.toggle("album", album, "favorite")
    client_lib.merge_albums(album, web_sample.album_id)
    view = client.get("/fragments/view").text
    favorites = view[view.index("Favorite albums") : view.index("Favorite songs")]
    assert f'href="/albums/{web_sample.album_id}">Dummy</a>' in favorites
    assert f'href="/albums/{album}"' not in favorites

    data = export_data(client_lib)
    by_title = {a["title"]: a for a in data["albums"]}
    assert by_title["Dummy (Deluxe)"]["same_as"] == {"artist": "Portishead", "title": "Dummy"}
    client_lib.unmerge_album(album)
    report = import_data(client_lib, data)
    assert report.warnings == []
    assert client_lib.get_album(album).main_id == web_sample.album_id
    taste = duo._Taste(data)
    assert ("album", "portishead", "dummy (deluxe)") not in taste.records


def test_album_page_buttons_and_editions_card(client, client_lib: Library, web_sample):
    album, (d_first, _d_second, bonus) = deluxe(client_lib, web_sample)
    page = client.get(f"/albums/{album}").text
    for label in ("Played", "Edit", "Copy", "Delete"):
        assert f'aria-label="{label}"><svg' in page
    assert "Editions" in page and f'<option value="{web_sample.album_id}">Dummy (1994)' in page

    merged = client.post(
        f"/albums/{album}/merge", data={"other": web_sample.album_id, "direction": "into"}
    )
    assert "Merged into Dummy. So were 2 songs of the same name, theirs the main" in merged.text
    assert "albums-changed" in merged.headers["hx-trigger"]
    assert "Songs not merged" in merged.text and "Bonus" in merged.text
    info = client.get(f"/fragments/albums/{album}/info").text
    assert "Another edition of" in info
    assert (
        "The main edition of 2" in client.get(f"/fragments/albums/{web_sample.album_id}/info").text
    )
    assert client.post(f"/albums/{album}/merge", data={}).status_code == 422
    again = client.post(f"/albums/{album}/merge", data={"other": web_sample.album_id})
    assert again.status_code == 422 and "merged already" in again.text

    # By hand, the bonus track with Sour Times (made unmerged first).
    client_lib.unmerge_song(web_sample.song_ids[1])
    hand = client.post(
        f"/albums/{album}/merge-song", data={"song_id": bonus, "other_id": web_sample.song_ids[1]}
    )
    assert hand.status_code == 200 and client_lib.get_song(bonus).main_id == web_sample.song_ids[1]
    assert client.post(f"/albums/{album}/merge-song", data={"song_id": bonus}).status_code == 422

    client.post(f"/albums/{album}/main", data={"page": album})
    assert client_lib.get_album(web_sample.album_id).main_id == album
    assert client.post(f"/albums/{album}/main").status_code == 422
    gone = client.post(f"/albums/{web_sample.album_id}/unmerge", data={"page": album})
    assert gone.status_code == 200 and client_lib.get_song(d_first).main_id is None
    assert client.post(f"/albums/{album}/unmerge").status_code == 422


def test_discography_filters(client, client_lib: Library, web_sample):
    album, _songs = deluxe(client_lib, web_sample)
    ep = client_lib.create_album(AlbumIn(web_sample.artist_id, "Numb", 1994, kind="ep")).id
    client_lib.merge_albums(album, web_sample.album_id)
    base = f"/fragments/artists/{web_sample.artist_id}/albums"
    page = client.get(f"/artists/{web_sample.artist_id}").text
    assert 'id="discography-filters"' in page and 'hx-include="#discography-filters"' in page

    def shown(**params) -> set[int]:
        text = client.get(base, params=params).text
        return {i for i in (web_sample.album_id, album, ep) if f'href="/albums/{i}"' in text}

    assert shown() == {web_sample.album_id, album, ep}
    assert shown(kind="ep") == {ep}
    assert shown(kind="album") == {web_sample.album_id, album}
    assert shown(mains="1") == {web_sample.album_id, ep}
    assert shown(kind="album", mains="1") == {web_sample.album_id}
    assert "No albums match these filters." in client.get(base, params={"kind": "live"}).text
    client_lib.make_main_album(album)
    assert shown(mains="1") == {album, ep}
    assert client.get("/fragments/artists/9999/albums").status_code == 404
