from datetime import date
from pathlib import Path

import pytest

from implicated.concerts import read_concerts, set_status
from implicated.models import Concert

TODAY = date(2026, 10, 28)


def test_read_concerts(tmp_path: Path):
    path = tmp_path / "concerts.csv"
    assert read_concerts(path) == ([], [])  # no file yet
    path.write_text(
        "Date;Band;Country;City;Want to go;Going\n"
        "2027-05-11;Simple Plan;Argentina;Buenos Aires;true;false\n"
        "\n"
        "2026-07-18;My Chemical Romance;Spain;Madrid;TRUE;1\n"
        "2026-12-09;;Argentina;Buenos Aires;true;true\n"
        "2026-12-10;Short Row\n"
    )
    concerts, warnings = read_concerts(path)
    assert concerts == [  # by date
        Concert(date(2026, 7, 18), "My Chemical Romance", "Spain", "Madrid", True, True, line=4),
        Concert(date(2026, 12, 10), "Short Row", line=6),
        Concert(date(2027, 5, 11), "Simple Plan", "Argentina", "Buenos Aires", True, line=2),
    ]
    assert warnings == ["concerts.csv line 5: the band is missing"]


def test_concerts_with_commas_and_odd_columns(tmp_path: Path):
    path = tmp_path / "concerts.csv"
    path.write_text("﻿date,band,venue\n2026-01-02,Air,Olympia\n")
    concerts, warnings = read_concerts(path)
    assert concerts == [Concert(date(2026, 1, 2), "Air", line=2)]
    assert warnings == ["concerts.csv: unknown column 'venue'"]
    path.write_text("when;who\n2026-01-02;Air\n")
    concerts, warnings = read_concerts(path)
    assert concerts == [] and "the date and band column is missing" in warnings[-1]
    path.write_text("")
    assert read_concerts(path) == ([], [])


def test_concert_status(tmp_path: Path):
    path = tmp_path / "concerts.csv"
    path.write_bytes(
        b"\xef\xbb\xbfdate;band;tour;Want to go;Going\r\n"
        b'2026-12-09;Sleeping With Sirens;"A; B";;\r\n'
        b"2026-01-01;Air;;true;false\r\n"
        b"2026-02-02;Zero 7;;true;true"
    )
    concert = read_concerts(path)[0][-1]
    assert (concert.tour, concert.status(TODAY)) == ("A; B", "")
    set_status(path, 2, date(2026, 12, 9), "Sleeping With Sirens", "want", TODAY)
    assert read_concerts(path)[0][-1].status(TODAY) == "want"
    set_status(path, 2, date(2026, 12, 9), "Sleeping With Sirens", "going", TODAY)
    set_status(path, 3, date(2026, 1, 1), "Air", "went", TODAY)
    set_status(path, 4, date(2026, 2, 2), "Zero 7", "", TODAY)
    # Only the two cells change; the rest of the file stays as it was.
    assert path.read_bytes() == (
        b"\xef\xbb\xbfdate;band;tour;Want to go;Going\r\n"
        b'2026-12-09;Sleeping With Sirens;"A; B";true;true\r\n'
        b"2026-01-01;Air;;true;true\r\n"
        b"2026-02-02;Zero 7;;true;false"
    )
    assert [c.status(TODAY) for c in read_concerts(path)[0]] == ["went", "", "going"]

    with pytest.raises(ValueError, match="not a status for a past concert"):
        set_status(path, 3, date(2026, 1, 1), "Air", "want", TODAY)
    with pytest.raises(ValueError, match="not a status for a coming concert"):
        set_status(path, 2, date(2026, 12, 9), "Sleeping With Sirens", "went", TODAY)
    with pytest.raises(ValueError, match="changed since the page was loaded"):
        set_status(path, 3, date(2026, 12, 9), "Sleeping With Sirens", "going", TODAY)
    with pytest.raises(ValueError, match="changed since"):
        set_status(path, 9, date(2026, 12, 9), "Sleeping With Sirens", "going", TODAY)
    path.write_text("date;band\n2026-12-09;Air\n")
    with pytest.raises(ValueError, match="needs a Want to go and a Going column"):
        set_status(path, 2, date(2026, 12, 9), "Air", "going", TODAY)
