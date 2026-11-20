import json
import unittest
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import narezchik
import process_twitch
from find_twitch_clips import load_dotenv


class FetchVodsTests(unittest.TestCase):
    @patch("process_twitch.helix")
    def test_fetch_vods_filters_time_window_and_category(self, helix):
        helix.side_effect = [
            {
                "data": [
                    {"id": "12", "user_id": "user", "created_at": "2026-11-18T12:00:00Z"},
                    {"id": "11", "user_id": "other", "created_at": "2026-11-18T11:00:00Z"},
                ],
                "pagination": {"cursor": "next"},
            },
            {
                "data": [
                    {"id": "10", "user_id": "user", "created_at": "2026-11-18T10:00:00Z"},
                    {"id": "9", "user_id": "user", "created_at": "2026-11-18T09:00:00Z"},
                ],
                "pagination": {"cursor": "older"},
            },
        ]
        start = datetime(2026, 11, 18, 10, tzinfo=timezone.utc)
        end = datetime(2026, 11, 18, 12, tzinfo=timezone.utc)

        result = process_twitch.fetch_vods("user", start, end, "client", "token")

        self.assertEqual([vod["id"] for vod in result], ["12", "10"])
        self.assertEqual(helix.call_count, 2)
        self.assertEqual(helix.call_args_list[0].args[1]["user_id"], "user")
        self.assertNotIn("game_id", helix.call_args_list[0].args[1])
        self.assertEqual(helix.call_args_list[1].args[1]["after"], "next")


class SelectGameClipsTests(unittest.TestCase):
    def test_excludes_clips_from_other_categories(self):
        clips = [
            {"id": "gta-clip", "game_id": "gta"},
            {"id": "other-clip", "game_id": "other"},
            {"id": "unknown-category"},
        ]

        result = process_twitch.select_game_clips(clips, "gta")

        self.assertEqual([clip["id"] for clip in result], ["gta-clip"])


class SelectGameVodsTests(unittest.TestCase):
    def test_uses_category_id_when_available_and_title_alias_when_missing(self):
        vods = [
            {"id": "category-id", "game_id": "gta6", "title": "Unrelated title"},
            {"id": "title-alias", "game_id": None, "title": "GTA 6 с Дариночкой"},
            {"id": "full-title", "title": "Grand Theft Auto VI stream"},
            {"id": "wrong-game", "game_id": "other", "title": "GTA 6 stream"},
            {"id": "gta-five", "game_id": None, "title": "GTA 5 RP"},
        ]

        result = process_twitch.select_game_vods(
            vods, "gta6", "Grand Theft Auto VI"
        )

        self.assertEqual(
            [vod["id"] for vod in result],
            ["category-id", "title-alias", "full-title"],
        )


class MomentSelectionTests(unittest.TestCase):
    def test_selects_separated_multi_signal_peaks_without_a_fixed_count(self):
        size = 320
        audio = np.full(size, -30.0)
        motion = np.zeros(size)
        chat_bursts = np.zeros(size)
        laughs = np.zeros(size)
        audio[70:100] = -5
        audio[220:250] = -5
        chat_bursts[75:95] = 1
        chat_bursts[225:245] = 1
        laughs[80] = 1
        laughs[230] = 1

        candidates = process_twitch.find_chunk_candidates(
            audio, motion, chat_bursts, laughs, [],
            chunk_start=0, core_start=0, core_end=size, duration=size,
            min_score=0.45, min_separation=90,
        )

        self.assertEqual(len(candidates), 2)
        self.assertLess(candidates[0]["center"], candidates[1]["center"])
        self.assertTrue(all(candidate["score"] >= 0.45 for candidate in candidates))

    def test_rejects_moments_below_the_quality_threshold(self):
        size = 160
        candidates = process_twitch.find_chunk_candidates(
            np.full(size, -20.0), np.zeros(size), np.zeros(size), np.zeros(size), [],
            chunk_start=0, core_start=0, core_end=size, duration=size,
            min_score=0.45, min_separation=90,
        )

        self.assertEqual(candidates, [])


class ChatSignalTests(unittest.TestCase):
    def test_loads_chat_volume_and_laughter_from_twitchdownloader_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            chat_path = Path(temp_dir) / "chat.json"
            chat_path.write_text(
                json.dumps({
                    "comments": [
                        {
                            "content_offset_seconds": 10.2,
                            "message": {"body": "KEKW"},
                        },
                        {
                            "content_offset_seconds": 10.8,
                            "message": {"body": "nice"},
                        },
                    ],
                }),
                encoding="utf-8",
            )

            messages, laughs = process_twitch.load_chat_signals(chat_path, 20)

        self.assertEqual(messages[10], 2)
        self.assertEqual(laughs[10], 1)


class WhisperClipTests(unittest.TestCase):
    def test_transcribes_only_selected_audio_and_offsets_srt_times(self):
        whisper = SimpleNamespace(
            load_model=lambda _: SimpleNamespace(
                transcribe=lambda audio, language: {
                    "segments": [{"start": 1.25, "end": 2.5, "text": "test"}]
                },
            )
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            srt_path = Path(temp_dir) / "clip.srt"
            with patch.dict(sys.modules, {"whisper": whisper}), \
                    patch("narezchik.subprocess.run") as ffmpeg:
                self.assertTrue(narezchik.try_whisper("source.mp4", str(srt_path), 50, 90))

            self.assertEqual(ffmpeg.call_args.args[0][ffmpeg.call_args.args[0].index("-ss") + 1],
                             "50.000")
            self.assertIn("00:00:51,250 --> 00:00:52,500", srt_path.read_text())


class DotenvTests(unittest.TestCase):
    def test_loads_values_without_overriding_existing_environment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                'NAREZKI_TEST_ENV="quoted value"\n'
                "NAREZKI_TEST_EXISTING=from-file\n",
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {"NAREZKI_TEST_EXISTING": "from-shell"},
                clear=False,
            ):
                load_dotenv(env_path)
                self.assertEqual(os.environ["NAREZKI_TEST_ENV"], "quoted value")
                self.assertEqual(os.environ["NAREZKI_TEST_EXISTING"], "from-shell")


if __name__ == "__main__":
    unittest.main()
