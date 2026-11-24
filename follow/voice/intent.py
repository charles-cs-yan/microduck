"""Loose matching from a recognised English sentence to a show command. Pure logic.

A full recogniser writes what it heard ("LET PARTY", "LETS GO FOLLOW ME", "SAY HELLO EVERYONE");
the command is decided by a few distinctive words, so an accent that bends one word, or extra
words around it, still lands. Order matters: the more specific intent wins.
"""

from __future__ import annotations

import re
from typing import Optional

# (label, any of these word patterns). Word stems allow small spelling drift from the recogniser.
RULES = (
    ("home", (r"\bhome\b", r"\bhom\b")),
    ("dance", (r"\bpart(y|ie|i)\b", r"\bpardy\b", r"\bshake\b", r"\bdance\b",
               r"\b(let'?s|lets|less|last|let) (body|potty|patty|pody)\b")),  # "Last body" = Let's party
    ("follow", (r"\bfollow", r"\bfollo\b")),
    ("greet", (r"\bhello\b", r"\bhallo\b", r"\bhelo\b")),
    ("gaze", (r"\blook (at )?me\b", r"\blooking at me\b")),
    ("sit", (r"\b(sit|set|sat|seat) (down|done|town)\b",)),
    ("stand", (r"\b(get|got|gets|gate) up\b", r"\bstand up\b")),
    ("crouch", (r"\b(lie|lay|lied|lying|laid|light|life) (down|done|town)\b",)),
    ("stop", (r"\bstop\b", r"\bstopp?ed\b", r"^ (top|stab|stup) $")),  # "top": the S swallowed, said alone
    ("letsgo", (r"\blet'?s go\b", r"\blets go\b", r"\blet go\b")),
)


def intent_of(text: str) -> Optional[str]:
    t = " " + re.sub(r"[^a-z' ]+", " ", text.lower()) + " "
    t = re.sub(r"\s+", " ", t)
    for label, patterns in RULES:
        if any(re.search(p, t) for p in patterns):
            return label
    return None
