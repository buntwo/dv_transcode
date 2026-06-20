from __future__ import annotations

import argparse
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import video_preference_test
from multi_player import WindowGeometry


class FixedRng:
    def __init__(self, values: list[int]) -> None:
        self.values = values

    def randrange(self, stop: int) -> int:
        assert stop == 2
        return self.values.pop(0)


class TestVideoPreferenceArgs(unittest.TestCase):
    def test_parse_args_requires_two_videos_duration_and_rounds(self) -> None:
        args = video_preference_test.parse_args(["--duration", "3.5", "--rounds", "7", "one.mp4", "two.mp4"])

        self.assertEqual(args.videos, [Path("one.mp4"), Path("two.mp4")])
        self.assertEqual(args.duration, 3.5)
        self.assertEqual(args.rounds, 7)
        self.assertIsNone(args.ss)
        self.assertIsNone(args.ss1)
        self.assertIsNone(args.ss2)

        with self.assertRaises(SystemExit):
            video_preference_test.parse_args(["--duration", "3", "one.mp4", "two.mp4"])
        with self.assertRaises(SystemExit):
            video_preference_test.parse_args(["--rounds", "7", "one.mp4", "two.mp4"])
        with self.assertRaises(SystemExit):
            video_preference_test.parse_args(["--duration", "3", "--rounds", "7", "one.mp4"])

    def test_parse_args_rejects_non_positive_values(self) -> None:
        with self.assertRaises(SystemExit):
            video_preference_test.parse_args(["--duration", "0", "--rounds", "7", "one.mp4", "two.mp4"])
        with self.assertRaises(SystemExit):
            video_preference_test.parse_args(["--duration", "3", "--rounds", "0", "one.mp4", "two.mp4"])

    def test_collect_start_times_uses_global_start_seek_with_numbered_overrides(self) -> None:
        args = video_preference_test.parse_args(
            ["-ss", "5", "-ss1", "01:02.5", "--duration", "3", "--rounds", "7", "one.mp4", "two.mp4"]
        )

        self.assertEqual(video_preference_test.collect_start_times(args), [62.5, 5.0])

        args = video_preference_test.parse_args(
            ["-ss", "5", "-ss2", "1:02:03.5", "--duration", "3", "--rounds", "7", "one.mp4", "two.mp4"]
        )
        self.assertEqual(video_preference_test.collect_start_times(args), [5.0, 3723.5])

    def test_parse_timestamp_rejects_invalid_values(self) -> None:
        for value in ("-1", "nan", "inf", "1:2:3:4", "01:", "1.5:02", "abc"):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    video_preference_test.parse_timestamp(value)


class TestVideoPreferenceTrials(unittest.TestCase):
    def test_make_trial_randomizes_pair_order(self) -> None:
        self.assertEqual(
            video_preference_test.make_trial(1, FixedRng([0])),
            video_preference_test.Trial(round_index=1, source_for_a=1, source_for_b=2),
        )
        self.assertEqual(
            video_preference_test.make_trial(2, FixedRng([1])),
            video_preference_test.Trial(round_index=2, source_for_a=2, source_for_b=1),
        )

    def test_choice_maps_back_to_source_video_and_correctness(self) -> None:
        normal = video_preference_test.Trial(round_index=1, source_for_a=1, source_for_b=2)
        swapped = video_preference_test.Trial(round_index=2, source_for_a=2, source_for_b=1)

        self.assertEqual(video_preference_test.TrialResult(normal, "a").chosen_source, 1)
        self.assertTrue(video_preference_test.TrialResult(normal, "a").is_correct)
        self.assertEqual(video_preference_test.TrialResult(normal, "b").chosen_source, 2)
        self.assertFalse(video_preference_test.TrialResult(normal, "b").is_correct)
        self.assertEqual(video_preference_test.TrialResult(swapped, "a").chosen_source, 2)
        self.assertFalse(video_preference_test.TrialResult(swapped, "a").is_correct)
        self.assertEqual(video_preference_test.TrialResult(swapped, "b").chosen_source, 1)
        self.assertTrue(video_preference_test.TrialResult(swapped, "b").is_correct)

    def test_build_mpv_command_includes_clip_and_window_options(self) -> None:
        cmd = video_preference_test.build_mpv_command(
            Path("video.mp4"),
            "Round 3/12 - A",
            WindowGeometry(width=800, height=450, x=10, y=20),
            start_seconds=62.5,
            duration_seconds=3.0,
            screen=1,
        )

        self.assertEqual(cmd[0], "mpv")
        self.assertIn("--no-terminal", cmd)
        self.assertIn("--ontop", cmd)
        self.assertIn("--force-window=immediate", cmd)
        self.assertIn("--no-resume-playback", cmd)
        self.assertIn("--start=62.5", cmd)
        self.assertIn("--length=3.0", cmd)
        self.assertIn("--geometry=800x450+10+20", cmd)
        self.assertIn("--title=Round 3/12 - A", cmd)
        self.assertIn("--screen=1", cmd)
        self.assertEqual(cmd[-1], "video.mp4")

    def test_prompt_choice_accepts_answers_replay_and_quit(self) -> None:
        stdout = io.StringIO()

        choice = video_preference_test.prompt_choice(stdin=io.StringIO("bad\nB\n"), stdout=stdout)

        self.assertEqual(choice, "b")
        self.assertIn("Please enter a, b, 1, 2, or q.", stdout.getvalue())
        self.assertEqual(video_preference_test.prompt_choice(stdin=io.StringIO("1\n"), stdout=io.StringIO()), "1")
        self.assertEqual(video_preference_test.prompt_choice(stdin=io.StringIO("2\n"), stdout=io.StringIO()), "2")
        self.assertEqual(video_preference_test.prompt_choice(stdin=io.StringIO("q\n"), stdout=io.StringIO()), "q")

    def test_run_trials_plays_two_clips_per_round_and_stops_on_quit(self) -> None:
        args = video_preference_test.parse_args(
            ["-ss1", "10", "-ss2", "20", "--duration", "4", "--rounds", "2", "one.mp4", "two.mp4"]
        )
        stdout = io.StringIO()

        with patch.object(video_preference_test, "play_clip") as play_clip:
            results = video_preference_test.run_trials(args, FixedRng([1, 0]), stdin=io.StringIO("a\nq\n"), stdout=stdout)

        self.assertEqual([result.chosen_source for result in results], [2])
        self.assertEqual([result.is_correct for result in results], [False])
        self.assertEqual(play_clip.call_count, 4)
        self.assertEqual(play_clip.call_args_list[0].args[0], Path("two.mp4"))
        self.assertEqual(play_clip.call_args_list[0].args[3], 20.0)
        self.assertEqual(play_clip.call_args_list[1].args[0], Path("one.mp4"))
        self.assertEqual(play_clip.call_args_list[1].args[3], 10.0)
        self.assertIn("Round 1/2: Playing A... Playing B...", stdout.getvalue())
        self.assertNotIn("Current stats:", stdout.getvalue())

    def test_run_trials_replays_current_round_presentations_before_answering(self) -> None:
        args = video_preference_test.parse_args(
            ["-ss1", "10", "-ss2", "20", "--duration", "4", "--rounds", "1", "one.mp4", "two.mp4"]
        )
        stdout = io.StringIO()

        with patch.object(video_preference_test, "play_clip") as play_clip:
            results = video_preference_test.run_trials(args, FixedRng([1]), stdin=io.StringIO("1\n2\nb\n"), stdout=stdout)

        self.assertEqual([result.chosen_source for result in results], [1])
        self.assertEqual([result.is_correct for result in results], [True])
        self.assertEqual(play_clip.call_count, 4)
        self.assertEqual([call.args[0] for call in play_clip.call_args_list], [
            Path("two.mp4"),
            Path("one.mp4"),
            Path("two.mp4"),
            Path("one.mp4"),
        ])
        self.assertIn("Round 1/1: Replaying A...", stdout.getvalue())
        self.assertIn("Round 1/1: Replaying B...", stdout.getvalue())


class TestVideoPreferenceStats(unittest.TestCase):
    def test_two_sided_binomial_p_value(self) -> None:
        self.assertEqual(video_preference_test.two_sided_binomial_p_value(10, 10), 0.001953125)
        self.assertEqual(video_preference_test.two_sided_binomial_p_value(8, 10), 0.109375)
        self.assertEqual(video_preference_test.two_sided_binomial_p_value(5, 10), 1.0)

    def test_one_sided_binomial_p_value(self) -> None:
        self.assertEqual(video_preference_test.one_sided_binomial_p_value(10, 10), 0.0009765625)
        self.assertEqual(video_preference_test.one_sided_binomial_p_value(8, 10), 0.0546875)
        self.assertEqual(video_preference_test.one_sided_binomial_p_value(5, 10), 0.623046875)

    def test_format_summary_reports_compact_table_total_and_significance(self) -> None:
        trial = video_preference_test.Trial(round_index=1, source_for_a=1, source_for_b=2)
        results = [video_preference_test.TrialResult(trial, "a") for _ in range(10)]

        summary = video_preference_test.format_summary(results)

        self.assertIn("Round  Correct", summary)
        self.assertIn("-----  -------", summary)
        self.assertIn(f"1      {video_preference_test.CORRECT_MARK}", summary)
        self.assertIn("Total: 10/10 correct (100.0%)", summary)
        self.assertIn("Exact binomial p-value: 0.000976562", summary)
        self.assertIn("Alpha 0.05: statistically significant", summary)

        mixed_results = [
            video_preference_test.TrialResult(trial, "a"),
            video_preference_test.TrialResult(trial, "b"),
        ]
        mixed_summary = video_preference_test.format_summary(mixed_results)
        self.assertIn(f"1      {video_preference_test.CORRECT_MARK}", mixed_summary)
        self.assertIn(f"1      {video_preference_test.INCORRECT_MARK}", mixed_summary)
        self.assertIn("Total: 1/2 correct (50.0%)", mixed_summary)
        self.assertIn("Alpha 0.05: not statistically significant", mixed_summary)

    def test_format_summary_handles_no_completed_rounds(self) -> None:
        self.assertEqual(video_preference_test.format_summary([]), "No completed rounds.")

    def test_run_validates_inputs_and_returns_nonzero_for_no_completed_rounds(self) -> None:
        with tempfile.NamedTemporaryFile() as one, tempfile.NamedTemporaryFile() as two:
            args = video_preference_test.parse_args(["--duration", "3", "--rounds", "1", one.name, two.name])
            with (
                patch.object(video_preference_test.shutil, "which", return_value="/opt/homebrew/bin/mpv"),
                patch.object(video_preference_test, "run_trials", return_value=[]),
            ):
                self.assertEqual(video_preference_test.run(args), 1)


if __name__ == "__main__":
    unittest.main()
