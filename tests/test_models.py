import pytest

from implicated.models import validate_ownership, validate_wiki, youtube_id


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


ID = "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "value",
    [
        ID,
        f"  {ID} ",
        f"https://www.youtube.com/watch?v={ID}",
        f"https://www.youtube.com/watch?v={ID}&t=42s&list=PL123",
        f"http://youtube.com/watch?feature=share&v={ID}",
        f"https://m.youtube.com/watch?v={ID}",
        f"https://music.youtube.com/watch?v={ID}",
        f"youtube.com/watch?v={ID}",
        f"https://youtu.be/{ID}",
        f"https://youtu.be/{ID}?si=abc&t=10",
        f"https://www.youtube.com/embed/{ID}",
        f"https://www.youtube-nocookie.com/embed/{ID}?start=3",
        f"https://www.youtube.com/shorts/{ID}",
        f"https://www.youtube.com/live/{ID}/",
        f"https://WWW.YOUTUBE.COM/watch?v={ID}",
    ],
)
def test_youtube_links_and_ids_are_accepted(value):
    assert youtube_id(value) == ID


@pytest.mark.parametrize("value", [None, "", "   "])
def test_no_youtube_video(value):
    assert youtube_id(value) is None


@pytest.mark.parametrize(
    "value",
    [
        "dQw4w9WgXc",  # too short
        "dQw4w9WgXcQQ",  # too long
        "dQw4w9WgXc!",
        f"https://www.youtube.com/watch?v={ID}x",
        f"https://www.youtube.com/watch?v={ID}&v={ID}",
        "https://www.youtube.com/watch",
        f"https://www.youtube.com/results?search_query={ID}",
        f"https://www.youtube.com/{ID}",
        f"https://youtube.com.evil.com/watch?v={ID}",
        f"https://evilyoutube.com/watch?v={ID}",
        f"https://www.youtube.com:8080/watch?v={ID}",
        f"https://user@www.youtube.com/watch?v={ID}",
        f"https://youtu.be/{ID}/extra",
        f"ftp://youtu.be/{ID}",
        f"javascript:alert('{ID}')",
        f"https://vimeo.com/{ID}",
        f'https://youtu.be/{ID}"onload="x',
    ],
)
def test_other_youtube_values_are_rejected(value):
    with pytest.raises(ValueError, match="YouTube"):
        youtube_id(value)


def test_validate_ownership():
    for value in (None, "wanted", "owned"):
        validate_ownership("cd", value)
    with pytest.raises(ValueError, match="vinyl must be one of wanted, owned"):
        validate_ownership("vinyl", "borrowed")
