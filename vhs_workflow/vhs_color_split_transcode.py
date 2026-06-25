#!/usr/bin/env python3
"""Generate and run VHS color-split transcode tasks."""

from __future__ import annotations

import os
import subprocess
import sys
from argparse import Namespace
from dataclasses import dataclass
from pathlib import Path


DEFAULT_DATA_ROOT = Path("/Users/btu/scratch/Videos")
DEFAULT_MASTER_ROOT = Path("/Volumes/TU/tu.brian.2026.05.09/data/masters/tape")
DEFAULT_TASK_FILE_NAME = "transcode_vhs_color_split.tasks"

COLOR_CORRECT_FILES = (
    "05_Brian_Tu.mkv",
    "06_Brian_15MO-1M.mkv",
    "07_Brian_19mo_-_24_Month.mkv",
    "10_Dinner_at_Tus_home_in_Shanghai_Nov._1991_Home_at_Duluth_1991.mkv",
    "11.mkv",
    "12_Master_Defense.mkv",
    "13_Brian_24_Month_-_36_Mo.mkv",
    "14.mkv",
    "15.mkv",
    "20_2.24.1998-10.5.1998_From_China_til_6.28.1998.mkv",
    "21_10.5.1998-11.5.1999.mkv",
    "22_11.5.1999-12.23.2000.mkv",
)

NO_COLOR_CORRECT_FILES = (
    "08.mkv",
    "16_6.17.1996-9.16.1996_From_China.mkv",
    "17_9.16.1996-1.12.1997_From_China.mkv",
    "18_2.8.1997-7.14.1997_From_China.mkv",
    "19_7.14.1997-2.24.1998_From_China.mkv",
    "23_12.23.2000.mkv",
    "24_Butterfield_Gallerie_of_Dance_5-6_Year_Olds_2000-2001.mkv",
    "25_Swim_Trial.mkv",
    "26_Zoe_Play_Narrator.mkv",
    "01_Y2K_1.mkv",
    "02_Y2K_2.mkv",
    "03_Y2K_3.mkv",
)


@dataclass(frozen=True)
class WorkflowConfig:
    repo_root: Path
    data_root: Path
    master_root: Path

    @property
    def default_task_file(self) -> Path:
        return self.data_root / DEFAULT_TASK_FILE_NAME


@dataclass(frozen=True)
class TranscodeTask:
    input_file: Path
    color_correct: bool
    audio_channel: str = "keep"


def parse_args(argv: list[str] | None = None) -> Namespace:
    return Namespace(args=list(sys.argv[1:] if argv is None else argv))


def usage() -> str:
    return """Usage:
  transcode_vhs_color_split.sh
      Run transcodes sequentially.

  transcode_vhs_color_split.sh --emit-tasks [TASK_FILE]
      Write one transcode command per source file.

  transcode_vhs_color_split.sh --print-tasks
      Print one transcode command per source file to stdout.

  transcode_vhs_color_split.sh --parallel [TASK_FILE] [--run-dir RUN_DIR]
      Write tasks, then run them through transcode_parallel.sh.

  transcode_vhs_color_split.sh --parallel [TASK_FILE] --resume RUN_DIR
      Write tasks from the current split, then resume an existing parallel run.

Set VHS_DATA_ROOT to override the default media/output root:
  VHS_DATA_ROOT=/path/to/Videos vhs_workflow/transcode_vhs_color_split.sh

Set VHS_MASTER_ROOT to override the default master media root:
  VHS_MASTER_ROOT=/path/to/masters/tape vhs_workflow/transcode_vhs_color_split.sh

Adjust live parallelism while --parallel is running:
  echo 1 > /Users/btu/scratch/Videos/vhs_transcode_parallelism
  echo 3 > /Users/btu/scratch/Videos/vhs_transcode_parallelism
  echo 0 > /Users/btu/scratch/Videos/vhs_transcode_parallelism
"""


def config_from_env(repo_root: Path, environ: dict[str, str] | None = None) -> WorkflowConfig:
    env = os.environ if environ is None else environ
    return WorkflowConfig(
        repo_root=repo_root,
        data_root=Path(env.get("VHS_DATA_ROOT", str(DEFAULT_DATA_ROOT))),
        master_root=Path(env.get("VHS_MASTER_ROOT", str(DEFAULT_MASTER_ROOT))),
    )


def build_tasks(config: WorkflowConfig) -> list[TranscodeTask]:
    tasks: list[TranscodeTask] = []
    for file_name in COLOR_CORRECT_FILES:
        tasks.append(TranscodeTask(config.master_root / file_name, color_correct=True))
    for file_name in NO_COLOR_CORRECT_FILES:
        audio_channel = "right" if file_name == "08.mkv" else "keep"
        tasks.append(TranscodeTask(config.master_root / file_name, color_correct=False, audio_channel=audio_channel))
    return tasks


def build_transcode_command(task: TranscodeTask, config: WorkflowConfig) -> list[str]:
    command = [
        "uv",
        "--project",
        str(config.repo_root),
        "run",
        str(config.repo_root / "scripts" / "transcode_access.py"),
        "--format",
        "vhs",
        "--encoder",
        "libx265",
        "--source-root",
        str(config.master_root),
        "--output-dir",
        str(config.data_root / "Access_crf22"),
        "--log-dir",
        str(config.data_root / "Logs_crf22"),
        "--crf",
        "22",
        "--yes",
    ]
    if task.color_correct:
        command.append("--vhs-color-correct")
    if task.audio_channel != "keep":
        command.extend(["--audio-channel", task.audio_channel])
    command.append(str(task.input_file))
    return command


def quote_command(command: list[str]) -> str:
    return " ".join(shell_quote(arg) for arg in command)


def shell_quote(value: str) -> str:
    if value == "":
        return "''"
    safe_chars = set("%+,-./0123456789:=@ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz")
    if all(char in safe_chars for char in value):
        return value
    return "".join(char if char in safe_chars else "\\" + char for char in value)


def task_lines(config: WorkflowConfig) -> list[str]:
    return [quote_command(build_transcode_command(task, config)) for task in build_tasks(config)]


def print_tasks(config: WorkflowConfig) -> None:
    for line in task_lines(config):
        print(line)


def write_tasks(config: WorkflowConfig, task_file: Path) -> None:
    task_file.write_text("\n".join(task_lines(config)) + "\n", encoding="utf-8")


def run_all_transcodes(config: WorkflowConfig) -> None:
    for task in build_tasks(config):
        subprocess.run(build_transcode_command(task, config), check=True)


def run_parallel(config: WorkflowConfig, args: list[str], *, exec_runner: bool = True) -> int:
    task_file = config.default_task_file
    task_file_set = False
    runner_args: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in {"--resume", "--run-dir"}:
            if index + 1 >= len(args):
                print(f"{arg} requires a directory path", file=sys.stderr)
                return 2
            runner_args.extend([arg, args[index + 1]])
            index += 2
            continue
        if arg in {"-h", "--help"}:
            print(usage(), end="")
            return 0
        if arg.startswith("-"):
            print(f"Unknown --parallel option: {arg}", file=sys.stderr)
            print(usage(), end="", file=sys.stderr)
            return 2
        if task_file_set:
            print("Only one TASK_FILE may be provided", file=sys.stderr)
            print(usage(), end="", file=sys.stderr)
            return 2
        task_file = Path(arg)
        task_file_set = True
        index += 1

    write_tasks(config, task_file)
    print(f"Wrote tasks to {task_file}")
    runner_command = [str(config.repo_root / "scripts" / "transcode_parallel.sh"), *runner_args, str(task_file)]
    if exec_runner:
        os.execv(runner_command[0], runner_command)
    subprocess.run(runner_command, check=True)
    return 0


def main(argv: list[str] | None = None, *, repo_root: Path | None = None, exec_parallel: bool = True) -> int:
    namespace = parse_args(argv)
    args = namespace.args
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent
    config = config_from_env(repo_root)
    mode = args[0] if args else "run"

    try:
        if mode in {"run", "--run"}:
            run_all_transcodes(config)
            return 0
        if mode == "--print-tasks":
            print_tasks(config)
            return 0
        if mode == "--emit-tasks":
            task_file = Path(args[1]) if len(args) > 1 else config.default_task_file
            write_tasks(config, task_file)
            print(f"Wrote tasks to {task_file}")
            print(f"Run with: {config.repo_root / 'scripts' / 'transcode_parallel.sh'} '{task_file}'")
            return 0
        if mode == "--parallel":
            return run_parallel(config, args[1:], exec_runner=exec_parallel)
        if mode in {"-h", "--help"}:
            print(usage(), end="")
            return 0

        print(usage(), end="", file=sys.stderr)
        return 2
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"vhs_color_split_transcode.py: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
