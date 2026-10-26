import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from conftest import Sample

from implicated.library import Library, NotFound
from implicated.models import AlbumIn, ArtistIn, MemberIn, SongIn

# Artists


def test_artist_crud(lib: Library):
    artist = lib.create_artist(
        ArtistIn("  Massive Attack ", genres=["trip hop", " ", " electronic "], languages=["en"])
    )
    assert artist.name == "Massive Attack"
    assert artist.genres == ["trip hop", "electronic"]
    assert artist.languages == ["en"]
    assert artist.created_at

    updated = lib.update_artist(artist.id, ArtistIn("Massive Attack", "UK", 1988, favorite=True))
    assert (updated.country, updated.formed_year, updated.favorite) == ("UK", 1988, True)
    assert updated.genres == []

    lib.delete_artist(artist.id)
    with pytest.raises(NotFound):
        lib.get_artist(artist.id)


def test_artist_validation_and_missing(lib: Library):
    with pytest.raises(ValueError, match="name is required"):
        lib.create_artist(ArtistIn("   "))
    with pytest.raises(NotFound):
        lib.update_artist(99, ArtistIn("Nobody"))
    with pytest.raises(NotFound):
        lib.delete_artist(99)


def test_list_artists_counts_filter_and_order(lib: Library, sample: Sample):
    lib.create_artist(ArtistIn("Air"))
    fav = lib.create_artist(ArtistIn("Zero 7", favorite=True))
    lib.record_play(sample.song_ids[0])

    names = [a.artist.name for a in lib.list_artists()]
    assert names == ["Zero 7", "Air", "Portishead"]  # favorites first, then by name

    [portishead] = lib.list_artists("tish")
    assert (portishead.album_count, portishead.song_count, portishead.play_count) == (1, 2, 1)
    assert lib.list_artists("nothing like this") == []
    assert fav.id in {a.artist.id for a in lib.list_artists("")}


def test_find_artist_ignores_case(lib: Library, sample: Sample):
    assert lib.find_artist(" PORTISHEAD ").id == sample.artist_id
    assert lib.find_artist("Unknown") is None


# Members


def test_members_replace_in_order(lib: Library, sample: Sample):
    members = lib.set_members(
        sample.artist_id,
        [
            MemberIn(" Beth Gibbons ", ["Vocals", ""]),
            MemberIn("Geoff Barrow", ["Drums", "Production"], from_year=1991),
            MemberIn("Adrian Utley", ["Guitar"], from_year=1993, to_year=2020),
        ],
    )
    assert [m.name for m in members] == ["Beth Gibbons", "Geoff Barrow", "Adrian Utley"]
    assert members[0].roles == ["Vocals"]
    assert (members[2].from_year, members[2].to_year) == (1993, 2020)
    assert all(m.artist_id == sample.artist_id for m in members)

    assert [m.name for m in lib.set_members(sample.artist_id, [MemberIn("Solo")])] == ["Solo"]
    assert lib.list_members(sample.artist_id)[0].name == "Solo"


def test_members_validation(lib: Library, sample: Sample):
    with pytest.raises(ValueError, match="member name"):
        lib.set_members(sample.artist_id, [MemberIn(" ")])
    with pytest.raises(ValueError, match="cannot leave before joining"):
        lib.set_members(sample.artist_id, [MemberIn("Late", from_year=2000, to_year=1999)])
    with pytest.raises(NotFound):
        lib.set_members(99, [])


def test_members_go_with_their_artist(lib: Library, sample: Sample):
    lib.set_members(sample.artist_id, [MemberIn("Beth Gibbons")])
    lib.delete_artist(sample.artist_id)
    assert lib.list_members(sample.artist_id) == []


# Albums


def test_album_crud(lib: Library, sample: Sample):
    album = lib.create_album(AlbumIn(sample.artist_id, " Portishead ", 1997, "album", 4))
    assert album.title == "Portishead"
    assert album.cover is None

    updated = lib.update_album(album.id, AlbumIn(sample.artist_id, "Third", 2008, "live"))
    assert (updated.title, updated.year, updated.kind, updated.rating) == (
        "Third",
        2008,
        "live",
        None,
    )

    lib.delete_album(album.id)
    with pytest.raises(NotFound):
        lib.get_album(album.id)


@pytest.mark.parametrize(
    "data, message",
    [
        (AlbumIn(0, "  "), "title is required"),
        (AlbumIn(0, "Dummy", rating=6), "rating"),
        (AlbumIn(0, "Dummy", kind="mixtape"), "kind must be one of"),
    ],
)
def test_album_validation(lib: Library, sample: Sample, data: AlbumIn, message: str):
    with pytest.raises(ValueError, match=message):
        lib.create_album(replace(data, artist_id=sample.artist_id))


def test_album_needs_existing_artist(lib: Library):
    with pytest.raises(NotFound):
        lib.create_album(AlbumIn(99, "Orphan"))
    with pytest.raises(NotFound):
        lib.update_album(99, AlbumIn(99, "Orphan"))


def test_list_albums_filters_and_summaries(lib: Library, sample: Sample):
    other = lib.create_artist(ArtistIn("Massive Attack"))
    lib.create_album(AlbumIn(other.id, "Mezzanine", 1998, favorite=True))
    lib.create_album(AlbumIn(other.id, "Undated"))
    lib.record_play(sample.song_ids[0])
    lib.record_play(sample.song_ids[1])

    assert [a.album.title for a in lib.list_albums()] == ["Dummy", "Mezzanine", "Undated"]
    [dummy] = lib.list_albums(sample.artist_id)
    assert (dummy.artist_name, dummy.song_count, dummy.total_seconds, dummy.play_count) == (
        "Portishead",
        2,
        553,
        2,
    )
    assert [a.album.title for a in lib.list_albums(favorites=True)] == ["Mezzanine"]
    assert [a.album.title for a in lib.list_albums(q="massive")] == ["Mezzanine", "Undated"]
    assert [a.album.title for a in lib.list_albums(q="dum")] == ["Dummy"]


def test_find_album(lib: Library, sample: Sample):
    assert lib.find_album(sample.artist_id, "dummy").id == sample.album_id
    assert lib.find_album(sample.artist_id, "Third") is None


# Covers


def test_set_cover_stores_a_copy(lib: Library, sample: Sample, image):
    album = lib.set_cover(sample.album_id, image("dummy.JPG", b"first"))
    assert album.cover.startswith(f"{sample.album_id}-") and album.cover.endswith(".jpg")
    path = lib.cover_path(sample.album_id)
    assert path.parent == lib.covers_dir
    assert path.read_bytes() == b"first"

    # Same content again: nothing changes.
    assert lib.set_cover(sample.album_id, image("again.jpg", b"first")).cover == album.cover

    # New content: new file name, old file removed.
    replaced = lib.set_cover(sample.album_id, image("new.webp", b"second"))
    assert replaced.cover != album.cover
    assert [p.name for p in lib.covers_dir.iterdir()] == [replaced.cover]


def test_set_cover_rejects_other_files(lib: Library, sample: Sample, image):
    with pytest.raises(ValueError, match="cover must be one of"):
        lib.set_cover(sample.album_id, image("notes.txt"))
    with pytest.raises(NotFound):
        lib.set_cover(99, image())


def test_cover_path_without_cover(lib: Library, sample: Sample, image):
    with pytest.raises(NotFound, match="no cover"):
        lib.cover_path(sample.album_id)
    lib.set_cover(sample.album_id, image())
    lib.cover_path(sample.album_id).unlink()
    with pytest.raises(NotFound, match="no cover"):
        lib.cover_path(sample.album_id)


def test_deleting_removes_cover_files(lib: Library, sample: Sample, image):
    lib.set_cover(sample.album_id, image("a.jpg", b"a"))
    lib.delete_album(sample.album_id)
    assert list(lib.covers_dir.iterdir()) == []

    album = lib.create_album(AlbumIn(sample.artist_id, "Third"))
    lib.set_cover(album.id, image("b.jpg", b"b"))
    lib.delete_artist(sample.artist_id)
    assert list(lib.covers_dir.iterdir()) == []


# Songs


def test_song_crud(lib: Library, sample: Sample):
    song = lib.create_song(SongIn(sample.artist_id, " Glory Box ", sample.album_id, 3, 306))
    assert song.title == "Glory Box"

    single = lib.update_song(song.id, SongIn(sample.artist_id, "Glory Box", rating=5))
    assert (single.album_id, single.rating) == (None, 5)

    lib.delete_song(song.id)
    with pytest.raises(NotFound):
        lib.get_song(song.id)
    with pytest.raises(NotFound):
        lib.update_song(song.id, SongIn(sample.artist_id, "Gone"))


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"title": " "}, "title is required"),
        ({"rating": -1}, "rating"),
        ({"duration_seconds": -5}, "duration must be positive"),
    ],
)
def test_song_validation(lib: Library, sample: Sample, changes: dict, message: str):
    data = SongIn(sample.artist_id, "Roads", sample.album_id)
    with pytest.raises(ValueError, match=message):
        lib.create_song(replace(data, **changes))


def test_song_album_must_belong_to_artist(lib: Library, sample: Sample):
    other = lib.create_artist(ArtistIn("Tricky"))
    with pytest.raises(ValueError, match="different artist"):
        lib.create_song(SongIn(other.id, "Overcome", sample.album_id))
    with pytest.raises(NotFound):
        lib.create_song(SongIn(other.id, "Overcome", 99))
    with pytest.raises(NotFound):
        lib.create_song(SongIn(99, "Overcome"))


def test_list_songs_filters_and_orders(lib: Library, sample: Sample):
    first, second = sample.song_ids
    single = lib.create_song(SongIn(sample.artist_id, "All Mine", favorite=True))
    now = datetime.now(timezone.utc)
    lib.record_play(second, (now - timedelta(days=2)).isoformat())
    lib.record_play(second, (now - timedelta(days=1)).isoformat())
    lib.record_play(first, now.isoformat())

    def titles(**kwargs):
        return [s.song.title for s in lib.list_songs(**kwargs)]

    assert titles() == ["All Mine", "Mysterons", "Sour Times"]
    assert titles(order="track") == ["Mysterons", "Sour Times", "All Mine"]
    assert titles(order="plays") == ["Sour Times", "Mysterons", "All Mine"]
    assert titles(order="recent") == ["Mysterons", "Sour Times", "All Mine"]
    assert titles(album_id=sample.album_id) == ["Mysterons", "Sour Times"]
    assert titles(favorites=True) == ["All Mine"]
    assert titles(q="dummy") == ["Mysterons", "Sour Times"]  # album title
    assert titles(q="portis") == titles()  # artist name
    assert titles(q="sour") == ["Sour Times"]

    summary = lib.get_song_summary(single.id)
    assert (summary.album_title, summary.play_count, summary.last_played) == (None, 0, None)
    with pytest.raises(NotFound):
        lib.get_song_summary(99)


def test_find_song(lib: Library, sample: Sample):
    assert lib.find_song(sample.artist_id, sample.album_id, "sour times").id == sample.song_ids[1]
    assert lib.find_song(sample.artist_id, None, "Sour Times") is None
    single = lib.create_song(SongIn(sample.artist_id, "Sour Times"))
    assert lib.find_song(sample.artist_id, None, "Sour Times").id == single.id


# Plays and favorites


def test_plays(lib: Library, sample: Sample):
    play = lib.record_play(sample.song_ids[0])
    assert play.song_id == sample.song_ids[0]
    lib.record_play(sample.song_ids[1], "2020-01-01T00:00:00+00:00")

    recent = lib.recent_plays()
    assert [p.id for p, _ in recent][0] == play.id
    assert recent[0][1].song.title == "Mysterons"
    assert len(lib.recent_plays(limit=1)) == 1
    with pytest.raises(NotFound):
        lib.record_play(99)


@pytest.mark.parametrize("kind", ["artist", "album", "song"])
def test_toggle_favorite(lib: Library, sample: Sample, kind: str):
    record_id = {"artist": sample.artist_id, "album": sample.album_id, "song": sample.song_ids[0]}
    assert lib.toggle_favorite(kind, record_id[kind]) is True
    assert lib.toggle_favorite(kind, record_id[kind]) is False
    with pytest.raises(NotFound):
        lib.toggle_favorite(kind, 99)


# Stats


def test_stats_without_plays(lib: Library, sample: Sample):
    other = lib.create_artist(ArtistIn("Bristol Band", genres=["trip hop", "Rock"]))
    lib.create_artist(ArtistIn("Silent", genres=["rock"]))
    lib.create_album(AlbumIn(other.id, "Old", 1987))

    stats = lib.stats(days=7)
    assert (stats.artists, stats.albums, stats.songs, stats.plays) == (3, 2, 2, 0)
    assert stats.total_seconds == 553
    assert len(stats.plays_per_day) == 7 and all(p.value == 0 for p in stats.plays_per_day)
    assert stats.top_artists == []
    assert [(p.label, p.value) for p in stats.songs_per_artist] == [
        ("Portishead", 2),
        ("Bristol Band", 0),
        ("Silent", 0),
    ]
    # Spellings differing in case count together, under the first one seen; ties go A-Z.
    assert [(p.label, p.value) for p in stats.genres] == [("Rock", 2), ("Trip hop", 2)]
    assert [(p.label, p.value) for p in stats.albums_per_decade] == [("1980s", 1), ("1990s", 1)]
    assert [(p.label, p.value) for p in stats.ratings][0] == ("★★★★★", 1)


def test_stats_with_plays(lib: Library, sample: Sample):
    lib.record_play(sample.song_ids[0])
    lib.record_play(sample.song_ids[0])
    lib.record_play(sample.song_ids[1], "2000-01-01T00:00:00+00:00")  # outside the window

    stats = lib.stats(days=3, top=1)
    assert stats.plays == 3
    assert [p.value for p in stats.plays_per_day] == [0, 0, 2]
    assert [(p.label, p.value) for p in stats.top_artists] == [("Portishead", 3)]
    assert len(stats.songs_per_artist) == 1


# Storage


def test_transaction_commits_once_and_rolls_back(lib: Library, sample: Sample):
    with lib.transaction():
        lib.create_artist(ArtistIn("Kept"))
        with lib.transaction():  # nesting joins the outer transaction
            lib.create_artist(ArtistIn("Also kept"))
    assert lib.find_artist("Kept") and lib.find_artist("Also kept")

    with pytest.raises(RuntimeError), lib.transaction():
        lib.create_artist(ArtistIn("Rolled back"))
        lib.delete_song(sample.song_ids[0])
        raise RuntimeError
    assert lib.find_artist("Rolled back") is None
    assert lib.get_song(sample.song_ids[0])


def test_old_databases_get_new_columns(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """CREATE TABLE artists (id INTEGER PRIMARY KEY, name TEXT NOT NULL, country TEXT,
                   formed_year INTEGER, genres TEXT NOT NULL DEFAULT '[]', bio TEXT,
                   favorite INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
               CREATE TABLE albums (id INTEGER PRIMARY KEY, artist_id INTEGER NOT NULL,
                   title TEXT NOT NULL, year INTEGER, kind TEXT NOT NULL DEFAULT 'album',
                   rating INTEGER, notes TEXT, favorite INTEGER NOT NULL DEFAULT 0,
                   created_at TEXT NOT NULL);
               INSERT INTO artists (name, created_at) VALUES ('Old', '2020-01-01');
               INSERT INTO albums (artist_id, title, created_at) VALUES (1, 'Old album', '2020');"""
        )
    conn.close()

    lib = Library(path)
    assert lib.get_artist(1).languages == []
    assert lib.get_album(1).cover is None
    Library(path)  # opening again is a no-op
