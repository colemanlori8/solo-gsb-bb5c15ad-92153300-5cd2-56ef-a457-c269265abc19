"""Core subtitle segmentation logic.

Pure Python (no web dependencies) so it can be unit-tested in isolation.

Given a time-ordered, non-overlapping list of words, find a segmentation
into subtitles that:

* covers every word exactly once and keeps the original order;
* gives every subtitle at most ``max_chars`` characters and a duration
  within ``[min_duration_ms, max_duration_ms]`` (hard constraints);
* minimises the total cost::

      sum over subtitles of (duration_s - target_s) ** 2
      + 4 * (number of crossed natural boundaries)

  where a *natural boundary* sits between two consecutive words when the
  first ends with sentence-ending punctuation or the gap between them is
  at least ``pause_threshold_ms``.  A boundary is "crossed" when it falls
  inside a subtitle rather than on its edge.
* ties (equal total cost) are broken in favour of fewer subtitles.

A classic O(n^2) dynamic program over word positions solves this exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from .config import CROSSED_BOUNDARY_PENALTY

SENTENCE_ENDING_CHARS = frozenset(".!?。！？…")
# Closing quotes / brackets that may trail the actual punctuation mark.
TRAILING_CLOSERS = frozenset("\"'”’»』」）)]】》〉")


class WordLike(Protocol):
    text: str
    start_ms: int
    end_ms: int


class SegmentationError(Exception):
    """Raised when the input cannot be segmented legally (mapped to HTTP 422)."""


@dataclass
class SegmentResult:
    index: int
    text: str
    start_ms: int
    end_ms: int
    duration_ms: int
    word_start: int  # index of first word, inclusive
    word_end: int  # index of last word, inclusive
    crossed_boundaries: int


@dataclass
class SegmentationResult:
    segments: list[SegmentResult]
    total_cost: float


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x4E00 <= cp <= 0x9FFF  # CJK unified ideographs
        or 0x3400 <= cp <= 0x4DBF  # CJK extension A
        or 0x3040 <= cp <= 0x30FF  # hiragana / katakana
        or 0xAC00 <= cp <= 0xD7AF  # hangul syllables
        or 0x3000 <= cp <= 0x303F  # CJK symbols and punctuation
        or 0xFF00 <= cp <= 0xFFEF  # fullwidth forms
    )


def _needs_space(prev: str, nxt: str) -> bool:
    """Heuristic: no space between tokens when either side is CJK."""
    if not prev or not nxt:
        return False
    return not (_is_cjk(prev[-1]) or _is_cjk(nxt[0]))


def join_word_texts(texts: Sequence[str]) -> str:
    parts: list[str] = []
    for i, text in enumerate(texts):
        if i and _needs_space(texts[i - 1], text):
            parts.append(" ")
        parts.append(text)
    return "".join(parts)


def ends_with_sentence_punctuation(text: str) -> bool:
    stripped = text.strip().rstrip("".join(TRAILING_CLOSERS))
    return bool(stripped) and stripped[-1] in SENTENCE_ENDING_CHARS


def segment_words(
    words: Sequence[WordLike],
    *,
    max_chars: int,
    min_duration_ms: int,
    max_duration_ms: int,
    target_duration_ms: int,
    pause_threshold_ms: int,
) -> SegmentationResult:
    n = len(words)
    if n == 0:
        raise SegmentationError("words must contain at least one word")

    # ---- input validation -------------------------------------------------
    for idx, w in enumerate(words):
        if not w.text or not w.text.strip():
            raise SegmentationError(f"word {idx} has empty text")
        if w.end_ms <= w.start_ms:
            raise SegmentationError(
                f"word {idx} ({w.text!r}) must satisfy end_ms > start_ms "
                f"(got {w.start_ms}..{w.end_ms})"
            )
        if len(w.text) > max_chars:
            raise SegmentationError(
                f"word {idx} ({w.text!r}) is {len(w.text)} characters and can "
                f"never fit within max_chars={max_chars}"
            )
        word_dur = w.end_ms - w.start_ms
        if word_dur > max_duration_ms:
            raise SegmentationError(
                f"word {idx} ({w.text!r}) lasts {word_dur} ms and can never "
                f"fit within max_duration_ms={max_duration_ms}"
            )
    for i in range(1, n):
        if words[i].start_ms < words[i - 1].end_ms:
            raise SegmentationError(
                f"words must be sorted by time and non-overlapping: word {i} "
                f"starts at {words[i].start_ms} ms but word {i - 1} ends at "
                f"{words[i - 1].end_ms} ms"
            )

    # ---- natural break points between consecutive words -------------------
    natural = [False] * max(n - 1, 0)
    for k in range(n - 1):
        gap = words[k + 1].start_ms - words[k].end_ms
        natural[k] = (
            gap >= pause_threshold_ms
            or ends_with_sentence_punctuation(words[k].text)
        )
    # nat_prefix[k] = number of natural boundaries among indices 0..k-1, so
    # the count crossed by a segment spanning words i..j is prefix[j]-prefix[i].
    nat_prefix = [0] * n
    for k in range(n - 1):
        nat_prefix[k + 1] = nat_prefix[k] + (1 if natural[k] else 0)

    space_before = [0] * n
    for k in range(1, n):
        space_before[k] = 1 if _needs_space(words[k - 1].text, words[k].text) else 0

    # ---- dynamic programming ----------------------------------------------
    INF = float("inf")
    EPS = 1e-9
    dp_cost = [INF] * (n + 1)
    dp_count = [0] * (n + 1)
    dp_cost[n] = 0.0
    nxt = [-1] * (n + 1)  # nxt[i] = inclusive end index of the segment starting at i
    target_s = target_duration_ms / 1000.0

    for i in range(n - 1, -1, -1):
        chars = 0
        for j in range(i, n):
            chars += len(words[j].text) + (space_before[j] if j > i else 0)
            if chars > max_chars:
                break  # only grows as j grows
            dur_ms = words[j].end_ms - words[i].start_ms
            if dur_ms > max_duration_ms:
                break  # only grows as j grows
            if dur_ms < min_duration_ms or dp_cost[j + 1] == INF:
                continue
            crossed = nat_prefix[j] - nat_prefix[i]
            dur_s = dur_ms / 1000.0
            cost = (
                (dur_s - target_s) ** 2
                + CROSSED_BOUNDARY_PENALTY * crossed
                + dp_cost[j + 1]
            )
            count = 1 + dp_count[j + 1]
            if cost < dp_cost[i] - EPS or (
                abs(cost - dp_cost[i]) <= EPS and count < dp_count[i]
            ):
                dp_cost[i] = cost
                dp_count[i] = count
                nxt[i] = j

    if dp_cost[0] == INF:
        raise SegmentationError(
            "no legal segmentation covers all words under the given constraints "
            f"(max_chars={max_chars}, min_duration_ms={min_duration_ms}, "
            f"max_duration_ms={max_duration_ms}); every subtitle — including "
            "the last one — must satisfy the duration limits, so try relaxing "
            "min_duration_ms or the other limits"
        )

    # ---- reconstruct the optimal path --------------------------------------
    segments: list[SegmentResult] = []
    i = 0
    while i < n:
        j = nxt[i]
        segments.append(
            SegmentResult(
                index=len(segments),
                text=join_word_texts([w.text for w in words[i : j + 1]]),
                start_ms=words[i].start_ms,
                end_ms=words[j].end_ms,
                duration_ms=words[j].end_ms - words[i].start_ms,
                word_start=i,
                word_end=j,
                crossed_boundaries=nat_prefix[j] - nat_prefix[i],
            )
        )
        i = j + 1

    return SegmentationResult(segments=segments, total_cost=dp_cost[0])
