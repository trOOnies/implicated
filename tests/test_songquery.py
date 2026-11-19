import pytest

from implicated.library import Library, NotFound
from implicated.models import AlbumIn, ArtistIn, PlaylistIn, SongIn
from implicated.songquery import parse_song_query


def test_parse_filters_out_of_the_text():
    query = parse_song_query([], 'love artist:abba from:1975 to:1979 fav: genre:"europop" song')
    assert query.text == "love song"
    assert (query.artist, query.from_year, query.to_year, query.genre) == (
        "abba",
        1975,
        1979,
        "europop",
    )
    assert query.favorites and not query.listen_later
    assert query.chips == [
        ("artist", "abba"), ("from", "1975"), ("to", "1979"), ("fav", ""), ("genre", "europop"),
    ]  # fmt: skip


def test_parse_tags_keep_their_spaces_and_later_filters_win():
    query = parse_song_query(["artist:The Cure", "stars:3", "later:"], "ARTIST:portishead stars:5")
    assert query.text is None
    assert (query.artist, query.stars, query.listen_later) == ("portishead", 5, True)
    assert query.chips == [("later", ""), ("artist", "portishead"), ("stars", "5")]


def test_parse_leaves_out_what_is_not_a_filter():
    query = parse_song_query(["nope:x", "artist:"], "time:12 a:b artist: fav:yes album:")
    assert query.text == "time:12 a:b artist: fav:yes album:"
    assert query.chips == [] and not query.filtered


def test_parse_reports_bad_numbers():
    query = parse_song_query([], "from:soon stars:9 stars:x to:2001")
    assert query.from_year is None and query.stars is None and query.to_year == 2001
    assert query.errors == [
        "from: needs a year, like from:1999.",
        "stars: needs a number of stars from 1 to 5.",
    ]
    assert query.chips == [("to", "2001")]


def test_list_songs_with_filters(lib: Library):
    abba = lib.create_artist(ArtistIn("ABBA", genres=["Europop"], languages=["English", "Swedish"]))
    cure = lib.create_artist(ArtistIn("The Cure", genres=["Post-punk"], languages=["English"]))
    arrival = lib.create_album(AlbumIn(abba.id, "Arrival", 1976))
    visitors = lib.create_album(AlbumIn(abba.id, "The Visitors", 1981))
    disintegration = lib.create_album(AlbumIn(cure.id, "Disintegration", 1989))
    queen = lib.create_song(SongIn(abba.id, "Dancing Queen", arrival.id, rating=5, favorite=True))
    money = lib.create_song(SongIn(abba.id, "Money, Money, Money", arrival.id, rating=4))
    visitor = lib.create_song(SongIn(abba.id, "The Visitors", visitors.id, listen_later=True))
    lullaby = lib.create_song(SongIn(cure.id, "Lullaby", disintegration.id, rating=5))
    single = lib.create_song(SongIn(cure.id, "Boys Don't Cry", favorite=True))

    def titles(*filters: str, text: str | None = None) -> set[str]:
        search = parse_song_query(list(filters), text)
        return {s.song.title for s in lib.list_songs(q=search.text, search=search)}

    assert titles("artist:abba") == {queen.title, money.title, visitor.title}
    assert titles("album:visit") == {visitor.title}
    assert titles("from:1980") == {visitor.title, lullaby.title}  # a single has no year
    assert titles("from:1976", "to:1981") == {queen.title, money.title, visitor.title}
    assert titles("language:swed") == {queen.title, money.title, visitor.title}
    assert titles("genre:punk") == {lullaby.title, single.title}
    assert titles("stars:5") == {queen.title, lullaby.title}
    assert titles("fav:") == {queen.title, single.title}
    assert titles("later:") == {visitor.title}
    assert titles("stars:5", "language:english", text="queen") == {queen.title}
    assert lib.count_songs(q="money") == 1  # searches without filters still work


def test_move_songs_in_playlist(lib: Library):
    artist = lib.create_artist(ArtistIn("Band"))
    a, b, c, d, e = (lib.create_song(SongIn(artist.id, name)).id for name in "ABCDE")
    playlist = lib.create_playlist(PlaylistIn("Mix")).id
    lib.add_to_playlist(playlist, [a, b, c, d, e])

    lib.move_songs_in_playlist(playlist, [d, b], a)  # they keep their order in the playlist
    assert lib.playlist_song_ids(playlist) == [b, d, a, c, e]
    lib.move_songs_in_playlist(playlist, [b, d], None)
    assert lib.playlist_song_ids(playlist) == [a, c, e, b, d]
    lib.move_songs_in_playlist(playlist, [e], c)
    assert lib.playlist_song_ids(playlist) == [a, e, c, b, d]

    with pytest.raises(ValueError):
        lib.move_songs_in_playlist(playlist, [a, c], c)
    other = lib.create_song(SongIn(artist.id, "F")).id
    with pytest.raises(NotFound):
        lib.move_songs_in_playlist(playlist, [other], a)
    with pytest.raises(NotFound):
        lib.move_songs_in_playlist(playlist, [a], other)
    assert lib.playlist_song_ids(playlist) == [a, e, c, b, d]
