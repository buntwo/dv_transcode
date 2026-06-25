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
    def test_default_print_tasks_matches_legacy_byte_for_byte(self) -> None:
        self.assertEqual(python_print_tasks(), legacy_print_tasks())

    def test_simple_env_overrides_match_legacy_byte_for_byte(self) -> None:
        env = {
            "VHS_DATA_ROOT": "/tmp/VideosRoot",
            "VHS_MASTER_ROOT": "/tmp/MastersRoot/tape",
        }

        self.assertEqual(python_print_tasks(env), legacy_print_tasks(env))

    def test_env_overrides_with_spaces_are_argv_equivalent(self) -> None:
        env = {
            "VHS_DATA_ROOT": str(TEST_TMP_ROOT / "Videos Root"),
            "VHS_MASTER_ROOT": str(TEST_TMP_ROOT / "Masters Root" / "tape"),
        }

        python_lines = python_print_tasks(env).splitlines()
        legacy_lines = legacy_print_tasks(env).splitlines()

        self.assertEqual(len(python_lines), len(legacy_lines))
        self.assertEqual([shlex.split(line) for line in python_lines], [shlex.split(line) for line in legacy_lines])

    def test_audio_channel_and_color_correction_grouping(self) -> None:
        lines = python_print_tasks().splitlines()

        self.assertEqual(len(lines), 24)
        self.assertTrue(all("--vhs-color-correct" in line for line in lines[:12]))
        self.assertTrue(all("--vhs-color-correct" not in line for line in lines[12:]))
        self.assertIn("--audio-channel right", lines[12])
        self.assertTrue(lines[12].endswith("/08.mkv"))


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
        self.assertEqual(run_mock.call_count, 24)
        first_command = run_mock.call_args_list[0].args[0]
        self.assertEqual(first_command[-1], str(vhs_color_split_transcode.DEFAULT_MASTER_ROOT / "05_Brian_Tu.mkv"))
        self.assertIn("--vhs-color-correct", first_command)
        thirteenth_command = run_mock.call_args_list[12].args[0]
        self.assertEqual(thirteenth_command[-3:], ["--audio-channel", "right", str(vhs_color_split_transcode.DEFAULT_MASTER_ROOT / "08.mkv")])

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
