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
    ("sit", (r"\b(sit|set|sat|seat|say|said) (down|done|town)\b",)),  # "Say down" heard for Sit down
    ("stand", (r"\b(get|got|gets|gate) up\b", r"\bstand up\b")),
    ("crouch", (r"\b(lie|lay|lied|lying|laid|light|life) (down|done|town)\b",)),
    ("stop", (r"\bstop\b", r"\bstopp?ed\b", r"^ (top|stab|stup) $")),  # "top": the S swallowed, said alone
    ("letsgo", (r"\blet'?s go\b", r"\blets go\b", r"\blet go\b")),
)


# Fallback for words the recogniser bends in ways no rule lists yet ("lass party", "say hallo"):
# a multi-word command phrase matches a run of the same number of heard words when their spelling
# is close enough. Single words are never fuzzy-matched — too many ordinary words sit near them.
FUZZY_PHRASES = (
    ("home", "lets go home"), ("dance", "lets party"), ("dance", "shake it"),
    ("follow", "follow me"), ("greet", "say hello"), ("gaze", "look at me"),
    ("sit", "sit down"), ("stand", "get up"), ("crouch", "lie down"),
)
FUZZY_MIN = 0.84  # SequenceMatcher ratio on the joined words; checked against real transcripts


def _fuzzy(words) -> Optional[str]:
    from difflib import SequenceMatcher
    best, best_label = 0.0, None
    for label, phrase in FUZZY_PHRASES:
        n = len(phrase.split())
        for i in range(len(words) - n + 1):
            chunk = " ".join(words[i:i + n])
            ratio = SequenceMatcher(None, chunk, phrase).ratio()
            if ratio > best:
                best, best_label = ratio, label
    return best_label if best >= FUZZY_MIN else None


def intent_of(text: str) -> Optional[str]:
    t = " " + re.sub(r"[^a-z' ]+", " ", text.lower()) + " "
    t = re.sub(r"\s+", " ", t)
    for label, patterns in RULES:
        if any(re.search(p, t) for p in patterns):
            return label
    return _fuzzy(t.replace("'", "").split())
