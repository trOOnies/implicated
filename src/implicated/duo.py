"""Listening recommendations from your data and a friend's, for the Duo page.

A friend shares the file the Duo page (or ``python -m implicated.userdata export``) downloads,
and you upload it: it is kept in ``friends/``, next to the library, and never mixed into your
library. ``compare`` lines the two exports up by name (see ``implicated.userdata.key``) and sorts
what they say into ``Section``s, such as the songs you both love, what you both have on listen
later, or the albums one of you owns and the other wants.

"Liking" a record means a favorite or a rating of 4 stars or more; a song is "on repeat" with
``REPEAT`` plays or more. An album or artist counts as played when any of its songs was. Songs
merged into a main version (``same_as``) count as that one song, with all their plays, and
albums merged into a main edition as that one album.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from implicated.library import Library
from implicated.userdata import check, key

# Plays that make a song one you have on repeat.
REPEAT = 3
# Stars that make a rating a "like".
LIKED_RATING = 4
# Characters a friend's file name (``slug``) can have.
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
KIND_ORDER = {"artist": 0, "album": 1, "song": 2}


# Friends' files


@dataclass
class Friend:
    slug: str  # the file's name in ``friends/``, without ``.json``
    name: str
    exported_at: str | None
    artists: int
    songs: int
    plays: int


def friends_dir(lib: Library) -> Path:
    return lib.path.parent / "friends"


def slugify(name: str) -> str:
    slug = "-".join(re.findall(r"[a-z0-9]+", name.casefold()))
    return slug or "friend"


def save_friend(lib: Library, data: dict, name: str | None = None) -> Friend:
    """Keep a friend's export under ``name`` (or the file's owner), replacing the one of that
    name. ``ValueError`` when it is not an export."""
    check(data)
    name = (name or "").strip() or (data.get("owner") or "").strip() or "Friend"
    data = {**data, "owner": name}
    folder = friends_dir(lib)
    folder.mkdir(parents=True, exist_ok=True)
    slug = slugify(name)
    (folder / f"{slug}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return _friend(slug, data)


def list_friends(lib: Library) -> list[Friend]:
    folder = friends_dir(lib)
    friends = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            friends.append(_friend(path.stem, load_friend(lib, path.stem)))
        except (OSError, ValueError):
            continue  # not readable any more: skipped rather than breaking the page
    return sorted(friends, key=lambda f: f.name.casefold())


def load_friend(lib: Library, slug: str) -> dict:
    """A saved friend's export; ``FileNotFoundError`` without one of that slug."""
    if not SLUG.fullmatch(slug):
        raise FileNotFoundError(slug)
    path = friends_dir(lib) / f"{slug}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    check(data)
    return data


def delete_friend(lib: Library, slug: str) -> None:
    if not SLUG.fullmatch(slug):
        raise FileNotFoundError(slug)
    (friends_dir(lib) / f"{slug}.json").unlink()


def _friend(slug: str, data: dict) -> Friend:
    songs = data.get("songs", [])
    return Friend(
        slug=slug,
        name=data.get("owner") or slug,
        exported_at=data.get("exported_at"),
        artists=len(data.get("artists", [])),
        songs=len(songs),
        plays=sum(len(s.get("plays") or []) for s in songs if isinstance(s, dict)),
    )


# Comparing


@dataclass
class Item:
    kind: str  # "artist", "album" or "song"
    title: str
    artist: str | None = None  # of albums and songs
    album: str | None = None  # of songs on an album
    note: str = ""  # why it is here
    # Its id in your library, to link to it; ``None`` when you do not have it.
    id: int | None = None
    genres: list[str] = field(default_factory=list)
    score: float = 0  # higher goes first


@dataclass
class Section:
    id: str
    emoji: str
    title: str
    blurb: str
    items: list[Item]

    @property
    def song_ids(self) -> list[int]:
        """Your songs in it, to make a playlist of."""
        return list(dict.fromkeys(i.id for i in self.items if i.kind == "song" and i.id))


@dataclass
class Duo:
    you: str
    friend: str
    match: int  # 0-100: how much of what either of you likes, both of you like
    shared_artists: int  # artists in both libraries
    shared_songs: int
    genres: list[tuple[str, int]]  # genres you both like, most shared first
    sections: list[Section]


@dataclass
class _Record:
    """One artist, album or song of an export, with what its owner did with it."""

    kind: str
    data: dict
    plays: int = 0

    @property
    def favorite(self) -> bool:
        return self.data.get("favorite") is True

    @property
    def later(self) -> bool:
        return self.data.get("listen_later") is True

    @property
    def rating(self) -> int | None:
        rating = self.data.get("rating")
        return rating if isinstance(rating, int) and not isinstance(rating, bool) else None

    @property
    def liked(self) -> bool:
        return self.favorite or (self.rating or 0) >= LIKED_RATING

    def why(self, who: str) -> str:
        """What ``who`` did with it, briefly: "Sol: favorite, ★★★★, 12 plays"."""
        parts = []
        if self.favorite:
            parts.append("favorite")
        if self.rating:
            parts.append("★" * self.rating)
        if self.plays:
            parts.append(f"{self.plays} play{'s' if self.plays != 1 else ''}")
        return f"{who}: {', '.join(parts)}" if parts else ""


class _Taste:
    """An export, indexed by ``key`` of each record's names."""

    def __init__(self, data: dict) -> None:
        self.name = data.get("owner") or "?"
        self.records: dict[tuple, _Record] = {}
        album_plays: Counter = Counter()
        artist_plays: Counter = Counter()
        merged = []
        for s in _dicts(data, "songs"):
            plays = len(s.get("plays") or [])
            k = ("song", *key(s.get("artist"), s.get("album"), s.get("title")))
            self.records[k] = _Record("song", s, plays)
            album_plays[key(s.get("artist"), s.get("album"))] += plays
            artist_plays[key(s.get("artist"))] += plays
            if isinstance(s.get("same_as"), dict):
                merged.append((k, s["same_as"]))
        for a in _dicts(data, "albums"):
            k = key(a.get("artist"), a.get("title"))
            self.records[("album", *k)] = _Record("album", a, album_plays[k])
            if isinstance(a.get("same_as"), dict):
                merged.append((("album", *k), a["same_as"]))
        for k, ref in merged:
            if k[0] == "album":
                main = ("album", *key(ref.get("artist"), ref.get("title")))
            else:
                main = ("song", *key(ref.get("artist"), ref.get("album"), ref.get("title")))
            if main != k and main in self.records:
                self.records[main].plays += self.records.pop(k).plays
        for a in _dicts(data, "artists"):
            k = key(a.get("name"))
            self.records[("artist", *k)] = _Record("artist", a, artist_plays[k])
        self.genres = {
            k[1]: [g for g in r.data.get("genres") or [] if isinstance(g, str)]
            for k, r in self.records.items()
            if r.kind == "artist"
        }

    def liked_artists(self) -> set[str]:
        """Artists liked, or with a liked album or song, or with a song on repeat."""
        out = set()
        for k, r in self.records.items():
            if r.liked or (r.kind == "song" and r.plays >= REPEAT):
                out.add(k[1])
        return out


def _dicts(data: dict, name: str) -> list[dict]:
    return [r for r in data.get(name) or [] if isinstance(r, dict)]


def compare(mine: dict, theirs: dict, ids: dict[tuple, int] | None = None) -> Duo:
    """What your export (``mine``) and a friend's (``theirs``) say together. ``ids`` gives the
    library id of your records by kind and ``key`` (see ``library_ids``), for links."""
    you, friend = _Taste(mine), _Taste(theirs)
    ids = ids or {}
    f = friend.name

    def item(k: tuple, record: _Record, note: str, score: float = 0) -> Item:
        d = record.data
        if record.kind == "artist":
            names = {"title": d.get("name") or ""}
        else:
            names = {"title": d.get("title") or "", "artist": d.get("artist")}
            if record.kind == "song":
                names["album"] = d.get("album")
        return Item(
            record.kind,
            **names,
            note=note,
            id=ids.get(k),
            genres=(d.get("genres") or [])[:3] if record.kind == "artist" else [],
            score=score,
        )

    both = [(k, you.records[k], r) for k, r in friend.records.items() if k in you.records]
    only_theirs = [(k, r) for k, r in friend.records.items() if k not in you.records]

    def pick(pairs, test, note, score) -> list[Item]:
        return [item(k, mine_r, note(mine_r, their_r), score(mine_r, their_r))
                for k, mine_r, their_r in pairs if test(mine_r, their_r)]  # fmt: skip

    sections = [
        Section(
            "love", "💞", "You both love",
            f"Favorites you and {f} share.",
            pick(both, lambda m, t: m.favorite and t.favorite,
                 lambda m, t: f"{m.plays + t.plays} plays between you" if m.plays + t.plays
                 else "",
                 lambda m, t: m.plays + t.plays + (m.rating or 0) + (t.rating or 0)),
        ),
        Section(
            "later", "⧖", "Listen later, together",
            "On both your listen later lists: a listening session for two.",
            pick(both, lambda m, t: m.later and t.later, lambda m, t: "",
                 lambda m, t: 0),
        ),
        Section(
            "repeat", "🔁", "Both on repeat",
            f"Songs you and {f} have each played {REPEAT} times or more.",
            pick(both, lambda m, t: m.kind == "song" and min(m.plays, t.plays) >= REPEAT,
                 lambda m, t: f"you {m.plays}, {f} {t.plays}",
                 lambda m, t: m.plays + t.plays),
        ),
        Section(
            "queue", "⏩", "Move it up your queue",
            f"On your listen later list, and {f} already loves it or plays it a lot.",
            pick(both, lambda m, t: m.later and (t.liked or t.plays >= REPEAT),
                 lambda m, t: t.why(f), lambda m, t: _taste_score(t)),
        ),
        Section(
            "recommends", "🎁", f"{f} recommends",
            f"In your library, loved by {f}, and you haven't listened to it yet.",
            pick(both, lambda m, t: t.liked and not m.plays and not m.liked and not m.later,
                 lambda m, t: t.why(f), lambda m, t: _taste_score(t)),
        ),
        Section(
            "rotation", "🔥", f"{f}'s heavy rotation",
            f"Songs {f} keeps playing that you have never played.",
            pick(both, lambda m, t: m.kind == "song" and t.plays >= REPEAT and not m.plays
                 and not t.liked and not m.later,
                 lambda m, t: t.why(f), lambda m, t: t.plays),
        ),
        Section(
            "share", "📣", f"Share with {f}",
            f"Your favorites {f} hasn't listened to yet, or doesn't even have.",
            _share(you, friend, ids, item),
        ),
        Section(
            "discover", "🔭", "Discover",
            f"Artists and albums {f} loves that aren't in your library yet.",
            [item(k, r, r.why(f), _taste_score(r)) for k, r in only_theirs
             if r.kind != "song" and (r.liked or r.plays >= REPEAT)],
        ),
        Section(
            "swap", "💿", "Swap shelf",
            "Albums one of you owns on CD or vinyl and the other wants.",
            _swap(both, f, item),
        ),
    ]  # fmt: skip
    for section in sections:
        section.items.sort(key=lambda i: (-i.score, KIND_ORDER[i.kind], i.title.casefold()))

    yours, theirs_liked = you.liked_artists(), friend.liked_artists()
    union = yours | theirs_liked
    match = round(100 * len(yours & theirs_liked) / len(union)) if union else 0
    genres: Counter = Counter()
    for artist in yours & theirs_liked:
        for genre in {g.casefold(): g for g in friend.genres.get(artist) or you.genres.get(artist)
                      or []}.values():  # fmt: skip
            genres[genre] += 1
    return Duo(
        you=mine.get("owner") or "You",
        friend=f,
        match=match,
        shared_artists=sum(1 for k, *_ in both if k[0] == "artist"),
        shared_songs=sum(1 for k, *_ in both if k[0] == "song"),
        genres=genres.most_common(6),
        sections=sections,
    )


def _taste_score(r: _Record) -> float:
    return (2 if r.favorite else 0) + (r.rating or 0) + r.plays / 10


def _share(you: _Taste, friend: _Taste, ids: dict, item) -> list[Item]:
    out = []
    for k, r in you.records.items():
        if not r.favorite:
            continue
        theirs = friend.records.get(k)
        if theirs is None:
            if r.kind == "song":
                continue  # the album or artist says it already, and the song may be on another
            out.append(item(k, r, f"not in {friend.name}'s library", _taste_score(r)))
        elif not theirs.plays and not theirs.liked:
            out.append(item(k, r, f"{friend.name} hasn't played it", _taste_score(r)))
    return out


def _swap(both: list, f: str, item) -> list[Item]:
    out = []
    for k, m, t in both:
        if m.kind != "album":
            continue
        for fmt, label in (("cd", "CD"), ("vinyl", "vinyl")):
            mine, theirs = m.data.get(fmt), t.data.get(fmt)
            if mine == "wanted" and theirs == "owned":
                out.append(item(k, m, f"borrow {f}'s {label}", 1))
            elif mine == "owned" and theirs == "wanted":
                out.append(item(k, m, f"lend {f} your {label}", 0))
    return out


def library_ids(lib: Library) -> dict[tuple, int]:
    """Your records' ids by kind and ``key`` of their names, as ``compare`` looks them up."""
    with lib.connect() as conn:
        ids = {
            ("artist", *key(r["name"])): r["id"]
            for r in conn.execute("SELECT id, name FROM artists ORDER BY id DESC")
        }
        ids |= {
            ("album", *key(r["artist"], r["title"])): r["id"]
            for r in conn.execute(
                """SELECT al.id, ar.name AS artist, al.title FROM albums al
                 JOIN artists ar ON ar.id = al.artist_id ORDER BY al.id DESC"""
            )
        }
        ids |= {
            ("song", *key(r["artist"], r["album"], r["title"])): r["id"]
            for r in conn.execute(
                """SELECT s.id, ar.name AS artist, al.title AS album, s.title FROM songs s
                 JOIN artists ar ON ar.id = s.artist_id LEFT JOIN albums al ON al.id = s.album_id
                ORDER BY s.id DESC"""
            )
        }
    return ids  # fmt: skip
