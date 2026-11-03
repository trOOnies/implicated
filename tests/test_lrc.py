import pytest

from implicated.lrc import TimedLine, parse_lrc, plain_lyrics, read_lrc


@pytest.mark.parametrize(
    ("stamp", "ms"),
    [
        ("[01:23]", 83_000),
        ("[01:23.4]", 83_400),
        ("[01:23.45]", 83_450),
        ("[01:23.456]", 83_456),
        ("[01:23:45]", 83_450),
        ("[100:00.00]", 6_000_000),
    ],
)
def test_stamps(stamp, ms):
    assert parse_lrc(f"{stamp}Words") == [TimedLine(ms, "Words")]


def test_lines_are_sorted_and_a_line_can_be_sung_twice():
    text = "[00:20.00][00:05.00] Chorus \n[00:10.00]Verse"
    assert parse_lrc(text) == [
        TimedLine(5000, "Chorus"),
        TimedLine(10000, "Verse"),
        TimedLine(20000, "Chorus"),
    ]


@pytest.mark.parametrize(
    ("offset", "ms"), [("500", 500), ("+500", 500), ("-500", 1500), ("9000", 0)]
)
def test_offset_moves_every_line(offset, ms):
    # Wherever the tag is, and a positive one shows the lines sooner.
    assert parse_lrc(f"[00:01.00]Words\n[offset:{offset}]") == [TimedLine(ms, "Words")]


def test_tags_notes_and_word_stamps_are_left_out():
    text = "\ufeff[ar:Portishead]\n[length:03:20]\nA note\n\n[00:01.00]<00:01.00>Two <00:01.5>words"
    assert parse_lrc(text) == [TimedLine(1000, "Two words")]
    assert parse_lrc("[ar:Portishead]\nNo stamps here") == []


def test_gaps_are_kept_but_are_not_lyrics():
    lines = parse_lrc("[00:01.00]One\n[00:02.00]\n[00:03.00]Two")
    assert lines[1] == TimedLine(2000, "")
    assert plain_lyrics(lines) == "One\nTwo"


def test_read_lrc(tmp_path):
    path = tmp_path / "Song.lrc"
    path.write_text("[00:01.00]Ñandú", encoding="utf-8")
    assert read_lrc(path) == [TimedLine(1000, "Ñandú")]
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(ValueError, match="Song.lrc is not UTF-8"):
        read_lrc(path)
