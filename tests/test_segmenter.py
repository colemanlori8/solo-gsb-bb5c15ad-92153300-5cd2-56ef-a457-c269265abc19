"""Unit tests for the pure segmentation logic.

Run with either:
    python3 -m unittest discover -s tests
    pytest
"""

from __future__ import annotations

import math
import unittest
from types import SimpleNamespace
from typing import Sequence

from app.segmenter import (
    SegmentationError,
    ends_with_sentence_punctuation,
    join_word_texts,
    segment_words,
)
from app.srt import format_timestamp, to_srt


def W(text: str, start_ms: int, end_ms: int) -> SimpleNamespace:
    return SimpleNamespace(text=text, start_ms=start_ms, end_ms=end_ms)


def _segment(texts: Sequence[str], dur_ms: int = 450, **kwargs):
    words = [W(t, i * dur_ms, (i + 1) * dur_ms) for i, t in enumerate(texts)]
    opts = dict(
        max_chars=100,
        min_duration_ms=0,
        max_duration_ms=100000,
        target_duration_ms=2000,
        pause_threshold_ms=1000,
    )
    opts.update(kwargs)
    return segment_words(words, **opts)


class TestPunctuationAndJoining(unittest.TestCase):
    def test_sentence_endings(self):
        self.assertTrue(ends_with_sentence_punctuation("Hello."))
        self.assertTrue(ends_with_sentence_punctuation("真的吗？"))
        self.assertTrue(ends_with_sentence_punctuation('"Wow!"'))
        self.assertFalse(ends_with_sentence_punctuation("hello"))
        self.assertFalse(ends_with_sentence_punctuation("word,"))

    def test_join_spaces(self):
        self.assertEqual(join_word_texts(["Hello", "world"]), "Hello world")
        self.assertEqual(join_word_texts(["你好", "世界"]), "你好世界")
        self.assertEqual(join_word_texts(["你好", "world"]), "你好world")
        self.assertEqual(join_word_texts(["hello", "世界"]), "hello世界")


class TestSegmentation(unittest.TestCase):
    def _check_coverage(self, result, n):
        self.assertEqual(result.segments[0].word_start, 0)
        self.assertEqual(result.segments[-1].word_end, n - 1)
        for a, b in zip(result.segments, result.segments[1:]):
            self.assertEqual(b.word_start, a.word_end + 1)
        # total cost must equal the sum of per-segment costs advertised below
        # (here we only assert it is a finite non-negative number)
        self.assertTrue(math.isfinite(result.total_cost))
        self.assertGreaterEqual(result.total_cost, 0.0)

    def test_covers_all_words_in_order_and_respects_max_chars(self):
        texts = ["a", "b", "c", "d", "e", "f"]
        result = _segment(texts, max_chars=3)
        for seg in result.segments:
            self.assertLessEqual(len(seg.text), 3)
        self._check_coverage(result, len(texts))
        self.assertEqual(
            "".join(seg.text.replace(" ", "") for seg in result.segments),
            "abcdef",
        )

    def test_respects_duration_limits(self):
        texts = [f"w{i}" for i in range(10)]
        result = _segment(
            texts,
            dur_ms=400,
            min_duration_ms=800,
            max_duration_ms=1600,
            target_duration_ms=1200,
        )
        for seg in result.segments:
            self.assertGreaterEqual(seg.duration_ms, 800)
            self.assertLessEqual(seg.duration_ms, 1600)
        self._check_coverage(result, len(texts))

    def test_sentence_punctuation_discourages_crossing(self):
        # Target duration would make the single 2 s segment optimal on
        # duration alone (cost 0); crossing the sentence boundary costs +4,
        # so splitting into two 1 s segments (cost 2) must win.
        words = [W("Hi.", 0, 1000), W("there", 1000, 2000)]
        result = segment_words(
            words,
            max_chars=100,
            min_duration_ms=0,
            max_duration_ms=100000,
            target_duration_ms=2000,
            pause_threshold_ms=1000,
        )
        self.assertEqual(len(result.segments), 2)
        self.assertAlmostEqual(result.total_cost, 2.0, places=9)

    def test_long_pause_discourages_crossing(self):
        # Two 1.0 s words with a 300 ms gap -> single subtitle spans 2.3 s.
        # Target 2.3 s makes one segment perfect but it crosses the pause
        # (+4). Splitting costs 2 * (1.0 - 2.3)^2 = 3.38, so split wins.
        words = [W("Hi", 0, 1000), W("there", 1300, 2300)]  # 300 ms gap
        result = segment_words(
            words,
            max_chars=100,
            min_duration_ms=0,
            max_duration_ms=100000,
            target_duration_ms=2300,
            pause_threshold_ms=200,
        )
        self.assertEqual(len(result.segments), 2)
        self.assertAlmostEqual(result.total_cost, 3.38, places=9)

    def test_penalty_charged_when_crossed(self):
        # Big max_chars / perfect one-segment duration but only one subtitle
        # possible: crossing cost must appear in total_cost.
        # min_duration_ms=1500 makes single-word (1 s) subtitles illegal, so
        # the only legal solution crosses the sentence boundary (+4).
        words = [W("Hi.", 0, 1000), W("there", 1000, 2000)]
        result = segment_words(
            words,
            max_chars=100,
            min_duration_ms=1500,
            max_duration_ms=100000,
            target_duration_ms=2000,
            pause_threshold_ms=1000,
        )
        self.assertEqual(len(result.segments), 1)
        self.assertAlmostEqual(result.total_cost, 4.0, places=9)
        self.assertEqual(result.segments[0].crossed_boundaries, 1)

    def test_tie_break_prefers_fewer_subtitles(self):
        # One segment of 1.5 s vs two segments of 1.0 s + 0.5 s, target 1.0 s:
        # both cost exactly 0.25. Fewer subtitles (1) must win.
        words = [W("a", 0, 1000), W("b", 1000, 1500)]
        result = segment_words(
            words,
            max_chars=100,
            min_duration_ms=0,
            max_duration_ms=100000,
            target_duration_ms=1000,
            pause_threshold_ms=1000,
        )
        self.assertEqual(len(result.segments), 1)
        self.assertAlmostEqual(result.total_cost, 0.25, places=9)

    def test_target_deviation_measured_in_seconds(self):
        words = [W("a", 0, 2000)]
        result = segment_words(
            words,
            max_chars=100,
            min_duration_ms=0,
            max_duration_ms=100000,
            target_duration_ms=3000,
            pause_threshold_ms=1000,
        )
        self.assertAlmostEqual(result.total_cost, 1.0, places=9)  # (2-3)^2

    def test_word_too_long_raises(self):
        with self.assertRaises(SegmentationError) as ctx:
            segment_words(
                [W("abcdef", 0, 100)],
                max_chars=3,
                min_duration_ms=0,
                max_duration_ms=100000,
                target_duration_ms=1000,
                pause_threshold_ms=1000,
            )
        self.assertIn("max_chars", str(ctx.exception))

    def test_word_exceeding_max_duration_raises(self):
        with self.assertRaises(SegmentationError) as ctx:
            segment_words(
                [W("a", 0, 9000)],
                max_chars=100,
                min_duration_ms=0,
                max_duration_ms=5000,
                target_duration_ms=3000,
                pause_threshold_ms=1000,
            )
        self.assertIn("max_duration_ms", str(ctx.exception))

    def test_overlapping_words_raise(self):
        with self.assertRaises(SegmentationError) as ctx:
            segment_words(
                [W("a", 0, 1000), W("b", 900, 1900)],
                max_chars=100,
                min_duration_ms=0,
                max_duration_ms=100000,
                target_duration_ms=1000,
                pause_threshold_ms=1000,
            )
        self.assertIn("non-overlapping", str(ctx.exception))

    def test_no_legal_segmentation_due_to_min_duration(self):
        with self.assertRaises(SegmentationError) as ctx:
            segment_words(
                [W("a", 0, 400)],
                max_chars=100,
                min_duration_ms=1000,
                max_duration_ms=5000,
                target_duration_ms=1000,
                pause_threshold_ms=1000,
            )
        self.assertIn("no legal segmentation", str(ctx.exception))

    def test_empty_words_raise(self):
        with self.assertRaises(SegmentationError):
            segment_words(
                [],
                max_chars=100,
                min_duration_ms=0,
                max_duration_ms=100000,
                target_duration_ms=1000,
                pause_threshold_ms=1000,
            )


class TestSRT(unittest.TestCase):
    def test_timestamp(self):
        self.assertEqual(format_timestamp(0), "00:00:00,000")
        self.assertEqual(format_timestamp(3_661_500), "01:01:01,500")

    def test_srt_document(self):
        result = _segment(["Hello", "world", "Bye"], dur_ms=500)
        srt = to_srt(result.segments)
        lines = srt.splitlines()
        self.assertEqual(lines[0], "1")
        self.assertIn(" --> ", lines[1])
        self.assertIn("Hello", srt)
        self.assertIn("Bye", srt)


if __name__ == "__main__":
    unittest.main()
