from __future__ import annotations

import csv
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import audio_volume_analysis
import normalize_access_audio


class TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


class TestNormalizeAccessAudioArgs(unittest.TestCase):
    def test_parse_args_defaults(self) -> None:
        args = normalize_access_audio.parse_args(
            [
                "--access-copy-dir",
                "/mnt/access",
                "--audio-dir",
                "/mnt/audio",
                "--output-dir",
                "/mnt/output",
            ]
        )

        self.assertEqual(args.access_copy_dir, Path("/mnt/access"))
        self.assertEqual(args.audio_dir, Path("/mnt/audio"))
        self.assertEqual(args.output_dir, Path("/mnt/output"))
        self.assertEqual(args.gain, 12.0)
        self.assertEqual(args.peak_ceiling, -1.5)
        self.assertEqual(args.audio_bitrate, "192k")
        self.assertEqual(args.duration_tolerance, 0.05)
        self.assertEqual(args.vhs_notch, "off")
        self.assertFalse(args.force)
        self.assertFalse(args.yes)
        self.assertFalse(args.verbose)

    def test_parse_args_fixed_gain_options(self) -> None:
        args = normalize_access_audio.parse_args(
            [
                "--access-copy-dir",
                "/mnt/access",
                "--audio-dir",
                "/mnt/audio",
                "--output-dir",
                "/mnt/output",
                "--gain",
                "9.5",
                "--peak-ceiling",
                "-1.0",
                "--yes",
            ]
        )

        self.assertEqual(args.gain, 9.5)
        self.assertEqual(args.peak_ceiling, -1.0)
        self.assertTrue(args.yes)

    def test_audio_file_for_access_uses_exact_stem(self) -> None:
        self.assertEqual(
            normalize_access_audio.audio_file_for_access(
                Path("/mnt/access/Foo.mp4"),
                Path("/mnt/audio"),
            ),
            Path("/mnt/audio/Foo.flac"),
        )


class TestNormalizeAccessAudioPreflight(unittest.TestCase):
    def test_missing_flac_fails_before_processing(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            access_dir = Path(root) / "access"
            audio_dir = Path(root) / "audio"
            output_dir = Path(root) / "output"
            access_dir.mkdir()
            audio_dir.mkdir()
            (access_dir / "Movie.mp4").write_bytes(b"video")

            with (
                patch.object(normalize_access_audio.shutil, "which", return_value="/bin/ffmpeg"),
                patch.object(normalize_access_audio.subprocess, "run") as run_mock,
                patch.object(normalize_access_audio.sys, "stdout", io.StringIO()) as stdout_mock,
            ):
                code = normalize_access_audio.main(
                    [
                        "--access-copy-dir",
                        str(access_dir),
                        "--audio-dir",
                        str(audio_dir),
                        "--output-dir",
                        str(output_dir),
                        "--yes",
                    ]
                )

        self.assertEqual(code, 1)
        stdout = stdout_mock.getvalue()
        self.assertIn("Preflight normalization list", stdout)
        self.assertIn("Movie.flac", stdout)
        self.assertIn("missing", stdout)
        self.assertLess(
            stdout.index("Preflight normalization list"),
            stdout.index("normalize_access_audio.py: error: preflight failed"),
        )
        run_mock.assert_not_called()

    def test_output_existing_requires_force(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            access_dir = Path(root) / "access"
            audio_dir = Path(root) / "audio"
            output_dir = Path(root) / "output"
            access_dir.mkdir()
            audio_dir.mkdir()
            (access_dir / "Movie.mp4").write_bytes(b"video")
            (audio_dir / "Movie.flac").write_bytes(b"audio")
            output_dir.mkdir()
            (output_dir / "Movie.mp4").write_bytes(b"output")

            with (
                patch.object(normalize_access_audio.shutil, "which", return_value="/bin/ffmpeg"),
                patch.object(normalize_access_audio.subprocess, "run") as run_mock,
                patch.object(normalize_access_audio, "probe_media_duration_seconds", return_value=10.0),
                patch.object(normalize_access_audio.sys, "stdout", io.StringIO()) as stdout_mock,
            ):
                code = normalize_access_audio.main(
                    [
                        "--access-copy-dir",
                        str(access_dir),
                        "--audio-dir",
                        str(audio_dir),
                        "--output-dir",
                        str(output_dir),
                    ]
                )

        self.assertEqual(code, 1)
        stdout = stdout_mock.getvalue()
        self.assertIn("Preflight normalization list", stdout)
        self.assertIn("exists, use --force", stdout)
        self.assertLess(
            stdout.index("Preflight normalization list"),
            stdout.index("normalize_access_audio.py: error: preflight failed"),
        )
        run_mock.assert_not_called()

    def test_duration_mismatch_fails_over_tolerance(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            access_dir = Path(root) / "access"
            audio_dir = Path(root) / "audio"
            output_dir = Path(root) / "output"
            access_dir.mkdir()
            audio_dir.mkdir()
            (access_dir / "Movie.mp4").write_bytes(b"video")
            (audio_dir / "Movie.flac").write_bytes(b"audio")

            def probe_duration(path: Path) -> float:
                return 10.0 if path.name == "Movie.mp4" else 10.2

            with (
                patch.object(normalize_access_audio.shutil, "which", return_value="/bin/ffmpeg"),
                patch.object(normalize_access_audio.subprocess, "run") as run_mock,
                patch.object(normalize_access_audio, "probe_media_duration_seconds", side_effect=probe_duration),
                patch.object(normalize_access_audio.sys, "stdout", io.StringIO()) as stdout_mock,
            ):
                code = normalize_access_audio.main(
                    [
                        "--access-copy-dir",
                        str(access_dir),
                        "--audio-dir",
                        str(audio_dir),
                        "--output-dir",
                        str(output_dir),
                        "--duration-tolerance",
                        "0.05",
                    ]
                )

        self.assertEqual(code, 1)
        stdout = stdout_mock.getvalue()
        self.assertIn("Preflight normalization list", stdout)
        self.assertIn("0.200", stdout)
        self.assertIn("mismatch", stdout)
        self.assertLess(
            stdout.index("Preflight normalization list"),
            stdout.index("normalize_access_audio.py: error: preflight failed"),
        )
        run_mock.assert_not_called()

    def test_duration_preflight_prints_progress_before_processing(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            access_dir = Path(root) / "access"
            audio_dir = Path(root) / "audio"
            output_dir = Path(root) / "output"
            access_dir.mkdir()
            audio_dir.mkdir()
            (access_dir / "Movie.mp4").write_bytes(b"video")
            (audio_dir / "Movie.flac").write_bytes(b"audio")
            stderr = io.StringIO()
            args = normalize_access_audio.parse_args(
                [
                    "--access-copy-dir",
                    str(access_dir),
                    "--audio-dir",
                    str(audio_dir),
                    "--output-dir",
                    str(output_dir),
                ]
            )

            with (
                patch.object(normalize_access_audio, "probe_media_duration_seconds", return_value=10.0),
                patch.object(normalize_access_audio.sys, "stderr", stderr),
            ):
                jobs = normalize_access_audio.build_jobs(args)

        self.assertEqual(len(jobs), 1)
        self.assertIn("Checking durations", stderr.getvalue())
        self.assertIn("1/1", stderr.getvalue())
        self.assertIn("Movie.mp4", stderr.getvalue())

    def test_build_jobs_records_vhs_notch_filter(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            access_dir = Path(root) / "access"
            audio_dir = Path(root) / "audio"
            output_dir = Path(root) / "output"
            access_dir.mkdir()
            audio_dir.mkdir()
            access_file = access_dir / "Movie.mp4"
            access_file.write_bytes(b"video")
            (audio_dir / "Movie.flac").write_bytes(b"audio")
            args = normalize_access_audio.parse_args(
                [
                    "--access-copy-dir",
                    str(access_dir),
                    "--audio-dir",
                    str(audio_dir),
                    "--output-dir",
                    str(output_dir),
                    "--vhs-notch",
                    "auto",
                ]
            )

            with (
                patch.object(normalize_access_audio, "probe_media_duration_seconds", return_value=10.0),
                patch.object(
                    normalize_access_audio,
                    "build_vhs_audio_filter",
                    return_value="highpass=f=60:p=1,equalizer=f=15734:width_type=q:width=30:g=-24",
                ) as audio_filter,
            ):
                jobs = normalize_access_audio.build_jobs(args)

        self.assertEqual(jobs[0].audio_filter, "highpass=f=60:p=1,equalizer=f=15734:width_type=q:width=30:g=-24")
        audio_filter.assert_called_once()
        self.assertEqual(audio_filter.call_args.args[1], access_file)


class TestNormalizeAccessAudioCommands(unittest.TestCase):
    def test_build_fixed_gain_command_remuxes_video_copy_with_volume_filter(self) -> None:
        args = normalize_access_audio.parse_args(
            [
                "--access-copy-dir",
                "/mnt/access",
                "--audio-dir",
                "/mnt/audio",
                "--output-dir",
                "/mnt/output",
                "--gain",
                "12",
            ]
        )

        self.assertEqual(
            normalize_access_audio.build_fixed_gain_command(
                Path("/mnt/access/Movie.mp4"),
                Path("/mnt/audio/Movie.flac"),
                Path("/mnt/output/Movie.mp4"),
                args,
                overwrite=True,
            ),
            [
                "ffmpeg",
                "-hide_banner",
                "-stats",
                "-loglevel",
                "info",
                "-i",
                "/mnt/access/Movie.mp4",
                "-i",
                "/mnt/audio/Movie.flac",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-af",
                "volume=12dB",
                "-y",
                "/mnt/output/Movie.mp4",
            ],
        )

    def test_build_fixed_gain_command_composes_audio_filter_before_gain(self) -> None:
        args = normalize_access_audio.parse_args(
            [
                "--access-copy-dir",
                "/mnt/access",
                "--audio-dir",
                "/mnt/audio",
                "--output-dir",
                "/mnt/output",
                "--gain",
                "12",
            ]
        )

        cmd = normalize_access_audio.build_fixed_gain_command(
            Path("/mnt/access/Movie.mp4"),
            Path("/mnt/audio/Movie.flac"),
            Path("/mnt/output/Movie.mp4"),
            args,
            overwrite=True,
            audio_filter="highpass=f=60:p=1",
        )

        self.assertEqual(cmd[cmd.index("-af") + 1], "highpass=f=60:p=1,volume=12dB")

    def test_analyze_fixed_gain_passes_job_audio_filter_to_volumedetect(self) -> None:
        args = normalize_access_audio.parse_args(
            [
                "--access-copy-dir",
                "/mnt/access",
                "--audio-dir",
                "/mnt/audio",
                "--output-dir",
                "/mnt/output",
            ]
        )
        job = normalize_access_audio.NormalizationJob(
            access_file=Path("/mnt/access/Movie.mp4"),
            audio_file=Path("/mnt/audio/Movie.flac"),
            output_file=Path("/mnt/output/Movie.mp4"),
            video_duration_seconds=120.0,
            audio_duration_seconds=120.0,
            duration_delta_seconds=0.0,
            audio_filter="highpass=f=60:p=1",
        )

        with patch.object(
            normalize_access_audio,
            "run_volumedetect",
            return_value=audio_volume_analysis.VolumeDetectStats(mean_volume=-30.0, max_volume=-14.0),
        ) as volumedetect:
            reviews = normalize_access_audio.analyze_fixed_gain(args, [job])

        self.assertEqual(len(reviews), 1)
        volumedetect.assert_called_once_with(
            Path("/mnt/audio/Movie.flac"),
            audio_filter="highpass=f=60:p=1",
            verbose=False,
        )

    def test_analyze_fixed_gain_prints_labeled_progress(self) -> None:
        args = normalize_access_audio.parse_args(
            [
                "--access-copy-dir",
                "/mnt/access",
                "--audio-dir",
                "/mnt/audio",
                "--output-dir",
                "/mnt/output",
            ]
        )
        job = normalize_access_audio.NormalizationJob(
            access_file=Path("/mnt/access/Movie.mp4"),
            audio_file=Path("/mnt/audio/Movie.flac"),
            output_file=Path("/mnt/output/Movie.mp4"),
            video_duration_seconds=120.0,
            audio_duration_seconds=120.0,
            duration_delta_seconds=0.0,
            audio_filter=None,
        )
        stderr = io.StringIO()

        with (
            patch.object(
                normalize_access_audio,
                "run_volumedetect",
                return_value=audio_volume_analysis.VolumeDetectStats(mean_volume=-30.0, max_volume=-14.0),
            ),
            patch.object(normalize_access_audio.sys, "stderr", stderr),
        ):
            normalize_access_audio.analyze_fixed_gain(args, [job])

        self.assertIn("Analyzing audio peaks", stderr.getvalue())
        self.assertIn("1/1", stderr.getvalue())
        self.assertIn("Movie.flac", stderr.getvalue())

class TestFixedGainMode(unittest.TestCase):
    def test_safe_gain_prints_table_remuxes_after_confirmation_and_writes_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            access_dir = Path(root) / "access"
            audio_dir = Path(root) / "audio"
            output_dir = Path(root) / "output"
            access_dir.mkdir()
            audio_dir.mkdir()
            (access_dir / "Movie.mp4").write_bytes(b"video")
            (audio_dir / "Movie.flac").write_bytes(b"audio")

            with (
                patch.object(normalize_access_audio.shutil, "which", return_value="/bin/ffmpeg"),
                patch.object(
                    normalize_access_audio,
                    "probe_media_duration_seconds",
                    side_effect=[120.0, 120.0],
                ),
                patch.object(
                    normalize_access_audio,
                    "run_volumedetect",
                    return_value=audio_volume_analysis.VolumeDetectStats(mean_volume=-30.0, max_volume=-14.0),
                ),
                patch.object(normalize_access_audio, "run_fixed_gain_remux") as remux_mock,
                patch.object(normalize_access_audio.sys, "stdin", TtyStringIO("y\n")),
                patch.object(normalize_access_audio.sys, "stdout", io.StringIO()) as stdout_mock,
                patch.object(normalize_access_audio.sys, "stderr", io.StringIO()),
            ):
                code = normalize_access_audio.main(
                    [
                        "--access-copy-dir",
                        str(access_dir),
                        "--audio-dir",
                        str(audio_dir),
                        "--output-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(code, 0)
            self.assertIn("Fixed-gain analysis", stdout_mock.getvalue())
            self.assertIn("Movie.mp4", stdout_mock.getvalue())
            remux_mock.assert_called_once()
            rows = list(csv.DictReader((output_dir / "metadata.csv").read_text(encoding="utf-8").splitlines()))
            self.assertEqual(rows[0]["gain"], "12")
            self.assertEqual(rows[0]["peak_ceiling"], "-1.5")
            self.assertEqual(rows[0]["mean_volume"], "-30.0")
            self.assertEqual(rows[0]["max_volume"], "-14.0")
            self.assertEqual(rows[0]["estimated_post_gain_peak"], "-2.0")
            self.assertEqual(rows[0]["headroom"], "0.5")
            self.assertEqual(rows[0]["status"], "ok")
            self.assertEqual(rows[0]["normalization_type"], "fixed-gain")

    def test_declining_preflight_exits_after_analysis_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            access_dir = Path(root) / "access"
            audio_dir = Path(root) / "audio"
            output_dir = Path(root) / "output"
            access_dir.mkdir()
            audio_dir.mkdir()
            (access_dir / "Movie.mp4").write_bytes(b"video")
            (audio_dir / "Movie.flac").write_bytes(b"audio")

            with (
                patch.object(normalize_access_audio.shutil, "which", return_value="/bin/ffmpeg"),
                patch.object(
                    normalize_access_audio,
                    "probe_media_duration_seconds",
                    side_effect=[120.0, 120.0],
                ),
                patch.object(
                    normalize_access_audio,
                    "run_volumedetect",
                    return_value=audio_volume_analysis.VolumeDetectStats(mean_volume=-30.0, max_volume=-14.0),
                ) as volumedetect_mock,
                patch.object(normalize_access_audio, "run_fixed_gain_remux") as remux_mock,
                patch.object(normalize_access_audio.sys, "stdin", TtyStringIO("n\n")),
                patch.object(normalize_access_audio.sys, "stdout", io.StringIO()) as stdout_mock,
                patch.object(normalize_access_audio.sys, "stderr", io.StringIO()),
            ):
                code = normalize_access_audio.main(
                    [
                        "--access-copy-dir",
                        str(access_dir),
                        "--audio-dir",
                        str(audio_dir),
                        "--output-dir",
                        str(output_dir),
                    ]
                )

        self.assertEqual(code, 0)
        self.assertIn("Proceed with these normalization jobs?", stdout_mock.getvalue())
        volumedetect_mock.assert_called_once()
        remux_mock.assert_not_called()
        self.assertFalse(output_dir.exists())

    def test_unsafe_gain_exits_before_writing_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            access_dir = Path(root) / "access"
            audio_dir = Path(root) / "audio"
            output_dir = Path(root) / "output"
            access_dir.mkdir()
            audio_dir.mkdir()
            (access_dir / "Movie.mp4").write_bytes(b"video")
            (audio_dir / "Movie.flac").write_bytes(b"audio")

            with (
                patch.object(normalize_access_audio.shutil, "which", return_value="/bin/ffmpeg"),
                patch.object(
                    normalize_access_audio,
                    "probe_media_duration_seconds",
                    side_effect=[120.0, 120.0],
                ),
                patch.object(
                    normalize_access_audio,
                    "run_volumedetect",
                    return_value=audio_volume_analysis.VolumeDetectStats(mean_volume=-20.0, max_volume=-10.0),
                ),
                patch.object(normalize_access_audio, "run_fixed_gain_remux") as remux_mock,
                patch.object(normalize_access_audio.sys, "stdout", io.StringIO()) as stdout_mock,
                patch.object(normalize_access_audio.sys, "stderr", io.StringIO()),
            ):
                code = normalize_access_audio.main(
                    [
                        "--access-copy-dir",
                        str(access_dir),
                        "--audio-dir",
                        str(audio_dir),
                        "--output-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(code, 1)
            stdout = stdout_mock.getvalue()
            self.assertIn("Fixed-gain analysis", stdout)
            self.assertIn("exceeds ceiling", stdout)
            self.assertLess(
                stdout.index("Fixed-gain analysis"),
                stdout.index("normalize_access_audio.py: error: fixed gain would exceed peak ceiling"),
            )
            remux_mock.assert_not_called()
            self.assertFalse((output_dir / "metadata.csv").exists())

    def test_yes_bypasses_interactive_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            access_dir = Path(root) / "access"
            audio_dir = Path(root) / "audio"
            output_dir = Path(root) / "output"
            access_dir.mkdir()
            audio_dir.mkdir()
            (access_dir / "Movie.mp4").write_bytes(b"video")
            (audio_dir / "Movie.flac").write_bytes(b"audio")

            with (
                patch.object(normalize_access_audio.shutil, "which", return_value="/bin/ffmpeg"),
                patch.object(
                    normalize_access_audio,
                    "probe_media_duration_seconds",
                    side_effect=[120.0, 120.0],
                ),
                patch.object(
                    normalize_access_audio,
                    "run_volumedetect",
                    return_value=audio_volume_analysis.VolumeDetectStats(mean_volume=-30.0, max_volume=-14.0),
                ),
                patch.object(normalize_access_audio, "run_fixed_gain_remux") as remux_mock,
                patch.object(normalize_access_audio, "confirm_preflight") as preflight_mock,
                patch.object(normalize_access_audio.sys, "stdout", io.StringIO()),
                patch.object(normalize_access_audio.sys, "stderr", io.StringIO()),
            ):
                code = normalize_access_audio.main(
                    [
                        "--access-copy-dir",
                        str(access_dir),
                        "--audio-dir",
                        str(audio_dir),
                        "--output-dir",
                        str(output_dir),
                        "--yes",
                    ]
                )

            self.assertEqual(code, 0)
            preflight_mock.assert_not_called()
            remux_mock.assert_called_once()

    def test_fixed_gain_noninteractive_without_yes_exits_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            access_dir = Path(root) / "access"
            audio_dir = Path(root) / "audio"
            output_dir = Path(root) / "output"
            access_dir.mkdir()
            audio_dir.mkdir()
            (access_dir / "Movie.mp4").write_bytes(b"video")
            (audio_dir / "Movie.flac").write_bytes(b"audio")

            with (
                patch.object(normalize_access_audio.shutil, "which", return_value="/bin/ffmpeg"),
                patch.object(
                    normalize_access_audio,
                    "probe_media_duration_seconds",
                    side_effect=[120.0, 120.0],
                ),
                patch.object(
                    normalize_access_audio,
                    "run_volumedetect",
                    return_value=audio_volume_analysis.VolumeDetectStats(mean_volume=-30.0, max_volume=-14.0),
                ),
                patch.object(normalize_access_audio, "run_fixed_gain_remux") as remux_mock,
                patch.object(normalize_access_audio.sys, "stdin", io.StringIO()),
                patch.object(normalize_access_audio.sys, "stdout", io.StringIO()) as stdout_mock,
                patch.object(normalize_access_audio.sys, "stderr", io.StringIO()),
            ):
                code = normalize_access_audio.main(
                    [
                        "--access-copy-dir",
                        str(access_dir),
                        "--audio-dir",
                        str(audio_dir),
                        "--output-dir",
                        str(output_dir),
                    ]
                )

            self.assertEqual(code, 1)
            self.assertIn("interactive confirmation required", stdout_mock.getvalue())
            remux_mock.assert_not_called()
            self.assertFalse((output_dir / "metadata.csv").exists())

    def test_output_conflict_fails_before_fixed_gain_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            access_dir = Path(root) / "access"
            audio_dir = Path(root) / "audio"
            output_dir = Path(root) / "output"
            access_dir.mkdir()
            audio_dir.mkdir()
            output_dir.mkdir()
            (access_dir / "Movie.mp4").write_bytes(b"video")
            (audio_dir / "Movie.flac").write_bytes(b"audio")
            (output_dir / "Movie.mp4").write_bytes(b"old")

            with (
                patch.object(normalize_access_audio.shutil, "which", return_value="/bin/ffmpeg"),
                patch.object(normalize_access_audio, "probe_media_duration_seconds", return_value=120.0),
                patch.object(normalize_access_audio, "run_volumedetect") as volumedetect_mock,
            ):
                code = normalize_access_audio.main(
                    [
                        "--access-copy-dir",
                        str(access_dir),
                        "--audio-dir",
                        str(audio_dir),
                        "--output-dir",
                        str(output_dir),
                    ]
                )

        self.assertEqual(code, 1)
        volumedetect_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
