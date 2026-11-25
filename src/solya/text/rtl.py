"""RTL helpers: keep Latin/digit tokens from flipping inside Hebrew lines."""
from __future__ import annotations

import re

RLM = "‏"
_HEBREW = re.compile("[֐-׿]")
_TOKEN_AFTER_SPACE = re.compile(r"(?<= )(?=[0-9A-Za-z])")
_URL_LINE = re.compile(r"^\s*https?://\S+\s*$")


def rlm_tokens(line: str) -> str:
    """Insert RLM before each space-preceded Latin/digit token that follows Hebrew.

    Idempotent: an already-prefixed token starts with RLM, so it no longer matches.
    A line that consists solely of a URL is left untouched.
    """
    if _URL_LINE.match(line):
        return line
    first = _HEBREW.search(line)
    if first is None:
        return line
    head, tail = line[: first.end()], line[first.end():]
    return head + _TOKEN_AFTER_SPACE.sub(RLM, tail)


def rtl_text(text: str) -> str:
    return "\n".join(rlm_tokens(line) for line in text.split("\n"))
