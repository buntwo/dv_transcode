#!/usr/bin/env python3
"""Batch normalize Access MP4s using corresponding master FLAC audio."""

from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


AUDIO_EXTENSION = ".flac"
DEFAULT_TARGET_LUFS = -20.0
DEFAULT_TRUE_PEAK = -1.5
DEFAULT_LRA = 11.0
DEFAULT_AUDIO_BITRATE = "192k"
DEFAULT_DURATION_TOLERANCE = 0.05
NORMALIZATION_TYPE = "loudnorm-two-pass"
METADATA_FIELDS = [
    "access_file",
    "audio_file",
    "output_file",
    "video_duration_seconds",
    "audio_duration_seconds",
    "duration_delta_seconds",
    "target_lufs",
    "true_peak",
    "lra",
    "audio_bitrate",
    "input_i",
    "input_tp",
    "input_lra",
    "input_thresh",
    "output_i",
    "output_tp",
    "output_lra",
    "output_thresh",
    "normalization_type",
    "target_offset",
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replace Access audio with loudness-normalized AAC from matching FLAC files.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--access-copy-dir", type=Path, required=True)
    parser.add_argument("--audio-dir", type=Path, required=True)
    parser.add_argument("--target-lufs", type=float, default=DEFAULT_TARGET_LUFS)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--true-peak", type=float, default=DEFAULT_TRUE_PEAK)
    parser.add_argument("--lra", type=float, default=DEFAULT_LRA)
    parser.add_argument("--audio-bitrate", default=DEFAULT_AUDIO_BITRATE)
    parser.add_argument("--duration-tolerance", type=positive_float, default=DEFAULT_DURATION_TOLERANCE)
    parser.add_argument("--force", action="store_true")
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
    args.output_dir.mkdir(parents=True, exist_ok=True)


def audio_file_for_access(access_file: Path, audio_dir: Path) -> Path:
    return audio_dir / f"{access_file.stem}{AUDIO_EXTENSION}"


def list_access_files(access_copy_dir: Path) -> list[Path]:
    return sorted(path for path in access_copy_dir.glob("*.mp4") if path.is_file())


def format_float(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:g}"


def build_pass1_filter(target_lufs: float, true_peak: float, lra: float) -> str:
    return (
        f"loudnorm=I={format_float(target_lufs)}:"
        f"TP={format_float(true_peak)}:"
        f"LRA={format_float(lra)}:"
        "print_format=json"
    )


def build_pass1_command(audio_file: Path, args: argparse.Namespace) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-stats",
        "-loglevel",
        "info",
        "-i",
        str(audio_file),
        "-map",
        "0:a:0",
        "-af",
        build_pass1_filter(args.target_lufs, args.true_peak, args.lra),
        "-f",
        "null",
        "-",
    ]


def build_pass2_filter(
    target_lufs: float,
    true_peak: float,
    lra: float,
    stats: dict[str, object],
) -> str:
    measured_i = require_float(stats, "input_i")
    measured_tp = require_float(stats, "input_tp")
    measured_lra = require_float(stats, "input_lra")
    measured_thresh = require_float(stats, "input_thresh")
    target_offset = require_float(stats, "target_offset")

    return (
        f"loudnorm=I={format_float(target_lufs)}:"
        f"TP={format_float(true_peak)}:"
        f"LRA={format_float(lra)}:"
        f"measured_I={format_float(measured_i)}:"
        f"measured_TP={format_float(measured_tp)}:"
        f"measured_LRA={format_float(measured_lra)}:"
        f"measured_thresh={format_float(measured_thresh)}:"
        f"offset={format_float(target_offset)}:"
        "linear=true:"
        "print_format=json"
    )


def build_pass2_command(
    access_file: Path,
    audio_file: Path,
    output_file: Path,
    args: argparse.Namespace,
    pass1_stats: dict[str, object],
    overwrite: bool,
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
        build_pass2_filter(args.target_lufs, args.true_peak, args.lra, pass1_stats),
        *(["-y"] if overwrite else []),
        str(output_file),
    ]


def require_float(data: dict[str, object], key: str) -> float:
    value = data.get(key)
    if value is None:
        raise ValueError(f"missing loudnorm value: {key}")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid loudnorm value for {key}: {value!r}") from exc


def extract_loudnorm_json(stdout: str, stderr: str) -> dict[str, object]:
    payload = _find_first_json_object((stdout or "") + (stderr or ""))
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("failed to parse loudnorm JSON metadata") from exc


def _find_first_json_object(text: str) -> str:
    start = None
    depth = 0
    in_string = False
    escape = False

    for index, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
                continue
            if char == "\\":
                escape = True
                continue
            if char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
            continue
        if char == "{":
            if start is None:
                start = index
                depth = 1
            else:
                depth += 1
            continue
        if char == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start is not None:
                return text[start : index + 1]

    if start is None:
        raise ValueError("loudnorm output did not contain JSON")

    raise ValueError("loudnorm output contained unterminated JSON")


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


def run_pass1(audio_file: Path, args: argparse.Namespace) -> dict[str, object]:
    result = execute_ffmpeg(build_pass1_command(audio_file, args), stream_output=True)
    return extract_loudnorm_json(result.stdout, result.stderr)


def run_pass2(
    access_file: Path,
    audio_file: Path,
    output_file: Path,
    args: argparse.Namespace,
    pass1_stats: dict[str, object],
    overwrite: bool,
) -> dict[str, object]:
    result = execute_ffmpeg(
        build_pass2_command(access_file, audio_file, output_file, args, pass1_stats, overwrite),
        stream_output=True,
    )
    return extract_loudnorm_json(result.stdout, result.stderr)


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
    for access_file in access_files:
        audio_file = audio_file_for_access(access_file, args.audio_dir)
        output_file = args.output_dir / access_file.name
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
            )
        )

    return jobs


def metadata_row(
    job: NormalizationJob,
    pass1_stats: dict[str, object],
    pass2_stats: dict[str, object],
    target_lufs: float,
    true_peak: float,
    lra: float,
    audio_bitrate: str,
) -> dict[str, object]:
    return {
        "access_file": str(job.access_file.name),
        "audio_file": str(job.audio_file.name),
        "output_file": str(job.output_file.name),
        "video_duration_seconds": f"{job.video_duration_seconds:.6f}",
        "audio_duration_seconds": f"{job.audio_duration_seconds:.6f}",
        "duration_delta_seconds": f"{job.duration_delta_seconds:.6f}",
        "target_lufs": format_float(target_lufs),
        "true_peak": format_float(true_peak),
        "lra": format_float(lra),
        "audio_bitrate": audio_bitrate,
        "input_i": str(require_float(pass1_stats, "input_i")),
        "input_tp": str(require_float(pass1_stats, "input_tp")),
        "input_lra": str(require_float(pass1_stats, "input_lra")),
        "input_thresh": str(require_float(pass1_stats, "input_thresh")),
        "output_i": str(require_float(pass2_stats, "output_i")),
        "output_tp": str(require_float(pass2_stats, "output_tp")),
        "output_lra": str(require_float(pass2_stats, "output_lra")),
        "output_thresh": str(require_float(pass2_stats, "output_thresh")),
        "normalization_type": NORMALIZATION_TYPE,
        "target_offset": str(require_float(pass2_stats, "target_offset")),
    }


def write_metadata_csv(rows: list[dict[str, object]], metadata_path: Path) -> None:
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    with metadata_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=METADATA_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        validate_args(args)
        jobs = build_jobs(args)
        metadata_path = args.output_dir / "metadata.csv"
        rows: list[dict[str, object]] = []
        for job in jobs:
            pass1_stats = run_pass1(job.audio_file, args)
            pass2_stats = run_pass2(
                job.access_file,
                job.audio_file,
                job.output_file,
                args,
                pass1_stats,
                overwrite=args.force,
            )
            rows.append(
                metadata_row(
                    job,
                    pass1_stats,
                    pass2_stats,
                    args.target_lufs,
                    args.true_peak,
                    args.lra,
                    args.audio_bitrate,
                )
            )
            print(f"Wrote {job.output_file}")

        write_metadata_csv(rows, metadata_path)
        print(f"Wrote {metadata_path}")
    except (OSError, ValueError, subprocess.CalledProcessError, KeyError) as exc:
        print(f"normalize_access_audio.py: error: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
