"""Read the concerts listed in ``concerts.csv``, in the info folder, and keep track there of
which ones you want to go to and went to.

The file is read again on every visit to the Concerts page, so edits show straight away. Its
first line names the columns, separated by ``;`` (or ``,``)::

    date;band;tour;country;city;Want to go;Going
    2026-12-09;Sleeping With Sirens;Complete Collapse Tour;Argentina;Buenos Aires;true;true

Dates are ``YYYY-MM-DD``; *Want to go* and *Going* are ``true`` or ``false`` (also ``yes``/``no``,
``1``/``0``, or empty for false), and *Going* on a past concert means you went. Column names
ignore case, spaces and underscores; only ``date`` and ``band`` are required, and the tour is
optional.
"""

from __future__ import annotations

import csv
import io
import os
from datetime import date
from pathlib import Path

from implicated.models import CONCERT_STATUSES, PAST_CONCERT_STATUSES, Concert

CONCERTS_FILE = "concerts.csv"
COLUMNS = ("date", "band", "tour", "country", "city", "want_to_go", "going")
TRUE = {"true", "yes", "y", "1", "x"}
FALSE = {"false", "no", "n", "0", ""}


def _column(name: str) -> str:
    return "_".join(name.strip().casefold().replace("_", " ").split())


def _flag(value: str | None, where: str, warnings: list[str]) -> bool:
    value = (value or "").strip().casefold()
    if value in TRUE:
        return True
    if value not in FALSE:
        warnings.append(f"{where}: {value!r} is not true or false; read as false")
    return False


def _read(path: Path) -> tuple[str, list[str], str]:
    """The file's text (without a byte order mark), its lines with their endings, and its
    delimiter."""
    with path.open(encoding="utf-8-sig", newline="") as file:  # keeping the line endings
        text = file.read()
    lines = text.splitlines(keepends=True)
    delimiter = ";" if lines and ";" in lines[0] else ","
    return text, lines, delimiter


def _cells(line: str, delimiter: str) -> list[str]:
    return next(csv.reader([line.rstrip("\r\n")], delimiter=delimiter), [])


def read_concerts(path: str | Path) -> tuple[list[Concert], list[str]]:
    """The concerts in the file, by date, and what could not be read in it; nothing (and no
    warnings) when there is no such file. Rows without a valid date or a band are skipped."""
    path = Path(path)
    if not path.is_file():
        return [], []
    _text, lines, delimiter = _read(path)
    if not lines:
        return [], []
    header = [_column(name) for name in _cells(lines[0], delimiter)]
    warnings = [f"{path.name}: unknown column {name!r}" for name in header if name not in COLUMNS]
    missing = [name for name in ("date", "band") if name not in header]
    if missing:
        warnings.append(f"{path.name}: the {' and '.join(missing)} column is missing")
        return [], warnings
    concerts = []
    for number, line in enumerate(lines[1:], start=2):
        row = _cells(line, delimiter)
        if not any(cell.strip() for cell in row):
            continue
        where = f"{path.name} line {number}"
        values = {name: cell.strip() for name, cell in zip(header, row, strict=False)}
        try:
            day = date.fromisoformat(values.get("date", ""))
        except ValueError:
            warnings.append(f"{where}: {values.get('date', '')!r} is not a date like 2026-12-09")
            continue
        if not values.get("band"):
            warnings.append(f"{where}: the band is missing")
            continue
        concerts.append(
            Concert(
                date=day,
                band=values["band"],
                country=values.get("country") or None,
                city=values.get("city") or None,
                want_to_go=_flag(values.get("want_to_go"), where, warnings),
                going=_flag(values.get("going"), where, warnings),
                tour=values.get("tour") or None,
                line=number,
            )
        )
    concerts.sort(key=lambda c: (c.date, c.band.casefold()))
    return concerts, warnings


def set_status(
    path: str | Path, line: int, day: date, band: str, status: str, today: date | None = None
) -> None:
    """Mark the concert on ``line`` of the file with ``status``: for one still to come, one of
    ``CONCERT_STATUSES`` (nothing, want to go, going); for a past one, one of
    ``PAST_CONCERT_STATUSES`` (nothing or went). Only its Want to go and Going cells change.

    ``day`` and ``band`` must still be on that line, so a file edited since it was read is not
    changed in the wrong place."""
    path = Path(path)
    today = today or date.today()
    past = day < today
    if status not in (PAST_CONCERT_STATUSES if past else CONCERT_STATUSES):
        raise ValueError(f"{status!r} is not a status for a {'past' if past else 'coming'} concert")
    _text, lines, delimiter = _read(path)
    header = [_column(name) for name in _cells(lines[0], delimiter)] if lines else []
    if "want_to_go" not in header or "going" not in header:
        raise ValueError(f"{path.name} needs a Want to go and a Going column")
    row = _cells(lines[line - 1], delimiter) if 1 < line <= len(lines) else []
    row += [""] * (len(header) - len(row))
    values = dict(zip(header, (cell.strip() for cell in row), strict=False))
    if values.get("date") != day.isoformat() or values.get("band") != band:
        raise ValueError(f"{path.name} changed since the page was loaded; reload it")
    if past:
        # Whether you wanted to go no longer shows, so it stays as it was.
        flags = {"going": status == "went"} | ({"want_to_go": True} if status == "went" else {})
    else:
        flags = {"want_to_go": status in ("want", "going"), "going": status == "going"}
    for name, value in flags.items():
        row[header.index(name)] = "true" if value else "false"
    out = io.StringIO()
    ending = lines[line - 1][len(lines[line - 1].rstrip("\r\n")) :]
    csv.writer(out, delimiter=delimiter, lineterminator=ending).writerow(row)
    lines[line - 1] = out.getvalue()
    bom = "﻿" if path.read_bytes().startswith(b"\xef\xbb\xbf") else ""
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(bom + "".join(lines), encoding="utf-8", newline="")
    os.replace(temp, path)
