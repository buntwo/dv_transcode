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

            with (
                patch.object(transcode3, "run_checked", side_effect=fake_run_checked),
                patch.object(transcode3, "extract_first_rdt_yyyymmdd", return_value=None),
            ):
                transcode3.generate_digital8_sidecars(paths)

            self.assertEqual(len(calls), 3)
            dvrescue_args, dvrescue_stdout, dvrescue_stderr, dvrescue_stdout_dest = calls[0]
            self.assertEqual(dvrescue_args[:2], ["dvrescue", "--csv"])
            self.assertEqual(dvrescue_args[-2:], ["-m", "-"])
            self.assertIsNone(dvrescue_stdout)
            self.assertEqual(dvrescue_stderr, paths.csv_raw)
            self.assertEqual(dvrescue_stdout_dest, transcode3.subprocess.DEVNULL)


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

            cfg = transcode3.Config(
                mode="preview",
                format_type="digital8",
                start=None,
                end=None,
                crop_bottom=0,
                pad_bottom=0,
                denoise="off",
                q=70,
                codec="hevc",
                deint_mode="send_field",
                map_both_audio=False,
                log_level="warning",
                assume_yes=False,
                output_suffix="",
                originals_dirname="Originals",
                access_dirname="Access",
                logs_dirname="Logs",
            )

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

            cfg = transcode3.Config(
                mode="preview",
                format_type="video8",
                start=None,
                end=None,
                crop_bottom=0,
                pad_bottom=0,
                denoise="off",
                q=70,
                codec="hevc",
                deint_mode="send_field",
                map_both_audio=False,
                log_level="warning",
                assume_yes=False,
                output_suffix="",
                originals_dirname="Originals",
                access_dirname="Access",
                logs_dirname="Logs",
            )

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

            cfg = transcode3.Config(
                mode="transcode",
                format_type="digital8",
                start=None,
                end=None,
                crop_bottom=0,
                pad_bottom=0,
                denoise="off",
                q=70,
                codec="hevc",
                deint_mode="send_field",
                map_both_audio=False,
                log_level="warning",
                assume_yes=False,
                output_suffix="",
                originals_dirname="Originals",
                access_dirname="Access",
                logs_dirname="Logs",
            )

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
            self.assertEqual(result.crop_bottom, 0)

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

            cfg = transcode3.Config(
                mode="transcode",
                format_type="video8",
                start=None,
                end=None,
                crop_bottom=7,
                pad_bottom=7,
                denoise="light",
                q=70,
                codec="hevc",
                deint_mode="send_field",
                map_both_audio=False,
                log_level="warning",
                assume_yes=True,
                output_suffix="",
                originals_dirname="Originals",
                access_dirname="Access",
                logs_dirname="Logs",
            )

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
            self.assertEqual(result.crop_bottom, 7)

    def test_main_prints_transcode_time_summary(self) -> None:
        cfg = transcode3.Config(
            mode="transcode",
            format_type="video8",
            start=None,
            end=None,
            crop_bottom=0,
            pad_bottom=0,
            denoise="off",
            q=70,
            codec="hevc",
            deint_mode="send_field",
            map_both_audio=False,
            log_level="warning",
            assume_yes=True,
            output_suffix="",
            originals_dirname="Originals",
            access_dirname="Access",
            logs_dirname="Logs",
        )
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
        self.assertIn("video8 | denoise=off | crop=0", output)
        self.assertIn("b.dv", output)
        self.assertIn("5s", output)
        self.assertIn("8s", output)
        self.assertIn("digital8 | denoise=light | crop=7", output)
        self.assertIn("TOTAL", output)
        self.assertIn("1m 10s", output)
        self.assertEqual(output.count("8s"), 2)
        self.assertEqual(output.count("End-of-run summary:"), 1)

    def test_main_skips_timing_summary_for_preview(self) -> None:
        cfg = transcode3.Config(
            mode="preview",
            format_type="video8",
            start=None,
            end=None,
            crop_bottom=0,
            pad_bottom=0,
            denoise="off",
            q=70,
            codec="hevc",
            deint_mode="send_field",
            map_both_audio=False,
            log_level="warning",
            assume_yes=True,
            output_suffix="",
            originals_dirname="Originals",
            access_dirname="Access",
            logs_dirname="Logs",
        )
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


if __name__ == "__main__":
    unittest.main()
