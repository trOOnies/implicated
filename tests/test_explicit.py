import pytest

from implicated.explicit import curse_words, is_explicit


@pytest.mark.parametrize(
    "text",
    [
        "Fuck you and your new love for yourself",
        "You motherfuckers",
        "it's a clusterfuck",
        "Don't know SHIT",
        "that's bullshit",
        "a cold heart bitch",
        "you dickhead",
        "what an asshole",
        "Whores and sluts",
    ],
)
def test_swearing_is_explicit(text):
    assert is_explicit(text)


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "Give 'em hell, kid, damn it",
        "Smoke some fags and play some pool",  # cigarettes
        "What's new pussycat",
        "A rooster's cock-a-doodle-doo",
        "Kick some ass, you bastards",
        "Moby Dick and the Scunthorpe shitake",  # not whole words
        "Shitake mushrooms and a cockpit",
    ],
)
def test_innocent_words_are_not(text):
    assert not is_explicit(text)


def test_curse_words():
    text = "Shit, FUCK this shit\nfucking hell"
    assert curse_words(text) == ["shit", "fuck", "fucking"]
    assert curse_words(None) == []
