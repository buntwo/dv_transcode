from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import normalize_access_audio


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
        self.assertEqual(args.target_lufs, -20.0)
        self.assertEqual(args.true_peak, -1.5)
        self.assertEqual(args.lra, 11.0)
        self.assertEqual(args.audio_bitrate, "192k")
        self.assertEqual(args.duration_tolerance, 0.05)
        self.assertFalse(args.force)

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
        run_mock.assert_not_called()


class TestNormalizeAccessAudioCommands(unittest.TestCase):
    def test_build_pass1_command_maps_first_audio_stream(self) -> None:
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

        self.assertEqual(
            normalize_access_audio.build_pass1_command(Path("/mnt/audio/Movie.flac"), args),
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
                "loudnorm=I=-20:TP=-1.5:LRA=11:print_format=json",
                "-f",
                "null",
                "-",
            ],
        )

    def test_build_pass2_command_uses_loudnorm_2pass_parameters(self) -> None:
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
        pass1 = {
            "input_i": "-23.1",
            "input_tp": "-1.7",
            "input_lra": "8.3",
            "input_thresh": "-31.0",
            "target_offset": "-0.8",
        }

        self.assertEqual(
            normalize_access_audio.build_pass2_command(
                Path("/mnt/access/Movie.mp4"),
                Path("/mnt/audio/Movie.flac"),
                Path("/mnt/output/Movie.mp4"),
                args,
                pass1,
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
                "loudnorm=I=-20:TP=-1.5:LRA=11:measured_I=-23.1:measured_TP=-1.7:measured_LRA=8.3:measured_thresh=-31:offset=-0.8:linear=true:print_format=json",
                "-y",
                "/mnt/output/Movie.mp4",
            ],
        )


class TestNormalizeAccessAudioMetadataCsv(unittest.TestCase):
    def test_metadata_csv_writes_rows_from_loudnorm_json(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            access_dir = Path(root) / "access"
            audio_dir = Path(root) / "audio"
            output_dir = Path(root) / "output"
            access_dir.mkdir()
            audio_dir.mkdir()
            output_dir.mkdir()
            (access_dir / "Movie.mp4").write_bytes(b"video")
            (audio_dir / "Movie.flac").write_bytes(b"audio")

            pass1 = {
                "input_i": "-22.2",
                "input_tp": "-1.0",
                "input_lra": "6.1",
                "input_thresh": "-33.5",
                "target_offset": "-0.7",
            }
            pass2 = {
                "output_i": "-20.0",
                "output_tp": "-1.5",
                "output_lra": "7.2",
                "output_thresh": "-30.8",
                "target_offset": "-0.4",
            }

            with (
                patch.object(normalize_access_audio.shutil, "which", return_value="/bin/ffmpeg"),
                patch.object(
                    normalize_access_audio,
                    "probe_media_duration_seconds",
                    side_effect=[120.0, 120.0],
                ),
                patch.object(normalize_access_audio, "run_pass1", return_value=pass1),
                patch.object(normalize_access_audio, "run_pass2", return_value=pass2),
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
            metadata_path = output_dir / "metadata.csv"
            self.assertTrue(metadata_path.exists())
            rows = list(csv.DictReader(metadata_path.read_text(encoding="utf-8").splitlines()))
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["access_file"], "Movie.mp4")
            self.assertEqual(row["audio_file"], "Movie.flac")
            self.assertEqual(row["output_file"], "Movie.mp4")
            self.assertEqual(row["target_lufs"], "-20")
            self.assertEqual(row["true_peak"], "-1.5")
            self.assertEqual(row["lra"], "11")
            self.assertEqual(row["audio_bitrate"], "192k")
            self.assertEqual(row["input_i"], "-22.2")
            self.assertEqual(row["output_i"], "-20.0")
            self.assertEqual(row["normalization_type"], "loudnorm-two-pass")
            self.assertEqual(row["target_offset"], "-0.4")


if __name__ == "__main__":
    unittest.main()
