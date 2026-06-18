from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sync_compare


class TestSyncCompareArgs(unittest.TestCase):
    def test_parse_args_defaults(self) -> None:
        args = sync_compare.parse_args(["left.mp4", "right.mp4"])

        self.assertEqual(args.left_video, Path("left.mp4"))
        self.assertEqual(args.right_video, Path("right.mp4"))
        self.assertEqual(args.width, sync_compare.DEFAULT_WIDTH)
        self.assertEqual(args.height, sync_compare.DEFAULT_HEIGHT)
        self.assertEqual(args.seek_small, sync_compare.DEFAULT_SEEK_SMALL)
        self.assertEqual(args.nudge_small, sync_compare.DEFAULT_NUDGE_SMALL)

    def test_parse_args_rejects_non_positive_values(self) -> None:
        with self.assertRaises(SystemExit):
            sync_compare.parse_args(["--width", "0", "left.mp4", "right.mp4"])

        with self.assertRaises(SystemExit):
            sync_compare.parse_args(["--seek-small", "0", "left.mp4", "right.mp4"])


class TestSyncCompareGeometry(unittest.TestCase):
    def test_calculate_geometry_places_right_window_after_gap(self) -> None:
        geometry = sync_compare.calculate_geometry(width=800, height=450, gap=16, x=30, y=40)

        self.assertEqual(geometry.left.mpv_value(), "800x450+30+40")
        self.assertEqual(geometry.right.mpv_value(), "800x450+846+40")


class TestSyncCompareOffsets(unittest.TestCase):
    def test_offset_state_tracks_relative_right_minus_left(self) -> None:
        offsets = sync_compare.OffsetState()

        offsets.nudge_left(0.5)
        offsets.nudge_right(-0.25)

        self.assertEqual(offsets.left_seconds, 0.5)
        self.assertEqual(offsets.right_seconds, -0.25)
        self.assertEqual(offsets.relative_seconds, -0.75)


class TestSyncCompareAudioMix(unittest.TestCase):
    def test_audio_mix_starts_balanced_and_keeps_sum_at_100(self) -> None:
        mix = sync_compare.AudioMix(step=0.1)

        self.assertEqual(mix.left_volume, 50)
        self.assertEqual(mix.right_volume, 50)

        mix.move_left()
        self.assertEqual(mix.left_volume, 60)
        self.assertEqual(mix.right_volume, 40)
        self.assertEqual(mix.left_volume + mix.right_volume, 100)

        mix.move_right()
        mix.move_right()
        self.assertEqual(mix.left_volume, 40)
        self.assertEqual(mix.right_volume, 60)
        self.assertEqual(mix.left_volume + mix.right_volume, 100)

    def test_audio_mix_clamps_to_ends(self) -> None:
        mix = sync_compare.AudioMix(step=0.8)

        mix.move_left()
        mix.move_left()
        self.assertEqual(mix.left_volume, 100)
        self.assertEqual(mix.right_volume, 0)

        mix.move_right()
        mix.move_right()
        mix.move_right()
        self.assertEqual(mix.left_volume, 0)
        self.assertEqual(mix.right_volume, 100)


class TestSyncCompareKeys(unittest.TestCase):
    def test_normalize_key_maps_requested_shortcuts(self) -> None:
        cases = {
            b" ": "space",
            b"\x1b[D": "seek_back",
            b"\x1b[C": "seek_forward",
            b"\x1b[1;2D": "seek_back_large",
            b"\x1b[1;2C": "seek_forward_large",
            b"c": "left_nudge_back",
            b"v": "left_nudge_forward",
            b"C": "left_nudge_back_large",
            b"V": "left_nudge_forward_large",
            b"m": "right_nudge_back",
            b",": "right_nudge_forward",
            b"M": "right_nudge_back_large",
            b"<": "right_nudge_forward_large",
            b"g": "audio_left",
            b"h": "audio_right",
            b"0": "mute",
            b"q": "quit",
        }

        for sequence, expected in cases.items():
            with self.subTest(sequence=sequence):
                self.assertEqual(sync_compare.normalize_key(sequence), expected)


class TestSyncCompareValidation(unittest.TestCase):
    def test_validate_inputs_rejects_missing_files(self) -> None:
        args = argparse.Namespace(left_video=Path("/missing/left.mp4"), right_video=Path("/missing/right.mp4"))

        with self.assertRaisesRegex(ValueError, "left video does not exist"):
            sync_compare.validate_inputs(args)

    def test_validate_inputs_rejects_missing_mpv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            left = Path(tmp) / "left.mp4"
            right = Path(tmp) / "right.mp4"
            left.write_bytes(b"left")
            right.write_bytes(b"right")
            args = argparse.Namespace(left_video=left, right_video=right)

            with patch.object(sync_compare.shutil, "which", return_value=None):
                with self.assertRaisesRegex(ValueError, "mpv is required"):
                    sync_compare.validate_inputs(args)
