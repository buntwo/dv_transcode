from __future__ import annotations

import io
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "vhs_workflow" / "vhs_color_split_audio.py"
spec = importlib.util.spec_from_file_location("vhs_color_split_audio", MODULE_PATH)
assert spec is not None
vhs_color_split_audio = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = vhs_color_split_audio
spec.loader.exec_module(vhs_color_split_audio)


class TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


class TestVhsColorSplitAudioTasks(unittest.TestCase):
    def test_parse_print_tasks_output_with_quoted_paths(self) -> None:
        stdout = (
            "uv --project /repo run /repo/scripts/transcode_access.py --format vhs "
            "'/masters/Tape One.mkv'\n"
            "uv --project /repo run /repo/scripts/transcode_access.py --format vhs "
            "--audio-channel right '/masters/08.mkv'\n"
        )

        tasks = vhs_color_split_audio.parse_task_lines(stdout)

        self.assertEqual(tasks[0].input_file, Path("/masters/Tape One.mkv"))
        self.assertEqual(tasks[0].audio_channel, "keep")
        self.assertEqual(tasks[1].input_file, Path("/masters/08.mkv"))
        self.assertEqual(tasks[1].audio_channel, "right")

    def test_load_tasks_uses_print_tasks_subcommand(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            script = Path(root) / "transcode_vhs_color_split.sh"
            script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            script.chmod(0o755)

            completed = subprocess.CompletedProcess(
                [str(script), "--print-tasks"],
                0,
                stdout="uv --project /repo run tool '/masters/A.mkv'\n",
                stderr="",
            )
            with patch.object(vhs_color_split_audio.subprocess, "run", return_value=completed) as run_mock:
                tasks = vhs_color_split_audio.load_tasks(script)

        run_mock.assert_called_once_with(
            [str(script), "--print-tasks"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(tasks, [vhs_color_split_audio.TranscodeTask(Path("/masters/A.mkv"), "keep")])


class TestVhsColorSplitAudioPlans(unittest.TestCase):
    def test_extract_plan_groups_channels_and_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            output_dir = root_path / "audio"
            output_dir.mkdir()
            existing_output = output_dir / "08.flac"
            existing_output.write_bytes(b"old")
            tasks = [
                vhs_color_split_audio.TranscodeTask(Path("/masters/Tape One.mkv"), "keep"),
                vhs_color_split_audio.TranscodeTask(Path("/masters/08.mkv"), "right"),
            ]

            plan = vhs_color_split_audio.build_extract_plan(
                tasks,
                output_dir,
                force=False,
                transcode_list=Path("/repo/vhs_workflow/transcode_vhs_color_split.sh"),
            )

        self.assertEqual(plan.channel_counts, {"keep": 1, "left": 0, "right": 1})
        self.assertEqual(plan.jobs[0].output_file, output_dir / "Tape One.flac")
        self.assertEqual(plan.jobs[0].output_status, "will write")
        self.assertEqual(plan.jobs[1].output_file, existing_output)
        self.assertEqual(plan.jobs[1].output_status, "exists, needs --force")

    def test_normalize_plan_detects_missing_flacs_and_existing_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            access_dir = root_path / "access"
            audio_dir = root_path / "audio"
            output_dir = root_path / "normalized"
            access_dir.mkdir()
            audio_dir.mkdir()
            output_dir.mkdir()
            (access_dir / "A.mp4").write_bytes(b"video")
            (access_dir / "B.mp4").write_bytes(b"video")
            (audio_dir / "A.flac").write_bytes(b"audio")
            (output_dir / "B.mp4").write_bytes(b"old")
            tasks = [
                vhs_color_split_audio.TranscodeTask(Path("/masters/A.mkv"), "keep"),
                vhs_color_split_audio.TranscodeTask(Path("/masters/B.mkv"), "keep"),
            ]

            plan = vhs_color_split_audio.build_normalize_plan(
                tasks,
                access_dir,
                audio_dir,
                output_dir,
                force=False,
                transcode_list=Path("/repo/vhs_workflow/transcode_vhs_color_split.sh"),
            )

        self.assertEqual(plan.missing_audio_count, 1)
        self.assertEqual(plan.existing_output_count, 1)
        self.assertEqual(plan.jobs[0].audio_status, "found")
        self.assertEqual(plan.jobs[1].audio_status, "missing")
        self.assertEqual(plan.jobs[1].output_status, "exists, needs --force")

    def test_normalize_plan_records_custom_fixed_gain_settings(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            access_dir = root_path / "access"
            audio_dir = root_path / "audio"
            output_dir = root_path / "normalized"
            access_dir.mkdir()
            audio_dir.mkdir()
            (access_dir / "A.mp4").write_bytes(b"video")
            (audio_dir / "A.flac").write_bytes(b"audio")
            tasks = [vhs_color_split_audio.TranscodeTask(Path("/masters/A.mkv"), "keep")]

            plan = vhs_color_split_audio.build_normalize_plan(
                tasks,
                access_dir,
                audio_dir,
                output_dir,
                force=False,
                transcode_list=Path("/repo/vhs_workflow/transcode_vhs_color_split.sh"),
                gain=9.5,
                peak_ceiling=-1.0,
            )

        self.assertEqual(plan.gain, 9.5)
        self.assertEqual(plan.peak_ceiling, -1.0)

    def test_extract_table_uses_dynamic_widths(self) -> None:
        plan = vhs_color_split_audio.ExtractPlan(
            transcode_list=Path("/repo/vhs_workflow/transcode_vhs_color_split.sh"),
            output_dir=Path("/out"),
            force=False,
            jobs=[
                vhs_color_split_audio.ExtractJob(
                    Path("/masters/Very Long Tape Name.mkv"),
                    "right",
                    Path("/out/Very Long Tape Name.flac"),
                    "will write",
                )
            ],
        )

        output = vhs_color_split_audio.format_extract_plan(plan)

        self.assertIn("Very Long Tape Name.mkv", output)
        self.assertIn("/out/Very Long Tape Name.flac", output)
        self.assertIn("channel", output)


class TestVhsColorSplitAudioConfirmation(unittest.TestCase):
    def test_declining_confirmation_does_not_invoke_extract(self) -> None:
        tasks = [vhs_color_split_audio.TranscodeTask(Path("/masters/A.mkv"), "keep")]
        with (
            patch.object(vhs_color_split_audio, "load_tasks", return_value=tasks),
            patch.object(vhs_color_split_audio, "run_extract") as run_extract,
            patch.object(vhs_color_split_audio.sys, "stdin", TtyStringIO("n\n")),
            patch.object(vhs_color_split_audio.sys, "stdout", io.StringIO()),
        ):
            code = vhs_color_split_audio.main(
                [
                    "extract-audio",
                    "--output-dir",
                    "/tmp/audio",
                    "--transcode-list",
                    "/repo/vhs_workflow/transcode_vhs_color_split.sh",
                ]
            )

        self.assertEqual(code, 0)
        run_extract.assert_not_called()

    def test_confirmation_invokes_extract_after_yes(self) -> None:
        tasks = [vhs_color_split_audio.TranscodeTask(Path("/masters/A.mkv"), "keep")]
        with (
            patch.object(vhs_color_split_audio, "load_tasks", return_value=tasks),
            patch.object(vhs_color_split_audio, "run_extract") as run_extract,
            patch.object(vhs_color_split_audio.sys, "stdin", TtyStringIO("y\n")),
            patch.object(vhs_color_split_audio.sys, "stdout", io.StringIO()),
        ):
            code = vhs_color_split_audio.main(
                [
                    "extract-audio",
                    "--output-dir",
                    "/tmp/audio",
                    "--transcode-list",
                    "/repo/vhs_workflow/transcode_vhs_color_split.sh",
                ]
            )

        self.assertEqual(code, 0)
        run_extract.assert_called_once()

    def test_run_extract_batches_by_channel(self) -> None:
        plan = vhs_color_split_audio.ExtractPlan(
            transcode_list=Path("/repo/vhs_workflow/transcode_vhs_color_split.sh"),
            output_dir=Path("/audio"),
            force=True,
            jobs=[
                vhs_color_split_audio.ExtractJob(Path("/masters/A.mkv"), "keep", Path("/audio/A.flac"), "will write"),
                vhs_color_split_audio.ExtractJob(
                    Path("/masters/08.mkv"),
                    "right",
                    Path("/audio/08.flac"),
                    "will write",
                ),
            ],
        )

        with patch.object(vhs_color_split_audio.subprocess, "run") as run_mock:
            vhs_color_split_audio.run_extract(plan, Path("/repo"))

        self.assertEqual(run_mock.call_count, 2)
        self.assertEqual(
            run_mock.call_args_list[0].args[0],
            [
                "uv",
                "--project",
                "/repo",
                "run",
                "/repo/scripts/extract_master_audio.py",
                "--output-dir",
                "/audio",
                "--force",
                "/masters/A.mkv",
            ],
        )
        self.assertEqual(
            run_mock.call_args_list[1].args[0],
            [
                "uv",
                "--project",
                "/repo",
                "run",
                "/repo/scripts/extract_master_audio.py",
                "--output-dir",
                "/audio",
                "--force",
                "--audio-channel",
                "right",
                "/masters/08.mkv",
            ],
        )

    def test_run_normalize_passes_fixed_gain_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            access_dir = root_path / "access"
            audio_dir = root_path / "audio"
            output_dir = root_path / "normalized"
            access_dir.mkdir()
            audio_dir.mkdir()
            (access_dir / "A.mp4").write_bytes(b"video")
            (audio_dir / "A.flac").write_bytes(b"audio")
            plan = vhs_color_split_audio.NormalizePlan(
                transcode_list=Path("/repo/vhs_workflow/transcode_vhs_color_split.sh"),
                access_copy_dir=access_dir,
                audio_dir=audio_dir,
                output_dir=output_dir,
                force=False,
                jobs=[
                    vhs_color_split_audio.NormalizeJob(
                        "A",
                        access_dir / "A.mp4",
                        audio_dir / "A.flac",
                        output_dir / "A.mp4",
                        "found",
                        "will write",
                    )
                ],
            )

            with patch.object(vhs_color_split_audio.subprocess, "run") as run_mock:
                vhs_color_split_audio.run_normalize(plan, Path("/repo"))

        cmd = run_mock.call_args.args[0]
        self.assertIn("--method", cmd)
        self.assertEqual(cmd[cmd.index("--method") + 1], "fixed-gain")
        self.assertEqual(cmd[cmd.index("--gain") + 1], "12")
        self.assertEqual(cmd[cmd.index("--peak-ceiling") + 1], "-1.5")

    def test_run_normalize_symlinks_absolute_access_paths(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            access_dir = root_path / "Access_crf22"
            audio_dir = root_path / "audio"
            output_dir = root_path / "normalized"
            access_dir.mkdir()
            audio_dir.mkdir()
            (access_dir / "A.mp4").write_bytes(b"video")
            (audio_dir / "A.flac").write_bytes(b"audio")
            plan = vhs_color_split_audio.NormalizePlan(
                transcode_list=Path("/repo/vhs_workflow/transcode_vhs_color_split.sh"),
                access_copy_dir=Path("Access_crf22"),
                audio_dir=audio_dir,
                output_dir=output_dir,
                force=False,
                jobs=[
                    vhs_color_split_audio.NormalizeJob(
                        "A",
                        Path("Access_crf22/A.mp4"),
                        audio_dir / "A.flac",
                        output_dir / "A.mp4",
                        "found",
                        "will write",
                    )
                ],
            )

            original_cwd = Path.cwd()
            os.chdir(root_path)
            try:
                def inspect_symlink(cmd: list[str], check: bool) -> None:
                    self.assertTrue(check)
                    tmp_access = Path(cmd[cmd.index("--access-copy-dir") + 1])
                    link_target = Path(os.readlink(tmp_access / "A.mp4"))
                    self.assertEqual(link_target, (access_dir / "A.mp4").resolve())

                with patch.object(vhs_color_split_audio.subprocess, "run", side_effect=inspect_symlink):
                    vhs_color_split_audio.run_normalize(plan, Path("/repo"))
            finally:
                os.chdir(original_cwd)

    def test_run_normalize_forwards_custom_gain_and_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            access_dir = root_path / "access"
            audio_dir = root_path / "audio"
            output_dir = root_path / "normalized"
            access_dir.mkdir()
            audio_dir.mkdir()
            (access_dir / "A.mp4").write_bytes(b"video")
            (audio_dir / "A.flac").write_bytes(b"audio")
            plan = vhs_color_split_audio.NormalizePlan(
                transcode_list=Path("/repo/vhs_workflow/transcode_vhs_color_split.sh"),
                access_copy_dir=access_dir,
                audio_dir=audio_dir,
                output_dir=output_dir,
                force=True,
                jobs=[
                    vhs_color_split_audio.NormalizeJob(
                        "A",
                        access_dir / "A.mp4",
                        audio_dir / "A.flac",
                        output_dir / "A.mp4",
                        "found",
                        "will write",
                    )
                ],
                gain=9.5,
                peak_ceiling=-1.0,
            )

            with patch.object(vhs_color_split_audio.subprocess, "run") as run_mock:
                vhs_color_split_audio.run_normalize(plan, Path("/repo"))

        cmd = run_mock.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--gain") + 1], "9.5")
        self.assertEqual(cmd[cmd.index("--peak-ceiling") + 1], "-1")
        self.assertIn("--force", cmd)


if __name__ == "__main__":
    unittest.main()
