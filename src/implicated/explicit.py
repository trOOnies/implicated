"""Spot lyrics that are obviously explicit, so a song gets marked as such when its words arrive.

It aims for precision over recall: only words that are swearing in every context count, and
words with an innocent meaning (``ass``, ``dick``, ``cock``, ``hell``, ``damn``, ``bastard``,
``fag`` as in a cigarette, ``pussy`` as in a cat) are left out. A song this misses can still be
marked by hand.
"""

from __future__ import annotations

import re

# Whole words, ignoring case. "fuck" is swearing wherever it appears ("motherfucker",
# "clusterfuck"), so it may sit inside a longer word; the others must stand alone.
_PATTERN = re.compile(
    r"""
    \w*fuck\w*
    | \b(?:
        (?:bull|horse|dip|chicken|dog)?shit(?:s|ty|tier|tiest|ting|ted|head|heads|hole|holes)?
        | cunts?
        | bitch(?:es|ing|y)?
        | nigg(?:a|as|az|er|ers)
        | faggots?
        | whores?
        | sluts?
        | assholes?
        | cocksuckers?
        | dickheads?
        | twats?
        | wankers?
    )\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def curse_words(text: str | None) -> list[str]:
    """The swear words in ``text``, lowercased, each once, in the order they first appear."""
    found: dict[str, None] = {}
    for match in _PATTERN.finditer(text or ""):
        found.setdefault(match.group(0).lower(), None)
    return list(found)


def is_explicit(text: str | None) -> bool:
    """Whether ``text`` has any word that makes a song obviously explicit."""
    return _PATTERN.search(text or "") is not None
