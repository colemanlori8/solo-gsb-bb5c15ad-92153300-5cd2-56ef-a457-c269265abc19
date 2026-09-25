"""API integration tests (require fastapi + httpx).

Run with either:
    python3 -m unittest discover -s tests
    pytest
"""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app.main import app


def _word(text, start_ms, end_ms):
    return {"text": text, "start_ms": start_ms, "end_ms": end_ms}


BODY = {
    "words": [
        _word("Hello", 0, 500),
        _word("world.", 500, 1400),
        _word("How", 2000, 2400),
        _word("are", 2400, 2700),
        _word("you", 2700, 3200),
        _word("doing", 3200, 3800),
        _word("today?", 3800, 4600),
        _word("Fine", 5200, 5600),
        _word("thanks.", 5600, 6400),
    ],
    "max_chars": 42,
    "min_duration_ms": 1000,
    "max_duration_ms": 7000,
    "target_duration_ms": 3000,
    "pause_threshold_ms": 500,
}


class TestAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_health(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "ok"})

    def test_segment_json(self):
        r = self.client.post("/segment", json=BODY)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["segment_count"], 3)
        segs = data["segments"]
        self.assertEqual(segs[0]["start_ms"], 0)
        self.assertEqual(segs[-1]["end_ms"], 6400)
        # natural boundaries (punctuation or >=500 ms gaps) should land on
        # segment edges, not be crossed
        self.assertEqual(sum(s["crossed_boundaries"] for s in segs), 0)
        # every word covered once, in order
        self.assertEqual(segs[0]["word_start"], 0)
        for a, b in zip(segs, segs[1:]):
            self.assertEqual(b["word_start"], a["word_end"] + 1)
        self.assertEqual(segs[-1]["word_end"], len(BODY["words"]) - 1)
        for s in segs:
            self.assertLessEqual(len(s["text"]), 42)
            self.assertGreaterEqual(s["duration_ms"], 1000)
            self.assertLessEqual(s["duration_ms"], 7000)

    def test_segment_srt(self):
        r = self.client.post("/segment/srt", json=BODY)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.headers["content-type"].startswith("text/plain"))
        srt = r.text
        self.assertTrue(srt.startswith("1\n"))
        self.assertIn("00:00:00,000 --> 00:00:01,400", srt)
        self.assertIn("Hello world.", srt)
        self.assertIn("Fine thanks.", srt)

    def test_422_on_overlong_word(self):
        body = {**BODY, "words": [_word("x" * 50, 0, 500)]}
        r = self.client.post("/segment", json=body)
        self.assertEqual(r.status_code, 422)
        self.assertIn("max_chars", r.json()["detail"])

    def test_422_on_overlapping_words(self):
        body = {
            **BODY,
            "words": [_word("a", 0, 500), _word("b", 400, 900)],
        }
        r = self.client.post("/segment", json=body)
        self.assertEqual(r.status_code, 422)
        self.assertIn("non-overlapping", r.json()["detail"])

    def test_422_on_impossible_min_duration(self):
        body = {
            **BODY,
            "words": [_word("a", 0, 300)],
            "min_duration_ms": 1000,
        }
        r = self.client.post("/segment", json=body)
        self.assertEqual(r.status_code, 422)
        self.assertIn("no legal segmentation", r.json()["detail"])

    def test_422_on_schema_violation(self):
        body = {**BODY, "words": [_word("a", 500, 100)]}  # end < start
        r = self.client.post("/segment", json=body)
        self.assertEqual(r.status_code, 422)

    def test_defaults_applied_when_parameters_omitted(self):
        r = self.client.post(
            "/segment",
            json={"words": BODY["words"]},
        )
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main()
