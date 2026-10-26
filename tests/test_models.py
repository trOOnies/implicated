import pytest

from implicated.models import validate_wiki


@pytest.mark.parametrize(
    "url",
    [
        None,
        "https://en.wikipedia.org/wiki/My_Chemical_Romance",
        "https://en.wikipedia.org/wiki/AC/DC",
        "https://en.wikipedia.org/wiki/Star_Wars:_Episode_IV_%E2%80%93_A_New_Hope",
        "https://es.wikipedia.org/wiki/Beyoncé#Carrera",
        "https://pt-br.m.wikipedia.org/wiki/Beyonc%C3%A9",
    ],
)
def test_wikipedia_articles_are_accepted(url):
    validate_wiki(url)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "javascript:alert(1)",
        "http://en.wikipedia.org/wiki/X",  # not https
        "//en.wikipedia.org/wiki/X",
        "https://wikipedia.org/wiki/X",
        "https://en.wikipedia.org.evil.com/wiki/X",
        "https://evilwikipedia.org/wiki/X",
        "https://evil.com/wiki/https://en.wikipedia.org/wiki/X",
        "https://user@en.wikipedia.org/wiki/X",
        "https://en.wikipedia.org@evil.com/wiki/X",
        "https://en.wikipedia.org\\@evil.com/wiki/X",
        "https://en.wikipedia.org:8080/wiki/X",
        "https://EN.wikipedia.org/wiki/X",
        "https://en.wikipedia.org/",
        "https://en.wikipedia.org/wiki/",
        "https://en.wikipedia.org/w/index.php?title=X",
        "https://en.wikipedia.org/wiki/X?action=edit",
        "https://en.wikipedia.org/wiki/X?",
        "https://en.wikipedia.org/wiki/../w/index.php",
        "https://en.wikipedia.org/wiki/%2e%2e/w/index.php",
        "https://en.wikipedia.org/wiki/Special:Random",
        "https://en.wikipedia.org/wiki/user_talk:Someone",
        "https://en.wikipedia.org/wiki/File:Cover.jpg",
        "https://en.wikipedia.org/wiki/X Y",
        "https://en.wikipedia.org/wiki/X\nY",
        "https://en.wikipedia.org/wiki/X%0AY",
        'https://en.wikipedia.org/wiki/X"onmouseover="alert(1)',
        "https://en.wikipedia.org/wiki/" + "x" * 2000,
    ],
)
def test_other_links_are_rejected(url):
    with pytest.raises(ValueError, match="Wikipedia article"):
        validate_wiki(url)
