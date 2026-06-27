#!/usr/bin/env python3
"""Analyze audio peak headroom for fixed-gain workflows."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path

from utils import format_progress


AUDIO_EXTENSION = ".flac"
DEFAULT_GAIN = 12.0
DEFAULT_PEAK_CEILING = -1.5
DEFAULT_AUDIO_STREAM = "0:a:0"
ANALYSIS_SCHEMA = "audio-volume-analysis-v1"


@dataclass(frozen=True)
class VolumeDetectStats:
    mean_volume: float
    max_volume: float


@dataclass(frozen=True)
class SourceVolumeAnalysis:
    input_file: Path
    stats: VolumeDetectStats
    audio_stream: str
    audio_filter: str | None
    start: str | None
    end: str | None
    command: tuple[str, ...]
    input_size: int
    input_mtime_ns: int
    analysis_file: Path | None = None
    created_at: str | None = None


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
    parser.add_argument("--audio-stream", default=DEFAULT_AUDIO_STREAM, help="ffmpeg audio stream selector to analyze")
    parser.add_argument("--audio-filter", "--af", dest="audio_filter", help="Pre-gain audio filter chain")
    parser.add_argument("--start", help="Optional start timestamp for analysis")
    parser.add_argument("--end", help="Optional end timestamp for analysis")
    parser.add_argument("--json-output", type=Path, help="Write analysis JSON to this file or directory")
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


def build_volumedetect_command(
    audio_file: Path,
    audio_filter: str | None = None,
    *,
    audio_stream: str = DEFAULT_AUDIO_STREAM,
    start: str | None = None,
    end: str | None = None,
) -> list[str]:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-stats",
        "-loglevel",
        "info",
    ]
    if start:
        command += ["-ss", start]
    if end:
        command += ["-to", end]
    return command + [
        "-i",
        str(audio_file),
        "-map",
        audio_stream,
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


def run_volumedetect(
    audio_file: Path,
    *,
    audio_filter: str | None = None,
    audio_stream: str = DEFAULT_AUDIO_STREAM,
    start: str | None = None,
    end: str | None = None,
    verbose: bool = False,
) -> VolumeDetectStats:
    result = execute_ffmpeg(
        build_volumedetect_command(
            audio_file,
            audio_filter,
            audio_stream=audio_stream,
            start=start,
            end=end,
        ),
        verbose=verbose,
    )
    return parse_volumedetect_stats(result.stdout, result.stderr)


def input_fingerprint(input_file: Path) -> dict[str, int | str]:
    resolved = input_file.resolve()
    stat = resolved.stat()
    return {
        "path": str(resolved),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def current_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def analyze_source_volume(
    input_file: Path,
    *,
    audio_filter: str | None = None,
    audio_stream: str = DEFAULT_AUDIO_STREAM,
    start: str | None = None,
    end: str | None = None,
    verbose: bool = False,
) -> SourceVolumeAnalysis:
    command = build_volumedetect_command(
        input_file,
        audio_filter,
        audio_stream=audio_stream,
        start=start,
        end=end,
    )
    result = execute_ffmpeg(command, verbose=verbose)
    fingerprint = input_fingerprint(input_file)
    return SourceVolumeAnalysis(
        input_file=Path(str(fingerprint["path"])),
        stats=parse_volumedetect_stats(result.stdout, result.stderr),
        audio_stream=audio_stream,
        audio_filter=audio_filter,
        start=start,
        end=end,
        command=tuple(command),
        input_size=int(fingerprint["size"]),
        input_mtime_ns=int(fingerprint["mtime_ns"]),
        created_at=current_timestamp(),
    )


def source_volume_analysis_to_dict(analysis: SourceVolumeAnalysis) -> dict[str, object]:
    return {
        "schema": ANALYSIS_SCHEMA,
        "created_at": analysis.created_at,
        "input": {
            "path": str(analysis.input_file),
            "size": analysis.input_size,
            "mtime_ns": analysis.input_mtime_ns,
        },
        "audio_stream": analysis.audio_stream,
        "start": analysis.start,
        "end": analysis.end,
        "pre_gain_audio_filter": analysis.audio_filter,
        "volumedetect_audio_filter": combine_audio_filters(analysis.audio_filter, "volumedetect"),
        "command": list(analysis.command),
        "mean_volume_db": analysis.stats.mean_volume,
        "max_volume_db": analysis.stats.max_volume,
    }


def source_volume_analysis_from_dict(
    payload: dict[str, object],
    *,
    analysis_file: Path | None = None,
) -> SourceVolumeAnalysis:
    if payload.get("schema") != ANALYSIS_SCHEMA:
        raise ValueError("audio analysis JSON has an unsupported schema")
    input_payload = payload.get("input")
    if not isinstance(input_payload, dict):
        raise ValueError("audio analysis JSON missing input fingerprint")

    try:
        input_path = Path(str(input_payload["path"]))
        input_size = int(input_payload["size"])
        input_mtime_ns = int(input_payload["mtime_ns"])
        mean_volume = float(payload["mean_volume_db"])
        max_volume = float(payload["max_volume_db"])
        audio_stream = str(payload["audio_stream"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("audio analysis JSON missing required fields") from exc

    command_payload = payload.get("command") or []
    if not isinstance(command_payload, list):
        raise ValueError("audio analysis JSON command must be a list")
    command = tuple(str(part) for part in command_payload)

    audio_filter = payload.get("pre_gain_audio_filter")
    start = payload.get("start")
    end = payload.get("end")
    created_at = payload.get("created_at")
    return SourceVolumeAnalysis(
        input_file=input_path,
        stats=VolumeDetectStats(mean_volume=mean_volume, max_volume=max_volume),
        audio_stream=audio_stream,
        audio_filter=str(audio_filter) if audio_filter is not None else None,
        start=str(start) if start is not None else None,
        end=str(end) if end is not None else None,
        command=command,
        input_size=input_size,
        input_mtime_ns=input_mtime_ns,
        analysis_file=analysis_file,
        created_at=str(created_at) if created_at is not None else None,
    )


def write_source_volume_analysis(analysis: SourceVolumeAnalysis, output_path: Path) -> SourceVolumeAnalysis:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(source_volume_analysis_to_dict(analysis), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return SourceVolumeAnalysis(
        input_file=analysis.input_file,
        stats=analysis.stats,
        audio_stream=analysis.audio_stream,
        audio_filter=analysis.audio_filter,
        start=analysis.start,
        end=analysis.end,
        command=analysis.command,
        input_size=analysis.input_size,
        input_mtime_ns=analysis.input_mtime_ns,
        analysis_file=output_path,
        created_at=analysis.created_at,
    )


def read_source_volume_analysis(path: Path) -> SourceVolumeAnalysis:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("audio analysis JSON root must be an object")
    return source_volume_analysis_from_dict(payload, analysis_file=path)


def source_volume_analysis_matches(
    analysis: SourceVolumeAnalysis,
    input_file: Path,
    *,
    audio_filter: str | None = None,
    audio_stream: str = DEFAULT_AUDIO_STREAM,
    start: str | None = None,
    end: str | None = None,
) -> bool:
    fingerprint = input_fingerprint(input_file)
    return (
        str(analysis.input_file) == str(fingerprint["path"])
        and analysis.input_size == int(fingerprint["size"])
        and analysis.audio_stream == audio_stream
        and analysis.audio_filter == audio_filter
        and analysis.start == start
        and analysis.end == end
    )


def load_valid_source_volume_analysis(
    path: Path,
    input_file: Path,
    *,
    audio_filter: str | None = None,
    audio_stream: str = DEFAULT_AUDIO_STREAM,
    start: str | None = None,
    end: str | None = None,
) -> SourceVolumeAnalysis | None:
    if not path.is_file():
        return None
    try:
        analysis = read_source_volume_analysis(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not source_volume_analysis_matches(
        analysis,
        input_file,
        audio_filter=audio_filter,
        audio_stream=audio_stream,
        start=start,
        end=end,
    ):
        return None
    return analysis


def analyze_audio_files(
    audio_files: list[Path],
    gain: float,
    peak_ceiling: float,
    *,
    audio_filter: str | None = None,
    audio_stream: str = DEFAULT_AUDIO_STREAM,
    start: str | None = None,
    end: str | None = None,
    json_output: Path | None = None,
    verbose: bool = False,
    show_progress: bool = False,
    progress_stream: object | None = None,
) -> list[VolumeAnalysis]:
    if progress_stream is None:
        progress_stream = sys.stderr

    analyses: list[VolumeAnalysis] = []
    total = len(audio_files)
    for index, audio_file in enumerate(audio_files, start=1):
        detect_kwargs: dict[str, object] = {"verbose": verbose}
        if audio_filter is not None:
            detect_kwargs["audio_filter"] = audio_filter
        if audio_stream != DEFAULT_AUDIO_STREAM:
            detect_kwargs["audio_stream"] = audio_stream
        if start is not None:
            detect_kwargs["start"] = start
        if end is not None:
            detect_kwargs["end"] = end
        stats = run_volumedetect(audio_file, **detect_kwargs)
        analyses.append(VolumeAnalysis(audio_file, stats, gain, peak_ceiling))
        if json_output is not None:
            output_path = resolve_json_output_path(json_output, audio_file, len(audio_files))
            fingerprint = input_fingerprint(audio_file)
            write_source_volume_analysis(
                SourceVolumeAnalysis(
                    input_file=Path(str(fingerprint["path"])),
                    stats=stats,
                    audio_stream=audio_stream,
                    audio_filter=audio_filter,
                    start=start,
                    end=end,
                    command=tuple(
                        build_volumedetect_command(
                            audio_file,
                            audio_filter,
                            audio_stream=audio_stream,
                            start=start,
                            end=end,
                        )
                    ),
                    input_size=int(fingerprint["size"]),
                    input_mtime_ns=int(fingerprint["mtime_ns"]),
                    created_at=current_timestamp(),
                ),
                output_path,
            )
        if show_progress and not verbose:
            print(format_progress(index, total, audio_file), file=progress_stream, flush=True)
    return analyses


def default_json_name(input_file: Path) -> str:
    return f"{input_file.stem}.audio_analysis.json"


def resolve_json_output_path(json_output: Path, input_file: Path, input_count: int) -> Path:
    if input_count > 1 and json_output.suffix == ".json" and not json_output.is_dir():
        raise ValueError("--json-output must be a directory when analyzing multiple inputs")
    if input_count == 1 and json_output.suffix == ".json" and not json_output.is_dir():
        return json_output
    return json_output / default_json_name(input_file)


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
            audio_filter=args.audio_filter,
            audio_stream=args.audio_stream,
            start=args.start,
            end=args.end,
            json_output=args.json_output,
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
