"""Lossless model projection of repeated current-state prose; SQL stays unchanged."""

import re
from collections import Counter
from functools import lru_cache

from pipmind.contracts import loads
from pipmind.shadow.common import encoded

FIELDS = ("views", "waiting_for")


def factor_working(working):
    if not isinstance(working, dict) or any(
        not isinstance(working.get(field), list)
        or len(working[field]) > 64
        or any(not isinstance(value, str) for value in working[field])
        for field in FIELDS
    ):
        return working
    text = encoded(working)
    if not 512 <= len(text.encode()) <= 32768:
        return working  # Oversized mandatory state still reaches the refusal guard.
    return loads(_factored(text))


@lru_cache(maxsize=8)
def _factored(text):
    working = loads(text)
    parts = {field: [[s] for s in working[field]] for field in FIELDS}
    eligible = {field: [s == " ".join(s.split()) for s in working[field]] for field in FIELDS}
    counts = Counter()
    for field in FIELDS:
        for value, ok in zip(working[field], eligible[field], strict=True):
            if not ok:
                continue
            words = value.split()
            for width in (2, 3, 4, 6, 10, 14, 20):
                for i in range(len(words) - width + 1):
                    phrase = " ".join(words[i : i + width])
                    if 12 <= len(phrase.encode()) <= 240:
                        counts[phrase] += 1
    candidates = [p for p, n in counts.items() if n >= 2]
    phrases = []
    for _ in range(48):
        literals = [
            p
            for field, rows in parts.items()
            for row, ok in zip(rows, eligible[field], strict=True)
            if ok
            for p in row
            if isinstance(p, str)
        ]
        scored = []
        for phrase in candidates:
            count = sum((" " + p + " ").count(" " + phrase + " ") for p in literals)
            score = count * (len(encoded(phrase)) - len(str(len(phrases))) - 4)
            score -= len(encoded(phrase)) + 1
            if count >= 2 and score > 0:
                scored.append((score, phrase))
        if not scored:
            break
        _, phrase = max(scored)
        index = len(phrases)
        phrases.append(phrase)
        candidates.remove(phrase)
        pattern = re.compile(r"(?<!\S)" + re.escape(phrase) + r"(?!\S)")
        for field, rows in parts.items():
            for i, row in enumerate(rows):
                if not eligible[field][i]:
                    continue
                pieces = []
                for piece in row:
                    if not isinstance(piece, str):
                        pieces.append(piece)
                        continue
                    for j, chunk in enumerate(pattern.split(piece)):
                        if j:
                            pieces.append(index)
                        if chunk.strip():
                            pieces.append(chunk.strip())
                rows[i] = pieces
    result = {**working, "encoding": "working-phrases-v1", "phrases": phrases}
    for field, rows in parts.items():
        result[field] = [r[0] if len(r) == 1 and isinstance(r[0], str) else r for r in rows]
    candidate = encoded(result)
    return candidate if len(candidate.encode()) < len(text.encode()) else text
