# implicated
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
so edits to the YAML files show up after a restart. `make import` (or
`python -m implicated.importer <dir>`) runs the same import on its own. The folder looks like this:

```
info/
  bands.yaml                      # artists: name, origin, creation_year, genres,
                                  #   languages, description, wiki, members
  bands/<artist name>/albums.yaml # album title -> release_year, songs (name, duration,
                                  #   no_lyrics: true for instrumentals)
  bands/<artist name>/band.jpg    # picture of the artist (.jpg, .png, .webp, .avif, ...)
  bands/<artist name>/covers/     # cover images, matched to albums by file name
  bands/<artist name>/songs/      # lyrics, one <song title>.txt per song (empty = missing)
```

Re-importing updates what changed and keeps your ratings, notes, favorites, CD and vinyl
ownership, music videos, listen later marks and plays; a broken
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
Songs with `no_lyrics` (set in `albums.yaml`, or with the No lyrics box when adding or editing a
song) have no words, so they are never missing lyrics and do not count in that share.

## Your own data

Besides what the import brings, the app keeps:

- **Notes** on artists, albums and songs (artists' notes are separate from their imported description).
- **CD and vinyl ownership** of albums: each format is unset, wanted or owned, picked with a
  selector on the album page and in the artist's discography. The Albums page filters by
  Wanted (CD), Owned (CD), Wanted (Vinyl) and Owned (Vinyl).
- **Music videos** of songs on YouTube, entered as a link or the bare 11-character video id.
  Links from `youtube.com` (`watch?v=`, `/embed/`, `/shorts/`, `/live/`, `/v/`, also `m.` and
  `music.`), `youtu.be` and `youtube-nocookie.com` are accepted; anything else is refused, and
  only the id is stored. Song pages embed the video.
- **Listen later** marks on artists, albums and songs. A song's mark goes away when you log a play
  of it.

## Layout

- `src/implicated/models.py`: dataclass models (`Artist`, `Member`, `Album`, `Song`, `Play`, and their `*In` inputs)
- `src/implicated/library.py`: SQLite storage and stats
- `src/implicated/importer.py`: YAML importer for artists, members, albums, songs, covers and lyrics
- `src/implicated/api/`: JSON routes under `/api/...`, one router (and API page tag) per resource
- `src/implicated/web/`: server-rendered pages (Jinja2 + vendored htmx, plain CSS, a little plain JS for sortable tables, no build step)
