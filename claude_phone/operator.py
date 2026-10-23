"""Operator (dial 0): match a spoken request like "the backend on the AGS branch" to a live session."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from .registry import Session

# Spoken word -> tokens it can stand for in repo and branch names.
SYNONYMS = {
    "backend": {"be", "backend", "api", "server"},
    "back": {"be", "backend"},
    "frontend": {"fe", "frontend", "web", "ui", "app"},
    "front": {"fe", "frontend"},
    "website": {"site", "web"},
    "site": {"site"},
    "main": {"main", "master"},
    "master": {"master", "main"},
    "phone": {"phone"},
}
FILLER = {"the", "a", "an", "on", "in", "to", "for", "of", "please", "branch", "repo", "session", "put", "me",
          "through", "can", "i", "speak", "talk", "with", "get", "connect", "and", "one", "um", "uh", "like",
          "id", "i'd", "would", "want", "just", "that", "this", "is", "it", "be", "there", "hi", "hello"}
DIGITS = {"zero": "0", "oh": "0", "o": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
          "six": "6", "seven": "7", "eight": "8", "nine": "9"}


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _name_tokens(s: Session) -> set[str]:
    out: set[str] = set()
    for part in (s.repo, s.branch, s.worktree):
        if part:
            toks = _tokens(part)
            out.update(toks)
            out.add("".join(toks))  # "lemon-stand" also as "lemonstand"
    return out


def spoken_extension(text: str) -> int | None:
    """'three oh one' or '301' -> 301."""
    m = re.search(r"\b([1-9]\d\d)\b", text)
    if m:
        return int(m.group(1))
    digits = "".join(DIGITS.get(t, " ") for t in _tokens(text)).split()
    for run in digits:
        if len(run) == 3:
            return int(run)
    return None


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def score(text: str, s: Session) -> float:
    words = [w for w in _tokens(text) if w not in FILLER]
    names = _name_tokens(s)
    total = 0.0
    for i, w in enumerate(words):
        if w == "back" and words[i + 1:i + 2] != ["end"]:
            continue  # "back end" is the backend; "turn them back on" isn't
        cands = SYNONYMS.get(w, {w})
        best = 0.0
        for c in cands:
            for n in names:
                if c == n:
                    best = max(best, 1.0)
                elif len(c) >= 3 and len(n) >= 3:
                    best = max(best, _similar(c, n) if _similar(c, n) >= 0.75 else 0.0)
        # Two spoken words that make one name: "lemon stand" -> "lemonstand", "a g s" -> "ags".
        if i + 1 < len(words) and words[i] + words[i + 1] in names:
            best = max(best, 1.0)
        total += best
    return total


def match(text: str, sessions: list[Session]) -> tuple[Session | None, list[Session]]:
    """(confident match, candidates). Candidates are the tied best when it isn't confident."""
    ext = spoken_extension(text)
    if ext is not None:
        hit = next((s for s in sessions if s.ext == ext), None)
        if hit:
            return hit, [hit]
    # Spelled-out letters ("a g s") joined, so they can match a short branch name.
    letters = re.sub(r"\b([a-z])\s+(?=[a-z]\b)", r"\1", text.lower())
    scored = sorted(((max(score(text, s), score(letters, s)), s) for s in sessions), key=lambda x: -x[0])
    if not scored or scored[0][0] < 0.75:
        return None, []
    top = scored[0][0]
    tied = [s for sc, s in scored if sc >= top - 0.25]
    return (tied[0], tied) if len(tied) == 1 else (None, tied)
