from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import contextmanager
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import transcode3  # noqa: E402
import transcode_access  # noqa: E402


def make_config(**overrides) -> transcode3.Config:
    values = {
        "mode": "transcode",
        "validate_duration": True,
        "validate_duration_tolerance": transcode3.DEFAULT_VALIDATE_DURATION_TOLERANCE,
        "format_type": "video8",
        "start": None,
        "end": None,
        "mask_top": 0,
        "mask_bottom": 0,
        "denoise": "off",
        "q": 70,
        "codec": "hevc",
        "encoder": "videotoolbox",
        "preset": None,
        "crf": None,
        "video_filter": None,
        "lut": None,
        "vhs_color_correct": False,
        "deint_mode": "send_field",
        "map_both_audio": False,
        "log_level": "warning",
        "assume_yes": True,
        "no_logs": False,
        "output_suffix": "",
        "originals_dirname": "Originals",
        "access_dirname": "Access",
        "logs_dirname": "Logs",
        "vhs_notch": "auto",
        "audio_channel": "keep",
    }
    values.update(overrides)
    return transcode3.Config(**values)


class TestParseArgs(unittest.TestCase):
    def test_parse_args_supports_validate_duration_mode_and_defaults(self) -> None:
        argv = [
            "transcode3.py",
            "--mode",
            "validate-duration",
            "--format",
            "video8",
            "Originals/set/tape/out.dv",
        ]
        with patch.object(sys, "argv", argv):
            cfg, input_files = transcode3.parse_args()

        self.assertEqual(cfg.mode, "validate-duration")
        self.assertTrue(cfg.validate_duration)
        self.assertEqual(cfg.validate_duration_tolerance, transcode3.DEFAULT_VALIDATE_DURATION_TOLERANCE)
        self.assertEqual(input_files, [Path("Originals/set/tape/out.dv")])

    def test_parse_args_supports_no_validate_duration(self) -> None:
        argv = [
            "transcode3.py",
            "--format",
            "video8",
            "--no-validate-duration",
            "Originals/set/tape/out.dv",
        ]
        with patch.object(sys, "argv", argv):
            cfg, _ = transcode3.parse_args()

        self.assertFalse(cfg.validate_duration)

    def test_parse_args_supports_no_logs(self) -> None:
        argv = [
            "transcode3.py",
            "--format",
            "video8",
            "--no-logs",
            "Originals/set/tape/out.dv",
        ]
        with patch.object(sys, "argv", argv):
            cfg, _ = transcode3.parse_args()

        self.assertTrue(cfg.no_logs)

    def test_parse_args_supports_vhs_defaults(self) -> None:
        argv = [
            "transcode3.py",
            "--format",
            "vhs",
            "Originals/set/tape/out.mkv",
        ]
        with patch.object(sys, "argv", argv):
            cfg, input_files = transcode3.parse_args()

        self.assertEqual(cfg.format_type, "vhs")
        self.assertEqual(cfg.denoise, "verylight")
        self.assertEqual(cfg.mask_top, 3)
        self.assertEqual(cfg.mask_bottom, 12)
        self.assertEqual(cfg.vhs_notch, "auto")
        self.assertEqual(cfg.audio_channel, "keep")
        self.assertEqual(cfg.encoder, "videotoolbox")
        self.assertIsNone(cfg.preset)
        self.assertIsNone(cfg.crf)
        self.assertEqual(input_files, [Path("Originals/set/tape/out.mkv")])

    def test_parse_args_supports_libx265_defaults(self) -> None:
        argv = [
            "transcode3.py",
            "--format",
            "vhs",
            "--encoder",
            "libx265",
            "Originals/set/tape/out.mkv",
        ]
        with patch.object(sys, "argv", argv):
            cfg, _ = transcode3.parse_args()

        self.assertEqual(cfg.encoder, "libx265")
        self.assertEqual(cfg.codec, "hevc")
        self.assertEqual(cfg.preset, "slow")
        self.assertEqual(cfg.crf, 22.0)

    def test_parse_args_supports_non_vhs_libx265_defaults(self) -> None:
        argv = [
            "transcode3.py",
            "--format",
            "video8",
            "--encoder",
            "libx265",
            "Originals/set/tape/out.dv",
        ]
        with patch.object(sys, "argv", argv):
            cfg, _ = transcode3.parse_args()

        self.assertEqual(cfg.preset, "medium")
        self.assertEqual(cfg.crf, 20.0)

    def test_parse_args_supports_libx265_preset_and_crf(self) -> None:
        argv = [
            "transcode3.py",
            "--format",
            "vhs",
            "--encoder",
            "libx265",
            "--preset",
            "slow",
            "--crf",
            "22",
            "Originals/set/tape/out.mkv",
        ]
        with patch.object(sys, "argv", argv):
            cfg, _ = transcode3.parse_args()

        self.assertEqual(cfg.preset, "slow")
        self.assertEqual(cfg.crf, 22.0)

    def test_parse_args_rejects_libx265_options_with_videotoolbox(self) -> None:
        argv = [
            "transcode3.py",
            "--format",
            "vhs",
            "--preset",
            "slow",
            "Originals/set/tape/out.mkv",
        ]
        with patch.object(sys, "argv", argv), self.assertRaises(SystemExit):
            transcode3.parse_args()

    def test_parse_args_rejects_libx265_with_h264_codec(self) -> None:
        argv = [
            "transcode3.py",
            "--format",
            "vhs",
            "--encoder",
            "libx265",
            "--codec",
            "h264",
            "Originals/set/tape/out.mkv",
        ]
        with patch.object(sys, "argv", argv), self.assertRaises(SystemExit):
            transcode3.parse_args()

    def test_help_mentions_out_dv_example(self) -> None:
        argv = ["transcode3.py", "--help"]
        captured = io.StringIO()
        with (
            patch.object(sys, "argv", argv),
            redirect_stdout(captured),
            self.assertRaises(SystemExit),
        ):
            transcode3.parse_args()

        output = captured.getvalue()
        self.assertIn("--mode validate-duration", output)
        self.assertIn("out.dv", output)
        self.assertNotIn("capture.dv", output)


class TestDurationGrouping(unittest.TestCase):
    def test_infer_logical_original_path_maps_parts_and_plain_names(self) -> None:
        part = Path("/tmp/Originals/set/tape/out_partA.dv")
        plain = Path("/tmp/Originals/set/tape/out.dv")

        self.assertEqual(transcode3.infer_logical_original_path(part), plain)
        self.assertEqual(transcode3.infer_logical_original_path(plain), plain)

    def test_build_duration_validation_groups_handles_split_and_unsplit_batches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            split_a = root / "Originals" / "set" / "tape1" / "out_partA.dv"
            split_b = root / "Originals" / "set" / "tape1" / "out_partB.dv"
            plain = root / "Originals" / "set" / "tape2" / "plain.dv"
            other = root / "Originals" / "set" / "tape3" / "other_part1.dv"
            for path in (split_a, split_b, plain, other):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"")

            cfg = make_config()
            groups = transcode3.build_duration_validation_groups(cfg, [split_b, plain, split_a, other])

        self.assertEqual([group.original_file.name for group in groups], ["out.dv", "plain.dv", "other.dv"])
        first = groups[0]
        self.assertEqual([path.name for path in first.input_files], ["out_partA.dv", "out_partB.dv"])
        self.assertEqual(first.original_file.name, "out.dv")
        self.assertEqual([path.suffix for path in first.output_files], [".mp4", ".mp4"])
        self.assertEqual(groups[1].original_file.name, "plain.dv")
        self.assertEqual([path.name for path in groups[1].input_files], ["plain.dv"])

    def test_build_duration_validation_groups_resolves_single_dated_digital8_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_file = root / "Originals" / "set" / "tape1" / "out.dv"
            input_file.parent.mkdir(parents=True, exist_ok=True)
            input_file.write_bytes(b"")

            cfg = make_config(format_type="digital8")
            expected_output = transcode3.build_paths(cfg, input_file).output_file
            dated_output = expected_output.parent / f"20260421_{expected_output.name}"
            dated_output.parent.mkdir(parents=True, exist_ok=True)
            dated_output.write_bytes(b"")

            groups = transcode3.build_duration_validation_groups(cfg, [input_file])

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].output_files, [dated_output.resolve()])
        self.assertEqual(groups[0].output_resolution_errors, [None])

    def test_build_duration_validation_groups_does_not_create_output_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_file = root / "Originals" / "set" / "tape1" / "out.dv"
            input_file.parent.mkdir(parents=True, exist_ok=True)
            input_file.write_bytes(b"")

            cfg = make_config()
            transcode3.build_duration_validation_groups(cfg, [input_file])

            self.assertFalse((root / "Access").exists())
            self.assertFalse((root / "Logs").exists())

    def test_build_paths_preserves_archive_output_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_file = root / "Originals" / "Set 1" / "1 Disney" / "out.dv"
            input_file.parent.mkdir(parents=True, exist_ok=True)
            input_file.write_bytes(b"")

            paths = transcode3.build_paths(make_config(output_suffix="_access"), input_file)

        resolved_root = root.resolve()
        self.assertEqual(paths.out_dir, resolved_root / "Access" / "Set 1" / "1 Disney")
        self.assertEqual(paths.log_dir, resolved_root / "Logs" / "Set 1" / "1 Disney")
        self.assertEqual(paths.output_file, paths.out_dir / "Set_1_1_out_access.mp4")


class TestDurationValidation(unittest.TestCase):
    def test_validate_duration_group_passes_within_tolerance(self) -> None:
        group = transcode3.DurationGroup(
            logical_source=Path("/tmp/out.dv"),
            original_file=Path("/tmp/out.dv"),
            input_files=[Path("/tmp/out_partA.dv"), Path("/tmp/out_partB.dv")],
            output_files=[Path("/tmp/out_partA.mp4"), Path("/tmp/out_partB.mp4")],
        )
        durations = {
            str(group.original_file): 10.0,
            str(group.input_files[0]): 4.9,
            str(group.input_files[1]): 5.0,
            str(group.output_files[0]): 4.8,
            str(group.output_files[1]): 5.1,
        }

        with patch.object(transcode3, "probe_media_duration_seconds", side_effect=lambda path: durations[str(path)]):
            result = transcode3.validate_duration_group(group, tolerance=0.5)

        self.assertTrue(result.passed)
        self.assertAlmostEqual(result.delta_input_vs_output or 0.0, 0.0)

    def test_validate_duration_group_fails_when_original_vs_input_exceeds_tolerance(self) -> None:
        group = transcode3.DurationGroup(
            logical_source=Path("/tmp/out.dv"),
            original_file=Path("/tmp/out.dv"),
            input_files=[Path("/tmp/out_partA.dv"), Path("/tmp/out_partB.dv")],
            output_files=[Path("/tmp/out_partA.mp4"), Path("/tmp/out_partB.mp4")],
        )
        durations = {
            str(group.original_file): 10.0,
            str(group.input_files[0]): 4.0,
            str(group.input_files[1]): 5.0,
            str(group.output_files[0]): 5.0,
            str(group.output_files[1]): 5.0,
        }

        with patch.object(transcode3, "probe_media_duration_seconds", side_effect=lambda path: durations[str(path)]):
            result = transcode3.validate_duration_group(group, tolerance=0.5)

        self.assertFalse(result.passed)
        self.assertIn("input DV total", "\n".join(result.errors))

    def test_validate_duration_group_fails_when_original_vs_mp4_exceeds_tolerance(self) -> None:
        group = transcode3.DurationGroup(
            logical_source=Path("/tmp/out.dv"),
            original_file=Path("/tmp/out.dv"),
            input_files=[Path("/tmp/out.dv")],
            output_files=[Path("/tmp/out.mp4")],
        )
        durations = {
            str(group.original_file): 10.0,
            str(group.input_files[0]): 10.0,
            str(group.output_files[0]): 11.0,
        }

        with patch.object(transcode3, "probe_media_duration_seconds", side_effect=lambda path: durations[str(path)]):
            result = transcode3.validate_duration_group(group, tolerance=0.5)

        self.assertFalse(result.passed)
        self.assertAlmostEqual(result.delta_input_vs_output or 0.0, 1.0)
        self.assertIn("MP4 total", "\n".join(result.errors))

    def test_validate_duration_group_reports_missing_original_and_output(self) -> None:
        group = transcode3.DurationGroup(
            logical_source=Path("/tmp/out.dv"),
            original_file=Path("/tmp/out.dv"),
            input_files=[Path("/tmp/out_partA.dv")],
            output_files=[Path("/tmp/out_partA.mp4")],
        )

        def fake_probe(path: Path) -> float:
            if path == group.input_files[0]:
                return 10.0
            raise RuntimeError(f"missing: {path}")

        with patch.object(transcode3, "probe_media_duration_seconds", side_effect=fake_probe):
            result = transcode3.validate_duration_group(group, tolerance=0.5)

        self.assertFalse(result.passed)
        combined = "\n".join(result.errors)
        self.assertIn("Original DV probe failed", combined)
        self.assertIn("Output MP4 probe failed", combined)

    def test_validate_duration_group_uses_resolved_dated_digital8_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_file = root / "Originals" / "set" / "tape1" / "out.dv"
            input_file.parent.mkdir(parents=True, exist_ok=True)
            input_file.write_bytes(b"")

            cfg = make_config(format_type="digital8")
            expected_output = transcode3.build_paths(cfg, input_file).output_file
            dated_output = expected_output.parent / f"20260421_{expected_output.name}"
            dated_output.parent.mkdir(parents=True, exist_ok=True)
            dated_output.write_bytes(b"")

            group = transcode3.build_duration_validation_groups(cfg, [input_file])[0]
            durations = {
                str(group.original_file): 10.0,
                str(group.input_files[0]): 10.0,
                str(dated_output.resolve()): 10.0,
            }

            with patch.object(transcode3, "probe_media_duration_seconds", side_effect=lambda path: durations[str(path)]):
                result = transcode3.validate_duration_group(group, tolerance=0.5)

        self.assertTrue(result.passed)
        self.assertEqual([row.path for row in result.output_rows], [dated_output.resolve()])

    def test_validate_durations_caches_duplicate_probes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_file = root / "Originals" / "set" / "tape1" / "out.dv"
            input_file.parent.mkdir(parents=True, exist_ok=True)
            input_file.write_bytes(b"")
            output_file = root / "Access" / "set" / "tape1" / "set_tape1_out.mp4"
            output_file.parent.mkdir(parents=True, exist_ok=True)
            output_file.write_bytes(b"")
            cfg = make_config()
            calls: list[Path] = []

            def fake_probe(path: Path) -> float:
                calls.append(path)
                return 10.0

            with (
                patch.object(transcode3, "probe_media_duration_seconds", side_effect=fake_probe),
                redirect_stdout(io.StringIO()),
            ):
                results = transcode3.validate_durations(cfg, [input_file])

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].passed)
        self.assertEqual(len(calls), 2)

    def test_validate_duration_group_reports_missing_digital8_output_when_no_dated_match_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_file = root / "Originals" / "set" / "tape1" / "out.dv"
            input_file.parent.mkdir(parents=True, exist_ok=True)
            input_file.write_bytes(b"")

            cfg = make_config(format_type="digital8")
            group = transcode3.build_duration_validation_groups(cfg, [input_file])[0]

            def fake_probe(path: Path) -> float:
                if path in (group.original_file, group.input_files[0]):
                    return 10.0
                raise RuntimeError(f"missing: {path}")

            with patch.object(transcode3, "probe_media_duration_seconds", side_effect=fake_probe):
                result = transcode3.validate_duration_group(group, tolerance=0.5)

        self.assertFalse(result.passed)
        combined = "\n".join(result.errors)
        self.assertIn("Output MP4 probe failed", combined)
        self.assertIn("no dated Digital8 match found", combined)
        self.assertIn("set_tape1_out.mp4", combined)

    def test_validate_duration_group_reports_ambiguous_digital8_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_file = root / "Originals" / "set" / "tape1" / "out.dv"
            input_file.parent.mkdir(parents=True, exist_ok=True)
            input_file.write_bytes(b"")

            cfg = make_config(format_type="digital8")
            expected_output = transcode3.build_paths(cfg, input_file).output_file
            output_dir = expected_output.parent
            output_dir.mkdir(parents=True, exist_ok=True)
            first = output_dir / f"20260421_{expected_output.name}"
            second = output_dir / f"20260422_{expected_output.name}"
            first.write_bytes(b"")
            second.write_bytes(b"")

            group = transcode3.build_duration_validation_groups(cfg, [input_file])[0]

            with patch.object(transcode3, "probe_media_duration_seconds", side_effect=lambda path: 10.0):
                result = transcode3.validate_duration_group(group, tolerance=0.5)

        self.assertFalse(result.passed)
        combined = "\n".join(result.errors)
        self.assertIn("ambiguous dated Digital8 matches", combined)
        self.assertIn(first.name, combined)
        self.assertIn(second.name, combined)

    def test_print_duration_validation_result_includes_sections_totals_and_status(self) -> None:
        result = transcode3.DurationValidationResult(
            group=transcode3.DurationGroup(
                logical_source=Path("/tmp/out.dv"),
                original_file=Path("/tmp/out.dv"),
                input_files=[Path("/tmp/out_partA.dv"), Path("/tmp/out_partB.dv")],
                output_files=[Path("/tmp/out_partA.mp4"), Path("/tmp/out_partB.mp4")],
            ),
            original_row=transcode3.DurationRow(Path("/tmp/out.dv"), 10.0),
            input_rows=[
                transcode3.DurationRow(Path("/tmp/out_partA.dv"), 5.0),
                transcode3.DurationRow(Path("/tmp/out_partB.dv"), 5.0),
            ],
            output_rows=[
                transcode3.DurationRow(Path("/tmp/out_partA.mp4"), 4.9),
                transcode3.DurationRow(Path("/tmp/out_partB.mp4"), 5.0),
            ],
            original_total=10.0,
            input_total=10.0,
            output_total=9.9,
            delta_original_vs_input=0.0,
            delta_original_vs_output=0.1,
            delta_input_vs_output=0.1,
            tolerance=0.5,
            errors=[],
        )
        captured = io.StringIO()

        with redirect_stdout(captured):
            transcode3.print_duration_validation_result(result)

        output = captured.getvalue()
        self.assertIn("Original DV", output)
        self.assertIn("Input DVs", output)
        self.assertIn("Output MP4s", output)
        header_line = next(line for line in output.splitlines() if "Original DV" in line and "Input DVs" in line)
        self.assertIn("Output MP4s", header_line)
        self.assertIn("out.dv", output)
        self.assertIn("out_partA.dv", output)
        self.assertIn("out_partA.mp4", output)
        self.assertGreaterEqual(output.count("TOTAL"), 2)
        self.assertNotIn("Original DV total", output)
        self.assertNotIn("Input DV total", output)
        self.assertNotIn("MP4 total", output)
        self.assertIn("Delta input vs mp4", output)
        self.assertIn("PASS", output)


class TestGenerateDigital8Sidecars(unittest.TestCase):
    def test_dvrescue_uses_dash_merge_and_writes_csv_from_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            add_play_time_script = root / "add_play_time_columns.py"
            create_srt_script = root / "create_srt.py"
            add_play_time_script.write_text("", encoding="utf-8")
            create_srt_script.write_text("", encoding="utf-8")

            paths = transcode3.Paths(
                input_file=root / "input.dv",
                stem="input",
                out_dir=root,
                log_dir=root,
                output_file=root / "output.mp4",
                ffmpeg_log_file=root / "ffmpeg.log",
                command_log_file=root / "cmd.log",
                csv_raw=root / "input.frameinfo.csv",
                csv_with_play=root / "input.frameinfo.with_play_time.csv",
                srt_file=root / "input.record_time_overlay.srt",
                add_play_time_script=add_play_time_script,
                create_srt_script=create_srt_script,
            )
            paths.input_file.write_bytes(b"")

            calls: list[tuple[list[str], Path | None, Path | None, object | None]] = []

            def fake_run_checked(
                args: list[str],
                stdout_path: Path | None = None,
                stderr_path: Path | None = None,
                stdout=None,
            ) -> None:
                calls.append((args, stdout_path, stderr_path, stdout))
                if stderr_path is not None:
                    stderr_path.write_text("FramePos,rdt\n0,2026-04-21 12:00:00.000\n", encoding="utf-8")

            with patch.object(transcode3, "run_checked", side_effect=fake_run_checked):
                transcode3.generate_digital8_sidecars(paths)

            self.assertEqual(len(calls), 1)
            dvrescue_args, dvrescue_stdout, dvrescue_stderr, dvrescue_stdout_dest = calls[0]
            self.assertEqual(dvrescue_args[:2], ["dvrescue", "--csv"])
            self.assertEqual(dvrescue_args[-2:], ["-m", "-"])
            self.assertIsNone(dvrescue_stdout)
            self.assertEqual(dvrescue_stderr, paths.csv_raw)
            self.assertEqual(dvrescue_stdout_dest, transcode3.subprocess.DEVNULL)
            self.assertIn("play_time_seconds", paths.csv_with_play.read_text(encoding="utf-8"))
            self.assertIn("2026-04-21 12:00:00", paths.srt_file.read_text(encoding="utf-8"))

    def test_digital8_sidecar_date_prefix_still_updates_output_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            add_play_time_script = root / "add_play_time_columns.py"
            create_srt_script = root / "create_srt.py"
            add_play_time_script.write_text("", encoding="utf-8")
            create_srt_script.write_text("", encoding="utf-8")

            paths = transcode3.Paths(
                input_file=root / "input.dv",
                stem="input",
                out_dir=root,
                log_dir=root,
                output_file=root / "output.mp4",
                ffmpeg_log_file=root / "ffmpeg.log",
                command_log_file=root / "cmd.log",
                csv_raw=root / "input.frameinfo.csv",
                csv_with_play=root / "input.frameinfo.with_play_time.csv",
                srt_file=root / "input.record_time_overlay.srt",
                add_play_time_script=add_play_time_script,
                create_srt_script=create_srt_script,
            )
            paths.input_file.write_bytes(b"")

            def fake_run_checked(*args, **kwargs) -> None:
                paths.csv_raw.write_text("FramePos,rdt\n0,2026-04-21 12:00:00.000\n", encoding="utf-8")

            with patch.object(transcode3, "run_checked", side_effect=fake_run_checked):
                transcode3.generate_digital8_sidecars(paths)

            self.assertEqual(paths.output_file, root / "20260421_output.mp4")


class TestFiltersAndArgs(unittest.TestCase):
    def test_limited_range_tagging_omits_hardcoded_smpte_color_metadata(self) -> None:
        paths = transcode3.Paths(
            input_file=Path("/tmp/input.mkv"),
            stem="input",
            out_dir=Path("/tmp/Access"),
            log_dir=Path("/tmp/Logs"),
            output_file=Path("/tmp/Access/input.mp4"),
            ffmpeg_log_file=Path("/tmp/Logs/input.log"),
            command_log_file=Path("/tmp/Logs/input.cmd.log"),
            csv_raw=Path("/tmp/Logs/input.csv"),
            csv_with_play=Path("/tmp/Logs/input.with_play.csv"),
            srt_file=Path("/tmp/Logs/input.srt"),
            add_play_time_script=Path("/tmp/add_play_time_columns.py"),
            create_srt_script=Path("/tmp/create_srt.py"),
        )
        cfg = make_config(format_type="vhs", denoise="verylight")

        vf = transcode3.build_vf(cfg, paths)
        with patch.object(transcode3, "probe_video_standard", return_value="ntsc"):
            args = transcode3.build_ffmpeg_args(cfg, paths, vf, preview=False)

        self.assertIn("bwdif=mode=send_field:parity=auto:deint=all", vf)
        self.assertIn("setparams=range=limited", vf)
        self.assertIn("scale=trunc(ih*dar/2)*2:ih:flags=lanczos+accurate_rnd+full_chroma_int", vf)
        self.assertNotIn("color_primaries", vf)
        self.assertNotIn("color_trc", vf)
        self.assertNotIn("colorspace", vf)
        self.assertIn("-color_range", args)
        self.assertIn("tv", args)
        self.assertNotIn("-color_primaries", args)
        self.assertNotIn("-color_trc", args)
        self.assertNotIn("-colorspace", args)

    def test_videotoolbox_args_use_quality_options(self) -> None:
        paths = transcode3.Paths(
            input_file=Path("/tmp/input.mkv"),
            stem="input",
            out_dir=Path("/tmp/Access"),
            log_dir=Path("/tmp/Logs"),
            output_file=Path("/tmp/Access/input.mp4"),
            ffmpeg_log_file=Path("/tmp/Logs/input.log"),
            command_log_file=Path("/tmp/Logs/input.cmd.log"),
            csv_raw=Path("/tmp/Logs/input.csv"),
            csv_with_play=Path("/tmp/Logs/input.with_play.csv"),
            srt_file=Path("/tmp/Logs/input.srt"),
            add_play_time_script=Path("/tmp/add_play_time_columns.py"),
            create_srt_script=Path("/tmp/create_srt.py"),
        )

        args = transcode3.build_ffmpeg_args(make_config(codec="hevc"), paths, "null", preview=False)

        self.assertIn("hevc_videotoolbox", args)
        self.assertIn("-spatial_aq", args)
        self.assertIn("-max_ref_frames", args)
        self.assertIn("-q:v", args)
        self.assertIn("-g", args)
        self.assertIn("60", args)
        self.assertNotIn("libx265", args)
        self.assertNotIn("-x265-params", args)
        self.assertNotIn("-crf", args)
        self.assertNotIn("-preset", args)

    def test_yes_adds_ffmpeg_overwrite_for_transcode(self) -> None:
        paths = transcode3.Paths(
            input_file=Path("/tmp/input.mkv"),
            stem="input",
            out_dir=Path("/tmp/Access"),
            log_dir=Path("/tmp/Logs"),
            output_file=Path("/tmp/Access/input.mp4"),
            ffmpeg_log_file=Path("/tmp/Logs/input.log"),
            command_log_file=Path("/tmp/Logs/input.cmd.log"),
            csv_raw=Path("/tmp/Logs/input.csv"),
            csv_with_play=Path("/tmp/Logs/input.with_play.csv"),
            srt_file=Path("/tmp/Logs/input.srt"),
            add_play_time_script=Path("/tmp/add_play_time_columns.py"),
            create_srt_script=Path("/tmp/create_srt.py"),
        )

        yes_args = transcode3.build_ffmpeg_args(make_config(assume_yes=True), paths, "null", preview=False)
        interactive_args = transcode3.build_ffmpeg_args(make_config(assume_yes=False), paths, "null", preview=False)

        self.assertIn("-y", yes_args)
        self.assertNotIn("-y", interactive_args)

    def test_yes_does_not_add_overwrite_for_preview(self) -> None:
        paths = transcode3.Paths(
            input_file=Path("/tmp/input.mkv"),
            stem="input",
            out_dir=Path("/tmp/Access"),
            log_dir=Path("/tmp/Logs"),
            output_file=Path("/tmp/Access/input.mp4"),
            ffmpeg_log_file=Path("/tmp/Logs/input.log"),
            command_log_file=Path("/tmp/Logs/input.cmd.log"),
            csv_raw=Path("/tmp/Logs/input.csv"),
            csv_with_play=Path("/tmp/Logs/input.with_play.csv"),
            srt_file=Path("/tmp/Logs/input.srt"),
            add_play_time_script=Path("/tmp/add_play_time_columns.py"),
            create_srt_script=Path("/tmp/create_srt.py"),
        )

        args = transcode3.build_ffmpeg_args(make_config(assume_yes=True), paths, "null", preview=True)

        self.assertNotIn("-y", args)

    def test_libx265_args_use_preset_crf_and_apple_hevc_tag(self) -> None:
        paths = transcode3.Paths(
            input_file=Path("/tmp/input.mkv"),
            stem="input",
            out_dir=Path("/tmp/Access"),
            log_dir=Path("/tmp/Logs"),
            output_file=Path("/tmp/Access/input.mp4"),
            ffmpeg_log_file=Path("/tmp/Logs/input.log"),
            command_log_file=Path("/tmp/Logs/input.cmd.log"),
            csv_raw=Path("/tmp/Logs/input.csv"),
            csv_with_play=Path("/tmp/Logs/input.with_play.csv"),
            srt_file=Path("/tmp/Logs/input.srt"),
            add_play_time_script=Path("/tmp/add_play_time_columns.py"),
            create_srt_script=Path("/tmp/create_srt.py"),
        )

        args = transcode3.build_ffmpeg_args(
            make_config(format_type="vhs", encoder="libx265", preset="slow", crf=22.0, vhs_notch="off"),
            paths,
            "null",
            preview=False,
        )

        self.assertIn("libx265", args)
        self.assertIn("-preset", args)
        self.assertIn("slow", args)
        self.assertIn("-crf", args)
        self.assertIn("22", args)
        self.assertIn("-profile:v", args)
        self.assertIn("main10", args)
        self.assertIn("-pix_fmt", args)
        self.assertIn("yuv420p10le", args)
        self.assertIn("-tag:v", args)
        self.assertIn("hvc1", args)
        self.assertIn("-g", args)
        self.assertIn("60", args)
        self.assertIn("-movflags", args)
        self.assertIn("+faststart", args)
        self.assertIn("-x265-params", args)
        self.assertIn("aq-mode=3:aq-strength=0.8:psy-rd=2.0:psy-rdoq=1.0", args)
        self.assertNotIn("-spatial_aq", args)
        self.assertNotIn("-max_ref_frames", args)
        self.assertNotIn("-q:v", args)

    def test_libx265_vhs_params_are_not_used_for_non_vhs_formats(self) -> None:
        paths = transcode3.Paths(
            input_file=Path("/tmp/input.mkv"),
            stem="input",
            out_dir=Path("/tmp/Access"),
            log_dir=Path("/tmp/Logs"),
            output_file=Path("/tmp/Access/input.mp4"),
            ffmpeg_log_file=Path("/tmp/Logs/input.log"),
            command_log_file=Path("/tmp/Logs/input.cmd.log"),
            csv_raw=Path("/tmp/Logs/input.csv"),
            csv_with_play=Path("/tmp/Logs/input.with_play.csv"),
            srt_file=Path("/tmp/Logs/input.srt"),
            add_play_time_script=Path("/tmp/add_play_time_columns.py"),
            create_srt_script=Path("/tmp/create_srt.py"),
        )

        args = transcode3.build_ffmpeg_args(
            make_config(format_type="video8", encoder="libx265", preset="medium", crf=20.0),
            paths,
            "null",
            preview=False,
        )

        self.assertIn("libx265", args)
        self.assertNotIn("-x265-params", args)

    def test_vhs_audio_notch_auto_uses_detected_ntsc_frequency(self) -> None:
        paths = transcode3.Paths(
            input_file=Path("/tmp/input.mkv"),
            stem="input",
            out_dir=Path("/tmp/Access"),
            log_dir=Path("/tmp/Logs"),
            output_file=Path("/tmp/Access/input.mp4"),
            ffmpeg_log_file=Path("/tmp/Logs/input.log"),
            command_log_file=Path("/tmp/Logs/input.cmd.log"),
            csv_raw=Path("/tmp/Logs/input.csv"),
            csv_with_play=Path("/tmp/Logs/input.with_play.csv"),
            srt_file=Path("/tmp/Logs/input.srt"),
            add_play_time_script=Path("/tmp/add_play_time_columns.py"),
            create_srt_script=Path("/tmp/create_srt.py"),
        )
        cfg = make_config(format_type="vhs", vhs_notch="auto")

        with patch.object(transcode3, "probe_video_standard", return_value="ntsc") as mock_probe:
            args = transcode3.build_ffmpeg_args(cfg, paths, "null", preview=False)

        mock_probe.assert_called_once_with(paths.input_file)
        self.assertIn("-af", args)
        self.assertIn("highpass=f=60:p=1,equalizer=f=15734:width_type=q:width=30:g=-24", args)

    def test_audio_channel_can_copy_left_or_right_to_stereo(self) -> None:
        paths = transcode3.Paths(
            input_file=Path("/tmp/input.mkv"),
            stem="input",
            out_dir=Path("/tmp/Access"),
            log_dir=Path("/tmp/Logs"),
            output_file=Path("/tmp/Access/input.mp4"),
            ffmpeg_log_file=Path("/tmp/Logs/input.log"),
            command_log_file=Path("/tmp/Logs/input.cmd.log"),
            csv_raw=Path("/tmp/Logs/input.csv"),
            csv_with_play=Path("/tmp/Logs/input.with_play.csv"),
            srt_file=Path("/tmp/Logs/input.srt"),
            add_play_time_script=Path("/tmp/add_play_time_columns.py"),
            create_srt_script=Path("/tmp/create_srt.py"),
        )

        left_args = transcode3.build_ffmpeg_args(
            make_config(format_type="video8", audio_channel="left"),
            paths,
            "null",
            preview=False,
        )
        right_args = transcode3.build_ffmpeg_args(
            make_config(format_type="video8", audio_channel="right"),
            paths,
            "null",
            preview=False,
        )

        self.assertIn("pan=stereo|c0=c0|c1=c0", left_args)
        self.assertIn("pan=stereo|c0=c1|c1=c1", right_args)

    def test_audio_channel_copy_composes_with_vhs_notch_filter(self) -> None:
        paths = transcode3.Paths(
            input_file=Path("/tmp/input.mkv"),
            stem="input",
            out_dir=Path("/tmp/Access"),
            log_dir=Path("/tmp/Logs"),
            output_file=Path("/tmp/Access/input.mp4"),
            ffmpeg_log_file=Path("/tmp/Logs/input.log"),
            command_log_file=Path("/tmp/Logs/input.cmd.log"),
            csv_raw=Path("/tmp/Logs/input.csv"),
            csv_with_play=Path("/tmp/Logs/input.with_play.csv"),
            srt_file=Path("/tmp/Logs/input.srt"),
            add_play_time_script=Path("/tmp/add_play_time_columns.py"),
            create_srt_script=Path("/tmp/create_srt.py"),
        )

        args = transcode3.build_ffmpeg_args(
            make_config(format_type="vhs", vhs_notch="ntsc", audio_channel="left"),
            paths,
            "null",
            preview=False,
        )

        self.assertIn(
            "highpass=f=60:p=1,equalizer=f=15734:width_type=q:width=30:g=-24,pan=stereo|c0=c0|c1=c0",
            args,
        )

    def test_vhs_audio_notch_can_force_pal_frequency_or_disable(self) -> None:
        paths = transcode3.Paths(
            input_file=Path("/tmp/input.mkv"),
            stem="input",
            out_dir=Path("/tmp/Access"),
            log_dir=Path("/tmp/Logs"),
            output_file=Path("/tmp/Access/input.mp4"),
            ffmpeg_log_file=Path("/tmp/Logs/input.log"),
            command_log_file=Path("/tmp/Logs/input.cmd.log"),
            csv_raw=Path("/tmp/Logs/input.csv"),
            csv_with_play=Path("/tmp/Logs/input.with_play.csv"),
            srt_file=Path("/tmp/Logs/input.srt"),
            add_play_time_script=Path("/tmp/add_play_time_columns.py"),
            create_srt_script=Path("/tmp/create_srt.py"),
        )

        pal_args = transcode3.build_ffmpeg_args(make_config(format_type="vhs", vhs_notch="pal"), paths, "null", preview=False)
        off_args = transcode3.build_ffmpeg_args(make_config(format_type="vhs", vhs_notch="off"), paths, "null", preview=False)

        self.assertIn("highpass=f=60:p=1,equalizer=f=15625:width_type=q:width=30:g=-24", pal_args)
        self.assertNotIn("-af", off_args)

    def test_probe_video_standard_classifies_ntsc_and_pal_metadata(self) -> None:
        self.assertEqual(transcode3.classify_video_standard(720, 480, 29.97002997002997), "ntsc")
        self.assertEqual(transcode3.classify_video_standard(720, 576, 25.0), "pal")
        self.assertAlmostEqual(transcode3.parse_frame_rate("30000/1001") or 0.0, 29.97002997002997)

    def test_mask_top_and_bottom_crop_before_denoise_and_pad_before_scale(self) -> None:
        paths = transcode3.Paths(
            input_file=Path("/tmp/input.mkv"),
            stem="input",
            out_dir=Path("/tmp/Access"),
            log_dir=Path("/tmp/Logs"),
            output_file=Path("/tmp/Access/input.mp4"),
            ffmpeg_log_file=Path("/tmp/Logs/input.log"),
            command_log_file=Path("/tmp/Logs/input.cmd.log"),
            csv_raw=Path("/tmp/Logs/input.csv"),
            csv_with_play=Path("/tmp/Logs/input.with_play.csv"),
            srt_file=Path("/tmp/Logs/input.srt"),
            add_play_time_script=Path("/tmp/add_play_time_columns.py"),
            create_srt_script=Path("/tmp/create_srt.py"),
        )

        vf = transcode3.build_vf(make_config(mask_top=6, mask_bottom=8), paths)

        crop_filter = "crop=w=iw:h=ih-14:x=0:y=6"
        pad_filter = "pad=w=iw:h=ih+14:x=0:y=6:color=black"
        self.assertIn(crop_filter, vf)
        self.assertIn(pad_filter, vf)
        self.assertLess(vf.index("bwdif="), vf.index(crop_filter))
        self.assertLess(vf.index(crop_filter), vf.index(transcode3.SCALE_FILTER))
        self.assertLess(vf.index(pad_filter), vf.index(transcode3.SCALE_FILTER))
        self.assertNotIn("drawbox=", vf)

    def test_crop_pad_masking_applies_to_video8_defaults_too(self) -> None:
        paths = transcode3.Paths(
            input_file=Path("/tmp/input.dv"),
            stem="input",
            out_dir=Path("/tmp/Access"),
            log_dir=Path("/tmp/Logs"),
            output_file=Path("/tmp/Access/input.mp4"),
            ffmpeg_log_file=Path("/tmp/Logs/input.log"),
            command_log_file=Path("/tmp/Logs/input.cmd.log"),
            csv_raw=Path("/tmp/Logs/input.csv"),
            csv_with_play=Path("/tmp/Logs/input.with_play.csv"),
            srt_file=Path("/tmp/Logs/input.srt"),
            add_play_time_script=Path("/tmp/add_play_time_columns.py"),
            create_srt_script=Path("/tmp/create_srt.py"),
        )

        vf = transcode3.build_vf(make_config(format_type="video8", mask_top=0, mask_bottom=7), paths)

        self.assertIn("crop=w=iw:h=ih-7:x=0:y=0", vf)
        self.assertIn("pad=w=iw:h=ih+7:x=0:y=0:color=black", vf)
        self.assertNotIn("drawbox=", vf)

    def test_libx265_filter_chain_ends_with_10bit_format(self) -> None:
        paths = transcode3.Paths(
            input_file=Path("/tmp/input.mkv"),
            stem="input",
            out_dir=Path("/tmp/Access"),
            log_dir=Path("/tmp/Logs"),
            output_file=Path("/tmp/Access/input.mp4"),
            ffmpeg_log_file=Path("/tmp/Logs/input.log"),
            command_log_file=Path("/tmp/Logs/input.cmd.log"),
            csv_raw=Path("/tmp/Logs/input.csv"),
            csv_with_play=Path("/tmp/Logs/input.with_play.csv"),
            srt_file=Path("/tmp/Logs/input.srt"),
            add_play_time_script=Path("/tmp/add_play_time_columns.py"),
            create_srt_script=Path("/tmp/create_srt.py"),
        )

        vf = transcode3.build_vf(make_config(encoder="libx265"), paths)

        self.assertTrue(vf.endswith(",format=yuv420p10le"))

    def test_videotoolbox_filter_chain_does_not_force_10bit_format(self) -> None:
        paths = transcode3.Paths(
            input_file=Path("/tmp/input.mkv"),
            stem="input",
            out_dir=Path("/tmp/Access"),
            log_dir=Path("/tmp/Logs"),
            output_file=Path("/tmp/Access/input.mp4"),
            ffmpeg_log_file=Path("/tmp/Logs/input.log"),
            command_log_file=Path("/tmp/Logs/input.cmd.log"),
            csv_raw=Path("/tmp/Logs/input.csv"),
            csv_with_play=Path("/tmp/Logs/input.with_play.csv"),
            srt_file=Path("/tmp/Logs/input.srt"),
            add_play_time_script=Path("/tmp/add_play_time_columns.py"),
            create_srt_script=Path("/tmp/create_srt.py"),
        )

        vf = transcode3.build_vf(make_config(encoder="videotoolbox"), paths)

        self.assertNotIn("format=yuv420p10le", vf)

    def test_lut_adds_lut3d_stage_before_subtitles_and_final_format(self) -> None:
        paths = transcode3.Paths(
            input_file=Path("/tmp/input.mkv"),
            stem="input",
            out_dir=Path("/tmp/Access"),
            log_dir=Path("/tmp/Logs"),
            output_file=Path("/tmp/Access/input.mp4"),
            ffmpeg_log_file=Path("/tmp/Logs/input.log"),
            command_log_file=Path("/tmp/Logs/input.cmd.log"),
            csv_raw=Path("/tmp/Logs/input.csv"),
            csv_with_play=Path("/tmp/Logs/input.with_play.csv"),
            srt_file=Path("/tmp/Logs/input.srt"),
            add_play_time_script=Path("/tmp/add_play_time_columns.py"),
            create_srt_script=Path("/tmp/create_srt.py"),
        )
        lut = Path("/tmp/general_vhs_to_video8_strength85.cube")

        vf = transcode3.build_vf(make_config(format_type="digital8", encoder="libx265", lut=lut), paths)

        lut_stage = "lut3d=/tmp/general_vhs_to_video8_strength85.cube:interp=tetrahedral"
        self.assertIn(lut_stage, vf)
        self.assertLess(vf.index("setparams=range=limited"), vf.index(lut_stage))
        self.assertLess(vf.index(lut_stage), vf.index("subtitles="))
        self.assertLess(vf.index("subtitles="), vf.index("format=yuv420p10le"))

    def test_vhs_color_correction_inserts_before_scale(self) -> None:
        paths = transcode3.Paths(
            input_file=Path("/tmp/input.mkv"),
            stem="input",
            out_dir=Path("/tmp/Access"),
            log_dir=Path("/tmp/Logs"),
            output_file=Path("/tmp/Access/input.mp4"),
            ffmpeg_log_file=Path("/tmp/Logs/input.log"),
            command_log_file=Path("/tmp/Logs/input.cmd.log"),
            csv_raw=Path("/tmp/Logs/input.csv"),
            csv_with_play=Path("/tmp/Logs/input.with_play.csv"),
            srt_file=Path("/tmp/Logs/input.srt"),
            add_play_time_script=Path("/tmp/add_play_time_columns.py"),
            create_srt_script=Path("/tmp/create_srt.py"),
        )

        vf = transcode3.build_vf(
            make_config(format_type="vhs", denoise="verylight", vhs_color_correct=True),
            paths,
        )

        self.assertIn(transcode3.VHS_COLOR_CORRECTION_FILTER, vf)
        self.assertLess(vf.index("hqdn3d="), vf.index(transcode3.VHS_COLOR_CORRECTION_FILTER))
        self.assertLess(vf.index(transcode3.VHS_COLOR_CORRECTION_FILTER), vf.index(transcode3.SCALE_FILTER))

    def test_custom_vf_replaces_generated_filter_chain_exactly(self) -> None:
        paths = transcode3.Paths(
            input_file=Path("/tmp/input.mkv"),
            stem="input",
            out_dir=Path("/tmp/Access"),
            log_dir=Path("/tmp/Logs"),
            output_file=Path("/tmp/Access/input.mp4"),
            ffmpeg_log_file=Path("/tmp/Logs/input.log"),
            command_log_file=Path("/tmp/Logs/input.cmd.log"),
            csv_raw=Path("/tmp/Logs/input.csv"),
            csv_with_play=Path("/tmp/Logs/input.with_play.csv"),
            srt_file=Path("/tmp/Logs/input.srt"),
            add_play_time_script=Path("/tmp/add_play_time_columns.py"),
            create_srt_script=Path("/tmp/create_srt.py"),
        )
        custom_vf = "bwdif=mode=send_frame,scale=640:-2,format=yuv420p"
        cfg = make_config(
            format_type="digital8",
            encoder="libx265",
            mask_top=8,
            mask_bottom=8,
            denoise="strong",
            video_filter=custom_vf,
        )

        vf = transcode3.build_vf(cfg, paths)
        args = transcode3.build_ffmpeg_args(cfg, paths, vf, preview=False)

        self.assertEqual(vf, custom_vf)
        self.assertEqual(args[args.index("-vf") + 1], custom_vf)
        self.assertNotIn("drawbox", vf)
        self.assertNotIn("hqdn3d", vf)
        self.assertNotIn("subtitles=", vf)
        self.assertNotIn("yuv420p10le", vf)


class TestTranscodeAccess(unittest.TestCase):
    def test_access_parse_args_sets_vhs_defaults_and_access_layout(self) -> None:
        argv = ["transcode_access.py", "--format", "vhs", "masters/tape/08.mkv"]
        with patch.object(sys, "argv", argv):
            cfg, input_files = transcode_access.parse_args()

        self.assertEqual(cfg.layout, "access")
        self.assertEqual(cfg.format_type, "vhs")
        self.assertEqual(cfg.denoise, "verylight")
        self.assertEqual(cfg.mask_top, 3)
        self.assertEqual(cfg.mask_bottom, 12)
        self.assertEqual(cfg.vhs_notch, "auto")
        self.assertEqual(cfg.audio_channel, "keep")
        self.assertEqual(input_files, [Path("masters/tape/08.mkv")])

    def test_access_parse_args_can_override_vhs_mask_defaults(self) -> None:
        argv = [
            "transcode_access.py",
            "--format",
            "vhs",
            "--mask-top",
            "0",
            "--mask-bottom",
            "0",
            "masters/tape/08.mkv",
        ]
        with patch.object(sys, "argv", argv):
            cfg, _ = transcode_access.parse_args()

        self.assertEqual(cfg.mask_top, 0)
        self.assertEqual(cfg.mask_bottom, 0)

    def test_access_parse_args_supports_audio_channel_copy(self) -> None:
        argv = [
            "transcode_access.py",
            "--format",
            "vhs",
            "--audio-channel",
            "right",
            "masters/tape/08.mkv",
        ]
        with patch.object(sys, "argv", argv):
            cfg, _ = transcode_access.parse_args()

        self.assertEqual(cfg.audio_channel, "right")

    def test_access_parse_args_supports_no_logs(self) -> None:
        argv = [
            "transcode_access.py",
            "--format",
            "vhs",
            "--no-logs",
            "masters/tape/08.mkv",
        ]
        with patch.object(sys, "argv", argv):
            cfg, _ = transcode_access.parse_args()

        self.assertTrue(cfg.no_logs)

    def test_access_parse_args_supports_custom_vf(self) -> None:
        argv = [
            "transcode_access.py",
            "--format",
            "vhs",
            "--vf",
            "bwdif=mode=send_frame,scale=640:-2",
            "masters/tape/08.mkv",
        ]
        with patch.object(sys, "argv", argv):
            cfg, _ = transcode_access.parse_args()

        self.assertEqual(cfg.video_filter, "bwdif=mode=send_frame,scale=640:-2")

    def test_access_parse_args_supports_lut(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lut = Path(tmp) / "general_vhs_to_video8_strength85.cube"
            lut.write_text("", encoding="utf-8")
            argv = [
                "transcode_access.py",
                "--format",
                "vhs",
                "--lut",
                str(lut),
                "masters/tape/08.mkv",
            ]
            with patch.object(sys, "argv", argv):
                cfg, _ = transcode_access.parse_args()

        self.assertEqual(cfg.lut, lut)

    def test_access_parse_args_supports_vhs_color_correction(self) -> None:
        argv = [
            "transcode_access.py",
            "--format",
            "vhs",
            "--vhs-color-correct",
            "masters/tape/08.mkv",
        ]
        with patch.object(sys, "argv", argv):
            cfg, _ = transcode_access.parse_args()

        self.assertTrue(cfg.vhs_color_correct)

    def test_access_parse_args_rejects_vhs_color_correction_for_non_vhs(self) -> None:
        argv = [
            "transcode_access.py",
            "--format",
            "video8",
            "--vhs-color-correct",
            "masters/tape/08.dv",
        ]
        with patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit):
                transcode_access.parse_args()

    def test_access_parse_args_rejects_blank_custom_vf(self) -> None:
        argv = [
            "transcode_access.py",
            "--format",
            "vhs",
            "--vf",
            " ",
            "masters/tape/08.mkv",
        ]
        with patch.object(sys, "argv", argv), self.assertRaises(SystemExit):
            transcode_access.parse_args()

    def test_access_parse_args_rejects_missing_lut(self) -> None:
        argv = [
            "transcode_access.py",
            "--format",
            "vhs",
            "--lut",
            "/tmp/missing.cube",
            "masters/tape/08.mkv",
        ]
        with patch.object(sys, "argv", argv), self.assertRaises(SystemExit):
            transcode_access.parse_args()

    def test_access_parse_args_rejects_lut_with_custom_vf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            lut = Path(tmp) / "general_vhs_to_video8_strength85.cube"
            lut.write_text("", encoding="utf-8")
            argv = [
                "transcode_access.py",
                "--format",
                "vhs",
                "--vf",
                "bwdif",
                "--lut",
                str(lut),
                "masters/tape/08.mkv",
            ]
            with patch.object(sys, "argv", argv), self.assertRaises(SystemExit):
                transcode_access.parse_args()

    def test_access_parse_args_supports_libx265_defaults(self) -> None:
        argv = [
            "transcode_access.py",
            "--format",
            "vhs",
            "--encoder",
            "libx265",
            "masters/tape/08.mkv",
        ]
        with patch.object(sys, "argv", argv):
            cfg, _ = transcode_access.parse_args()

        self.assertEqual(cfg.encoder, "libx265")
        self.assertEqual(cfg.preset, "slow")
        self.assertEqual(cfg.crf, 22.0)

    def test_access_parse_args_rejects_libx265_options_with_videotoolbox(self) -> None:
        argv = [
            "transcode_access.py",
            "--format",
            "vhs",
            "--crf",
            "20",
            "masters/tape/08.mkv",
        ]
        with patch.object(sys, "argv", argv), self.assertRaises(SystemExit):
            transcode_access.parse_args()

    def test_access_build_paths_uses_parent_parent_source_root_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_file = root / "masters" / "tape" / "08.mkv"
            input_file.parent.mkdir(parents=True, exist_ok=True)
            input_file.write_bytes(b"")
            cfg = make_config(
                format_type="vhs",
                denoise="verylight",
                output_suffix="_access",
                layout="access",
            )

            paths = transcode3.build_paths(cfg, input_file)

        source_root = (root / "masters").resolve()
        self.assertEqual(paths.out_dir, source_root / "Access" / "tape")
        self.assertEqual(paths.log_dir, source_root / "Logs" / "tape")
        self.assertEqual(paths.output_file, source_root / "Access" / "tape" / "08_access.mp4")

    def test_access_build_paths_honors_explicit_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_file = root / "masters" / "tape" / "08.mkv"
            output_dir = root / "mp4s"
            log_dir = root / "reports"
            input_file.parent.mkdir(parents=True, exist_ok=True)
            input_file.write_bytes(b"")
            cfg = make_config(
                format_type="vhs",
                layout="access",
                source_root=root / "masters",
                output_dir=output_dir,
                log_dir=log_dir,
            )

            paths = transcode3.build_paths(cfg, input_file)

        self.assertEqual(paths.out_dir, output_dir.resolve())
        self.assertEqual(paths.log_dir, log_dir.resolve())
        self.assertEqual(paths.output_file, output_dir.resolve() / "08.mp4")

    def test_access_duration_groups_validate_each_input_against_resolved_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_file = root / "masters" / "tape" / "08.mkv"
            input_file.parent.mkdir(parents=True, exist_ok=True)
            input_file.write_bytes(b"")
            cfg = make_config(format_type="vhs", layout="access")

            group = transcode3.build_duration_validation_groups(cfg, [input_file])[0]

        self.assertEqual(group.original_file, input_file.resolve())
        self.assertEqual(group.input_files, [input_file.resolve()])
        self.assertEqual(group.output_files, [(root / "masters").resolve() / "Access" / "tape" / "08.mp4"])
        self.assertEqual(group.output_resolution_errors, ["missing exact output " + str(group.output_files[0])])


class TestPreviewArtifactPaths(unittest.TestCase):
    def test_build_runtime_paths_moves_preview_artifacts_out_of_log_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = transcode3.Paths(
                input_file=root / "Originals" / "input.dv",
                stem="input",
                out_dir=root / "Access",
                log_dir=root / "Logs",
                output_file=root / "Access" / "output.mkv",
                ffmpeg_log_file=root / "Logs" / "input_access_20260421_120000.log",
                command_log_file=root / "Logs" / "input_transcode_cmd_20260421_120000.log",
                csv_raw=root / "Logs" / "input.frameinfo.csv",
                csv_with_play=root / "Logs" / "input.frameinfo.with_play_time.csv",
                srt_file=root / "Logs" / "input.record_time_overlay.srt",
                add_play_time_script=root / "add_play_time_columns.py",
                create_srt_script=root / "create_srt.py",
            )
            artifact_dir = root / "preview-temp"
            runtime_paths = transcode3.build_runtime_paths(paths, artifact_dir)

            self.assertEqual(runtime_paths.log_dir, artifact_dir)
            self.assertEqual(runtime_paths.ffmpeg_log_file.parent, artifact_dir)
            self.assertEqual(runtime_paths.csv_raw.parent, artifact_dir)
            self.assertEqual(runtime_paths.csv_with_play.parent, artifact_dir)
            self.assertEqual(runtime_paths.srt_file.parent, artifact_dir)
            self.assertEqual(runtime_paths.command_log_file, paths.command_log_file)
            self.assertNotEqual(runtime_paths.ffmpeg_log_file.parent, paths.log_dir)

    def test_process_one_file_uses_temp_paths_for_digital8_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_file = root / "Originals" / "input.dv"
            input_file.parent.mkdir()
            input_file.write_bytes(b"")

            persistent_paths = transcode3.Paths(
                input_file=input_file,
                stem="input",
                out_dir=root / "Access",
                log_dir=root / "Logs",
                output_file=root / "Access" / "output.mkv",
                ffmpeg_log_file=root / "Logs" / "input_access_20260421_120000.log",
                command_log_file=root / "Logs" / "input_transcode_cmd_20260421_120000.log",
                csv_raw=root / "Logs" / "input.frameinfo.csv",
                csv_with_play=root / "Logs" / "input.frameinfo.with_play_time.csv",
                srt_file=root / "Logs" / "input.record_time_overlay.srt",
                add_play_time_script=root / "add_play_time_columns.py",
                create_srt_script=root / "create_srt.py",
            )
            preview_dir = root / "preview-runtime"
            preview_dir.mkdir()

            @contextmanager
            def fake_tempdir(*args, **kwargs):
                yield str(preview_dir)

            cfg = make_config(mode="preview", format_type="digital8", assume_yes=False)

            captured: dict[str, object] = {}

            def fake_generate(paths: transcode3.Paths) -> None:
                captured["sidecar_paths"] = paths

            def fake_run(ffmpeg_args: list[str], log_path: Path, preview_stem: str | None = None) -> int:
                captured["ffmpeg_args"] = ffmpeg_args
                captured["log_path"] = log_path
                captured["preview_stem"] = preview_stem
                return 0

            with (
                patch.object(transcode3, "build_paths", return_value=persistent_paths),
                patch.object(transcode3.tempfile, "TemporaryDirectory", fake_tempdir),
                patch.object(transcode3, "generate_digital8_sidecars", side_effect=fake_generate),
                patch.object(transcode3, "run_ffmpeg", side_effect=fake_run),
            ):
                result = transcode3.process_one_file(cfg, input_file, prompt=False)

            self.assertEqual(result.rc, 0)
            self.assertIsNone(result.transcode_seconds)
            self.assertIsNone(result.sidecar_seconds)
            sidecar_paths = captured["sidecar_paths"]
            assert isinstance(sidecar_paths, transcode3.Paths)
            self.assertEqual(sidecar_paths.csv_raw.parent, preview_dir)
            self.assertEqual(sidecar_paths.csv_with_play.parent, preview_dir)
            self.assertEqual(sidecar_paths.srt_file.parent, preview_dir)
            self.assertEqual(captured["log_path"], preview_dir / persistent_paths.ffmpeg_log_file.name)
            self.assertEqual(captured["preview_stem"], persistent_paths.stem)
            ffmpeg_args = captured["ffmpeg_args"]
            assert isinstance(ffmpeg_args, list)
            self.assertIn(str(preview_dir / persistent_paths.srt_file.name), " ".join(ffmpeg_args))

    def test_process_one_file_uses_temp_log_for_non_digital8_preview(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_file = root / "Originals" / "input.dv"
            input_file.parent.mkdir()
            input_file.write_bytes(b"")

            persistent_paths = transcode3.Paths(
                input_file=input_file,
                stem="input",
                out_dir=root / "Access",
                log_dir=root / "Logs",
                output_file=root / "Access" / "output.mkv",
                ffmpeg_log_file=root / "Logs" / "input_access_20260421_120000.log",
                command_log_file=root / "Logs" / "input_transcode_cmd_20260421_120000.log",
                csv_raw=root / "Logs" / "input.frameinfo.csv",
                csv_with_play=root / "Logs" / "input.frameinfo.with_play_time.csv",
                srt_file=root / "Logs" / "input.record_time_overlay.srt",
                add_play_time_script=root / "add_play_time_columns.py",
                create_srt_script=root / "create_srt.py",
            )
            preview_dir = root / "preview-runtime"
            preview_dir.mkdir()

            @contextmanager
            def fake_tempdir(*args, **kwargs):
                yield str(preview_dir)

            cfg = make_config(mode="preview", format_type="video8", assume_yes=False)

            with (
                patch.object(transcode3, "build_paths", return_value=persistent_paths),
                patch.object(transcode3.tempfile, "TemporaryDirectory", fake_tempdir),
                patch.object(transcode3, "generate_digital8_sidecars") as mock_sidecars,
                patch.object(transcode3, "run_ffmpeg", return_value=0) as mock_run_ffmpeg,
            ):
                result = transcode3.process_one_file(cfg, input_file, prompt=False)

            self.assertEqual(result.rc, 0)
            self.assertIsNone(result.transcode_seconds)
            self.assertIsNone(result.sidecar_seconds)
            mock_sidecars.assert_not_called()
            _, log_path = mock_run_ffmpeg.call_args.args[:2]
            self.assertEqual(log_path, preview_dir / persistent_paths.ffmpeg_log_file.name)


class TestTranscodeTiming(unittest.TestCase):
    def test_process_one_file_records_separate_sidecar_and_transcode_times(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_file = root / "Originals" / "input.dv"
            input_file.parent.mkdir()
            input_file.write_bytes(b"")

            paths = transcode3.Paths(
                input_file=input_file,
                stem="input",
                out_dir=root / "Access",
                log_dir=root / "Logs",
                output_file=root / "Access" / "output.mkv",
                ffmpeg_log_file=root / "Logs" / "input_access_20260421_120000.log",
                command_log_file=root / "Logs" / "input_transcode_cmd_20260421_120000.log",
                csv_raw=root / "Logs" / "input.frameinfo.csv",
                csv_with_play=root / "Logs" / "input.frameinfo.with_play_time.csv",
                srt_file=root / "Logs" / "input.record_time_overlay.srt",
                add_play_time_script=root / "add_play_time_columns.py",
                create_srt_script=root / "create_srt.py",
            )

            cfg = make_config(mode="transcode", format_type="digital8", assume_yes=False)

            calls: list[str] = []

            def fake_sidecars(arg_paths: transcode3.Paths) -> None:
                self.assertIs(arg_paths, paths)
                calls.append("sidecars")

            def fake_input(prompt: str) -> str:
                calls.append("prompt")
                return ""

            def fake_write(*args, **kwargs) -> None:
                calls.append("write")

            def fake_run(*args, **kwargs) -> int:
                calls.append("run")
                return 0

            with (
                patch.object(transcode3, "build_paths", return_value=paths),
                patch.object(transcode3, "generate_digital8_sidecars", side_effect=fake_sidecars),
                patch.object(transcode3, "write_command_log", side_effect=fake_write),
                patch.object(transcode3, "run_ffmpeg", side_effect=fake_run),
                patch("builtins.input", side_effect=fake_input),
                patch.object(transcode3.time, "perf_counter", side_effect=[10.0, 12.5, 100.0, 107.4]),
            ):
                result = transcode3.process_one_file(cfg, input_file, prompt=True)

            self.assertEqual(calls, ["sidecars", "prompt", "write", "run"])
            self.assertEqual(result.rc, 0)
            self.assertEqual(result.input_file, input_file)
            self.assertEqual(result.output_file, paths.output_file)
            self.assertAlmostEqual(result.sidecar_seconds or 0.0, 2.5)
            self.assertAlmostEqual(result.transcode_seconds or 0.0, 7.4)
            self.assertEqual(result.format_type, "digital8")
            self.assertEqual(result.denoise, "off")
            self.assertEqual(result.mask_bottom, 0)

    def test_process_one_file_uses_na_sidecar_time_for_non_digital8(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_file = root / "Originals" / "input.dv"
            input_file.parent.mkdir()
            input_file.write_bytes(b"")

            paths = transcode3.Paths(
                input_file=input_file,
                stem="input",
                out_dir=root / "Access",
                log_dir=root / "Logs",
                output_file=root / "Access" / "output.mkv",
                ffmpeg_log_file=root / "Logs" / "input_access_20260421_120000.log",
                command_log_file=root / "Logs" / "input_transcode_cmd_20260421_120000.log",
                csv_raw=root / "Logs" / "input.frameinfo.csv",
                csv_with_play=root / "Logs" / "input.frameinfo.with_play_time.csv",
                srt_file=root / "Logs" / "input.record_time_overlay.srt",
                add_play_time_script=root / "add_play_time_columns.py",
                create_srt_script=root / "create_srt.py",
            )

            cfg = make_config(mode="transcode", format_type="video8", mask_bottom=7, denoise="light")

            with (
                patch.object(transcode3, "build_paths", return_value=paths),
                patch.object(transcode3, "write_command_log"),
                patch.object(transcode3, "run_ffmpeg", return_value=0),
                patch.object(transcode3.time, "perf_counter", side_effect=[100.0, 104.2]),
            ):
                result = transcode3.process_one_file(cfg, input_file, prompt=False)

            self.assertIsNone(result.sidecar_seconds)
            self.assertAlmostEqual(result.transcode_seconds or 0.0, 4.2)
            self.assertEqual(result.format_type, "video8")
            self.assertEqual(result.denoise, "light")
            self.assertEqual(result.mask_bottom, 7)

    def test_process_one_file_no_logs_skips_command_log_and_ffmpeg_log_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_file = root / "Originals" / "input.dv"
            input_file.parent.mkdir()
            input_file.write_bytes(b"")

            paths = transcode3.Paths(
                input_file=input_file,
                stem="input",
                out_dir=root / "Access",
                log_dir=root / "Logs",
                output_file=root / "Access" / "output.mkv",
                ffmpeg_log_file=root / "Logs" / "input_access_20260421_120000.log",
                command_log_file=root / "Logs" / "input_transcode_cmd_20260421_120000.log",
                csv_raw=root / "Logs" / "input.frameinfo.csv",
                csv_with_play=root / "Logs" / "input.frameinfo.with_play_time.csv",
                srt_file=root / "Logs" / "input.record_time_overlay.srt",
                add_play_time_script=root / "add_play_time_columns.py",
                create_srt_script=root / "create_srt.py",
            )

            cfg = make_config(mode="transcode", format_type="video8", no_logs=True)

            with (
                patch.object(transcode3, "build_paths", return_value=paths) as mock_build_paths,
                patch.object(transcode3, "write_command_log") as mock_write_command_log,
                patch.object(transcode3, "run_ffmpeg", return_value=0) as mock_run_ffmpeg,
                patch.object(transcode3.time, "perf_counter", side_effect=[100.0, 104.2]),
            ):
                result = transcode3.process_one_file(cfg, input_file, prompt=False)

            mock_build_paths.assert_called_once_with(cfg, input_file, create_dirs=False)
            mock_write_command_log.assert_not_called()
            _, log_path = mock_run_ffmpeg.call_args.args[:2]
            self.assertIsNone(log_path)
            self.assertTrue(paths.out_dir.is_dir())
            self.assertFalse(paths.log_dir.exists())
            self.assertEqual(result.rc, 0)

    def test_process_one_file_no_logs_uses_temp_digital8_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_file = root / "Originals" / "input.dv"
            input_file.parent.mkdir()
            input_file.write_bytes(b"")

            paths = transcode3.Paths(
                input_file=input_file,
                stem="input",
                out_dir=root / "Access",
                log_dir=root / "Logs",
                output_file=root / "Access" / "output.mp4",
                ffmpeg_log_file=root / "Logs" / "input_access_20260421_120000.log",
                command_log_file=root / "Logs" / "input_transcode_cmd_20260421_120000.log",
                csv_raw=root / "Logs" / "input.frameinfo.csv",
                csv_with_play=root / "Logs" / "input.frameinfo.with_play_time.csv",
                srt_file=root / "Logs" / "input.record_time_overlay.srt",
                add_play_time_script=root / "add_play_time_columns.py",
                create_srt_script=root / "create_srt.py",
            )
            runtime_dir = root / "runtime"
            runtime_dir.mkdir()
            captured: dict[str, object] = {}

            class FakeTempDir:
                name = str(runtime_dir)

                def cleanup(self) -> None:
                    pass

            def fake_sidecars(arg_paths: transcode3.Paths) -> None:
                captured["sidecar_paths"] = arg_paths
                arg_paths.output_file = arg_paths.output_file.with_name(f"20260421_{arg_paths.output_file.name}")

            cfg = make_config(mode="transcode", format_type="digital8", no_logs=True)

            with (
                patch.object(transcode3, "build_paths", return_value=paths),
                patch.object(transcode3.tempfile, "TemporaryDirectory", return_value=FakeTempDir()),
                patch.object(transcode3, "generate_digital8_sidecars", side_effect=fake_sidecars),
                patch.object(transcode3, "write_command_log") as mock_write_command_log,
                patch.object(transcode3, "run_ffmpeg", return_value=0) as mock_run_ffmpeg,
                patch.object(transcode3.time, "perf_counter", side_effect=[10.0, 12.0, 100.0, 104.0]),
            ):
                result = transcode3.process_one_file(cfg, input_file, prompt=False)

            sidecar_paths = captured["sidecar_paths"]
            assert isinstance(sidecar_paths, transcode3.Paths)
            self.assertEqual(sidecar_paths.csv_raw.parent, runtime_dir)
            self.assertEqual(sidecar_paths.csv_with_play.parent, runtime_dir)
            self.assertEqual(sidecar_paths.srt_file.parent, runtime_dir)
            self.assertIn(str(runtime_dir / paths.srt_file.name), " ".join(mock_run_ffmpeg.call_args.args[0]))
            _, log_path = mock_run_ffmpeg.call_args.args[:2]
            self.assertIsNone(log_path)
            mock_write_command_log.assert_not_called()
            self.assertFalse(paths.log_dir.exists())
            self.assertEqual(result.output_file, root / "Access" / "20260421_output.mp4")
            self.assertEqual(result.rc, 0)

    def test_main_prints_transcode_time_summary(self) -> None:
        cfg = make_config(mode="transcode")
        file_a = Path("/tmp/a.dv")
        file_b = Path("/tmp/b.dv")
        results = [
            transcode3.ProcessResult(file_a, Path("/tmp/a.mkv"), 0, 65.0, None, "video8", "off", 0),
            transcode3.ProcessResult(file_b, Path("/tmp/b.mkv"), 1, 5.0, 8.0, "digital8", "light", 7),
        ]
        captured = io.StringIO()

        with (
            patch.object(transcode3, "parse_args", return_value=(cfg, [file_a, file_b])),
            patch.object(transcode3, "process_one_file", side_effect=results),
            patch.object(transcode3, "validate_durations", return_value=[]),
            redirect_stdout(captured),
        ):
            rc = transcode3.main()

        output = captured.getvalue()
        self.assertEqual(rc, 1)
        self.assertIn("End-of-run summary:", output)
        self.assertIn("Filename", output)
        self.assertIn("Transcode", output)
        self.assertIn("Sidecar Gen", output)
        self.assertIn("Options", output)
        self.assertIn("a.dv", output)
        self.assertIn("1m 05s", output)
        self.assertIn("n/a", output)
        self.assertIn("video8 | denoise=off | mask_bottom=0", output)
        self.assertIn("b.dv", output)
        self.assertIn("5s", output)
        self.assertIn("8s", output)
        self.assertIn("digital8 | denoise=light | mask_bottom=7", output)
        self.assertIn("TOTAL", output)
        self.assertIn("1m 10s", output)
        self.assertEqual(output.count("8s"), 2)
        self.assertEqual(output.count("End-of-run summary:"), 1)

    def test_main_skips_timing_summary_for_preview(self) -> None:
        cfg = make_config(mode="preview")
        input_file = Path("/tmp/preview.dv")
        captured = io.StringIO()

        with (
            patch.object(transcode3, "parse_args", return_value=(cfg, [input_file])),
            patch.object(
                transcode3,
                "process_one_file",
                return_value=transcode3.ProcessResult(input_file, None, 0, None, None, "video8", "off", 0),
            ),
            redirect_stdout(captured),
        ):
            rc = transcode3.main()

        output = captured.getvalue()
        self.assertEqual(rc, 0)
        self.assertNotIn("End-of-run summary:", output)

    def test_main_transcode_auto_runs_validation_by_default(self) -> None:
        cfg = make_config(mode="transcode", validate_duration=True)
        input_file = Path("/tmp/input.dv")

        with (
            patch.object(transcode3, "parse_args", return_value=(cfg, [input_file])),
            patch.object(
                transcode3,
                "process_one_file",
                return_value=transcode3.ProcessResult(input_file, Path("/tmp/input.mp4"), 0, 1.0, None, "video8", "off", 0),
            ),
            patch.object(
                transcode3,
                "validate_durations",
                return_value=[
                    transcode3.DurationValidationResult(
                        group=transcode3.DurationGroup(input_file, input_file, [input_file], [Path("/tmp/input.mp4")]),
                        original_row=transcode3.DurationRow(input_file, 1.0),
                        input_rows=[transcode3.DurationRow(input_file, 1.0)],
                        output_rows=[transcode3.DurationRow(Path("/tmp/input.mp4"), 1.0)],
                        original_total=1.0,
                        input_total=1.0,
                        output_total=1.0,
                        delta_original_vs_input=0.0,
                        delta_original_vs_output=0.0,
                        delta_input_vs_output=0.0,
                        tolerance=0.5,
                        errors=[],
                    )
                ],
            ) as mock_validate,
        ):
            rc = transcode3.main()

        self.assertEqual(rc, 0)
        mock_validate.assert_called_once_with(cfg, [input_file])

    def test_main_transcode_can_skip_validation(self) -> None:
        cfg = make_config(mode="transcode", validate_duration=False)
        input_file = Path("/tmp/input.dv")

        with (
            patch.object(transcode3, "parse_args", return_value=(cfg, [input_file])),
            patch.object(
                transcode3,
                "process_one_file",
                return_value=transcode3.ProcessResult(input_file, Path("/tmp/input.mp4"), 0, 1.0, None, "video8", "off", 0),
            ),
            patch.object(transcode3, "validate_durations") as mock_validate,
        ):
            rc = transcode3.main()

        self.assertEqual(rc, 0)
        mock_validate.assert_not_called()

    def test_main_validate_duration_mode_runs_validation_only(self) -> None:
        cfg = make_config(mode="validate-duration")
        input_file = Path("/tmp/input.dv")

        with (
            patch.object(transcode3, "parse_args", return_value=(cfg, [input_file])),
            patch.object(transcode3, "process_one_file") as mock_process,
            patch.object(transcode3, "validate_durations", return_value=[]) as mock_validate,
        ):
            rc = transcode3.main()

        self.assertEqual(rc, 0)
        mock_process.assert_not_called()
        mock_validate.assert_called_once_with(cfg, [input_file])

    def test_main_preview_never_validates(self) -> None:
        cfg = make_config(mode="preview")
        input_file = Path("/tmp/preview.dv")

        with (
            patch.object(transcode3, "parse_args", return_value=(cfg, [input_file])),
            patch.object(
                transcode3,
                "process_one_file",
                return_value=transcode3.ProcessResult(input_file, None, 0, None, None, "video8", "off", 0),
            ),
            patch.object(transcode3, "validate_durations") as mock_validate,
        ):
            rc = transcode3.main()

        self.assertEqual(rc, 0)
        mock_validate.assert_not_called()

    def test_main_validation_failure_makes_exit_non_zero(self) -> None:
        cfg = make_config(mode="validate-duration")
        input_file = Path("/tmp/input.dv")

        with (
            patch.object(transcode3, "parse_args", return_value=(cfg, [input_file])),
            patch.object(
                transcode3,
                "validate_durations",
                return_value=[
                    transcode3.DurationValidationResult(
                        group=transcode3.DurationGroup(input_file, input_file, [input_file], [Path("/tmp/input.mp4")]),
                        original_row=None,
                        input_rows=[],
                        output_rows=[],
                        original_total=None,
                        input_total=None,
                        output_total=None,
                        delta_original_vs_input=None,
                        delta_original_vs_output=None,
                        delta_input_vs_output=None,
                        tolerance=0.5,
                        errors=["missing output"],
                    )
                ],
            ),
        ):
            rc = transcode3.main()

        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
