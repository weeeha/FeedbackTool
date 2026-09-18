"""Unit tests for feedback_video.py.

Runs with stdlib unittest only: `python3 -m unittest -v` from this directory.
No test here touches adb, the network, or a real whisper run. The smoke test
shells out to a local ffmpeg to build a throwaway clip, and stands in a canned
whisper.json for the transcribe stage.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import feedback_video as fv


# ---------------------------------------------------------------------------
# parse_recording_name
# ---------------------------------------------------------------------------
class TestParseRecordingName(unittest.TestCase):
    def test_valid_name(self):
        result = fv.parse_recording_name("com.weeeha.vrroom-20260918-170744-0.mp4")
        self.assertEqual(result["package"], "com.weeeha.vrroom")
        self.assertEqual(result["date"], "2026-09-18")
        self.assertEqual(result["time"], "17:07:44")
        self.assertEqual(result["slug_hint"], "vrroom-1707")
        self.assertEqual(result["stem"], "com.weeeha.vrroom-20260918-170744-0")

    def test_valid_name_other_package(self):
        result = fv.parse_recording_name("com.weeeha.thingspuzzle-20260101-000000-3.mp4")
        self.assertEqual(result["package"], "com.weeeha.thingspuzzle")
        self.assertEqual(result["date"], "2026-01-01")
        self.assertEqual(result["time"], "00:00:00")
        self.assertEqual(result["slug_hint"], "thingspuzzle-0000")

    def test_malformed_name_returns_none_fields(self):
        result = fv.parse_recording_name("not-a-recording.mp4")
        self.assertIsNone(result["package"])
        self.assertIsNone(result["date"])
        self.assertIsNone(result["time"])
        self.assertTrue(result["slug_hint"])
        self.assertEqual(result["stem"], "not-a-recording")

    def test_malformed_name_has_expected_keys(self):
        result = fv.parse_recording_name("weird file!!.mp4")
        self.assertEqual(
            set(result.keys()), {"package", "date", "time", "slug_hint", "stem"}
        )
        # slug_hint must be filesystem/slug friendly
        self.assertNotIn(" ", result["slug_hint"])
        self.assertNotIn("!", result["slug_hint"])

    def test_never_raises_on_empty_string(self):
        result = fv.parse_recording_name("")
        self.assertIsNone(result["package"])
        self.assertTrue(result["slug_hint"])

    def test_never_raises_on_none(self):
        result = fv.parse_recording_name(None)
        self.assertIsNone(result["package"])
        self.assertTrue(result["slug_hint"])

    def test_never_raises_on_no_extension(self):
        result = fv.parse_recording_name("just-some-name")
        self.assertIsNone(result["package"])
        self.assertEqual(result["stem"], "just-some-name")


# ---------------------------------------------------------------------------
# format_clock
# ---------------------------------------------------------------------------
class TestFormatClock(unittest.TestCase):
    def test_seconds_only(self):
        self.assertEqual(fv.format_clock(5.0), "00:05")

    def test_minutes_and_seconds(self):
        self.assertEqual(fv.format_clock(65.4), "01:05")

    def test_past_one_hour(self):
        self.assertEqual(fv.format_clock(3725.0), "1:02:05")

    def test_zero(self):
        self.assertEqual(fv.format_clock(0.0), "00:00")

    def test_exactly_one_hour(self):
        self.assertEqual(fv.format_clock(3600.0), "1:00:00")


# ---------------------------------------------------------------------------
# format_transcript_line
# ---------------------------------------------------------------------------
class TestFormatTranscriptLine(unittest.TestCase):
    def test_basic_line(self):
        seg = {"start": 1.0, "end": 12.0, "text": "okay I wanted to show feedback"}
        self.assertEqual(
            fv.format_transcript_line(seg),
            "[00:01-00:12] okay I wanted to show feedback",
        )

    def test_strips_and_collapses_whitespace(self):
        seg = {"start": 1.0, "end": 12.0, "text": "  okay   I wanted \n to show  "}
        self.assertEqual(
            fv.format_transcript_line(seg),
            "[00:01-00:12] okay I wanted to show",
        )

    def test_past_one_hour(self):
        seg = {"start": 3600.0, "end": 3661.0, "text": "an hour in"}
        self.assertEqual(
            fv.format_transcript_line(seg),
            "[1:00:00-1:01:01] an hour in",
        )


# ---------------------------------------------------------------------------
# filter_segments
# ---------------------------------------------------------------------------
class TestFilterSegments(unittest.TestCase):
    def test_drops_high_no_speech_prob(self):
        segments = [
            {"start": 0.0, "end": 1.0, "text": "a", "no_speech_prob": 0.9},
            {"start": 1.0, "end": 2.0, "text": "b", "no_speech_prob": 0.1},
        ]
        result = fv.filter_segments(segments)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["text"], "b")

    def test_missing_key_counts_as_zero(self):
        segments = [{"start": 0.0, "end": 1.0, "text": "a"}]
        result = fv.filter_segments(segments)
        self.assertEqual(len(result), 1)

    def test_exactly_at_threshold_is_kept(self):
        segments = [{"start": 0.0, "end": 1.0, "text": "a", "no_speech_prob": 0.6}]
        result = fv.filter_segments(segments)
        self.assertEqual(len(result), 1)

    def test_custom_threshold(self):
        segments = [{"start": 0.0, "end": 1.0, "text": "a", "no_speech_prob": 0.3}]
        result = fv.filter_segments(segments, no_speech_max=0.2)
        self.assertEqual(len(result), 0)


# ---------------------------------------------------------------------------
# frame_cap
# ---------------------------------------------------------------------------
class TestFrameCap(unittest.TestCase):
    def test_short_recording(self):
        self.assertEqual(fv.frame_cap(60.0), 40)

    def test_exactly_five_minutes(self):
        self.assertEqual(fv.frame_cap(300.0), 40)

    def test_just_over_five_minutes(self):
        self.assertEqual(fv.frame_cap(301.0), 48)

    def test_six_minutes(self):
        self.assertEqual(fv.frame_cap(360.0), 48)

    def test_just_over_six_minutes(self):
        self.assertEqual(fv.frame_cap(361.0), 56)

    def test_grows_and_caps_at_72(self):
        self.assertEqual(fv.frame_cap(900.0), 72)

    def test_hard_ceiling(self):
        self.assertEqual(fv.frame_cap(100000.0), 72)


# ---------------------------------------------------------------------------
# pick_frames
# ---------------------------------------------------------------------------
class TestPickFrames(unittest.TestCase):
    def test_midpoint_of_short_segment(self):
        segments = [{"start": 10.0, "end": 14.0, "text": "hi"}]
        picks = fv.pick_frames(segments, [], duration=100.0, cap=40)
        speech_picks = [p for p in picks if p["reason"] == "speech"]
        self.assertEqual(len(speech_picks), 1)
        self.assertAlmostEqual(speech_picks[0]["time"], 12.0)
        self.assertEqual(speech_picks[0]["segment_index"], 0)

    def test_long_segment_splits(self):
        # 18s / 6s => 3 picks, spread evenly, avoiding both edges
        segments = [{"start": 0.0, "end": 18.0, "text": "long one"}]
        picks = fv.pick_frames(segments, [], duration=100.0, cap=40)
        speech_picks = sorted(
            (p for p in picks if p["reason"] == "speech"), key=lambda p: p["time"]
        )
        self.assertEqual(len(speech_picks), 3)
        expected = [3.0, 9.0, 15.0]
        for p, e in zip(speech_picks, expected):
            self.assertAlmostEqual(p["time"], e)
            self.assertGreater(p["time"], 0.0)
            self.assertLess(p["time"], 18.0)

    def test_segment_length_just_over_chunk_gets_two_picks(self):
        segments = [{"start": 0.0, "end": 7.0, "text": "a bit long"}]
        picks = fv.pick_frames(segments, [], duration=100.0, cap=40)
        speech_picks = [p for p in picks if p["reason"] == "speech"]
        self.assertEqual(len(speech_picks), math.ceil(7.0 / 6.0))

    def test_fill_appears_in_silence(self):
        picks = fv.pick_frames([], [], duration=45.0, cap=40)
        fill_picks = [p for p in picks if p["reason"] == "fill"]
        self.assertGreater(len(fill_picks), 0)
        for p in fill_picks:
            self.assertEqual(p["segment_index"], None)

    def test_empty_transcript_still_yields_picks(self):
        picks = fv.pick_frames([], [], duration=60.0, cap=40)
        self.assertGreater(len(picks), 0)
        self.assertTrue(all(p["reason"] == "fill" for p in picks))

    def test_no_picks_when_duration_zero(self):
        picks = fv.pick_frames([], [], duration=0.0, cap=40)
        self.assertEqual(picks, [])

    def test_scene_offset_applied(self):
        picks = fv.pick_frames([], [10.0], duration=100.0, cap=40)
        scene_picks = [p for p in picks if p["reason"] == "scene"]
        # a lone scene pick near t=10 should survive spacing/fill and land at 10.3
        self.assertTrue(any(abs(p["time"] - 10.3) < 1e-6 for p in scene_picks))

    def test_scene_pick_discarded_beyond_duration(self):
        picks = fv.pick_frames([], [99.9], duration=100.0, cap=40)
        for p in picks:
            self.assertLessEqual(p["time"], 100.0)

    def test_spacing_drops_close_picks_speech_wins_over_scene(self):
        # A speech pick at 10.0 and a scene change at 9.8 (-> 10.1 with offset)
        # collide within MIN_GAP (2.0s); speech must win regardless of order.
        segments = [{"start": 8.0, "end": 12.0, "text": "speaking"}]
        picks = fv.pick_frames(segments, [9.8], duration=100.0, cap=40)
        nearby = [p for p in picks if 8.5 <= p["time"] <= 11.5]
        self.assertEqual(len(nearby), 1)
        self.assertEqual(nearby[0]["reason"], "speech")

    def test_spacing_scene_wins_over_fill(self):
        # No speech at all; a scene pick near a spot a fill pick would also
        # want. Scene must survive, fill nearby must not double up.
        picks = fv.pick_frames([], [20.0], duration=41.0, cap=40)
        near_20 = [p for p in picks if 18.0 <= p["time"] <= 22.0]
        self.assertEqual(len(near_20), 1)
        self.assertEqual(near_20[0]["reason"], "scene")

    def test_min_gap_respected_between_kept_picks(self):
        picks = fv.pick_frames([], [], duration=120.0, cap=72)
        times = sorted(p["time"] for p in picks)
        for a, b in zip(times, times[1:]):
            self.assertGreaterEqual(b - a, fv.MIN_GAP - 1e-9)

    def test_cap_drops_fill_first(self):
        # Long silent duration produces many fill picks; cap forces trimming.
        # With no speech/scene, all picks are fill, so the cap must be honored
        # exactly and no other reason should appear out of nowhere.
        picks = fv.pick_frames([], [], duration=1000.0, cap=10)
        self.assertLessEqual(len(picks), 10)

    def test_cap_prefers_speech_and_scene_over_fill(self):
        # cap equals exactly the speech+scene count, so every fill pick must
        # be dropped to make room, even though there would be dozens of fill
        # candidates across a 200s silent stretch.
        segments = [{"start": 0.0, "end": 4.0, "text": "one"}]
        scenes = [50.0]
        picks = fv.pick_frames(segments, scenes, duration=200.0, cap=2)
        self.assertEqual(len(picks), 2)
        reasons = {p["reason"] for p in picks}
        self.assertEqual(reasons, {"speech", "scene"})

    def test_cap_leaves_room_for_a_fill_pick_when_budget_allows(self):
        # cap has exactly one slot more than speech+scene need, so a single
        # fill pick is kept to use the remaining budget -- fill is dropped
        # only as far as needed, not unconditionally to zero.
        segments = [{"start": 0.0, "end": 4.0, "text": "one"}]
        scenes = [50.0]
        picks = fv.pick_frames(segments, scenes, duration=200.0, cap=3)
        self.assertEqual(len(picks), 3)
        reasons = [p["reason"] for p in picks]
        self.assertEqual(reasons.count("speech"), 1)
        self.assertEqual(reasons.count("scene"), 1)
        self.assertEqual(reasons.count("fill"), 1)

    def test_cap_never_exceeded_even_with_many_speech_segments(self):
        segments = [
            {"start": float(i * 20), "end": float(i * 20 + 4), "text": f"seg {i}"}
            for i in range(30)
        ]
        picks = fv.pick_frames(segments, [], duration=650.0, cap=5)
        self.assertLessEqual(len(picks), 5)

    def test_deterministic(self):
        segments = [
            {"start": 0.0, "end": 18.0, "text": "long one"},
            {"start": 30.0, "end": 32.0, "text": "short"},
        ]
        scenes = [5.0, 40.0, 41.5]
        a = fv.pick_frames(segments, scenes, duration=120.0, cap=40)
        b = fv.pick_frames(segments, scenes, duration=120.0, cap=40)
        self.assertEqual(a, b)

    def test_picks_sorted_by_time(self):
        segments = [
            {"start": 30.0, "end": 32.0, "text": "short"},
            {"start": 0.0, "end": 18.0, "text": "long one"},
        ]
        picks = fv.pick_frames(segments, [5.0, 60.0], duration=120.0, cap=40)
        times = [p["time"] for p in picks]
        self.assertEqual(times, sorted(times))

    def test_times_clamped_to_duration(self):
        segments = [{"start": 0.0, "end": 4.0, "text": "edge"}]
        picks = fv.pick_frames(segments, [1000.0], duration=10.0, cap=40)
        for p in picks:
            self.assertGreaterEqual(p["time"], 0.0)
            self.assertLessEqual(p["time"], 10.0)


# ---------------------------------------------------------------------------
# Dependency checking
# ---------------------------------------------------------------------------
class TestCheckDependencies(unittest.TestCase):
    def test_all_present_returns_empty(self):
        def fake_which(name):
            return f"/usr/bin/{name}"

        missing = fv.check_dependencies(which=fake_which, pillow_available=True)
        self.assertEqual(missing, [])

    def test_missing_ffmpeg_reported(self):
        def fake_which(name):
            return None if name == "ffmpeg" else f"/usr/bin/{name}"

        missing = fv.check_dependencies(which=fake_which, pillow_available=True)
        self.assertIn("ffmpeg", missing)

    def test_missing_pillow_reported(self):
        def fake_which(name):
            return f"/usr/bin/{name}"

        missing = fv.check_dependencies(which=fake_which, pillow_available=False)
        self.assertIn("Pillow", missing)


# ---------------------------------------------------------------------------
# thumb signature diff / dedupe_picks
# ---------------------------------------------------------------------------
class TestDedupePicks(unittest.TestCase):
    def test_drops_near_duplicate(self):
        picks = [
            {"time": 0.0, "reason": "fill", "segment_index": None},
            {"time": 10.0, "reason": "fill", "segment_index": None},
            {"time": 20.0, "reason": "fill", "segment_index": None},
        ]
        # signature 1 is identical to signature 0 -> should be dropped;
        # signature 2 is very different -> kept.
        sig_a = [0] * 256
        sig_b = [255] * 256
        signatures = [sig_a, sig_a, sig_b]
        kept = fv.dedupe_picks(picks, signatures, threshold=4.0)
        self.assertEqual(len(kept), 2)
        self.assertEqual(kept[0]["time"], 0.0)
        self.assertEqual(kept[1]["time"], 20.0)

    def test_keeps_all_when_all_differ(self):
        picks = [
            {"time": 0.0, "reason": "fill", "segment_index": None},
            {"time": 10.0, "reason": "fill", "segment_index": None},
        ]
        signatures = [[0] * 256, [255] * 256]
        kept = fv.dedupe_picks(picks, signatures, threshold=4.0)
        self.assertEqual(len(kept), 2)


# ---------------------------------------------------------------------------
# Smoke test: real ffmpeg + Pillow, canned whisper.json, full pipeline
# ---------------------------------------------------------------------------
@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not available on PATH")
class TestSmokePipeline(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="feedback-video-smoke-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)

        self.cache_root = Path(self.tmpdir) / "cache"
        self.out_dir = Path(self.tmpdir) / "out"
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.out_dir.mkdir(parents=True, exist_ok=True)

        self._old_cache_env = os.environ.get(fv.CACHE_ROOT_ENV_VAR)
        os.environ[fv.CACHE_ROOT_ENV_VAR] = str(self.cache_root)
        self.addCleanup(self._restore_cache_env)

        # Generate a tiny throwaway clip with picture + tone.
        self.clip_path = Path(self.tmpdir) / "com.example.smoketest-20260918-101010-0.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=duration=12:size=640x480:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
            "-shortest",
            str(self.clip_path),
        ]
        import subprocess

        subprocess.run(cmd, capture_output=True, check=True)

        # Pre-seed the transcribe stage's cache output so the pipeline never
        # shells out to real whisper.
        stem = self.clip_path.stem
        stage_dir = self.cache_root / stem
        stage_dir.mkdir(parents=True, exist_ok=True)
        canned_whisper = {
            "text": "okay I wanted to show feedback here it is",
            "language": "en",
            "segments": [
                {
                    "id": 0,
                    "start": 0.5,
                    "end": 4.5,
                    "text": " okay I wanted to show feedback",
                    "no_speech_prob": 0.05,
                },
                {
                    "id": 1,
                    "start": 5.0,
                    "end": 11.0,
                    "text": " here it is, look at this",
                    "no_speech_prob": 0.1,
                },
            ],
        }
        with open(stage_dir / "whisper.json", "w") as fh:
            json.dump(canned_whisper, fh)

    def _restore_cache_env(self):
        if self._old_cache_env is None:
            os.environ.pop(fv.CACHE_ROOT_ENV_VAR, None)
        else:
            os.environ[fv.CACHE_ROOT_ENV_VAR] = self._old_cache_env

    def test_pipeline_produces_expected_outputs(self):
        argv = [
            str(self.clip_path),
            "--out", str(self.out_dir),
            "--slug", "smoketest",
            "--language", "en",
        ]
        exit_code = fv.main(argv)
        self.assertEqual(exit_code, 0)

        manifest_paths = list(self.out_dir.glob("*-manifest.json"))
        self.assertEqual(len(manifest_paths), 1)
        manifest = json.loads(manifest_paths[0].read_text())

        transcript_paths = list(self.out_dir.glob("*-transcript.txt"))
        self.assertEqual(len(transcript_paths), 1)
        transcript_text = transcript_paths[0].read_text()
        self.assertIn("[00:00-00:04]", transcript_text)

        sheet_paths = sorted(self.out_dir.glob("*-frames-*.jpg"))
        self.assertGreater(len(sheet_paths), 0)

        self.assertIn("picks", manifest)
        self.assertEqual(len(manifest["picks"]), len(sheet_paths and manifest["picks"]))

        # tile counts across sheets must match the number of picks in the
        # manifest (each pick lands on exactly one tile).
        total_picks = len(manifest["picks"])
        expected_sheets = math.ceil(total_picks / fv.TILES_PER_SHEET)
        self.assertEqual(len(sheet_paths), expected_sheets)

        self.assertEqual(manifest["source"], self.clip_path.name)
        self.assertIn("duration", manifest)
        self.assertGreater(manifest["duration"], 0)


if __name__ == "__main__":
    unittest.main()
