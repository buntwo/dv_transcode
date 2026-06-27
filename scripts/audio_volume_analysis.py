#!/usr/bin/env python3
"""Analyze audio peak headroom for fixed-gain workflows."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from utils import format_progress


AUDIO_EXTENSION = ".flac"
DEFAULT_GAIN = 12.0
DEFAULT_PEAK_CEILING = -1.5


@dataclass(frozen=True)
class VolumeDetectStats:
    mean_volume: float
    max_volume: float


@dataclass(frozen=True)
class VolumeAnalysis:
    audio_file: Path
    stats: VolumeDetectStats
    gain: float
    peak_ceiling: float

    @property
    def estimated_post_gain_peak(self) -> float:
        return self.stats.max_volume + self.gain

    @property
    def headroom(self) -> float:
        return self.peak_ceiling - self.estimated_post_gain_peak

    @property
    def status(self) -> str:
        return "ok" if self.headroom >= 0 else "exceeds ceiling"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze FLAC volume and fixed-gain peak headroom.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("audio_files", nargs="*", type=Path, help="Audio files to analyze")
    parser.add_argument("--audio-dir", type=Path, help="Directory containing FLAC files to analyze")
    parser.add_argument("--gain", type=float, default=DEFAULT_GAIN, help="Fixed gain in dB")
    parser.add_argument("--peak-ceiling", type=float, default=DEFAULT_PEAK_CEILING, help="Maximum post-gain peak in dB")
    parser.add_argument("--verbose", action="store_true", help="Show ffmpeg volumedetect output")
    return parser.parse_args(argv)


def format_float(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:g}"


def format_db(value: float) -> str:
    return f"{value:+.2f}"


def list_audio_files(audio_dir: Path) -> list[Path]:
    return sorted(path for path in audio_dir.glob(f"*{AUDIO_EXTENSION}") if path.is_file())


def collect_audio_files(audio_dir: Path | None, audio_files: list[Path]) -> list[Path]:
    collected: list[Path] = []
    if audio_dir is not None:
        if not audio_dir.exists():
            raise ValueError(f"audio directory does not exist: {audio_dir}")
        if not audio_dir.is_dir():
            raise ValueError(f"audio path is not a directory: {audio_dir}")
        collected.extend(list_audio_files(audio_dir))
    collected.extend(audio_files)

    if not collected:
        raise ValueError("no audio files provided")

    missing = [path for path in collected if not path.is_file()]
    if missing:
        raise ValueError("audio files not found: " + ", ".join(str(path) for path in missing))

    return collected


def combine_audio_filters(*filters: str | None) -> str:
    return ",".join(audio_filter for audio_filter in filters if audio_filter)


def build_volumedetect_command(audio_file: Path, audio_filter: str | None = None) -> list[str]:
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
        combine_audio_filters(audio_filter, "volumedetect"),
        "-f",
        "null",
        "-",
    ]


def parse_volumedetect_stats(stdout: str, stderr: str) -> VolumeDetectStats:
    text = (stdout or "") + (stderr or "")
    mean_match = re.search(r"\bmean_volume:\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*dB\b", text)
    max_match = re.search(r"\bmax_volume:\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*dB\b", text)
    if mean_match is None:
        raise ValueError("volumedetect output did not contain mean_volume")
    if max_match is None:
        raise ValueError("volumedetect output did not contain max_volume")
    return VolumeDetectStats(
        mean_volume=float(mean_match.group(1)),
        max_volume=float(max_match.group(1)),
    )


def execute_ffmpeg(cmd: list[str], *, verbose: bool = False) -> subprocess.CompletedProcess[str]:
    if not verbose:
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


def run_volumedetect(audio_file: Path, *, audio_filter: str | None = None, verbose: bool = False) -> VolumeDetectStats:
    result = execute_ffmpeg(build_volumedetect_command(audio_file, audio_filter), verbose=verbose)
    return parse_volumedetect_stats(result.stdout, result.stderr)


def analyze_audio_files(
    audio_files: list[Path],
    gain: float,
    peak_ceiling: float,
    *,
    verbose: bool = False,
    show_progress: bool = False,
    progress_stream: object | None = None,
) -> list[VolumeAnalysis]:
    if progress_stream is None:
        progress_stream = sys.stderr

    analyses: list[VolumeAnalysis] = []
    total = len(audio_files)
    for index, audio_file in enumerate(audio_files, start=1):
        analyses.append(VolumeAnalysis(audio_file, run_volumedetect(audio_file, verbose=verbose), gain, peak_ceiling))
        if show_progress and not verbose:
            print(format_progress(index, total, audio_file), file=progress_stream, flush=True)
    return analyses


def format_volume_analysis_table(analyses: list[VolumeAnalysis]) -> str:
    headers = ("#", "audio", "mean dB", "max dB", "post-gain peak", "headroom", "status")
    rows = [
        (
            str(index),
            analysis.audio_file.name,
            format_db(analysis.stats.mean_volume),
            format_db(analysis.stats.max_volume),
            format_db(analysis.estimated_post_gain_peak),
            format_db(analysis.headroom),
            analysis.status,
        )
        for index, analysis in enumerate(analyses, start=1)
    ]
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))

    def row_text(row: tuple[str, ...]) -> str:
        cells: list[str] = []
        for index, value in enumerate(row):
            if index == 0:
                cells.append(f"{value:>{widths[index]}}")
            else:
                cells.append(f"{value:<{widths[index]}}")
        return "  " + "  ".join(cells)

    separator = tuple("-" * len(header) for header in headers)
    return "\n".join([row_text(headers), row_text(separator), *[row_text(row) for row in rows]])


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if shutil.which("ffmpeg") is None:
            raise ValueError("ffmpeg is required")
        audio_files = collect_audio_files(args.audio_dir, args.audio_files)
        analyses = analyze_audio_files(
            audio_files,
            args.gain,
            args.peak_ceiling,
            verbose=args.verbose,
            show_progress=not args.verbose,
        )
        print(f"Fixed-gain analysis: gain={format_db(args.gain)} dB peak ceiling={format_db(args.peak_ceiling)} dB")
        print(format_volume_analysis_table(analyses))
        unsafe = [analysis for analysis in analyses if analysis.headroom < 0]
        if unsafe:
            raise ValueError(
                "fixed gain would exceed peak ceiling for: "
                + ", ".join(analysis.audio_file.name for analysis in unsafe)
            )
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"audio_volume_analysis.py: error: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
