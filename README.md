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
Album covers are stored next to it, in `local/covers/`.

## Importing artists

`make serve` imports `info/` every time it starts (set `IMPLICATED_INFO` to use another folder),
so edits to the YAML files show up after a restart. `make import` (or
`python -m implicated.importer <dir>`) runs the same import on its own. The folder looks like this:

```
info/
  bands.yaml                      # artists: name, creation_year, genres, languages, members
  bands/<artist name>/albums.yaml # album title -> release_year, songs (name, duration)
  bands/<artist name>/covers/     # cover images, matched to albums by file name
```

Re-importing updates what changed and keeps your ratings, notes, favorites and plays; a broken
file rolls the whole import back and is reported in the server log. The full format is described
in `src/implicated/importer.py`. `info/` is git-ignored.

## Layout

- `src/implicated/models.py`: dataclass models (`Artist`, `Member`, `Album`, `Song`, `Play`, and their `*In` inputs)
- `src/implicated/library.py`: SQLite storage and stats
- `src/implicated/importer.py`: YAML importer for artists, members, albums, songs and covers
- `src/implicated/api.py`: JSON routes under `/api/...`
- `src/implicated/web/`: server-rendered pages (Jinja2 + vendored htmx, plain CSS, no build step)
