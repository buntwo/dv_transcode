from __future__ import annotations

import io
import importlib.util
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "vhs_workflow" / "vhs_color_split_transcode.py"
LEGACY_SCRIPT = REPO_ROOT / "vhs_workflow" / "transcode_vhs_color_split.legacy.sh"
TEST_TMP_ROOT = REPO_ROOT / ".cache" / "test-vhs-color-split-transcode"
TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)

spec = importlib.util.spec_from_file_location("vhs_color_split_transcode", MODULE_PATH)
assert spec is not None
vhs_color_split_transcode = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = vhs_color_split_transcode
spec.loader.exec_module(vhs_color_split_transcode)


def legacy_print_tasks(env_overrides: dict[str, str] | None = None) -> str:
    env = os.environ.copy()
    env.pop("VHS_DATA_ROOT", None)
    env.pop("VHS_MASTER_ROOT", None)
    if env_overrides:
        env.update(env_overrides)
    result = subprocess.run(
        [str(LEGACY_SCRIPT), "--print-tasks"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout


def python_print_tasks(env_overrides: dict[str, str] | None = None) -> str:
    env = {}
    if env_overrides:
        env.update(env_overrides)
    config = vhs_color_split_transcode.config_from_env(REPO_ROOT, env)
    return "\n".join(vhs_color_split_transcode.task_lines(config)) + "\n"


class TestVhsColorSplitTranscodeTaskOutput(unittest.TestCase):
    def test_default_print_tasks_include_fixed_gain_analysis_flags(self) -> None:
        lines = python_print_tasks().splitlines()

        self.assertEqual(
            len(lines),
            len(vhs_color_split_transcode.COLOR_CORRECT_FILES)
            + len(vhs_color_split_transcode.NO_COLOR_CORRECT_FILES),
        )
        self.assertTrue(all("--audio-gain 12 --audio-peak-ceiling -1.5" in line for line in lines))

    def test_simple_env_overrides_are_used_in_task_lines(self) -> None:
        env = {
            "VHS_DATA_ROOT": "/tmp/VideosRoot",
            "VHS_MASTER_ROOT": "/tmp/MastersRoot/tape",
        }

        output = python_print_tasks(env)

        self.assertIn("--output-dir /tmp/VideosRoot/Access_crf22", output)
        self.assertIn("--log-dir /tmp/VideosRoot/Logs_crf22", output)
        first_task = vhs_color_split_transcode.build_tasks(
            vhs_color_split_transcode.config_from_env(REPO_ROOT, env)
        )[0]
        self.assertIn(str(first_task.input_file), output)

    def test_env_overrides_with_spaces_are_shell_argv_safe(self) -> None:
        env = {
            "VHS_DATA_ROOT": str(TEST_TMP_ROOT / "Videos Root"),
            "VHS_MASTER_ROOT": str(TEST_TMP_ROOT / "Masters Root" / "tape"),
        }

        python_lines = python_print_tasks(env).splitlines()

        self.assertEqual(
            len(python_lines),
            len(vhs_color_split_transcode.COLOR_CORRECT_FILES)
            + len(vhs_color_split_transcode.NO_COLOR_CORRECT_FILES),
        )
        first_argv = shlex.split(python_lines[0])
        self.assertIn(str(TEST_TMP_ROOT / "Videos Root" / "Access_crf22"), first_argv)
        self.assertIn(str(TEST_TMP_ROOT / "Videos Root" / "Logs_crf22"), first_argv)

    def test_audio_channel_and_color_correction_grouping(self) -> None:
        lines = python_print_tasks().splitlines()
        tasks = vhs_color_split_transcode.build_tasks(
            vhs_color_split_transcode.config_from_env(REPO_ROOT, {})
        )

        self.assertEqual(len(lines), len(tasks))
        for task, line in zip(tasks, lines, strict=True):
            self.assertEqual("--vhs-color-correct" in line, task.color_correct)
            self.assertEqual("--audio-channel right" in line, task.audio_channel == "right")
            self.assertTrue(line.endswith(str(task.input_file)))


class TestVhsColorSplitTranscodeModes(unittest.TestCase):
    def test_emit_tasks_writes_selected_task_file(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEST_TMP_ROOT) as root:
            task_file = Path(root) / "tasks.txt"
            stdout = io.StringIO()

            with (
                patch.dict(vhs_color_split_transcode.os.environ, {}, clear=True),
                patch.object(vhs_color_split_transcode.sys, "stdout", stdout),
            ):
                code = vhs_color_split_transcode.main(["--emit-tasks", str(task_file)], repo_root=REPO_ROOT)

            self.assertEqual(code, 0)
            self.assertEqual(task_file.read_text(encoding="utf-8"), python_print_tasks())
            self.assertIn(f"Wrote tasks to {task_file}", stdout.getvalue())
            self.assertIn(f"Run with: {REPO_ROOT / 'scripts' / 'transcode_parallel.sh'} '{task_file}'", stdout.getvalue())

    def test_run_invokes_all_transcode_commands_in_order(self) -> None:
        with (
            patch.dict(vhs_color_split_transcode.os.environ, {}, clear=True),
            patch.object(vhs_color_split_transcode.subprocess, "run") as run_mock,
        ):
            code = vhs_color_split_transcode.main(["run"], repo_root=REPO_ROOT)

        self.assertEqual(code, 0)
        self.assertEqual(
            run_mock.call_count,
            len(vhs_color_split_transcode.COLOR_CORRECT_FILES)
            + len(vhs_color_split_transcode.NO_COLOR_CORRECT_FILES),
        )
        tasks = vhs_color_split_transcode.build_tasks(
            vhs_color_split_transcode.config_from_env(REPO_ROOT, {})
        )
        for task, call in zip(tasks, run_mock.call_args_list, strict=True):
            command = call.args[0]
            self.assertEqual(command[-1], str(task.input_file))
            self.assertEqual("--vhs-color-correct" in command, task.color_correct)
            self.assertEqual("--audio-channel" in command, task.audio_channel != "keep")
            self.assertIn("--audio-gain", command)
            self.assertIn("12", command)

    def test_parallel_writes_tasks_and_invokes_runner_when_exec_disabled(self) -> None:
        with tempfile.TemporaryDirectory(dir=TEST_TMP_ROOT) as root:
            task_file = Path(root) / "tasks.txt"
            run_dir = Path(root) / "run"
            stdout = io.StringIO()

            with (
                patch.dict(vhs_color_split_transcode.os.environ, {}, clear=True),
                patch.object(vhs_color_split_transcode.subprocess, "run") as run_mock,
                patch.object(vhs_color_split_transcode.sys, "stdout", stdout),
            ):
                code = vhs_color_split_transcode.main(
                    ["--parallel", str(task_file), "--run-dir", str(run_dir)],
                    repo_root=REPO_ROOT,
                    exec_parallel=False,
                )

            self.assertEqual(code, 0)
            self.assertEqual(task_file.read_text(encoding="utf-8"), python_print_tasks())
            self.assertEqual(
                run_mock.call_args.args[0],
                [
                    str(REPO_ROOT / "scripts" / "transcode_parallel.sh"),
                    "--run-dir",
                    str(run_dir),
                    str(task_file),
                ],
            )
            self.assertIn(f"Wrote tasks to {task_file}", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
