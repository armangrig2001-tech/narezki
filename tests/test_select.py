import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import select as S  # noqa: E402

NOW = datetime(2026, 11, 18, 14, 0, tzinfo=timezone.utc)  # 18:00 Ереван


def clip(cid, minutes_ago, views=1, video=None, offset=None, dur=30):
    return {"id": cid, "created_at": S.iso(NOW - timedelta(minutes=minutes_ago)), "views": views,
            "video_id": video, "vod_offset": offset, "duration": dur, "views_history": []}


class GroupTests(unittest.TestCase):
    def test_vod_overlap_groups_same_event(self):
        c = {"a": clip("a", 10, video="v1", offset=100, dur=30),
             "b": clip("b", 9, video="v1", offset=120, dur=30),
             "c": clip("c", 8, video="v1", offset=400, dur=30)}
        g = S.group_clips(c)
        self.assertEqual(g["a"], g["b"])
        self.assertNotEqual(g["a"], g["c"])

    def test_proximity_without_vod_needs_check(self):
        c = {"a": clip("a", 10), "b": clip("b", 9), "c": clip("c", 100)}
        pairs = S.proximity_pairs(c)
        self.assertEqual(pairs, [("a", "b")] if ("a", "b") in pairs else [("b", "a")])
        self.assertEqual(S.proximity_pairs(c, ["a|b"]), [])
        g = S.group_clips(c, [["a", "b"]])
        self.assertEqual(g["a"], g["b"])

    def test_representative_prefers_longest_then_latest_end(self):
        c = {"a": clip("a", 10, video="v", offset=100, dur=30),
             "b": clip("b", 9, video="v", offset=110, dur=45)}
        self.assertEqual(S.representative(["a", "b"], c), "b")


class PrelimTests(unittest.TestCase):
    def test_neutral_when_all_one_view(self):
        c = {k: clip(k, m, views=1) for k, m in (("a", 30), ("b", 200), ("c", 600))}
        p = S.prelim_scores(c, S.group_clips(c), NOW)
        self.assertTrue(all(v["vel_pct"] == 0.5 and v["neutral_views"] for v in p.values()))

    def test_velocity_vs_same_age(self):
        c = {"a": clip("a", 30, 50), "b": clip("b", 35, 5), "c": clip("c", 40, 6),
             "d": clip("d", 20, 4)}
        p = S.prelim_scores(c, S.group_clips(c), NOW)
        self.assertGreater(p["a"]["vel_pct"], p["b"]["vel_pct"])
        self.assertGreaterEqual(p["a"]["views_points"], p["b"]["views_points"])

    def test_cluster_size_counts(self):
        c = {"a": clip("a", 10, video="v", offset=0), "b": clip("b", 9, video="v", offset=5),
             "c": clip("c", 9, video="v", offset=10), "d": clip("d", 300)}
        p = S.prelim_scores(c, S.group_clips(c), NOW)
        self.assertEqual(p["a"]["cluster_size"], 3)
        self.assertGreater(p["a"]["prelim"], p["d"]["prelim"])


class BudgetTests(unittest.TestCase):
    def test_allowance_grows_through_day(self):
        morning = datetime(2026, 11, 17, 21, 0, tzinfo=timezone.utc)  # 01:00 Ереван
        evening = datetime(2026, 11, 18, 18, 0, tzinfo=timezone.utc)  # 22:00 Ереван
        self.assertLess(S.eval_allowance(0, morning), S.eval_allowance(0, evening))
        self.assertLessEqual(S.eval_allowance(0, evening), 24)
        self.assertEqual(S.eval_allowance(24, evening), 0)

    def test_fresh_slots(self):
        c = {"old1": clip("old1", 600, 100), "old2": clip("old2", 500, 90), "new": clip("new", 20, 1)}
        p = S.prelim_scores(c, S.group_clips(c), NOW)
        self.assertIn("new", S.pick_for_eval(list(c), p, 2))


def v(cid, score, mode="claude", status="scored", t="other"):
    return {"id": cid, "score": score, "score_mode": mode, "status": status, "moment_type": t}


class DayTests(unittest.TestCase):
    def test_live_85_immediate_keep_two_late_slots(self):
        day = [v(f"c{i}", 90 - i) for i in range(6)] + [v("r", 80)]
        out = S.decide_day(day, finalize=False)
        queued = [k for k, s in out.items() if s == "render_queued"]
        self.assertEqual(len(queued), 4)            # 6 - 2 поздних места
        self.assertEqual(out["r"], "reserve")

    def test_finalize_fills_from_reserve_to_six(self):
        day = [v("a", 90, status="rendered"), v("b", 86, status="rendered"),
               v("r1", 80), v("r2", 78), v("r3", 77), v("r4", 76), v("low", 60)]
        out = S.decide_day(day, finalize=True)
        self.assertEqual(sorted(k for k, s in out.items() if s == "render_queued"), ["r1", "r2", "r3", "r4"])
        self.assertEqual(out["low"], "rejected")

    def test_seventh_only_if_85(self):
        base = [v(f"d{i}", 88, status="rendered") for i in range(6)]
        self.assertEqual(S.decide_day(base + [v("x", 84)], True).get("x"), "reserve")
        self.assertEqual(S.decide_day(base + [v("y", 86)], True).get("y"), "render_queued")
        eight = base + [v("y", 86), v("z", 87)]
        self.assertEqual(sum(1 for s in S.decide_day(eight, True).values() if s == "render_queued"), 1)

    def test_diversity_on_close_scores(self):
        day = [v("f1", 90, status="rendered", t="fight"), v("f2", 88, t="fight"), v("c1", 87, t="crash")]
        out = S.decide_day(day, finalize=False)
        q = [k for k, s in out.items() if s == "render_queued"]
        self.assertEqual(q[0] if len(q) == 1 else sorted(q), "c1" if len(q) == 1 else sorted(q))
        self.assertIn("c1", q)

    def test_fewer_than_six_if_weak(self):
        out = S.decide_day([v("a", 70), v("b", 60)], finalize=True)
        self.assertFalse([k for k, s in out.items() if s == "render_queued"])

    def test_meta_mode_no_claude_thresholds(self):
        day = [v("m1", 72, mode="meta"), v("m2", 50, mode="meta"), v("m3", 30, mode="meta")]
        live = S.decide_day(day, finalize=False)
        self.assertEqual(live["m1"], "render_queued")
        fin = S.decide_day([v("m2", 50, mode="meta"), v("m3", 30, mode="meta")], finalize=True)
        self.assertEqual(fin["m2"], "render_queued")
        self.assertEqual(fin["m3"], "reserve")    # не «отклонён»: пороги Claude к метаданным не применяются


class WindowTests(unittest.TestCase):
    def test_fit_window(self):
        self.assertEqual(S.fit_window(10, 20, 60), (0.0, 30.0))
        self.assertEqual(S.fit_window(20, 30, 60), (8.0, 38.0))
        self.assertEqual(S.fit_window(0, 60, 60), (20.0, 60.0))
        self.assertEqual(S.fit_window(3, 10, 25), (0.0, 25.0))
        s, e = S.fit_window(50, 58, 60)
        self.assertAlmostEqual(e - s, 30.0)
        self.assertLessEqual(e, 60)

    def test_finalize_rules(self):
        self.assertTrue(S.should_finalize({}, NOW))
        self.assertFalse(S.should_finalize({"live": False, "last_live_at": S.iso(NOW - timedelta(minutes=10))}, NOW))
        self.assertTrue(S.should_finalize({"live": False, "last_live_at": S.iso(NOW - timedelta(minutes=45))}, NOW))
        late = datetime(2026, 11, 18, 19, 30, tzinfo=timezone.utc)  # 23:30 Ереван
        self.assertFalse(S.should_finalize({"live": True}, NOW))
        self.assertTrue(S.should_finalize({"live": True}, late))


if __name__ == "__main__":
    unittest.main()
