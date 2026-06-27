#!/usr/bin/env python3
"""Batch replace Access MP4 audio with fixed-gain AAC from matching master FLAC audio."""

from __future__ import annotations

import argparse
import csv
import math
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from audio_volume_analysis import (
    DEFAULT_GAIN,
    DEFAULT_PEAK_CEILING,
    VolumeAnalysis,
    format_db,
    format_volume_analysis_table,
    run_volumedetect,
)
from transcode_core import build_vhs_audio_filter
from utils import format_progress

AUDIO_EXTENSION = ".flac"
DEFAULT_AUDIO_BITRATE = "192k"
DEFAULT_DURATION_TOLERANCE = 0.05
NORMALIZATION_TYPE = "fixed-gain"
METADATA_FIELDS = [
    "access_file",
    "audio_file",
    "output_file",
    "video_duration_seconds",
    "audio_duration_seconds",
    "duration_delta_seconds",
    "gain",
    "peak_ceiling",
    "mean_volume",
    "max_volume",
    "estimated_post_gain_peak",
    "headroom",
    "status",
    "audio_bitrate",
    "normalization_type",
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replace Access audio with fixed-gain AAC from matching FLAC files.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--access-copy-dir", type=Path, required=True)
    parser.add_argument("--audio-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--gain", type=float, default=DEFAULT_GAIN)
    parser.add_argument("--peak-ceiling", type=float, default=DEFAULT_PEAK_CEILING)
    parser.add_argument("--audio-bitrate", default=DEFAULT_AUDIO_BITRATE)
    parser.add_argument("--duration-tolerance", type=positive_float, default=DEFAULT_DURATION_TOLERANCE)
    parser.add_argument(
        "--vhs-notch",
        choices=("auto", "ntsc", "pal", "off"),
        default="off",
        help="Apply the VHS highpass and scan-frequency notch filter before fixed gain",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--yes", action="store_true", help="Skip interactive confirmations")
    parser.add_argument("--verbose", action="store_true", help="Show ffmpeg output during fixed-gain analysis")
    return parser.parse_args(argv)


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


@dataclass(frozen=True)
class NormalizationJob:
    access_file: Path
    audio_file: Path
    output_file: Path
    video_duration_seconds: float
    audio_duration_seconds: float
    duration_delta_seconds: float
    audio_filter: str | None


@dataclass(frozen=True)
class FixedGainReview:
    job: NormalizationJob
    analysis: VolumeAnalysis


@dataclass(frozen=True)
class VhsAudioFilterConfig:
    format_type: str
    vhs_notch: str


def validate_args(args: argparse.Namespace) -> None:
    if shutil.which("ffmpeg") is None:
        raise ValueError("ffmpeg is required")
    if shutil.which("ffprobe") is None:
        raise ValueError("ffprobe is required")
    if not args.access_copy_dir.exists():
        raise ValueError(f"access-copy directory does not exist: {args.access_copy_dir}")
    if not args.access_copy_dir.is_dir():
        raise ValueError(f"access-copy must be a directory: {args.access_copy_dir}")
    if not args.audio_dir.exists():
        raise ValueError(f"audio directory does not exist: {args.audio_dir}")
    if not args.audio_dir.is_dir():
        raise ValueError(f"audio must be a directory: {args.audio_dir}")


def audio_file_for_access(access_file: Path, audio_dir: Path) -> Path:
    return audio_dir / f"{access_file.stem}{AUDIO_EXTENSION}"


def list_access_files(access_copy_dir: Path) -> list[Path]:
    return sorted(path for path in access_copy_dir.glob("*.mp4") if path.is_file())


def format_float(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:g}"


def combine_audio_filters(*filters: str | None) -> str:
    return ",".join(audio_filter for audio_filter in filters if audio_filter)


def build_job_audio_filter(args: argparse.Namespace, access_file: Path) -> str | None:
    return build_vhs_audio_filter(VhsAudioFilterConfig("vhs", args.vhs_notch), access_file)


def build_fixed_gain_command(
    access_file: Path,
    audio_file: Path,
    output_file: Path,
    args: argparse.Namespace,
    overwrite: bool,
    audio_filter: str | None = None,
) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-stats",
        "-loglevel",
        "info",
        "-i",
        str(access_file),
        "-i",
        str(audio_file),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        args.audio_bitrate,
        "-af",
        combine_audio_filters(audio_filter, f"volume={format_float(args.gain)}dB"),
        *(["-y"] if overwrite else []),
        str(output_file),
    ]


def execute_ffmpeg(cmd: list[str], *, stream_output: bool = False) -> subprocess.CompletedProcess[str]:
    if not stream_output:
        return subprocess.run(cmd, check=True, capture_output=True, text=True)

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    output_parts: list[str] = []
    while True:
        chunk = process.stdout.read(1)
        if chunk == "":
            break
        output_parts.append(chunk)
        sys.stderr.write(chunk)
        sys.stderr.flush()
    returncode = process.wait()
    output = "".join(output_parts)
    if returncode != 0:
        raise subprocess.CalledProcessError(returncode, cmd, output=output, stderr=output)
    return subprocess.CompletedProcess(cmd, returncode, stdout=output, stderr="")


def run_fixed_gain_remux(
    access_file: Path,
    audio_file: Path,
    output_file: Path,
    args: argparse.Namespace,
    overwrite: bool,
    audio_filter: str | None = None,
) -> None:
    execute_ffmpeg(
        build_fixed_gain_command(access_file, audio_file, output_file, args, overwrite, audio_filter),
        stream_output=True,
    )


def probe_media_duration_seconds(path: Path) -> float:
    proc = execute_ffmpeg(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
    )
    value = proc.stdout.strip()
    if not value:
        raise ValueError(f"ffprobe returned no duration for {path}")
    try:
        seconds = float(value)
    except ValueError as exc:
        raise ValueError(f"invalid duration from ffprobe for {path}: {value!r}") from exc
    if not math.isfinite(seconds):
        raise ValueError(f"non-finite duration from ffprobe for {path}: {value!r}")
    return seconds


def build_jobs(args: argparse.Namespace) -> list[NormalizationJob]:
    access_files = list_access_files(args.access_copy_dir)
    if not access_files:
        raise ValueError(f"no MP4 files found in {args.access_copy_dir}")

    missing_audio: list[Path] = []
    output_conflicts: list[Path] = []
    for access_file in access_files:
        audio_file = audio_file_for_access(access_file, args.audio_dir)
        output_file = args.output_dir / access_file.name
        if not audio_file.exists():
            missing_audio.append(access_file)
        if output_file.exists() and not args.force:
            output_conflicts.append(output_file)

    if missing_audio:
        raise ValueError(
            "missing matching FLAC files for: "
            + ", ".join(file.name for file in missing_audio)
        )
    if output_conflicts:
        raise ValueError(
            "output files already exist (use --force): "
            + ", ".join(file.name for file in output_conflicts)
        )

    jobs: list[NormalizationJob] = []
    total = len(access_files)
    for access_file in access_files:
        print(
            "Checking durations "
            + format_progress(len(jobs) + 1, total, access_file),
            file=sys.stderr,
            flush=True,
        )
        audio_file = audio_file_for_access(access_file, args.audio_dir)
        output_file = args.output_dir / access_file.name
        audio_filter = build_job_audio_filter(args, access_file)
        video_duration = probe_media_duration_seconds(access_file)
        audio_duration = probe_media_duration_seconds(audio_file)
        delta = abs(video_duration - audio_duration)
        if delta > args.duration_tolerance:
            raise ValueError(
                f"duration mismatch for {access_file.name}: "
                f"video={video_duration:.3f}s audio={audio_duration:.3f}s delta={delta:.3f}s "
                f"(tolerance {args.duration_tolerance:.3f}s)"
            )

        jobs.append(
            NormalizationJob(
                access_file=access_file,
                audio_file=audio_file,
                output_file=output_file,
                video_duration_seconds=video_duration,
                audio_duration_seconds=audio_duration,
                duration_delta_seconds=delta,
                audio_filter=audio_filter,
            )
        )

    return jobs


def analyze_fixed_gain(args: argparse.Namespace, jobs: list[NormalizationJob]) -> list[FixedGainReview]:
    reviews: list[FixedGainReview] = []
    total = len(jobs)
    for index, job in enumerate(jobs, start=1):
        reviews.append(
            FixedGainReview(
                job,
                VolumeAnalysis(
                    job.audio_file,
                    run_volumedetect(job.audio_file, audio_filter=job.audio_filter, verbose=args.verbose),
                    args.gain,
                    args.peak_ceiling,
                ),
            )
        )
        if not args.verbose:
            print(format_progress(index, total, job.audio_file), file=sys.stderr, flush=True)
    return reviews


def fail_if_unsafe_fixed_gain(reviews: list[FixedGainReview]) -> None:
    unsafe = [review for review in reviews if review.analysis.headroom < 0]
    if unsafe:
        raise ValueError(
            "fixed gain would exceed peak ceiling for: "
            + ", ".join(review.job.access_file.name for review in unsafe)
        )


def format_preflight_report(
    args: argparse.Namespace,
    jobs: list[NormalizationJob],
    fixed_gain_reviews: list[FixedGainReview] | None = None,
) -> str:
    lines: list[str] = []
    lines.append(f"Found {len(jobs)} Access MP4 file(s): {args.access_copy_dir}")
    lines.append(f"Using audio directory: {args.audio_dir}")
    lines.append(f"Output directory: {args.output_dir}")
    lines.append(f"Metadata CSV: {args.output_dir / 'metadata.csv'}")
    lines.append(f"Method: fixed-gain")
    lines.append(f"Fixed gain: {format_float(args.gain)} dB, peak ceiling {format_float(args.peak_ceiling)} dB")
    lines.append(f"Duration tolerance: {args.duration_tolerance:.3f}s")
    lines.append(f"VHS notch: {args.vhs_notch}")

    rows = [
        (
            str(index),
            job.access_file.name,
            job.audio_file.name,
            job.output_file.name,
            f"{job.video_duration_seconds:.3f}",
            f"{job.audio_duration_seconds:.3f}",
            f"{job.duration_delta_seconds:.3f}",
        )
        for index, job in enumerate(jobs, start=1)
    ]
    headers = ("#", "access", "audio", "output", "video_s", "audio_s", "delta_s")
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))

    lines.append("Preflight normalization list:")
    lines.append(format_table_row(headers, widths, right_aligned={0, 4, 5, 6}))
    lines.append(format_table_row(tuple("-" * len(header) for header in headers), widths, right_aligned={0, 4, 5, 6}))
    for row in rows:
        lines.append(format_table_row(row, widths, right_aligned={0, 4, 5, 6}))

    if fixed_gain_reviews is not None:
        lines.append("")
        lines.append(
            f"Fixed-gain analysis: gain={format_db(args.gain)} dB "
            f"peak ceiling={format_db(args.peak_ceiling)} dB"
        )
        lines.append(format_volume_analysis_table([review.analysis for review in fixed_gain_reviews]))
    return "\n".join(lines)


def format_table_row(row: tuple[str, ...], widths: list[int], right_aligned: set[int]) -> str:
    cells: list[str] = []
    for index, value in enumerate(row):
        if index in right_aligned:
            cells.append(f"{value:>{widths[index]}}")
        else:
            cells.append(f"{value:<{widths[index]}}")
    return "  " + "  ".join(cells)


def confirm_preflight() -> bool:
    if hasattr(sys.stdin, "isatty") and not sys.stdin.isatty():
        raise ValueError("interactive confirmation required; re-run from a terminal or pass --yes")

    while True:
        print("Proceed with these normalization jobs? [y/N] ", end="", flush=True)
        answer = sys.stdin.readline()
        if answer == "":
            print("Aborted.")
            return False
        answer = answer.strip()
        if answer in {"y", "Y"}:
            return True
        if answer in {"", "n", "N"}:
            print("Aborted.")
            return False
        print("Please answer y or n.")


def fixed_gain_metadata_row(review: FixedGainReview, audio_bitrate: str) -> dict[str, object]:
    job = review.job
    analysis = review.analysis
    return {
        "access_file": str(job.access_file.name),
        "audio_file": str(job.audio_file.name),
        "output_file": str(job.output_file.name),
        "video_duration_seconds": f"{job.video_duration_seconds:.6f}",
        "audio_duration_seconds": f"{job.audio_duration_seconds:.6f}",
        "duration_delta_seconds": f"{job.duration_delta_seconds:.6f}",
        "gain": format_float(analysis.gain),
        "peak_ceiling": format_float(analysis.peak_ceiling),
        "mean_volume": str(analysis.stats.mean_volume),
        "max_volume": str(analysis.stats.max_volume),
        "estimated_post_gain_peak": str(analysis.estimated_post_gain_peak),
        "headroom": str(analysis.headroom),
        "status": analysis.status,
        "audio_bitrate": audio_bitrate,
        "normalization_type": NORMALIZATION_TYPE,
    }


def write_metadata_csv(
    rows: list[dict[str, object]],
    metadata_path: Path,
    fieldnames: list[str] | None = None,
) -> None:
    if fieldnames is None:
        fieldnames = METADATA_FIELDS
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    with metadata_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def run_fixed_gain_mode(
    args: argparse.Namespace,
    reviews: list[FixedGainReview],
    metadata_path: Path,
) -> None:
    rows: list[dict[str, object]] = []
    for review in reviews:
        job = review.job
        run_fixed_gain_remux(
            job.access_file,
            job.audio_file,
            job.output_file,
            args,
            overwrite=args.force,
            audio_filter=job.audio_filter,
        )
        rows.append(fixed_gain_metadata_row(review, args.audio_bitrate))
        print(f"Wrote {job.output_file}")

    write_metadata_csv(rows, metadata_path, METADATA_FIELDS)
    print(f"Wrote {metadata_path}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        validate_args(args)
        jobs = build_jobs(args)
        fixed_gain_reviews = analyze_fixed_gain(args, jobs)
        fail_if_unsafe_fixed_gain(fixed_gain_reviews)
        print(format_preflight_report(args, jobs, fixed_gain_reviews))
        if not args.yes and not confirm_preflight():
            return 0
        args.output_dir.mkdir(parents=True, exist_ok=True)
        metadata_path = args.output_dir / "metadata.csv"
        run_fixed_gain_mode(args, fixed_gain_reviews, metadata_path)
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError, KeyError) as exc:
        print(f"normalize_access_audio.py: error: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
