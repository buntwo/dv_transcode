from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import audio_volume_analysis


class TestAudioVolumeAnalysis(unittest.TestCase):
    def test_build_volumedetect_command_maps_first_audio_stream(self) -> None:
        self.assertEqual(
            audio_volume_analysis.build_volumedetect_command(Path("/mnt/audio/Movie.flac")),
            [
                "ffmpeg",
                "-hide_banner",
                "-stats",
                "-loglevel",
                "info",
                "-i",
                "/mnt/audio/Movie.flac",
                "-map",
                "0:a:0",
                "-af",
                "volumedetect",
                "-f",
                "null",
                "-",
            ],
        )

    def test_run_volumedetect_captures_ffmpeg_output_by_default(self) -> None:
        completed = audio_volume_analysis.subprocess.CompletedProcess(
            ["ffmpeg"],
            0,
            stdout="mean_volume: -23.4 dB\nmax_volume: -4.5 dB\n",
            stderr="",
        )
        with (
            patch.object(audio_volume_analysis.subprocess, "run", return_value=completed) as run_mock,
            patch.object(audio_volume_analysis.subprocess, "Popen") as popen_mock,
        ):
            stats = audio_volume_analysis.run_volumedetect(Path("/audio/A.flac"))

        self.assertEqual(stats.max_volume, -4.5)
        run_mock.assert_called_once()
        popen_mock.assert_not_called()
        self.assertTrue(run_mock.call_args.kwargs["capture_output"])

    def test_parse_volumedetect_stats(self) -> None:
        stats = audio_volume_analysis.parse_volumedetect_stats(
            "",
            """
            [Parsed_volumedetect_0 @ 0x123] mean_volume: -23.4 dB
            [Parsed_volumedetect_0 @ 0x123] max_volume: -4.5 dB
            """,
        )

        self.assertEqual(stats.mean_volume, -23.4)
        self.assertEqual(stats.max_volume, -4.5)

    def test_parse_volumedetect_stats_requires_mean_volume(self) -> None:
        with self.assertRaisesRegex(ValueError, "mean_volume"):
            audio_volume_analysis.parse_volumedetect_stats("", "max_volume: -4.5 dB")

    def test_parse_volumedetect_stats_requires_max_volume(self) -> None:
        with self.assertRaisesRegex(ValueError, "max_volume"):
            audio_volume_analysis.parse_volumedetect_stats("", "mean_volume: -23.4 dB")

    def test_parse_volumedetect_stats_rejects_malformed_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "mean_volume"):
            audio_volume_analysis.parse_volumedetect_stats("", "mean_volume: loud dB\nmax_volume: -4.5 dB")

    def test_collect_audio_files_from_directory_without_access_mp4s(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            audio_dir = Path(root) / "audio"
            audio_dir.mkdir()
            (audio_dir / "A.flac").write_bytes(b"audio")
            (audio_dir / "B.wav").write_bytes(b"audio")

            files = audio_volume_analysis.collect_audio_files(audio_dir, [])

        self.assertEqual(files, [audio_dir / "A.flac"])

    def test_format_volume_analysis_table_marks_unsafe_peak(self) -> None:
        analysis = audio_volume_analysis.VolumeAnalysis(
            Path("/audio/A.flac"),
            audio_volume_analysis.VolumeDetectStats(mean_volume=-20.0, max_volume=-10.0),
            gain=12.0,
            peak_ceiling=-1.5,
        )

        table = audio_volume_analysis.format_volume_analysis_table([analysis])

        self.assertIn("A.flac", table)
        self.assertIn("+2.00", table)
        self.assertIn("exceeds ceiling", table)

    def test_analyze_audio_files_prints_progress_when_quiet(self) -> None:
        progress = io.StringIO()
        with patch.object(
            audio_volume_analysis,
            "run_volumedetect",
            return_value=audio_volume_analysis.VolumeDetectStats(mean_volume=-30.0, max_volume=-14.0),
        ) as detect_mock:
            analyses = audio_volume_analysis.analyze_audio_files(
                [Path("/audio/A.flac"), Path("/audio/B.flac")],
                12.0,
                -1.5,
                show_progress=True,
                progress_stream=progress,
            )

        self.assertEqual(len(analyses), 2)
        self.assertIn("1/2 /audio/A.flac", progress.getvalue())
        self.assertIn("2/2 /audio/B.flac", progress.getvalue())
        detect_mock.assert_any_call(Path("/audio/A.flac"), verbose=False)

    def test_analyze_audio_files_verbose_suppresses_progress(self) -> None:
        progress = io.StringIO()
        with patch.object(
            audio_volume_analysis,
            "run_volumedetect",
            return_value=audio_volume_analysis.VolumeDetectStats(mean_volume=-30.0, max_volume=-14.0),
        ) as detect_mock:
            audio_volume_analysis.analyze_audio_files(
                [Path("/audio/A.flac")],
                12.0,
                -1.5,
                verbose=True,
                show_progress=True,
                progress_stream=progress,
            )

        self.assertEqual(progress.getvalue(), "")
        detect_mock.assert_called_once_with(Path("/audio/A.flac"), verbose=True)

    def test_main_analyzes_audio_dir_without_access_mp4s(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            audio_dir = Path(root) / "audio"
            audio_dir.mkdir()
            (audio_dir / "A.flac").write_bytes(b"audio")

            with (
                patch.object(audio_volume_analysis.shutil, "which", return_value="/bin/ffmpeg"),
                patch.object(
                    audio_volume_analysis,
                    "run_volumedetect",
                    return_value=audio_volume_analysis.VolumeDetectStats(mean_volume=-30.0, max_volume=-14.0),
                ),
                patch.object(audio_volume_analysis.sys, "stdout", io.StringIO()) as stdout_mock,
                patch.object(audio_volume_analysis.sys, "stderr", io.StringIO()) as stderr_mock,
            ):
                code = audio_volume_analysis.main(["--audio-dir", str(audio_dir)])

        self.assertEqual(code, 0)
        self.assertIn("Fixed-gain analysis", stdout_mock.getvalue())
        self.assertIn("A.flac", stdout_mock.getvalue())
        self.assertIn("1/1", stderr_mock.getvalue())


if __name__ == "__main__":
    unittest.main()
