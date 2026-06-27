#!/usr/bin/env python3
"""Plan and run VHS color-split audio extraction and normalization jobs."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


AUDIO_CHANNELS = ("keep", "left", "right")
DEFAULT_FIXED_GAIN = 12.0
DEFAULT_PEAK_CEILING = -1.5


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

    @property
    def skipped_output_count(self) -> int:
        return sum(1 for job in self.jobs if job.output_status == "exists, skip")

    @property
    def runnable_job_count(self) -> int:
        if self.force:
            return len(self.jobs)
        return sum(1 for job in self.jobs if job.output_status != "exists, skip")


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
    normalize.add_argument("--gain", type=float, default=DEFAULT_FIXED_GAIN, help="Fixed audio gain in dB")
    normalize.add_argument("--peak-ceiling", type=float, default=DEFAULT_PEAK_CEILING, help="Maximum post-gain peak in dB")
    normalize.add_argument(
        "--vhs-notch",
        choices=("auto", "ntsc", "pal", "off"),
        default="auto",
        help="Apply the VHS highpass and scan-frequency notch filter before fixed gain",
    )
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
            status = "exists, overwrite" if force else "exists, skip"
        jobs.append(ExtractJob(task.input_file, task.audio_channel, output_file, status))
    return ExtractPlan(transcode_list, output_dir, force, jobs)


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
    lines.append(f"Extraction jobs to run: {plan.runnable_job_count}")
    lines.append(f"Existing FLAC outputs skipped: {plan.skipped_output_count}")

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


def format_float(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:g}"


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
        if job.output_file.exists() and not plan.force:
            continue
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


def run_normalize(
    *,
    access_copy_dir: Path,
    audio_dir: Path,
    output_dir: Path,
    force: bool,
    gain: float,
    peak_ceiling: float,
    vhs_notch: str,
    repo_root: Path,
) -> None:
    script = repo_root / "scripts" / "normalize_access_audio.py"
    cmd = [
        "uv",
        "--project",
        str(repo_root),
        "run",
        str(script),
        "--access-copy-dir",
        str(access_copy_dir),
        "--audio-dir",
        str(audio_dir),
        "--output-dir",
        str(output_dir),
        "--gain",
        format_float(gain),
        "--peak-ceiling",
        format_float(peak_ceiling),
        "--vhs-notch",
        vhs_notch,
    ]
    if force:
        cmd.append("--force")
    subprocess.run(cmd, check=True)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "extract-audio":
            tasks = load_tasks(args.transcode_list)
            plan = build_extract_plan(tasks, args.output_dir, args.force, args.transcode_list)
            print(format_extract_plan(plan))
            if plan.runnable_job_count == 0:
                print("No extraction jobs to run.")
                return 0
            if not confirm("Proceed with these extraction jobs? [y/N] "):
                return 0
            run_extract(plan, args.repo_root)
            return 0

        if args.command == "normalize-audio":
            print(
                "Handing off normalization preflight to normalize_access_audio.py "
                f"for Access directory: {args.access_copy_dir}"
            )
            run_normalize(
                access_copy_dir=args.access_copy_dir,
                audio_dir=args.audio_dir,
                output_dir=args.output_dir,
                force=args.force,
                gain=args.gain,
                peak_ceiling=args.peak_ceiling,
                vhs_notch=args.vhs_notch,
                repo_root=args.repo_root,
            )
            return 0

        raise ValueError(f"unsupported command: {args.command}")
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"vhs_color_split_audio.py: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
