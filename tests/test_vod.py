import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import vod as V  # noqa: E402


class VodTests(unittest.TestCase):
    def test_parse_duration(self):
        self.assertEqual(V.parse_duration("3h2m10s"), 3 * 3600 + 130)
        self.assertEqual(V.parse_duration("45s"), 45)

    def test_pending_chunk_live_and_finished(self):
        v = {"duration": 4 * 3600}
        s, e = V.pending_chunk(v, live=True, state_vod={})
        self.assertEqual(s, 4 * 3600 - V.BACKFILL_SEC)            # не глубже 1,5 ч при первом знакомстве
        self.assertEqual(e - s, V.CHUNK_SEC)
        # во время эфира ждём, пока наберётся кусок
        self.assertIsNone(V.pending_chunk(v, True, {"analyzed_until": 4 * 3600 - 300}))
        # после эфира разбираем хвост
        self.assertIsNotNone(V.pending_chunk(v, False, {"analyzed_until": 4 * 3600 - 300}))
        self.assertIsNone(V.pending_chunk(v, False, {"analyzed_until": 4 * 3600}))

    def test_context_window_30_40(self):
        s, e = V.context_window(1000, 11)
        self.assertGreaterEqual(e - s, 30)
        self.assertLessEqual(e - s, 40)
        self.assertLess(s, 1000)
        self.assertGreater(e, 1011)                                 # реакция после момента
        s, e = V.context_window(3, 11)
        self.assertEqual(s, 0.0)

    def test_find_moments_on_peak(self):
        d = tempfile.mkdtemp()
        wav = os.path.join(d, "a.wav")
        # 10 мин: тихий фон, на 300-й секунде громкая реакция
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "anoisesrc=d=600:a=0.02:c=pink", "-f", "lavfi", "-i",
                        "sine=f=300:d=600", "-filter_complex",
                        "[1:a]volume='if(between(t,300,320),0.8,0.0)':eval=frame[s];[0:a][s]amix=2:normalize=0",
                        "-ar", "8000", wav], check=True)
        tr = {"segments": [{"start": 300, "end": 318, "text": "ВАУ! Офигеть, ты видел?! Ахаха",
                            "words": [{"start": 300 + i, "end": 300.5 + i, "word": "x"} for i in range(18)]}]}
        ms = V.find_moments(wav, tr, clip_offsets=[1305], base=1000)
        self.assertTrue(ms)
        self.assertTrue(1000 + 270 <= ms[0]["start"] <= 1000 + 310, ms[0])
        self.assertIn("здесь зрители делали клипы", ms[0]["reasons"])

    def test_moment_record_dedups_with_clip_by_vod(self):
        from pipeline import select as S
        rec = V.moment_record("zloyn", {"id": "v1", "created_at": "2026-11-18T10:00:00Z"},
                              {"start": 1290, "score": 2.0, "reasons": ["всплеск речи"]})
        clip = {"id": "c", "created_at": "2026-11-18T10:21:40Z", "video_id": "v1", "vod_offset": 1300,
                "duration": 20, "views": 10}
        g = S.group_clips({rec["id"]: rec, "c": clip})
        self.assertEqual(g[rec["id"]], g["c"])
        self.assertEqual(S.representative([rec["id"], "c"], {rec["id"]: rec, "c": clip}), "c")


if __name__ == "__main__":
    unittest.main()
