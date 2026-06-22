#!/usr/bin/env python3
# Rewrite of transcode2.sh to support Digital8 as well
# Coded by ChatGPT

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import add_play_time_columns
import create_srt
from transcode_naming import build_access_output_name
from utils import sibling_dir_for_path

DEFAULT_VALIDATE_DURATION_TOLERANCE = 0.17  # 5 NTSC DV frames at 29.97 fps.
SCALE_FILTER = "scale=trunc(ih*dar/2)*2:ih:flags=lanczos+accurate_rnd+full_chroma_int"
VHS_COLOR_CORRECTION_FILTER = (
    "split=2[orig][work];"
    "[work]eq=gamma=1.43214046,"
    "colorcorrect=rl=-0.004439:bl=0.012896:rh=-0.004175:bh=0.012128:saturation=0.880000[filt];"
    "[orig][filt]blend=all_expr='0.500000*A+0.500000*B'"
)


@dataclass
class Config:
    mode: str
    validate_duration: bool
    validate_duration_tolerance: float
    format_type: str
    start: str | None
    end: str | None
    mask_top: int
    mask_bottom: int
    denoise: str
    q: int
    codec: str
    encoder: str
    preset: str | None
    crf: float | None
    video_filter: str | None
    lut: Path | None
    vhs_color_correct: bool
    deint_mode: str
    map_both_audio: bool
    log_level: str
    assume_yes: bool
    no_logs: bool
    output_suffix: str
    originals_dirname: str
    access_dirname: str
    logs_dirname: str
    vhs_notch: str = "auto"
    audio_channel: str = "keep"
    layout: str = "archive"
    source_root: Path | None = None
    output_dir: Path | None = None
    log_dir: Path | None = None


@dataclass
class Paths:
    input_file: Path
    stem: str
    out_dir: Path
    log_dir: Path
    output_file: Path
    ffmpeg_log_file: Path
    command_log_file: Path
    csv_raw: Path
    csv_with_play: Path
    srt_file: Path
    add_play_time_script: Path
    create_srt_script: Path


@dataclass
class ProcessResult:
    input_file: Path
    output_file: Path | None
    rc: int
    transcode_seconds: float | None
    sidecar_seconds: float | None
    format_type: str
    denoise: str
    mask_bottom: int


@dataclass
class DurationRow:
    path: Path
    duration_seconds: float


@dataclass
class DurationGroup:
    logical_source: Path
    original_file: Path
    input_files: list[Path]
    output_files: list[Path]
    output_resolution_errors: list[str | None] = field(default_factory=list)


@dataclass
class DurationValidationResult:
    group: DurationGroup
    original_row: DurationRow | None
    input_rows: list[DurationRow]
    output_rows: list[DurationRow]
    original_total: float | None
    input_total: float | None
    output_total: float | None
    delta_original_vs_input: float | None
    delta_original_vs_output: float | None
    delta_input_vs_output: float | None
    tolerance: float
    errors: list[str]

    @property
    def passed(self) -> bool:
        return not self.errors


try:
    sys.stdout.reconfigure(line_buffering=True, write_through=True)
    sys.stderr.reconfigure(line_buffering=True, write_through=True)
except Exception:
    pass


def default_mask_top(format_type: str) -> int:
    """Return the default top mask for a source format."""
    return 3 if format_type == "vhs" else 0


def default_mask_bottom(format_type: str) -> int:
    """Return the default bottom mask for a source format."""
    if format_type == "vhs":
        return 12
    return 7 if format_type == "video8" else 0


def default_denoise(format_type: str) -> str:
    """Return the default denoise preset for a source format."""
    return "verylight" if format_type == "vhs" else "light"


def add_common_transcode_args(parser: argparse.ArgumentParser) -> None:
    """Add transcode options shared by archive and generic access CLIs."""
    parser.add_argument("--mode", choices=["transcode", "preview", "validate-duration"], default="transcode")
    parser.add_argument("--format", dest="format_type", choices=["video8", "digital8", "vhs"], required=True)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--mask-top", type=int)
    parser.add_argument("--mask-bottom", type=int)
    parser.add_argument("--denoise", choices=["off", "verylight", "light", "medium", "strong"])
    parser.add_argument("--q", type=int, default=70)
    parser.add_argument("--codec", choices=["h264", "hevc"], default="hevc")
    parser.add_argument("--encoder", choices=["videotoolbox", "libx265"], default="videotoolbox")
    parser.add_argument(
        "--preset",
        choices=["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"],
        help="libx265 preset; only valid with --encoder libx265 (default: medium)",
    )
    parser.add_argument("--crf", type=float, help="libx265 CRF; only valid with --encoder libx265 (default: 20)")
    parser.add_argument("--vf", dest="video_filter", help="Override the complete ffmpeg -vf filter string")
    parser.add_argument("--lut", type=Path, help="Add a lut3d stage using this .cube file")
    parser.add_argument(
        "--vhs-color-correct",
        action="store_true",
        help="Insert the selected VHS color correction filtergraph; only valid with --format vhs",
    )
    parser.add_argument("--deint-mode", choices=["send_frame", "send_field"], default="send_field")
    parser.add_argument("--map-both-audio", action="store_true")
    parser.add_argument("--log-level", choices=["quiet", "error", "warning", "info"], default="warning")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--no-logs", action="store_true", help="Do not write persistent ffmpeg or command logs")
    parser.add_argument("--no-validate-duration", action="store_true")
    parser.add_argument("--validate-duration-tolerance", type=float, default=DEFAULT_VALIDATE_DURATION_TOLERANCE)
    parser.add_argument("--vhs-notch", choices=["auto", "ntsc", "pal", "off"], default="auto")
    parser.add_argument("--audio-channel", choices=["keep", "left", "right"], default="keep")
    parser.add_argument("--output-suffix", default="")


def validate_common_transcode_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Validate cross-option constraints for shared transcode arguments."""
    if args.encoder == "videotoolbox":
        if args.preset is not None:
            parser.error("--preset is only valid with --encoder libx265")
        if args.crf is not None:
            parser.error("--crf is only valid with --encoder libx265")
    elif args.codec != "hevc":
        parser.error("--encoder libx265 requires --codec hevc")
    if args.video_filter is not None and not args.video_filter.strip():
        parser.error("--vf cannot be blank")
    if args.video_filter is not None and args.lut is not None:
        parser.error("--lut cannot be combined with --vf; include lut3d in --vf")
    if args.video_filter is not None and args.vhs_color_correct:
        parser.error("--vhs-color-correct cannot be combined with --vf; include it in --vf")
    if args.vhs_color_correct and args.format_type != "vhs":
        parser.error("--vhs-color-correct is only valid with --format vhs")
    if args.lut is not None and not args.lut.is_file():
        parser.error(f"--lut file does not exist: {args.lut}")


def config_from_args(args: argparse.Namespace, *, layout: str) -> Config:
    """Build a Config from parsed CLI args."""
    mask_top = args.mask_top if args.mask_top is not None else default_mask_top(args.format_type)
    mask_bottom = args.mask_bottom if args.mask_bottom is not None else default_mask_bottom(args.format_type)
    denoise = args.denoise if args.denoise is not None else default_denoise(args.format_type)
    default_preset = "slow" if args.format_type == "vhs" else "medium"
    default_crf = 22.0 if args.format_type == "vhs" else 20.0
    preset = args.preset if args.preset is not None else (default_preset if args.encoder == "libx265" else None)
    crf = args.crf if args.crf is not None else (default_crf if args.encoder == "libx265" else None)

    return Config(
        mode=args.mode,
        validate_duration=not args.no_validate_duration,
        validate_duration_tolerance=args.validate_duration_tolerance,
        format_type=args.format_type,
        start=args.start,
        end=args.end,
        mask_top=mask_top,
        mask_bottom=mask_bottom,
        denoise=denoise,
        q=args.q,
        codec=args.codec,
        encoder=args.encoder,
        preset=preset,
        crf=crf,
        video_filter=args.video_filter,
        lut=args.lut,
        vhs_color_correct=args.vhs_color_correct,
        deint_mode=args.deint_mode,
        map_both_audio=args.map_both_audio,
        log_level=args.log_level,
        assume_yes=args.yes,
        no_logs=args.no_logs,
        output_suffix=args.output_suffix,
        originals_dirname=getattr(args, "originals_dirname", "Originals"),
        access_dirname=args.access_dirname,
        logs_dirname=args.logs_dirname,
        vhs_notch=args.vhs_notch,
        audio_channel=args.audio_channel,
        layout=layout,
        source_root=getattr(args, "source_root", None),
        output_dir=getattr(args, "output_dir", None),
        log_dir=getattr(args, "log_dir", None),
    )


def parse_archive_args() -> tuple[Config, list[Path]]:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Transcode DV with VideoToolbox, with optional Digital8 DVRescue subtitle burn-in.",
        epilog=(
            "Examples:\n"
            "  transcode3.py --mode transcode --format video8 Originals/set/tape/out.dv\n"
            "  transcode3.py --mode validate-duration --format video8 Originals/set/tape/out.dv\n"
            "  transcode3.py --mode validate-duration --format video8 Originals/set/tape/out_partA.dv "
            "Originals/set/tape/out_partB.dv"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_common_transcode_args(parser)
    parser.add_argument("--originals-dirname", default="Originals")
    parser.add_argument("--access-dirname", default="Access")
    parser.add_argument("--logs-dirname", default="Logs")
    parser.add_argument("input_files", nargs="+")
    args = parser.parse_args()
    validate_common_transcode_args(parser, args)

    cfg = config_from_args(args, layout="archive")
    input_files = [Path(p) for p in args.input_files]
    return cfg, input_files


def build_archive_paths(cfg: Config, input_file: Path, *, create_dirs: bool = True) -> Paths:
    """Build input, output, and log paths for an Originals/Access/Logs archive."""
    input_file = input_file.resolve()
    if not input_file.is_file():
        raise SystemExit(f"Input is not a regular file: {input_file}")

    out_dir = sibling_dir_for_path(
        input_file,
        originals_dirname=cfg.originals_dirname,
        sibling_dirname=cfg.access_dirname,
    )
    log_dir = sibling_dir_for_path(
        input_file,
        originals_dirname=cfg.originals_dirname,
        sibling_dirname=cfg.logs_dirname,
    )
    if create_dirs:
        out_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)

    stem = input_file.stem
    try:
        output_name = build_access_output_name(
            input_file,
            originals_dirname=cfg.originals_dirname,
            output_suffix=cfg.output_suffix,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    script_dir = Path(__file__).resolve().parent

    return Paths(
        input_file=input_file,
        stem=stem,
        out_dir=out_dir,
        log_dir=log_dir,
        output_file=out_dir / output_name,
        ffmpeg_log_file=log_dir / f"{stem}_access_{timestamp}.log",
        command_log_file=log_dir / f"{stem}_transcode_cmd_{timestamp}.log",
        csv_raw=log_dir / f"{stem}.frameinfo.csv",
        csv_with_play=log_dir / f"{stem}.frameinfo.with_play_time.csv",
        srt_file=log_dir / f"{stem}.record_time_overlay.srt",
        add_play_time_script=script_dir / "add_play_time_columns.py",
        create_srt_script=script_dir / "create_srt.py",
    )


def build_generic_access_paths(cfg: Config, input_file: Path, *, create_dirs: bool = True) -> Paths:
    """Build input, output, and log paths for flat/non-Originals access copies."""
    input_file = input_file.resolve()
    if not input_file.is_file():
        raise SystemExit(f"Input is not a regular file: {input_file}")

    source_root = (cfg.source_root.resolve() if cfg.source_root is not None else input_file.parent.parent.resolve())
    try:
        rel_parent = input_file.parent.relative_to(source_root)
    except ValueError as exc:
        raise SystemExit(f"Input parent must be inside source root {source_root}: {input_file}") from exc

    out_dir = cfg.output_dir.resolve() if cfg.output_dir is not None else source_root / cfg.access_dirname / rel_parent
    log_dir = cfg.log_dir.resolve() if cfg.log_dir is not None else source_root / cfg.logs_dirname / rel_parent
    if create_dirs:
        out_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)

    stem = input_file.stem
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    script_dir = Path(__file__).resolve().parent

    return Paths(
        input_file=input_file,
        stem=stem,
        out_dir=out_dir,
        log_dir=log_dir,
        output_file=out_dir / f"{stem}{cfg.output_suffix}.mp4",
        ffmpeg_log_file=log_dir / f"{stem}_access_{timestamp}.log",
        command_log_file=log_dir / f"{stem}_transcode_cmd_{timestamp}.log",
        csv_raw=log_dir / f"{stem}.frameinfo.csv",
        csv_with_play=log_dir / f"{stem}.frameinfo.with_play_time.csv",
        srt_file=log_dir / f"{stem}.record_time_overlay.srt",
        add_play_time_script=script_dir / "add_play_time_columns.py",
        create_srt_script=script_dir / "create_srt.py",
    )


def build_paths(cfg: Config, input_file: Path, *, create_dirs: bool = True) -> Paths:
    """Build commonly used input, output, and log paths."""
    if cfg.layout == "access":
        return build_generic_access_paths(cfg, input_file, create_dirs=create_dirs)
    return build_archive_paths(cfg, input_file, create_dirs=create_dirs)


def build_runtime_paths(paths: Paths, artifact_dir: Path) -> Paths:
    """Return a copy of Paths that writes runtime artifacts under artifact_dir."""
    return replace(
        paths,
        log_dir=artifact_dir,
        ffmpeg_log_file=artifact_dir / paths.ffmpeg_log_file.name,
        csv_raw=artifact_dir / paths.csv_raw.name,
        csv_with_play=artifact_dir / paths.csv_with_play.name,
        srt_file=artifact_dir / paths.srt_file.name,
    )


PART_SUFFIX_RE = re.compile(r"^(?P<base>.+)_part(?P<part>[A-Za-z0-9]+)$")


def infer_logical_original_path(input_file: Path) -> Path:
    """Map part files back to their logical original DV path."""
    match = PART_SUFFIX_RE.match(input_file.stem)
    if match is None:
        return input_file
    return input_file.with_name(f"{match.group('base')}{input_file.suffix}")


def resolve_validation_output_path(cfg: Config, input_file: Path) -> tuple[Path, str | None]:
    """Resolve the MP4 path to probe during validation."""
    expected_output = build_paths(cfg, input_file, create_dirs=False).output_file
    if expected_output.exists():
        return expected_output, None

    if cfg.format_type != "digital8":
        return expected_output, f"missing exact output {expected_output}"

    dated_matches = sorted(expected_output.parent.glob(f"*_{expected_output.name}"))
    if len(dated_matches) == 1:
        return dated_matches[0], None
    if not dated_matches:
        return expected_output, (
            "no dated Digital8 match found for expected output "
            f"{expected_output.name} in {expected_output.parent}"
        )

    candidates = ", ".join(path.name for path in dated_matches)
    return expected_output, (
        f"ambiguous dated Digital8 matches for expected output {expected_output.name}: {candidates}"
    )


def build_duration_validation_groups(cfg: Config, input_files: list[Path]) -> list[DurationGroup]:
    """Group input DVs by their logical original DV."""
    if cfg.layout == "access":
        groups: list[DurationGroup] = []
        for input_file in sorted(input_files, key=lambda path: str(path)):
            resolved = input_file.resolve()
            output_file, output_error = resolve_validation_output_path(cfg, resolved)
            groups.append(
                DurationGroup(
                    logical_source=resolved,
                    original_file=resolved,
                    input_files=[resolved],
                    output_files=[output_file],
                    output_resolution_errors=[output_error],
                )
            )
        return groups

    grouped: dict[Path, list[Path]] = {}
    for input_file in input_files:
        resolved = input_file.resolve()
        logical_original = infer_logical_original_path(resolved)
        grouped.setdefault(logical_original, []).append(resolved)

    groups: list[DurationGroup] = []
    for logical_original in sorted(grouped):
        parts = sorted(grouped[logical_original], key=lambda path: path.name)
        resolved_outputs = [resolve_validation_output_path(cfg, part) for part in parts]
        groups.append(
            DurationGroup(
                logical_source=logical_original,
                original_file=logical_original,
                input_files=parts,
                output_files=[path for path, _ in resolved_outputs],
                output_resolution_errors=[error for _, error in resolved_outputs],
            )
        )
    return groups


def probe_media_duration_seconds(path: Path) -> float:
    """Return media duration from ffprobe in seconds."""
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else str(exc)
        raise RuntimeError(f"ffprobe failed for {path}: {stderr}") from exc

    value = proc.stdout.strip()
    if not value:
        raise RuntimeError(f"ffprobe returned no duration for {path}")
    try:
        seconds = float(value)
    except ValueError as exc:
        raise RuntimeError(f"ffprobe returned invalid duration for {path}: {value!r}") from exc
    if not math.isfinite(seconds):
        raise RuntimeError(f"ffprobe returned non-finite duration for {path}: {value!r}")
    return seconds


def format_seconds_precise(seconds: float | None) -> str:
    """Format a duration for audit display."""
    return "n/a" if seconds is None else f"{seconds:.3f}"


def cached_probe_media_duration_seconds(path: Path, cache: dict[Path, float]) -> float:
    """Return a probed media duration, caching by resolved path."""
    key = path.resolve()
    if key not in cache:
        cache[key] = probe_media_duration_seconds(path)
    return cache[key]


def build_duration_rows(paths: list[Path], cache: dict[Path, float] | None = None) -> list[DurationRow]:
    """Probe all paths and return rows in order."""
    if cache is None:
        return [DurationRow(path=path, duration_seconds=probe_media_duration_seconds(path)) for path in paths]
    return [DurationRow(path=path, duration_seconds=cached_probe_media_duration_seconds(path, cache)) for path in paths]


def validate_duration_group(
    group: DurationGroup,
    tolerance: float,
    cache: dict[Path, float] | None = None,
) -> DurationValidationResult:
    """Validate one logical source against its input DV parts and output MP4s."""
    duration_cache: dict[Path, float] = {} if cache is None else cache
    errors: list[str] = []
    original_row: DurationRow | None = None
    input_rows: list[DurationRow] = []
    output_rows: list[DurationRow] = []

    try:
        original_row = DurationRow(
            group.original_file,
            cached_probe_media_duration_seconds(group.original_file, duration_cache),
        )
    except Exception as exc:
        errors.append(f"Original DV probe failed: {exc}")

    try:
        input_rows = build_duration_rows(group.input_files, duration_cache)
    except Exception as exc:
        errors.append(f"Input DV probe failed: {exc}")

    output_resolution_errors = list(group.output_resolution_errors)
    if len(output_resolution_errors) < len(group.output_files):
        output_resolution_errors.extend([None] * (len(group.output_files) - len(output_resolution_errors)))
    for output_file, resolution_error in zip(group.output_files, output_resolution_errors):
        if resolution_error is not None and resolution_error.startswith("ambiguous"):
            errors.append(f"Output MP4 probe failed: {resolution_error}")
            continue
        try:
            output_rows.append(
                DurationRow(
                    path=output_file,
                    duration_seconds=cached_probe_media_duration_seconds(output_file, duration_cache),
                )
            )
        except Exception as exc:
            if resolution_error is not None:
                errors.append(f"Output MP4 probe failed: {resolution_error}; {exc}")
            else:
                errors.append(f"Output MP4 probe failed: {exc}")

    original_total = original_row.duration_seconds if original_row else None
    input_total = sum(row.duration_seconds for row in input_rows) if input_rows else None
    output_total = sum(row.duration_seconds for row in output_rows) if output_rows else None

    delta_original_vs_input = (
        abs(original_total - input_total) if original_total is not None and input_total is not None else None
    )
    delta_original_vs_output = (
        abs(original_total - output_total) if original_total is not None and output_total is not None else None
    )
    delta_input_vs_output = abs(input_total - output_total) if input_total is not None and output_total is not None else None

    if delta_original_vs_input is not None and delta_original_vs_input > tolerance:
        errors.append(
            "Original DV total differs from input DV total by "
            f"{delta_original_vs_input:.3f}s (tolerance {tolerance:.3f}s)"
        )
    if delta_original_vs_output is not None and delta_original_vs_output > tolerance:
        errors.append(
            "Original DV total differs from MP4 total by "
            f"{delta_original_vs_output:.3f}s (tolerance {tolerance:.3f}s)"
        )

    return DurationValidationResult(
        group=group,
        original_row=original_row,
        input_rows=input_rows,
        output_rows=output_rows,
        original_total=original_total,
        input_total=input_total,
        output_total=output_total,
        delta_original_vs_input=delta_original_vs_input,
        delta_original_vs_output=delta_original_vs_output,
        delta_input_vs_output=delta_input_vs_output,
        tolerance=tolerance,
        errors=errors,
    )


def print_duration_validation_result(result: DurationValidationResult) -> None:
    """Print a fixed-width validation report for one logical source."""
    def build_panel(title: str, rows: list[DurationRow], include_total: bool) -> list[str]:
        panel_name_width = max(
            len("Filename"),
            len("TOTAL"),
            *(len(row.path.name) for row in rows),
        )
        panel_duration_width = max(len("Duration (s)"), 12)
        lines = [
            title,
            f"  {'Filename'.ljust(panel_name_width)}  {'Duration (s)'.rjust(panel_duration_width)}",
            f"  {'-' * panel_name_width}  {'-' * panel_duration_width}",
        ]
        for row in rows:
            lines.append(
                f"  {row.path.name.ljust(panel_name_width)}  {format_seconds_precise(row.duration_seconds).rjust(panel_duration_width)}"
            )
        if include_total:
            total = sum(row.duration_seconds for row in rows) if rows else None
            lines.append(f"  {'TOTAL'.ljust(panel_name_width)}  {format_seconds_precise(total).rjust(panel_duration_width)}")
        return lines

    def print_side_by_side(panels: list[list[str]]) -> None:
        widths = [max(len(line) for line in panel) for panel in panels]
        height = max(len(panel) for panel in panels)
        padded_panels = [panel + [""] * (height - len(panel)) for panel in panels]
        for row_idx in range(height):
            print("    ".join(padded_panels[idx][row_idx].ljust(widths[idx]) for idx in range(len(panels))))

    print(f"\nDuration audit: {result.group.original_file.name}")
    print_side_by_side(
        [
            build_panel("Original DV", [result.original_row] if result.original_row is not None else [], include_total=False),
            build_panel("Input DVs", result.input_rows, include_total=True),
            build_panel("Output MP4s", result.output_rows, include_total=True),
        ]
    )
    print()

    print(f"Delta original vs input  {format_seconds_precise(result.delta_original_vs_input)}")
    print(f"Delta original vs mp4    {format_seconds_precise(result.delta_original_vs_output)}")
    print(f"Delta input vs mp4       {format_seconds_precise(result.delta_input_vs_output)}")
    print(f"Tolerance             {result.tolerance:.3f}")
    print("PASS" if result.passed else "FAIL")
    for error in result.errors:
        print(f"  {error}")


def validate_durations(cfg: Config, input_files: list[Path]) -> list[DurationValidationResult]:
    """Run duration validation for the batch and print a report."""
    duration_cache: dict[Path, float] = {}
    results = [
        validate_duration_group(group, cfg.validate_duration_tolerance, duration_cache)
        for group in build_duration_validation_groups(cfg, input_files)
    ]
    for result in results:
        print_duration_validation_result(result)
    return results


def get_hqdn3d_args(preset: str) -> str | None:
    """Return hqdn3d parameters for the named preset."""
    mapping = {
        "off": None,
        "verylight": "1.5:1.125:2.25:1.6875",
        "light": "2:1.5:3:2",
        "medium": "3:2.25:4.5:3.375",
        "strong": "4:3:6:4.5",
    }
    if preset not in mapping:
        raise SystemExit(f"Unknown denoise preset: {preset}")
    return mapping[preset]


def escape_ffmpeg_filter_value(value: str) -> str:
    """Escape a filename for safe use inside an ffmpeg filter expression."""
    for a, b in [
        ("\\", "\\\\"),
        (":", r"\:"),
        (",", r"\,"),
        (";", r"\;"),
        ("[", r"\["),
        ("]", r"\]"),
        ("=", r"\="),
        ("'", r"\'"),
    ]:
        value = value.replace(a, b)
    return value


def parse_frame_rate(rate: str | None) -> float | None:
    """Parse an ffprobe rational frame rate into a float."""
    if not rate or rate == "0/0":
        return None
    if "/" in rate:
        numerator, denominator = rate.split("/", 1)
        try:
            denominator_float = float(denominator)
            if denominator_float == 0:
                return None
            return float(numerator) / denominator_float
        except ValueError:
            return None
    try:
        return float(rate)
    except ValueError:
        return None


def classify_video_standard(width: int | None, height: int | None, frame_rate: float | None) -> str | None:
    """Classify a VHS capture as NTSC or PAL from stream metadata."""
    if height is not None:
        if height >= 560:
            return "pal"
        if height <= 500:
            return "ntsc"
    if frame_rate is not None:
        if 24.5 <= frame_rate <= 25.5:
            return "pal"
        if 29.0 <= frame_rate <= 30.5:
            return "ntsc"
    if width is not None and width >= 700 and height is not None:
        return "pal" if height > 520 else "ntsc"
    return None


def probe_video_standard(input_file: Path) -> str:
    """Probe the first video stream and classify it as NTSC or PAL."""
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height,avg_frame_rate,r_frame_rate",
                "-of",
                "json",
                str(input_file),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else str(exc)
        raise RuntimeError(f"ffprobe failed for {input_file}: {stderr}") from exc

    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe returned invalid JSON for {input_file}") from exc

    streams = payload.get("streams") or []
    if not streams:
        raise RuntimeError(f"ffprobe found no video stream in {input_file}")

    stream = streams[0]
    frame_rate = parse_frame_rate(stream.get("avg_frame_rate")) or parse_frame_rate(stream.get("r_frame_rate"))
    standard = classify_video_standard(stream.get("width"), stream.get("height"), frame_rate)
    if standard is None:
        raise RuntimeError(f"could not auto-detect NTSC/PAL video standard for {input_file}")
    return standard


def build_vhs_audio_filter(cfg: Config, input_file: Path) -> str | None:
    """Build the default VHS hum/scan-frequency audio cleanup filter."""
    if cfg.format_type != "vhs" or cfg.vhs_notch == "off":
        return None

    standard = probe_video_standard(input_file) if cfg.vhs_notch == "auto" else cfg.vhs_notch
    center_frequency = {"ntsc": 15734, "pal": 15625}[standard]
    return f"highpass=f=60:p=1,equalizer=f={center_frequency}:width_type=q:width=30:g=-24"


def build_channel_copy_audio_filter(cfg: Config) -> str | None:
    """Build a stereo pan filter that duplicates one source channel to both channels."""
    if cfg.audio_channel == "left":
        return "pan=stereo|c0=c0|c1=c0"
    if cfg.audio_channel == "right":
        return "pan=stereo|c0=c1|c1=c1"
    return None


def build_audio_filter(cfg: Config, input_file: Path) -> str | None:
    """Build the complete audio filter chain."""
    filters = [
        audio_filter
        for audio_filter in (
            build_vhs_audio_filter(cfg, input_file),
            build_channel_copy_audio_filter(cfg),
        )
        if audio_filter is not None
    ]
    return ",".join(filters) if filters else None


def build_vf(cfg: Config, paths: Paths) -> str:
    """Build the ffmpeg video filter chain."""
    if cfg.video_filter is not None:
        return cfg.video_filter

    filters = [f"bwdif=mode={cfg.deint_mode}:parity=auto:deint=all"]

    if cfg.mask_top > 0:
        filters.append(f"drawbox=x=0:y=0:w=iw:h={cfg.mask_top}:color=black:t=fill")
    if cfg.mask_bottom > 0:
        filters.append(f"drawbox=x=0:y=ih-{cfg.mask_bottom}:w=iw:h={cfg.mask_bottom}:color=black:t=fill")

    if hqdn3d := get_hqdn3d_args(cfg.denoise):
        filters.append(f"hqdn3d={hqdn3d}")

    if cfg.vhs_color_correct:
        filters.append(VHS_COLOR_CORRECTION_FILTER)

    filters += [
        SCALE_FILTER,
        "setsar=1",
        "setparams=range=limited",
    ]

    if cfg.lut is not None:
        filters.append(f"lut3d={escape_ffmpeg_filter_value(str(cfg.lut))}:interp=tetrahedral")

    if cfg.format_type == "digital8":
        style = (
            "Alignment=3,"
            "MarginV=3,"
            "MarginR=3,"
            "FontName=Helvetica,"
            "Fontsize=12,"
            "PrimaryColour=&H00FFFFFF,"
            "OutlineColour=&H00000000,"
            "BorderStyle=1,"
            "Outline=0.5,"
            "Shadow=0"
        )
        filters.append(
            f"subtitles=filename={escape_ffmpeg_filter_value(str(paths.srt_file))}:force_style='{style}'"
        )

    if cfg.encoder == "libx265":
        filters.append("format=yuv420p10le")

    return ",".join(filters)


def build_ffmpeg_args(cfg: Config, paths: Paths, vf: str, preview: bool) -> list[str]:
    """Build the ffmpeg command-line argument list."""
    args = ["ffmpeg", "-hide_banner", "-loglevel", cfg.log_level, "-stats", "-stats_period", "1"]

    if cfg.start:
        args += ["-ss", cfg.start]
    if cfg.end:
        args += ["-to", cfg.end]

    args += ["-i", str(paths.input_file), "-vf", vf, "-map", "0:v:0"]
    args += ["-map", "0:a:0?", "-map", "0:a:1?"] if cfg.map_both_audio else ["-map", "0:a:0?"]
    if audio_filter := build_audio_filter(cfg, paths.input_file):
        args += ["-af", audio_filter]

    if cfg.encoder == "libx265":
        args += [
            "-c:v",
            "libx265",
            "-preset",
            cfg.preset or "medium",
            "-crf",
            f"{cfg.crf if cfg.crf is not None else 20:g}",
            "-profile:v",
            "main10",
            "-pix_fmt",
            "yuv420p10le",
            "-tag:v",
            "hvc1",
        ]
        if cfg.format_type == "vhs":
            args += ["-x265-params", "aq-mode=3:aq-strength=0.8:psy-rd=2.0:psy-rdoq=1.0"]
    elif cfg.codec == "h264":
        args += ["-c:v", "h264_videotoolbox", "-profile:v", "high", "-coder", "cabac"]
    else:
        args += ["-c:v", "hevc_videotoolbox", "-profile:v", "main", "-tag:v", "hvc1"]

    if cfg.encoder == "videotoolbox":
        args += [
            "-spatial_aq", "1",
            "-max_ref_frames", "4",
            "-q:v", str(cfg.q),
        ]

    args += ["-g", "60", "-color_range", "tv", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]

    return args + (["-f", "matroska", "-"] if preview else [str(paths.output_file)])


def shjoin(args: list[str]) -> str:
    """Return a shell-escaped command string for display/logging."""
    return shlex.join(args)


def run_checked(
    args: list[str],
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
    stdout=None,
) -> None:
    """Run a subprocess, optionally redirecting stdout/stderr."""
    stdout_file = None
    stderr_file = None
    try:
        if stdout_path is not None:
            stdout_file = stdout_path.open("w", encoding="utf-8", newline="\n")
            stdout = stdout_file
        if stderr_path is not None:
            stderr_file = stderr_path.open("w", encoding="utf-8", newline="\n")
        subprocess.run(args, check=True, stdout=stdout, stderr=stderr_file)
    finally:
        if stdout_file is not None:
            stdout_file.close()
        if stderr_file is not None:
            stderr_file.close()


def extract_first_rdt_yyyymmdd(csv_path: Path) -> str | None:
    """Extract the first YYYYMMDD date prefix from the Digital8 frameinfo CSV."""
    with csv_path.open("r", newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if raw := (row.get("rdt") or "").strip():
                parts = raw.split(" ", 1)[0].split("-")
                if len(parts) == 3 and len(parts[0]) == 4 and len(parts[1]) == 2 and len(parts[2]) == 2:
                    return "".join(parts)
    return None


def generate_digital8_sidecars(paths: Paths) -> None:
    """Generate CSV, play-time CSV, and SRT sidecars for Digital8 inputs."""
    print("Generating Digital8 CSV/SRT sidecars...")
    run_checked(
        ["dvrescue", "--csv", str(paths.input_file), "-m", "-"],
        stderr_path=paths.csv_raw,
        stdout=subprocess.DEVNULL,
    )

    rows, fieldnames = add_play_time_columns.load_csv(paths.csv_raw, "FramePos")
    output_fields = add_play_time_columns.ensure_output_columns(fieldnames)
    rows, _, _ = add_play_time_columns.add_play_time_columns(rows, "FramePos")
    add_play_time_columns.write_csv(paths.csv_with_play, rows, output_fields)

    srt_rows = create_srt.load_rows(paths.csv_with_play)
    if srt_rows:
        header = srt_rows[0].keys()
        if "play_time_seconds" not in header:
            raise ValueError("Column 'play_time_seconds' not found")
        if "rdt" not in header:
            raise ValueError("Column 'rdt' not found")
    second_map = create_srt.collect_second_buckets(srt_rows, "play_time_seconds", "rdt")
    cues = create_srt.build_cues(second_map)
    create_srt.write_srt(paths.srt_file, cues)

    if yyyymmdd := extract_first_rdt_yyyymmdd(paths.csv_with_play):
        paths.output_file = paths.output_file.with_name(f"{yyyymmdd}_{paths.output_file.name}")
    else:
        print("Warning: could not find first-frame rdt date; leaving output filename unchanged.")


def print_summary(cfg: Config, paths: Paths, ffmpeg_args: list[str], preview: bool) -> None:
    """Print a summary of the transcode job and ffmpeg command."""
    print(f"Mode: {cfg.mode}")
    print(f"Format: {cfg.format_type}")
    print(f"Input: {paths.input_file}")
    print(f"Output dir: {paths.out_dir}")
    if cfg.no_logs:
        print("Logs: disabled")
    else:
        print(f"Log dir: {paths.log_dir}")
    if cfg.start or cfg.end:
        print(f"Range: {cfg.start or 'beginning'} -> {cfg.end or 'end'}")
    print(f"Codec: {cfg.codec}")
    print(f"Denoise preset: {cfg.denoise}")
    print(f"Top mask rows: {cfg.mask_top}")
    print(f"Bottom mask rows: {cfg.mask_bottom}")
    if cfg.format_type == "vhs":
        print(f"VHS audio notch: {cfg.vhs_notch}")
        print(f"VHS color correction: {'on' if cfg.vhs_color_correct else 'off'}")
    print(f"Audio channel: {cfg.audio_channel}")
    print(f"Deinterlace mode: {cfg.deint_mode}")
    if cfg.format_type == "digital8":
        print(f"CSV: {paths.csv_raw}")
        print(f"CSV w/ play time: {paths.csv_with_play}")
        print(f"SRT: {paths.srt_file}")

    if preview:
        print("\nPreview pipeline:\n")
    else:
        print(f"Output: {paths.output_file}")
        if not cfg.no_logs:
            print(f"Log: {paths.ffmpeg_log_file}")
        print()
        print("Running command:\n")

    print(shjoin(ffmpeg_args))
    print()


def write_command_log(cfg: Config, paths: Paths, ffmpeg_args: list[str]) -> None:
    """Write a human-readable command log for the transcode."""
    lines = [
        f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Input: {paths.input_file}",
        f"Output: {paths.output_file}",
        f"Mode: {cfg.mode}",
        f"Format: {cfg.format_type}",
        f"Codec: {cfg.codec}",
        f"Denoise: {cfg.denoise}",
        f"Mask top: {cfg.mask_top}",
        f"Mask bottom: {cfg.mask_bottom}",
    ]
    if cfg.format_type == "vhs":
        lines.append(f"VHS audio notch: {cfg.vhs_notch}")
        lines.append(f"VHS color correction: {'on' if cfg.vhs_color_correct else 'off'}")
    lines.append(f"Audio channel: {cfg.audio_channel}")
    if cfg.format_type == "digital8":
        lines += [
            f"CSV: {paths.csv_raw}",
            f"CSV with play time: {paths.csv_with_play}",
            f"SRT: {paths.srt_file}",
        ]
    lines += ["", "Command:", shjoin(ffmpeg_args)]
    paths.command_log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def format_duration(seconds: float | None) -> str:
    """Format an elapsed duration for human-readable summary output."""
    if seconds is None:
        return "n/a"

    total_seconds = int(seconds + 0.5)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def build_summary_options(result: ProcessResult) -> str:
    """Build the compact options string for the end-of-run summary table."""
    return f"{result.format_type} | denoise={result.denoise} | mask_bottom={result.mask_bottom}"


def print_transcode_time_summary(results: list[ProcessResult]) -> None:
    """Print a batch-end summary table of per-file durations and options."""
    if not results:
        return

    total_transcode_seconds = sum(result.transcode_seconds or 0.0 for result in results)
    total_sidecar_seconds = sum(result.sidecar_seconds or 0.0 for result in results)
    headers = ("Filename", "Transcode", "Sidecar Gen", "Options")
    rows = [
        (
            result.input_file.name,
            format_duration(result.transcode_seconds),
            format_duration(result.sidecar_seconds),
            build_summary_options(result),
        )
        for result in results
    ]
    rows.append(
        (
            "TOTAL",
            format_duration(total_transcode_seconds),
            format_duration(total_sidecar_seconds),
            "",
        )
    )
    widths = [
        max(len(headers[idx]), *(len(row[idx]) for row in rows))
        for idx in range(len(headers))
    ]

    print("\nEnd-of-run summary:")
    print("  ".join(header.ljust(widths[idx]) for idx, header in enumerate(headers)))
    print("  ".join("-" * widths[idx] for idx in range(len(headers))))
    for row in rows:
        print("  ".join(row[idx].ljust(widths[idx]) for idx in range(len(headers))))


def tee_stream(stream, outputs: list, mirror_to_stderr: bool = False) -> None:
    """Tee a binary stream to one or more outputs, optionally mirroring to stderr."""
    try:
        while chunk := stream.read(8192):
            for out in outputs:
                out.write(chunk)
                out.flush()
            if mirror_to_stderr:
                sys.stderr.buffer.write(chunk)
                sys.stderr.buffer.flush()
    finally:
        stream.close()


def run_ffmpeg(ffmpeg_args: list[str], log_path: Path | None, preview_stem: str | None = None) -> int:
    """Run ffmpeg, optionally piping preview output to ffplay, while teeing stderr to a log."""
    log_file = log_path.open("wb") if log_path is not None else None
    try:
        if preview_stem is None:
            if log_file is None:
                proc = subprocess.Popen(ffmpeg_args, bufsize=0)
                return proc.wait()
            proc = subprocess.Popen(ffmpeg_args, stderr=subprocess.PIPE, bufsize=0)
            assert proc.stderr is not None
            t = threading.Thread(target=tee_stream, args=(proc.stderr, [log_file], True), daemon=True)
            t.start()
            rc = proc.wait()
            t.join()
            return rc

        ffmpeg_stderr = None if log_file is None else subprocess.PIPE
        ffmpeg_proc = subprocess.Popen(ffmpeg_args, stdout=subprocess.PIPE, stderr=ffmpeg_stderr, bufsize=0)
        assert ffmpeg_proc.stdout is not None

        ffplay_proc = subprocess.Popen(
            ["ffplay", "-hide_banner", "-window_title", f"{preview_stem} preview", "-"],
            stdin=ffmpeg_proc.stdout,
            bufsize=0,
        )
        ffmpeg_proc.stdout.close()

        t = None
        if log_file is not None:
            assert ffmpeg_proc.stderr is not None
            t = threading.Thread(target=tee_stream, args=(ffmpeg_proc.stderr, [log_file], True), daemon=True)
            t.start()
        ffplay_rc = ffplay_proc.wait()
        ffmpeg_rc = ffmpeg_proc.wait()
        if t is not None:
            t.join()

        return ffmpeg_rc if ffmpeg_rc != 0 else ffplay_rc
    finally:
        if log_file is not None:
            log_file.close()


def build_process_result(
    paths: Paths,
    *,
    output_file: Path | None,
    rc: int,
    transcode_seconds: float | None,
    sidecar_seconds: float | None,
    cfg: Config,
) -> ProcessResult:
    """Build a ProcessResult from the completed per-file workflow."""
    return ProcessResult(
        input_file=paths.input_file,
        output_file=output_file,
        rc=rc,
        transcode_seconds=transcode_seconds,
        sidecar_seconds=sidecar_seconds,
        format_type=cfg.format_type,
        denoise=cfg.denoise,
        mask_bottom=cfg.mask_bottom,
    )


def prepare_digital8_sidecars(cfg: Config, paths: Paths) -> float | None:
    """Generate Digital8 sidecars when needed and return elapsed seconds."""
    if cfg.format_type != "digital8":
        return None

    sidecar_start = time.perf_counter()
    generate_digital8_sidecars(paths)
    return time.perf_counter() - sidecar_start


def process_preview_file(cfg: Config, input_file: Path) -> ProcessResult:
    """Process one input file in preview mode."""
    persistent_paths = build_paths(cfg, input_file, create_dirs=not cfg.no_logs)
    with tempfile.TemporaryDirectory(prefix=f"{persistent_paths.stem}_preview_") as tmp:
        paths = build_runtime_paths(persistent_paths, Path(tmp))
        prepare_digital8_sidecars(cfg, paths)
        ffmpeg_args = build_ffmpeg_args(cfg, paths, build_vf(cfg, paths), preview=True)
        print_summary(cfg, paths, ffmpeg_args, preview=True)
        log_path = None if cfg.no_logs else paths.ffmpeg_log_file
        rc = run_ffmpeg(ffmpeg_args, log_path, preview_stem=paths.stem)
        return build_process_result(
            paths,
            output_file=None,
            rc=rc,
            transcode_seconds=None,
            sidecar_seconds=None,
            cfg=cfg,
        )


def process_transcode_file(cfg: Config, input_file: Path, prompt: bool) -> ProcessResult:
    """Process one input file in normal transcode mode."""
    paths = build_paths(cfg, input_file, create_dirs=not cfg.no_logs)
    if cfg.no_logs:
        paths.out_dir.mkdir(parents=True, exist_ok=True)

    runtime_context = tempfile.TemporaryDirectory(prefix=f"{paths.stem}_transcode_") if cfg.no_logs else None
    try:
        runtime_paths = build_runtime_paths(paths, Path(runtime_context.name)) if runtime_context is not None else paths
        sidecar_seconds = prepare_digital8_sidecars(cfg, runtime_paths)
        if runtime_paths.output_file != paths.output_file:
            paths.output_file = runtime_paths.output_file

        ffmpeg_args = build_ffmpeg_args(cfg, runtime_paths, build_vf(cfg, runtime_paths), preview=False)
        print_summary(cfg, paths, ffmpeg_args, preview=False)

        if prompt:
            input("Press Enter to start transcode batch, or Ctrl-C to cancel...")

        start = time.perf_counter()
        try:
            if not cfg.no_logs:
                write_command_log(cfg, paths, ffmpeg_args)
            log_path = None if cfg.no_logs else paths.ffmpeg_log_file
            rc = run_ffmpeg(ffmpeg_args, log_path)
        finally:
            transcode_seconds = time.perf_counter() - start
    finally:
        if runtime_context is not None:
            runtime_context.cleanup()
    if rc == 0:
        print(f"Done: {paths.output_file}")
    return build_process_result(
        paths,
        output_file=paths.output_file,
        rc=rc,
        transcode_seconds=transcode_seconds,
        sidecar_seconds=sidecar_seconds,
        cfg=cfg,
    )


def process_one_file(cfg: Config, input_file: Path, prompt: bool) -> ProcessResult:
    """Process one input file through the transcode workflow."""
    if cfg.mode == "preview":
        return process_preview_file(cfg, input_file)
    return process_transcode_file(cfg, input_file, prompt)


def run_transcode_workflow(cfg: Config, input_files: list[Path]) -> int:
    """Run the transcode workflow for one or more files."""
    failures = 0
    prompted = cfg.assume_yes or cfg.mode == "preview"
    results: list[ProcessResult] = []

    if cfg.mode != "validate-duration":
        for input_file in input_files:
            print(f'processing {input_file.name}')
            try:
                result = process_one_file(cfg, input_file, prompt=not prompted)
                results.append(result)
                prompted = True
                if result.rc != 0:
                    failures += 1
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"ERROR processing {input_file}: {e}", file=sys.stderr)
                failures += 1

    if cfg.mode == "transcode":
        print_transcode_time_summary(results)
        if cfg.validate_duration:
            validation_results = validate_durations(cfg, input_files)
            failures += sum(1 for result in validation_results if not result.passed)
    elif cfg.mode == "validate-duration":
        validation_results = validate_durations(cfg, input_files)
        failures += sum(1 for result in validation_results if not result.passed)

    return 0 if failures == 0 else 1


def main() -> int:
    """Run the archive-layout CLI."""
    cfg, input_files = parse_archive_args()
    return run_transcode_workflow(cfg, input_files)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
