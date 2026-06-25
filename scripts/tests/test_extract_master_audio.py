from __future__ import annotations

import unittest
from pathlib import Path

import extract_master_audio


class TestExtractMasterAudioArgs(unittest.TestCase):
    def test_parse_args_defaults(self) -> None:
        args = extract_master_audio.parse_args(["--output-dir", "/tmp/audio", "Video.mkv"])

        self.assertEqual(args.input_files, [Path("Video.mkv")])
        self.assertEqual(args.output_dir, Path("/tmp/audio"))
        self.assertFalse(args.force)
        self.assertEqual(args.audio_channel, "keep")

    def test_build_extract_command_targets_first_audio_only(self) -> None:
        cmd = extract_master_audio.build_extract_command(
            Path("/mnt/src/Video.Master.mkv"),
            Path("/mnt/audio/Video.Master.flac"),
            force=False,
        )

        self.assertEqual(
            cmd,
            [
                "ffmpeg",
                "-hide_banner",
                "-stats",
                "-loglevel",
                "info",
                "-i",
                "/mnt/src/Video.Master.mkv",
                "-map",
                "0:a:0",
                "-vn",
                "-c:a",
                "flac",
                "/mnt/audio/Video.Master.flac",
            ],
        )

    def test_build_extract_command_left_channel(self) -> None:
        cmd = extract_master_audio.build_extract_command(
            Path("/mnt/src/Video.mkv"),
            Path("/mnt/audio/Video.flac"),
            force=False,
            audio_channel="left",
        )

        self.assertEqual(
            cmd,
            [
                "ffmpeg",
                "-hide_banner",
                "-stats",
                "-loglevel",
                "info",
                "-i",
                "/mnt/src/Video.mkv",
                "-map",
                "0:a:0",
                "-vn",
                "-af",
                "pan=stereo|c0=c0|c1=c0",
                "-c:a",
                "flac",
                "/mnt/audio/Video.flac",
            ],
        )

    def test_build_extract_command_right_channel(self) -> None:
        cmd = extract_master_audio.build_extract_command(
            Path("Video.mkv"),
            Path("audio/Video.flac"),
            force=True,
            audio_channel="right",
        )

        self.assertEqual(
            cmd,
            [
                "ffmpeg",
                "-hide_banner",
                "-stats",
                "-loglevel",
                "info",
                "-y",
                "-i",
                "Video.mkv",
                "-map",
                "0:a:0",
                "-vn",
                "-af",
                "pan=stereo|c0=c1|c1=c1",
                "-c:a",
                "flac",
                "audio/Video.flac",
            ],
        )

    def test_build_extract_command_includes_force_overwrite(self) -> None:
        cmd = extract_master_audio.build_extract_command(
            Path("Video.mkv"),
            Path("audio/Video.flac"),
            force=True,
        )

        self.assertEqual(
            cmd,
            [
                "ffmpeg",
                "-hide_banner",
                "-stats",
                "-loglevel",
                "info",
                "-y",
                "-i",
                "Video.mkv",
                "-map",
                "0:a:0",
                "-vn",
                "-c:a",
                "flac",
                "audio/Video.flac",
            ],
        )


if __name__ == "__main__":
    unittest.main()
