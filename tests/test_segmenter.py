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


class TestLineLayout(unittest.TestCase):
    """Optional ``max_line_chars`` two-line layout behaviour."""

    def test_omitted_keeps_single_line_and_unchanged_cost(self):
        without = _segment(["Hello", "world", "bye"], dur_ms=500)
        wide = _segment(
            ["Hello", "world", "bye"], dur_ms=500, max_line_chars=1000
        )
        self.assertEqual(
            [s.text for s in without.segments],
            [s.text for s in wide.segments],
        )
        self.assertAlmostEqual(without.total_cost, wide.total_cost, places=9)
        for seg in wide.segments:
            self.assertNotIn("\n", seg.text)

    def test_single_line_fitting_has_zero_layout_cost(self):
        # One subtitle exactly matching its target duration; a width that
        # still fits the joined text must keep the total cost at zero.
        words = [W("ab", 0, 1000), W("cd", 1000, 2000)]
        result = segment_words(
            words,
            max_chars=100,
            min_duration_ms=0,
            max_duration_ms=100000,
            target_duration_ms=2000,
            pause_threshold_ms=1000,
            max_line_chars=5,  # "ab cd" is exactly 5
        )
        self.assertEqual(len(result.segments), 1)
        self.assertEqual(result.segments[0].text, "ab cd")
        self.assertAlmostEqual(result.total_cost, 0.0, places=9)

    def test_balanced_two_line_break_with_spaces(self):
        # Six 2-char words join to "aa bb cc dd ee ff" (17 chars). Width 9:
        # the only balanced legal break is after word 2 -> lines 8/8.
        words = [W(t, i * 400, (i + 1) * 400) for i, t in enumerate(
            ["aa", "bb", "cc", "dd", "ee", "ff"]
        )]
        result = segment_words(
            words,
            max_chars=100,
            min_duration_ms=0,
            max_duration_ms=100000,
            target_duration_ms=2400,
            pause_threshold_ms=1000,
            max_line_chars=9,
        )
        self.assertEqual(len(result.segments), 1)
        seg = result.segments[0]
        self.assertEqual(seg.text, "aa bb cc\ndd ee ff")
        line1, line2 = seg.text.split("\n")
        self.assertEqual(len(line1), 8)
        self.assertEqual(len(line2), 8)
        self.assertAlmostEqual(result.total_cost, 0.0, places=9)

    def test_line_balance_cost_added_to_total(self):
        # Forced single subtitle (min duration), width 3:
        # "a a" is 3 characters but > width ... it fits exactly (3 <= 3),
        # so instead use two words joining to 5 chars; width 2 forces the
        # break "a"/"a" with diff 0, duration 2 s vs target 1 s -> cost 1.
        words = [W("a", 0, 1000), W("a", 1000, 2000)]
        result = segment_words(
            words,
            max_chars=100,
            min_duration_ms=1500,
            max_duration_ms=100000,
            target_duration_ms=1000,
            pause_threshold_ms=1000,
            max_line_chars=2,
        )
        self.assertEqual(result.segments[0].text, "a\na")
        self.assertAlmostEqual(result.total_cost, 1.0, places=9)

    def test_unbalanced_break_pays_squared_difference(self):
        # CJK words (no joining spaces): "今天天" + "气好" = 5 chars, width 3.
        # The only legal break yields lines 3/2 -> (3-2)^2 / 3^2 = 1/9 on
        # top of the zero duration cost.
        words = [W("今天天", 0, 1000), W("气好", 1000, 2000)]
        result = segment_words(
            words,
            max_chars=100,
            min_duration_ms=0,
            max_duration_ms=100000,
            target_duration_ms=2000,
            pause_threshold_ms=1000,
            max_line_chars=3,
        )
        self.assertEqual(result.segments[0].text, "今天天\n气好")
        self.assertAlmostEqual(result.total_cost, 1 / 9, places=9)

    def test_tie_prefers_earlier_break_point(self):
        # "ab cd ef": width 5 allows breaks after word 0 (2/5) and after
        # word 1 (5/2); both cost (3/5)^2, so the earlier break wins.
        words = [W(t, i * 400, (i + 1) * 400) for i, t in enumerate(
            ["ab", "cd", "ef"]
        )]
        result = segment_words(
            words,
            max_chars=100,
            min_duration_ms=0,
            max_duration_ms=100000,
            target_duration_ms=1200,
            pause_threshold_ms=1000,
            max_line_chars=5,
        )
        self.assertEqual(result.segments[0].text, "ab\ncd ef")

    def test_cjk_chain_tie_prefers_earlier_break(self):
        # Three 2-char CJK words, width 4: breaks after word 0 (2/4) and
        # word 1 (4/2) both have diff 2; the earlier one wins.
        words = [W(t, i * 400, (i + 1) * 400) for i, t in enumerate(
            ["你好", "世界", "再见"]
        )]
        result = segment_words(
            words,
            max_chars=100,
            min_duration_ms=0,
            max_duration_ms=100000,
            target_duration_ms=1200,
            pause_threshold_ms=1000,
            max_line_chars=4,
        )
        self.assertEqual(result.segments[0].text, "你好\n世界再见")

    def test_breaks_only_between_words_and_lines_within_width(self):
        texts = ["one", "two", "three", "four", "five", "six"]
        result = _segment(texts, dur_ms=400, max_line_chars=10)
        # Reconstructed words, in order, must cover every input word once.
        seen = []
        for seg in result.segments:
            lines = seg.text.split("\n")
            self.assertLessEqual(len(lines), 2)
            for line in lines:
                self.assertLessEqual(len(line), 10)
            seen.extend(seg.text.replace("\n", " ").split(" "))
        self.assertEqual(seen, texts)

    def test_no_legal_layout_raises(self):
        # Three words inside a short span: min_duration forces each subtitle
        # to span at least two words, but width 1 cannot hold even a two-word
        # "a a" line, so no candidate layout is legal.
        with self.assertRaises(SegmentationError) as ctx:
            segment_words(
                [W("a", 0, 300), W("b", 300, 600), W("c", 600, 900)],
                max_chars=100,
                min_duration_ms=500,
                max_duration_ms=100000,
                target_duration_ms=500,
                pause_threshold_ms=1000,
                max_line_chars=1,
            )
        self.assertIn("no legal segmentation", str(ctx.exception))
        self.assertIn("max_line_chars=1", str(ctx.exception))

    def test_word_wider_than_line_raises(self):
        with self.assertRaises(SegmentationError) as ctx:
            segment_words(
                [W("abcdef", 0, 100)],
                max_chars=100,
                min_duration_ms=0,
                max_duration_ms=100000,
                target_duration_ms=1000,
                pause_threshold_ms=1000,
                max_line_chars=3,
            )
        self.assertIn("max_line_chars", str(ctx.exception))

    def test_srt_emits_line_break(self):
        words = [W("a", 0, 1000), W("a", 1000, 2000)]
        result = segment_words(
            words,
            max_chars=100,
            min_duration_ms=1500,
            max_duration_ms=100000,
            target_duration_ms=2000,
            pause_threshold_ms=1000,
            max_line_chars=2,
        )
        srt = to_srt(result.segments)
        self.assertEqual(
            srt, "1\n00:00:00,000 --> 00:00:02,000\na\na\n"
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
