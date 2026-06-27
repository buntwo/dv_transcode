from __future__ import annotations

import io
import importlib.util
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
        self.assertEqual(plan.jobs[1].output_status, "exists, skip")
        self.assertEqual(plan.runnable_job_count, 1)
        self.assertEqual(plan.skipped_output_count, 1)

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
        self.assertIn("Extraction jobs to run: 1", output)
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

    def test_run_extract_skips_existing_outputs_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            audio_dir = root_path / "audio"
            audio_dir.mkdir()
            existing_output = audio_dir / "A.flac"
            existing_output.write_bytes(b"old")
            plan = vhs_color_split_audio.ExtractPlan(
                transcode_list=Path("/repo/vhs_workflow/transcode_vhs_color_split.sh"),
                output_dir=audio_dir,
                force=False,
                jobs=[
                    vhs_color_split_audio.ExtractJob(
                        Path("/masters/A.mkv"),
                        "keep",
                        existing_output,
                        "exists, skip",
                    ),
                    vhs_color_split_audio.ExtractJob(
                        Path("/masters/B.mkv"),
                        "keep",
                        audio_dir / "B.flac",
                        "will write",
                    ),
                    vhs_color_split_audio.ExtractJob(
                        Path("/masters/08.mkv"),
                        "right",
                        audio_dir / "08.flac",
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
                str(audio_dir),
                "/masters/B.mkv",
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
                str(audio_dir),
                "--audio-channel",
                "right",
                "/masters/08.mkv",
            ],
        )

    def test_all_existing_extract_outputs_do_not_prompt_or_run(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            audio_dir = root_path / "audio"
            audio_dir.mkdir()
            (audio_dir / "A.flac").write_bytes(b"old")
            tasks = [vhs_color_split_audio.TranscodeTask(Path("/masters/A.mkv"), "keep")]
            stdout = io.StringIO()
            with (
                patch.object(vhs_color_split_audio, "load_tasks", return_value=tasks),
                patch.object(vhs_color_split_audio, "run_extract") as run_extract,
                patch.object(vhs_color_split_audio.sys, "stdin", io.StringIO()),
                patch.object(vhs_color_split_audio.sys, "stdout", stdout),
            ):
                code = vhs_color_split_audio.main(
                    [
                        "extract-audio",
                        "--output-dir",
                        str(audio_dir),
                        "--transcode-list",
                        "/repo/vhs_workflow/transcode_vhs_color_split.sh",
                    ]
                )

        self.assertEqual(code, 0)
        run_extract.assert_not_called()
        self.assertIn("Existing FLAC outputs skipped: 1", stdout.getvalue())
        self.assertIn("No extraction jobs to run.", stdout.getvalue())

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

            with patch.object(vhs_color_split_audio.subprocess, "run") as run_mock:
                vhs_color_split_audio.run_normalize(
                    access_copy_dir=access_dir,
                    audio_dir=audio_dir,
                    output_dir=output_dir,
                    force=False,
                    gain=vhs_color_split_audio.DEFAULT_FIXED_GAIN,
                    peak_ceiling=vhs_color_split_audio.DEFAULT_PEAK_CEILING,
                    vhs_notch="auto",
                    repo_root=Path("/repo"),
                )

        cmd = run_mock.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--access-copy-dir") + 1], str(access_dir))
        self.assertNotIn("--method", cmd)
        self.assertEqual(cmd[cmd.index("--gain") + 1], "12")
        self.assertEqual(cmd[cmd.index("--peak-ceiling") + 1], "-1.5")
        self.assertEqual(cmd[cmd.index("--vhs-notch") + 1], "auto")

    def test_normalize_command_hands_off_without_wrapper_confirmation(self) -> None:
        with (
            patch.object(vhs_color_split_audio, "load_tasks") as load_tasks,
            patch.object(vhs_color_split_audio, "confirm") as confirm,
            patch.object(vhs_color_split_audio, "run_normalize") as run_normalize,
            patch.object(vhs_color_split_audio.sys, "stdout", io.StringIO()) as stdout,
        ):
            code = vhs_color_split_audio.main(
                [
                    "normalize-audio",
                    "--access-copy-dir",
                    "/access",
                    "--audio-dir",
                    "/audio",
                    "--output-dir",
                    "/normalized",
                    "--repo-root",
                    "/repo",
                ]
            )

        self.assertEqual(code, 0)
        load_tasks.assert_not_called()
        confirm.assert_not_called()
        run_normalize.assert_called_once_with(
            access_copy_dir=Path("/access"),
            audio_dir=Path("/audio"),
            output_dir=Path("/normalized"),
            force=False,
            gain=vhs_color_split_audio.DEFAULT_FIXED_GAIN,
            peak_ceiling=vhs_color_split_audio.DEFAULT_PEAK_CEILING,
            vhs_notch="auto",
            repo_root=Path("/repo"),
        )
        self.assertIn("Handing off normalization preflight", stdout.getvalue())

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

            with patch.object(vhs_color_split_audio.subprocess, "run") as run_mock:
                vhs_color_split_audio.run_normalize(
                    access_copy_dir=access_dir,
                    audio_dir=audio_dir,
                    output_dir=output_dir,
                    force=True,
                    gain=9.5,
                    peak_ceiling=-1.0,
                    vhs_notch="pal",
                    repo_root=Path("/repo"),
                )

        cmd = run_mock.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--gain") + 1], "9.5")
        self.assertEqual(cmd[cmd.index("--peak-ceiling") + 1], "-1")
        self.assertEqual(cmd[cmd.index("--vhs-notch") + 1], "pal")
        self.assertIn("--force", cmd)


if __name__ == "__main__":
    unittest.main()
