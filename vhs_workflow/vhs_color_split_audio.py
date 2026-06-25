#!/usr/bin/env python3
"""Plan and run VHS color-split audio extraction and normalization jobs."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


AUDIO_CHANNELS = ("keep", "left", "right")


@dataclass(frozen=True)
class TranscodeTask:
    input_file: Path
    audio_channel: str = "keep"


@dataclass(frozen=True)
class ExtractJob:
    input_file: Path
    audio_channel: str
    output_file: Path
    output_status: str


@dataclass(frozen=True)
class ExtractPlan:
    transcode_list: Path
    output_dir: Path
    force: bool
    jobs: list[ExtractJob]

    @property
    def channel_counts(self) -> dict[str, int]:
        return {channel: sum(1 for job in self.jobs if job.audio_channel == channel) for channel in AUDIO_CHANNELS}


@dataclass(frozen=True)
class NormalizeJob:
    stem: str
    access_file: Path
    audio_file: Path
    output_file: Path
    audio_status: str
    output_status: str


@dataclass(frozen=True)
class NormalizePlan:
    transcode_list: Path
    access_copy_dir: Path
    audio_dir: Path
    output_dir: Path
    force: bool
    jobs: list[NormalizeJob]

    @property
    def missing_audio_count(self) -> int:
        return sum(1 for job in self.jobs if job.audio_status == "missing")

    @property
    def existing_output_count(self) -> int:
        return sum(1 for job in self.jobs if job.output_status.startswith("exists,"))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan VHS color-split audio extraction and normalization jobs.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--repo-root",
        "--project-root",
        dest="repo_root",
        type=Path,
        default=None,
        help="Repository root for uv Python scripts",
    )
    parser.add_argument(
        "--transcode-list",
        type=Path,
        default=None,
        help="Path to transcode_vhs_color_split.sh",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    extract = subparsers.add_parser(
        "extract-audio",
        help="Extract matching master audio FLAC files.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    extract.add_argument("--output-dir", type=Path, required=True, help="Directory to write extracted FLAC files")
    extract.add_argument("--force", action="store_true", help="Overwrite existing FLAC files")
    extract.add_argument("--repo-root", "--project-root", dest="repo_root", type=Path, default=argparse.SUPPRESS)
    extract.add_argument("--transcode-list", type=Path, default=argparse.SUPPRESS)

    normalize = subparsers.add_parser(
        "normalize-audio",
        help="Normalize Access MP4 audio from matching FLAC files.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    normalize.add_argument("--access-copy-dir", type=Path, required=True, help="Directory with Access MP4 files")
    normalize.add_argument("--audio-dir", type=Path, required=True, help="Directory containing matching FLAC files")
    normalize.add_argument("--output-dir", type=Path, required=True, help="Directory for normalized MP4 output")
    normalize.add_argument("--force", action="store_true", help="Overwrite existing outputs")
    normalize.add_argument("--repo-root", "--project-root", dest="repo_root", type=Path, default=argparse.SUPPRESS)
    normalize.add_argument("--transcode-list", type=Path, default=argparse.SUPPRESS)

    args = parser.parse_args(argv)
    default_repo_root = Path(__file__).resolve().parent.parent
    if args.repo_root is None:
        args.repo_root = default_repo_root
    if args.transcode_list is None:
        args.transcode_list = args.repo_root / "vhs_workflow" / "transcode_vhs_color_split.sh"
    return args


def parse_task_line(line: str) -> TranscodeTask:
    parts = shlex.split(line)
    if not parts:
        raise ValueError("empty task line")

    audio_channel = "keep"
    for index, part in enumerate(parts):
        if part == "--audio-channel":
            if index + 1 >= len(parts):
                raise ValueError(f"missing --audio-channel value in task: {line}")
            audio_channel = parts[index + 1]
            break
    if audio_channel not in AUDIO_CHANNELS:
        raise ValueError(f"unsupported audio channel in task: {audio_channel}")

    return TranscodeTask(input_file=Path(parts[-1]), audio_channel=audio_channel)


def parse_task_lines(stdout: str) -> list[TranscodeTask]:
    tasks: list[TranscodeTask] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        tasks.append(parse_task_line(line))
    return tasks


def load_tasks(transcode_list: Path) -> list[TranscodeTask]:
    if not transcode_list.exists():
        raise ValueError(f"transcode list script not found: {transcode_list}")
    if not os.access(transcode_list, os.X_OK):
        raise ValueError(f"transcode list script not executable: {transcode_list}")

    result = subprocess.run(
        [str(transcode_list), "--print-tasks"],
        check=True,
        capture_output=True,
        text=True,
    )
    return parse_task_lines(result.stdout)


def flac_output_path(input_file: Path, output_dir: Path) -> Path:
    return output_dir / f"{input_file.stem}.flac"


def build_extract_plan(tasks: list[TranscodeTask], output_dir: Path, force: bool, transcode_list: Path) -> ExtractPlan:
    jobs: list[ExtractJob] = []
    for task in tasks:
        output_file = flac_output_path(task.input_file, output_dir)
        status = "will write"
        if output_file.exists():
            status = "exists, overwrite" if force else "exists, needs --force"
        jobs.append(ExtractJob(task.input_file, task.audio_channel, output_file, status))
    return ExtractPlan(transcode_list, output_dir, force, jobs)


def build_normalize_plan(
    tasks: list[TranscodeTask],
    access_copy_dir: Path,
    audio_dir: Path,
    output_dir: Path,
    force: bool,
    transcode_list: Path,
) -> NormalizePlan:
    jobs: list[NormalizeJob] = []
    for task in tasks:
        stem = task.input_file.stem
        access_file = access_copy_dir / f"{stem}.mp4"
        audio_file = audio_dir / f"{stem}.flac"
        output_file = output_dir / f"{stem}.mp4"
        if not access_file.is_file():
            raise ValueError(f"Expected Access file not found: {access_file}")

        audio_status = "found" if audio_file.is_file() else "missing"
        output_status = "will write"
        if output_file.exists():
            output_status = "exists, overwrite" if force else "exists, needs --force"

        jobs.append(NormalizeJob(stem, access_file, audio_file, output_file, audio_status, output_status))
    return NormalizePlan(transcode_list, access_copy_dir, audio_dir, output_dir, force, jobs)


def common_parent(paths: list[Path]) -> str:
    if not paths:
        return ""
    parents = [str(path.parent) for path in paths]
    try:
        return os.path.commonpath(parents)
    except ValueError:
        return parents[0]


def relative_to_parent(parent: str, path: Path) -> str:
    path_text = str(path)
    if parent and path_text.startswith(parent.rstrip(os.sep) + os.sep):
        return path_text[len(parent.rstrip(os.sep)) + 1 :]
    return path_text


def format_extract_plan(plan: ExtractPlan) -> str:
    lines: list[str] = []
    total_files = len(plan.jobs)
    counts = plan.channel_counts
    lines.append(f"Found {total_files} source file(s) in transcode list: {plan.transcode_list}")
    lines.append(f"Keep channel: {counts['keep']}")
    lines.append(f"Left channel: {counts['left']}")
    lines.append(f"Right channel: {counts['right']}")
    lines.append(f"Planned FLAC outputs in: {plan.output_dir}")

    if total_files == 0:
        lines.append("No source files found.")
        return "\n".join(lines)

    input_parent = common_parent([job.input_file for job in plan.jobs])
    rows = [
        (
            str(index),
            job.audio_channel,
            relative_to_parent(input_parent, job.input_file),
            str(job.output_file),
            job.output_status,
        )
        for index, job in enumerate(plan.jobs, start=1)
    ]
    widths = _table_widths(
        ("#", "channel", "input", "output", "status"),
        rows,
        right_aligned_columns={0},
    )

    lines.append(f"Input parent: {input_parent}")
    lines.append("Planned extraction list:")
    lines.extend(_format_table(("#", "channel", "input", "output", "status"), rows, widths, {0}))
    return "\n".join(lines)


def format_normalize_plan(plan: NormalizePlan) -> str:
    lines: list[str] = []
    total_files = len(plan.jobs)
    lines.append(f"Found {total_files} source file(s) in transcode list: {plan.transcode_list}")
    lines.append(f"Using access directory: {plan.access_copy_dir}")
    lines.append(f"Using audio directory: {plan.audio_dir}")
    lines.append(f"Output directory: {plan.output_dir}")
    lines.append(f"Metadata CSV: {plan.output_dir / 'metadata.csv'}")
    lines.append(f"Matching FLAC files missing: {plan.missing_audio_count}")
    lines.append(f"Planned outputs already present: {plan.existing_output_count}")

    if total_files == 0:
        lines.append("No source files found.")
        return "\n".join(lines)

    access_parent = common_parent([job.access_file for job in plan.jobs])
    rows = [
        (
            str(index),
            f"{job.stem}.mp4",
            relative_to_parent(access_parent, job.access_file),
            str(job.audio_file),
            str(job.output_file),
            job.audio_status,
            job.output_status,
        )
        for index, job in enumerate(plan.jobs, start=1)
    ]
    headers = ("#", "file", "access", "audio", "output", "audio", "status")
    widths = _table_widths(headers, rows, right_aligned_columns={0})

    lines.append(f"Access parent: {access_parent}")
    lines.append("Planned normalization list:")
    lines.extend(_format_table(headers, rows, widths, {0}))
    return "\n".join(lines)


def _table_widths(
    headers: tuple[str, ...],
    rows: list[tuple[str, ...]],
    right_aligned_columns: set[int],
) -> list[int]:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    for index in right_aligned_columns:
        widths[index] = max(widths[index], 3)
    return widths


def _format_table(
    headers: tuple[str, ...],
    rows: list[tuple[str, ...]],
    widths: list[int],
    right_aligned_columns: set[int],
) -> list[str]:
    separator = tuple("-" * len(header) for header in headers)
    return [
        _format_table_row(headers, widths, right_aligned_columns),
        _format_table_row(separator, widths, right_aligned_columns),
        *[_format_table_row(row, widths, right_aligned_columns) for row in rows],
    ]


def _format_table_row(row: tuple[str, ...], widths: list[int], right_aligned_columns: set[int]) -> str:
    cells: list[str] = []
    for index, value in enumerate(row):
        if index in right_aligned_columns:
            cells.append(f"{value:>{widths[index]}}")
        else:
            cells.append(f"{value:<{widths[index]}}")
    return "  " + "  ".join(cells)


def confirm(prompt: str, *, input_stream: object | None = None, output_stream: object | None = None) -> bool:
    if input_stream is None:
        input_stream = sys.stdin
    if output_stream is None:
        output_stream = sys.stdout
    if hasattr(input_stream, "isatty") and not input_stream.isatty():
        print("Interactive confirmation required. Re-run from a terminal.", file=sys.stderr)
        return False

    while True:
        print(prompt, end="", file=output_stream, flush=True)
        answer = input_stream.readline()
        if answer == "":
            print("Aborted.", file=output_stream)
            return False
        answer = answer.strip()
        if answer in {"y", "Y"}:
            return True
        if answer in {"", "n", "N"}:
            print("Aborted.", file=output_stream)
            return False
        print("Please answer y or n.", file=output_stream)


def run_extract(plan: ExtractPlan, repo_root: Path) -> None:
    grouped: dict[str, list[Path]] = {channel: [] for channel in AUDIO_CHANNELS}
    for job in plan.jobs:
        grouped[job.audio_channel].append(job.input_file)

    script = repo_root / "scripts" / "extract_master_audio.py"
    for channel in AUDIO_CHANNELS:
        files = grouped[channel]
        if not files:
            continue
        cmd = [
            "uv",
            "--project",
            str(repo_root),
            "run",
            str(script),
            "--output-dir",
            str(plan.output_dir),
        ]
        if plan.force:
            cmd.append("--force")
        if channel != "keep":
            cmd.extend(["--audio-channel", channel])
        cmd.extend(str(path) for path in files)
        subprocess.run(cmd, check=True)


def run_normalize(plan: NormalizePlan, repo_root: Path) -> None:
    script = repo_root / "scripts" / "normalize_access_audio.py"
    with tempfile.TemporaryDirectory() as tmp_access:
        tmp_access_path = Path(tmp_access)
        for job in plan.jobs:
            os.symlink(job.access_file, tmp_access_path / f"{job.stem}.mp4")

        cmd = [
            "uv",
            "--project",
            str(repo_root),
            "run",
            str(script),
            "--access-copy-dir",
            str(tmp_access_path),
            "--audio-dir",
            str(plan.audio_dir),
            "--output-dir",
            str(plan.output_dir),
        ]
        if plan.force:
            cmd.append("--force")
        subprocess.run(cmd, check=True)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        tasks = load_tasks(args.transcode_list)
        if args.command == "extract-audio":
            plan = build_extract_plan(tasks, args.output_dir, args.force, args.transcode_list)
            print(format_extract_plan(plan))
            if not confirm("Proceed with these extraction jobs? [y/N] "):
                return 0
            run_extract(plan, args.repo_root)
            return 0

        if args.command == "normalize-audio":
            plan = build_normalize_plan(
                tasks,
                args.access_copy_dir,
                args.audio_dir,
                args.output_dir,
                args.force,
                args.transcode_list,
            )
            print(format_normalize_plan(plan))
            if not confirm("Proceed with these normalization jobs? [y/N] "):
                return 0
            run_normalize(plan, args.repo_root)
            return 0

        raise ValueError(f"unsupported command: {args.command}")
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"vhs_color_split_audio.py: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
