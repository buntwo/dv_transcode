#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Callable, TextIO


VIDEO_SUFFIX = ".mp4"
DEFAULT_DURATION_TOLERANCE = Fraction(1, 1000)


class VerificationError(RuntimeError):
    """Raised when a collection or media file cannot be verified."""


@dataclass(frozen=True)
class VideoDuration:
    seconds: Fraction
    source: str


def natural_sort_key(path: Path) -> tuple[tuple[int, int | str], ...]:
    """Sort path numbers numerically while retaining deterministic text ordering."""
    components: list[tuple[int, int | str]] = []
    for token in re.split(r"(\d+)", path.as_posix().casefold()):
        if token.isdigit():
            components.append((0, int(token)))
        elif token:
            components.append((1, token))
    return tuple(components)


def discover_videos(root: Path, batch: str) -> dict[Path, Path]:
    """Return MP4 files keyed by their exact path relative to the batch directory."""
    batch_root = root / batch
    if not batch_root.is_dir():
        raise VerificationError(f"batch directory not found: {batch_root}")

    videos: dict[Path, Path] = {}
    for path in batch_root.rglob("*"):
        if not path.is_file() or path.suffix.lower() != VIDEO_SUFFIX:
            continue
        relative = path.relative_to(batch_root)
        videos[relative] = path
    return videos


def _decimal_fraction(value: object, *, label: str, path: Path) -> Fraction:
    try:
        return Fraction(Decimal(str(value)))
    except (InvalidOperation, ValueError, ZeroDivisionError) as exc:
        raise VerificationError(f"invalid {label} from ffprobe for {path}: {value!r}") from exc


def probe_video_duration(path: Path, ffprobe_bin: str = "ffprobe") -> VideoDuration:
    """Read the first video stream duration as an exact rational number of seconds."""
    command = [
        ffprobe_bin,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=duration_ts,time_base,duration:format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise VerificationError(f"ffprobe command not found: {ffprobe_bin}") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or f"exit status {exc.returncode}"
        raise VerificationError(f"ffprobe failed for {path}: {detail}") from exc

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise VerificationError(f"invalid ffprobe JSON for {path}: {exc}") from exc

    streams = payload.get("streams") or []
    if len(streams) != 1:
        raise VerificationError(f"expected one selected video stream in {path}, found {len(streams)}")

    stream = streams[0]
    duration_ts = stream.get("duration_ts")
    time_base = stream.get("time_base")
    if duration_ts not in (None, "N/A") and time_base not in (None, "N/A"):
        try:
            seconds = Fraction(int(duration_ts)) * Fraction(str(time_base))
        except (ValueError, ZeroDivisionError) as exc:
            raise VerificationError(
                f"invalid duration_ts/time_base from ffprobe for {path}: "
                f"{duration_ts!r}, {time_base!r}"
            ) from exc
        return VideoDuration(seconds=seconds, source="stream duration_ts × time_base")

    stream_duration = stream.get("duration")
    if stream_duration not in (None, "N/A"):
        return VideoDuration(
            seconds=_decimal_fraction(stream_duration, label="stream duration", path=path),
            source="stream duration",
        )

    format_duration = (payload.get("format") or {}).get("duration")
    if format_duration not in (None, "N/A"):
        return VideoDuration(
            seconds=_decimal_fraction(format_duration, label="format duration", path=path),
            source="format duration fallback",
        )

    raise VerificationError(f"no usable video duration reported for {path}")


def format_duration(duration: Fraction) -> str:
    total = float(duration)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{seconds:09.6f}"


def verify_batch(
    *,
    local_root: Path,
    reference_root: Path,
    batch: str,
    ffprobe_bin: str = "ffprobe",
    duration_tolerance: Fraction = DEFAULT_DURATION_TOLERANCE,
    quiet: bool = False,
    output: TextIO = sys.stdout,
    probe: Callable[[Path, str], VideoDuration] = probe_video_duration,
) -> bool:
    """Compare file count, exact relative names, and video durations within tolerance."""
    local_videos = discover_videos(local_root, batch)
    reference_videos = discover_videos(reference_root, batch)
    local_names = set(local_videos)
    reference_names = set(reference_videos)
    missing_local = sorted(reference_names - local_names, key=natural_sort_key)
    extra_local = sorted(local_names - reference_names, key=natural_sort_key)
    common = sorted(local_names & reference_names, key=natural_sort_key)

    print(f"Batch:     {batch}", file=output)
    print(f"Local:     {local_root / batch}", file=output)
    print(f"Reference: {reference_root / batch}", file=output)
    print(f"File count: local={len(local_videos)}, reference={len(reference_videos)}", file=output)
    print(f"Duration tolerance: {float(duration_tolerance):.6f} seconds", file=output)

    passed = True
    if len(local_videos) != len(reference_videos):
        passed = False
        print("FAIL: file counts differ", file=output)
    for relative in missing_local:
        passed = False
        print(f"MISSING LOCAL: {relative}", file=output)
    for relative in extra_local:
        passed = False
        print(f"EXTRA LOCAL:   {relative}", file=output)

    duration_matches = 0
    duration_mismatches = 0
    duration_errors = 0
    for index, relative in enumerate(common, start=1):
        try:
            local_duration = probe(local_videos[relative], ffprobe_bin)
            reference_duration = probe(reference_videos[relative], ffprobe_bin)
        except VerificationError as exc:
            passed = False
            duration_errors += 1
            print(f"DURATION ERROR: {relative}: {exc}", file=output)
            continue

        duration_delta = abs(local_duration.seconds - reference_duration.seconds)
        if duration_delta > duration_tolerance:
            passed = False
            duration_mismatches += 1
            print(f"DURATION MISMATCH: {relative}", file=output)
            print(
                f"  local:     {format_duration(local_duration.seconds)} "
                f"({local_duration.seconds} seconds; {local_duration.source})",
                file=output,
            )
            print(
                f"  reference: {format_duration(reference_duration.seconds)} "
                f"({reference_duration.seconds} seconds; {reference_duration.source})",
                file=output,
            )
            print(f"  difference: {float(duration_delta):.6f} seconds", file=output)
            continue

        duration_matches += 1
        if not quiet:
            print(
                f"[{index:0{len(str(len(common)))}d}/{len(common)}] OK "
                f"{relative} ({format_duration(local_duration.seconds)}; "
                f"difference={float(duration_delta):.6f}s)",
                file=output,
                flush=True,
            )

    print(
        "Duration results: "
        f"matched={duration_matches}, mismatched={duration_mismatches}, errors={duration_errors}",
        file=output,
    )
    print(
        "PASS: file names match and durations are within tolerance"
        if passed
        else "FAIL: collections differ",
        file=output,
    )
    return passed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare Access MP4 count, exact relative filenames, and video durations."
    )
    parser.add_argument("--local-root", type=Path, required=True)
    parser.add_argument("--reference-root", type=Path, required=True)
    parser.add_argument("--batch", required=True)
    parser.add_argument("--ffprobe-bin", default="ffprobe")
    parser.add_argument(
        "--duration-tolerance",
        type=Decimal,
        default=Decimal("0.001"),
        help="Maximum allowed absolute duration difference in seconds (default: 0.001)",
    )
    parser.add_argument("--quiet", action="store_true", help="Only print mismatches and the summary")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if shutil.which(args.ffprobe_bin) is None:
        print(f"ERROR: ffprobe command not found: {args.ffprobe_bin}", file=sys.stderr)
        return 2
    try:
        passed = verify_batch(
            local_root=args.local_root,
            reference_root=args.reference_root,
            batch=args.batch,
            ffprobe_bin=args.ffprobe_bin,
            duration_tolerance=Fraction(args.duration_tolerance),
            quiet=args.quiet,
        )
    except VerificationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
