# <img src="src/implicated/web/static/favicon.svg" alt="" width="36" height="36"> implicated
App for managing and exploring your music taste

## Quick start

```sh
make install   # pip install -e ".[dev]"
make seed      # optional: demo artists, albums, songs and plays
make serve     # http://127.0.0.1:8000  (JSON API docs at /docs); imports info/ first
make test      # run the tests with coverage
```

The library is a SQLite file at `local/implicated.db`; set `IMPLICATED_DB` to use another path.
Album covers and artist images are stored next to it, in `local/covers/` and `local/artists/`.

## Importing artists

`make serve` imports `info/` every time it starts (set `IMPLICATED_INFO` to use another folder),
so edits to the YAML files show up after a restart. If the folder, its `bands.yaml` or its
`bands/` folder is missing, startup creates them (`bands.yaml` with a commented example), lists
them and stops, so you can fill them in and start again. `make import` (or
`python -m implicated.importer <dir>`) runs the same import on its own. The folder looks like this:

```
info/
  bands.yaml                      # artists: name, origin, creation_year, genres,
                                  #   languages, description, wiki, members
  bands/<artist name>/albums.yaml # album title -> release_year, unreleased: true for scrapped
                                  #   or leaked albums, songs (name, duration,
                                  #   no_lyrics: true for instrumentals)
  bands/<artist name>/band.jpg    # picture of the artist (.jpg, .png, .webp, .avif, ...)
  bands/<artist name>/covers/     # cover images, matched to albums by file name
  bands/<artist name>/songs/      # lyrics, one <song title>.txt per song (empty = missing)
  bands/<artist name>/tabs/       # optional guitar tabs, <song title>.gp5 (or .gp, .gpx, .gp4, .gp3)
```

Re-importing updates what changed and keeps your ratings, notes, favorites, CD and vinyl
ownership, music videos, song videos, listen later marks, playlists and plays; a broken
file rolls the whole import back and is reported in the server log. `wiki` must be a link to a
Wikipedia article (`https://<lang>.wikipedia.org/wiki/<title>`); any other link is ignored with a
warning, and the API and forms refuse it. The full format is described
in `src/implicated/importer.py`. `info/` is git-ignored.

Lyrics files are named after the song, with `/` turned into `_` and the characters Windows refuses
in file names (`<>:"\|?*`) left out, so `S/C/A/R/E/C/R/O/W` is `S_C_A_R_E_C_R_O_W.txt`. Every song
has a lyrics file: the import, the Add song forms and the API create an empty one when it is
missing (also for artists that are not in `bands.yaml`), and renaming a song renames its file.
Each song has its own page with its lyrics, read again from the file on every visit, and buttons
to open the file in your default text editor (under WSL, the Windows one) and to reload it. Album
pages mark the songs still missing lyrics, and album lists show the share of songs that have them.
The Albums and Songs pages can show only the albums and songs still missing lyrics. Songs with `no_lyrics` (set in `albums.yaml`, or with the No lyrics box when adding or editing a
song) have no words, so they are never missing lyrics and do not count in that share.

Songs can have a guitar tab: a Guitar Pro file in `bands/<artist name>/tabs/`, named like the
lyrics file but ending in `.gp5` (or `.gp`, `.gpx`, `.gp4`, `.gp3`). In TuxGuitar, use File > Save
As and pick Guitar Pro 5; its own `.tg` files cannot be read. The song page draws the tab as
notation and tablature with [alphaTab](https://www.alphatab.net/) (bundled in
`src/implicated/web/static/alphatab/`, MPL-2.0), can play it back, lets you pick the track when the
file holds several, and has buttons to open the file in TuxGuitar and to reload it after saving.

The **Budget** page is a cart of CDs and vinyls to buy: pick a band, then one of its albums, then
the format (each picker says which ones you marked *Wanted*), and give its price, quantity and, optionally, a link to it in a
shop; the cart adds up the total. Albums you already own on that format can go in too, but each is
flagged. *Make Budget* saves the cart under a name and empties it; *Buy* marks its albums owned on
their formats and keeps it as a bought budget. Saved budgets can be bought, deleted, or loaded back
into the cart to edit. Buying albums that other budgets also hold (on the same format) first asks
whether to drop them from those budgets, deleting any budget that leaves empty. Prices show with
a generic `$`, whatever currency the shops charge in. The API has it all under `/api/budget`.

Albums without a cover of their own are tagged *no cover* (the ♪ placeholder shows instead) or
*shared cover* (the same image as another album, say a deluxe edition that matched the original's
cover file), and the Albums page can show only those.

## Your own data

Besides what the import brings, the app keeps:

- **Ratings** of artists, albums and songs, from 1 to 5 stars, set by clicking the stars.
- **Notes** on artists, albums and songs (artists' notes are separate from their imported description).
- **CD and vinyl ownership** of albums: each format is unset, wanted or owned, picked with a
  selector on the album page and in the artist's discography. The Albums page filters by
  Wanted (CD), Owned (CD), Wanted (Vinyl) and Owned (Vinyl).
- **YouTube videos**: one music video per song, plus any number of other videos (lessons,
  covers, live takes), each with a title and a start time, in your own order. They are entered
  as a link or the bare 11-character video id. Links from `youtube.com` (`watch?v=`, `/embed/`,
  `/shorts/`, `/live/`, `/v/`, also `m.` and `music.`), `youtu.be` and `youtube-nocookie.com`
  are accepted; anything else is refused, and only the id (and a `t=` start) is stored. Song
  pages embed them.
- **Playlists** of songs, in your own order. Songs are added one by one from their page, or a
  whole album at a time from the album page; a song is in a playlist at most once.
  **Favorites** and **Listen Later** are built-in playlists that cannot be renamed or deleted:
  they always hold the songs with that mark, and adding or removing songs sets or clears it.
- **Listen later** marks on artists, albums and songs. A song's mark goes away when you log a play
  of it.

## Layout

- `src/implicated/models.py`: dataclass models (`Artist`, `Member`, `Album`, `Song`, `SongVideo`, `Play`, `Playlist`, and their `*In` inputs)
- `src/implicated/library.py`: SQLite storage and stats
- `src/implicated/importer.py`: YAML importer for artists, members, albums, songs, covers and lyrics
- `src/implicated/api/`: JSON routes under `/api/...`, one router (and API page tag) per resource
- `src/implicated/web/`: server-rendered pages (Jinja2 + vendored htmx, plain CSS, a little plain JS for sortable tables, no build step)
