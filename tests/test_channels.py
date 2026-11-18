import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import config as C  # noqa: E402
from pipeline import select as S  # noqa: E402


def v(cid, score, mode="claude", status="scored", t="other"):
    return {"id": cid, "score": score, "score_mode": mode, "status": status, "moment_type": t}


class ProfileTests(unittest.TestCase):
    def test_channels_arg(self):
        self.assertEqual(C.channels_from_arg("new"), ["evelone2004", "zloyn"])
        self.assertEqual(C.channels_from_arg("strogo"), ["strogo"])
        self.assertEqual(C.channels_from_arg(""), ["strogo", "evelone2004", "zloyn"])
        with self.assertRaises(ValueError):
            C.channels_from_arg("nobody")

    def test_apply_profile_and_category(self):
        C.apply_profile("zloyn", "all")
        self.assertEqual((C.CHANNEL, C.CATEGORY, C.DISCOVERY_HOURS, C.MAX_PER_DAY), ("zloyn", "all", 24, 6))
        C.apply_profile("strogo")
        self.assertEqual((C.CHANNEL, C.CATEGORY, C.MAX_PER_DAY), ("strogo", "Grand Theft Auto VI", 7))

    def test_publish_texts(self):
        from pipeline.render import publish_texts
        ev = publish_texts({"channel": "evelone2004", "title": "ЭВЕЛОН РЕШИЛ ВСЁ", "window": [0, 30], "url": "u"})
        self.assertIn("twitch: evelone2004", ev["tiktok"])
        self.assertIn("#evelone", ev["tiktok"])
        self.assertIn("twitch: evelone2004", ev["shorts_title"])
        self.assertIn("#evelone", ev["shorts_title"])
        zl = publish_texts({"channel": "zloyn", "title": "", "title_fallback": "АРТУР И ПОЛИЦИЯ",
                            "window": [0, 30], "url": "u"})
        for k in ("tiktok", "shorts_description", "reels"):
            self.assertIn("twitch: ZLOYn", zl[k])
            self.assertIn("https://www.twitch.tv/zloyn", zl[k])
        self.assertIn("профиле", zl["upload_note"])


class TitleTests(unittest.TestCase):
    def test_title_with_streamer_name(self):
        from pipeline.render import build_title
        t, name = build_title({"channel": "zloyn", "title": None, "title_fallback": "В СТРИПКЛУБЕ"})
        self.assertEqual((t, name), ("ZLOYn В СТРИПКЛУБЕ ГТА 6", "ZLOYn"))
        t, _ = build_title({"channel": "strogo", "title_fallback": "ДРАКА"})
        self.assertEqual(t, "STROGO ДРАКА ГТА 6")
        t, _ = build_title({"channel": "evelone2004", "title_fallback": "ЭВЕЛОН УГНАЛ ТАНК В GTA 6"})
        self.assertEqual(t, "EVELONE УГНАЛ ТАНК В GTA 6")
        t, _ = build_title({"channel": "zloyn", "title_fallback": ""})
        self.assertEqual(t, "ZLOYn В ГТА 6")


class WindowTests(unittest.TestCase):
    def test_short_complete_moment_stays_short(self):
        sp = [1] * 18 + [0] * 4
        s, e = S.choose_window(sp, [0.5] * 22, [0.3] * 22, 22.0)
        self.assertEqual(s, 0.0)
        self.assertLessEqual(e, 22.0)
        self.assertGreaterEqual(e, 18.0)

    def test_long_clip_window_30_40_and_tail_trim(self):
        n = 60
        sp = [0] * 10 + [1] * 30 + [0] * 20          # реплики 10..40
        cam = [0.1] * 35 + [1.0] * 5 + [0.0] * 20     # реакция 35..40
        game = [0.2] * n
        s, e = S.choose_window(sp, cam, game, float(n))
        self.assertGreaterEqual(e - s, 30.0 - 1e-6)
        self.assertLessEqual(e - s, 40.0 + 1e-6)
        self.assertLessEqual(s, 12)
        self.assertGreaterEqual(e, 40)                # реакция сохранена

    def test_popularity_drives_selection(self):
        # популярный клип (просмотры + много зрителей клипнули момент) выше тихого непопулярного
        sig = {"speech": 0.5, "reaction": 0.5, "action": 0.5, "context": 1.0}
        popular = {"vel_pct": 0.95, "abs_pct": 0.9, "cluster": 1.0, "fresh": 0.5}
        unpopular = {"vel_pct": 0.1, "abs_pct": 0.1, "cluster": 0.0, "fresh": 0.5}
        rich = {"speech": 1.0, "reaction": 1.0, "action": 1.0, "context": 1.0}
        self.assertGreater(S.meta_total(popular, sig), S.meta_total(unpopular, rich) + 40)
        # при равной популярности содержание — дополнительный критерий; громкости в формуле нет
        self.assertGreater(S.meta_total(unpopular, rich), S.meta_total(unpopular, {"speech": 0, "reaction": 0,
                                                                                   "action": 0, "context": 0}))

    def test_reactions_count_unique_viewers(self):
        from datetime import timedelta
        now = datetime(2026, 11, 18, 14, 0, tzinfo=timezone.utc)
        mk = lambda cid, who, off: {"id": cid, "created_at": S.iso(now - timedelta(minutes=30)), "views": 5,
                                    "video_id": "v", "vod_offset": off, "duration": 30, "creator": who}
        clips = {"a": mk("a", "u1", 100), "b": mk("b", "u1", 105), "c": mk("c", "u2", 110)}
        p = S.prelim_scores(clips, S.group_clips(clips), now)
        self.assertEqual(p["a"]["cluster_size"], 2)   # u1 дважды — одна реакция

    def test_reaction_peak(self):
        cam = [0.1] * 10 + [0.9] + [0.1] * 10
        self.assertEqual(S.reaction_peak(cam, (0, 21)), 10.0)
        self.assertIsNone(S.reaction_peak([0.5] * 21, (0, 21)))


class DayLimitTests(unittest.TestCase):
    def test_pace_spreads_through_day(self):
        early = datetime(2026, 11, 17, 21, 0, tzinfo=timezone.utc)   # 01:00 Ереван
        late = datetime(2026, 11, 18, 15, 0, tzinfo=timezone.utc)    # 19:00 Ереван
        self.assertEqual(S.render_pace(early), 1)
        self.assertGreater(S.render_pace(late), S.render_pace(early))
        day = [v(f"c{i}", 95 - i) for i in range(6)]
        out = S.decide_day(day, finalize=False, max_per_day=6, pace=S.render_pace(early))
        self.assertEqual(sum(1 for x in out.values() if x == "render_queued"), 1)

    def test_new_channel_max_six(self):
        base = [v(f"d{i}", 88, status="rendered") for i in range(6)]
        out = S.decide_day(base + [v("y", 95)], True, max_per_day=6)
        self.assertNotEqual(out.get("y"), "render_queued")


if __name__ == "__main__":
    unittest.main()
