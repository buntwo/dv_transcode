#!/usr/bin/env python3
"""Generate a timestamped video contact sheet."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from utils import build_text_annotations
from utils import format_progress


DEFAULT_COLUMNS = 5
DEFAULT_ROWS = 4
DEFAULT_SHEET_WIDTH = 2340
DEFAULT_HEADER_HEIGHT = 108
DEFAULT_FONT = "Helvetica"
DEFAULT_HEADER_FONT = "Helvetica-Neue"
DEFAULT_CJK_FONT = "Heiti-SC-Medium"
DEFAULT_CJK_FONT_SCALE = 0.94
DEFAULT_CJK_Y_OFFSET = -2
DEFAULT_MARGIN = 20
DEFAULT_PADDING = 5
DEFAULT_HEADER_SCALE = 4
DEFAULT_HEADER_STROKE_WIDTH = 0.0
DEFAULT_HEADER_TITLE_X = 36
DEFAULT_HEADER_TITLE_Y = 34
DEFAULT_HEADER_DETAIL_X = 36
DEFAULT_HEADER_DETAIL_Y = 75
DETAIL_SEPARATOR = "  ·  "


@dataclass(frozen=True)
class VideoMetadata:
    filename: str
    size_bytes: int
    duration_seconds: float
    width: int
    height: int
    display_aspect_ratio: str | None
    frame_rate: float | None
    video_codec: str
    audio_codec: str | None
    audio_channels: int | None
    audio_sample_rate: int | None

    @property
    def header_text(self) -> str:
        return self.filename

    @property
    def detail_text(self) -> str:
        parts = [
            format_size(self.size_bytes),
            format_duration(self.duration_seconds),
            format_dimensions(self.width, self.height, self.display_aspect_ratio),
        ]
        if self.frame_rate is not None:
            parts.append(f"{self.frame_rate:.2f}fps")
        parts.append(self.video_codec)
        if self.audio_codec:
            audio = self.audio_codec
            if self.audio_channels is not None:
                audio += f" {self.audio_channels}ch"
            if self.audio_sample_rate is not None:
                audio += f" {self.audio_sample_rate}Hz"
            parts.append(audio)
        return DETAIL_SEPARATOR.join(parts)


@dataclass(frozen=True)
class SheetConfig:
    columns: int
    rows: int
    sheet_width: int
    header_height: int
    margin: int
    padding: int
    font: str
    header_font: str
    cjk_font: str
    cjk_font_scale: float
    cjk_y_offset: int
    header_scale: int
    header_stroke_width: float
    header_title_x: int
    header_title_y: int
    header_detail_x: int
    header_detail_y: int
    point_size: int
    header_point_size: int
    detail_point_size: int

    @property
    def frame_count(self) -> int:
        return self.columns * self.rows

    @property
    def tile_width(self) -> int:
        grid_width = self.sheet_width - (2 * self.margin) - ((self.columns - 1) * self.padding)
        return grid_width // self.columns

    def tile_height_for(self, metadata: VideoMetadata) -> int:
        width_ratio, height_ratio = display_aspect_ratio(metadata)
        return round(self.tile_width * height_ratio / width_ratio)

    def tile_grid_width(self) -> int:
        return self.columns * self.tile_width + (2 * self.margin) + ((self.columns - 1) * self.padding)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a video contact sheet with timestamps and media metadata.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("inputs", type=Path, nargs="+", help="Input video file(s).")
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        help="Directory for <input filename>.contact_sheet.png. Cannot be combined with --output.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output PNG path for one input. Defaults to <input>.contact_sheet.png.",
    )
    parser.add_argument("--rows", type=positive_int, default=DEFAULT_ROWS, help="Number of thumbnail rows.")
    parser.add_argument("--columns", type=positive_int, default=DEFAULT_COLUMNS, help="Number of thumbnail columns.")
    parser.add_argument("--sheet-width", type=positive_int, default=DEFAULT_SHEET_WIDTH, help="Final PNG width in pixels.")
    return parser.parse_args(argv)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be 0 or greater")
    return parsed


def validate_args(args: argparse.Namespace) -> None:
    for input_path in args.inputs:
        if not input_path.exists():
            raise ValueError(f"input does not exist: {input_path}")
        if not input_path.is_file():
            raise ValueError(f"input is not a file: {input_path}")
    if args.output and args.output_dir:
        raise ValueError("--output and --output-dir cannot be combined")
    if args.output and len(args.inputs) > 1:
        raise ValueError("--output can only be used with one input")
    grid_width = args.sheet_width - (2 * DEFAULT_MARGIN) - ((args.columns - 1) * DEFAULT_PADDING)
    if grid_width < args.columns:
        raise ValueError("--sheet-width is too small for the requested number of columns")
    if shutil.which("ffmpeg") is None:
        raise ValueError("ffmpeg is required")
    if shutil.which("ffprobe") is None:
        raise ValueError("ffprobe is required")
    if shutil.which("magick") is None:
        raise ValueError("ImageMagick 'magick' is required")


def config_from_args(args: argparse.Namespace) -> SheetConfig:
    return SheetConfig(
        columns=args.columns,
        rows=args.rows,
        sheet_width=args.sheet_width,
        header_height=DEFAULT_HEADER_HEIGHT,
        margin=DEFAULT_MARGIN,
        padding=DEFAULT_PADDING,
        font=DEFAULT_FONT,
        header_font=DEFAULT_HEADER_FONT,
        cjk_font=DEFAULT_CJK_FONT,
        cjk_font_scale=DEFAULT_CJK_FONT_SCALE,
        cjk_y_offset=DEFAULT_CJK_Y_OFFSET,
        header_scale=DEFAULT_HEADER_SCALE,
        header_stroke_width=DEFAULT_HEADER_STROKE_WIDTH,
        header_title_x=DEFAULT_HEADER_TITLE_X,
        header_title_y=DEFAULT_HEADER_TITLE_Y,
        header_detail_x=DEFAULT_HEADER_DETAIL_X,
        header_detail_y=DEFAULT_HEADER_DETAIL_Y,
        point_size=18,
        header_point_size=36,
        detail_point_size=22,
    )


def default_output_path(input_path: Path) -> Path:
    return input_path.with_name(f"{input_path.name}.contact_sheet.png")


def resolve_output_path(input_path: Path, output_path: Path | None, output_dir: Path | None) -> Path:
    if output_path is not None:
        return output_path
    if output_dir is not None:
        return output_dir / f"{input_path.name}.contact_sheet.png"
    return default_output_path(input_path)


def probe_metadata(input_path: Path) -> VideoMetadata:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(input_path),
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return metadata_from_ffprobe(input_path, json.loads(result.stdout))


def metadata_from_ffprobe(input_path: Path, data: dict[str, object]) -> VideoMetadata:
    streams = data.get("streams", [])
    if not isinstance(streams, list):
        streams = []
    video = first_stream(streams, "video")
    if video is None:
        raise ValueError(f"no video stream found: {input_path}")
    audio = first_stream(streams, "audio")
    format_info = data.get("format", {})
    if not isinstance(format_info, dict):
        format_info = {}

    duration = parse_optional_float(format_info.get("duration"))
    if duration is None:
        duration = parse_optional_float(video.get("duration"))
    if duration is None:
        raise ValueError(f"could not determine video duration: {input_path}")

    size = parse_optional_int(format_info.get("size")) or input_path.stat().st_size
    width = require_int(video.get("width"), "video width")
    height = require_int(video.get("height"), "video height")
    frame_rate = parse_rate(video.get("avg_frame_rate")) or parse_rate(video.get("r_frame_rate"))

    return VideoMetadata(
        filename=input_path.name,
        size_bytes=size,
        duration_seconds=duration,
        width=width,
        height=height,
        display_aspect_ratio=parse_optional_str(video.get("display_aspect_ratio")),
        frame_rate=frame_rate,
        video_codec=parse_optional_str(video.get("codec_name")) or "unknown",
        audio_codec=parse_optional_str(audio.get("codec_name")) if audio else None,
        audio_channels=parse_optional_int(audio.get("channels")) if audio else None,
        audio_sample_rate=parse_optional_int(audio.get("sample_rate")) if audio else None,
    )


def first_stream(streams: list[object], codec_type: str) -> dict[str, object] | None:
    for stream in streams:
        if isinstance(stream, dict) and stream.get("codec_type") == codec_type:
            return stream
    return None


def require_int(value: object, label: str) -> int:
    parsed = parse_optional_int(value)
    if parsed is None:
        raise ValueError(f"missing {label}")
    return parsed


def parse_optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_optional_str(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def parse_rate(value: object) -> float | None:
    if not isinstance(value, str) or value in {"", "0/0"}:
        return None
    try:
        rate = Fraction(value)
    except ValueError:
        return None
    if rate <= 0:
        return None
    return float(rate)


def format_size(size_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(size_bytes)
    unit = units[0]
    for unit in units:
        if size < 1000 or unit == units[-1]:
            break
        size /= 1000
    if unit == "B":
        return f"{size_bytes} B"
    return f"{size:.2f} {unit}"


def format_duration(seconds: float) -> str:
    rounded = int(seconds + 0.5)
    hours = rounded // 3600
    minutes = (rounded % 3600) // 60
    secs = rounded % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def format_dimensions(width: int, height: int, display_aspect_ratio: str | None) -> str:
    if display_aspect_ratio:
        return f"{width}x{height} ({display_aspect_ratio})"
    divisor = math.gcd(width, height)
    return f"{width}x{height} ({width // divisor}:{height // divisor})"


def display_aspect_ratio(metadata: VideoMetadata) -> tuple[int, int]:
    if metadata.display_aspect_ratio:
        try:
            width, height = metadata.display_aspect_ratio.split(":", 1)
            return int(width), int(height)
        except ValueError:
            pass
    divisor = math.gcd(metadata.width, metadata.height)
    return metadata.width // divisor, metadata.height // divisor


def build_timestamp_drawtext(timestamp_text: str, config: SheetConfig) -> str:
    drawtext = ":".join(
        [
            f"font={escape_drawtext(config.font)}",
            f"text='{escape_drawtext(timestamp_text)}'",
            "fontcolor=white",
            f"fontsize={config.point_size}",
            "box=1",
            "boxcolor=black",
            "boxborderw=4",
            "x=w-tw-6",
            "y=h-th-6",
        ]
    )
    return f"drawtext={drawtext}"


def build_frame_filter(metadata: VideoMetadata, config: SheetConfig, timestamp_text: str) -> str:
    tile_width = config.tile_width
    tile_height = config.tile_height_for(metadata)
    return ",".join(
        [
            f"scale={tile_width}:{tile_height}:force_original_aspect_ratio=decrease",
            f"pad={tile_width}:{tile_height}:(ow-iw)/2:(oh-ih)/2:white",
            build_timestamp_drawtext(timestamp_text, config),
        ]
    )


def calculate_sample_step(duration_seconds: float, frame_count: int) -> float:
    return duration_seconds / (frame_count + 1)


def calculate_sample_times(duration_seconds: float, frame_count: int) -> list[float]:
    sample_step = calculate_sample_step(duration_seconds, frame_count)
    return [sample_step * index for index in range(1, frame_count + 1)]


def escape_drawtext(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def create_tile(input_path: Path, tile_path: Path, metadata: VideoMetadata, config: SheetConfig) -> None:
    with tempfile.TemporaryDirectory(prefix="contact-sheet-frames-") as tmp_name:
        frame_paths = extract_frames(input_path, Path(tmp_name), metadata, config)
        compose_tile(frame_paths, tile_path, metadata, config)


def extract_frames(input_path: Path, frame_dir: Path, metadata: VideoMetadata, config: SheetConfig) -> list[Path]:
    frame_paths: list[Path] = []
    for index, sample_time in enumerate(calculate_sample_times(metadata.duration_seconds, config.frame_count), start=1):
        frame_path = frame_dir / f"frame-{index:03d}.png"
        create_frame(input_path, frame_path, sample_time, metadata, config)
        frame_paths.append(frame_path)
    return frame_paths


def create_frame(
    input_path: Path,
    frame_path: Path,
    sample_time: float,
    metadata: VideoMetadata,
    config: SheetConfig,
) -> None:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        format_seconds(sample_time),
        "-i",
        str(input_path),
        "-filter:v",
        build_frame_filter(metadata, config, format_duration(sample_time)),
        "-frames:v",
        "1",
        str(frame_path),
    ]
    subprocess.run(cmd, check=True)


def compose_tile(frame_paths: list[Path], tile_path: Path, metadata: VideoMetadata, config: SheetConfig) -> None:
    tile_width = config.tile_width
    tile_height = config.tile_height_for(metadata)
    grid_width = config.tile_grid_width()
    grid_height = (config.rows * tile_height) + (2 * config.margin) + ((config.rows - 1) * config.padding)
    cmd = [
        "magick",
        "-size",
        f"{grid_width}x{grid_height}",
        "xc:white",
    ]
    for index, frame_path in enumerate(frame_paths):
        row = index // config.columns
        column = index % config.columns
        x = config.margin + column * (tile_width + config.padding)
        y = config.margin + row * (tile_height + config.padding)
        cmd.extend([str(frame_path), "-geometry", f"+{x}+{y}", "-composite"])
    cmd.append(str(tile_path))
    subprocess.run(cmd, check=True)


def format_seconds(seconds: float) -> str:
    return f"{seconds:.6f}"


def create_header(header_path: Path, metadata: VideoMetadata, config: SheetConfig) -> None:
    scale = config.header_scale
    working_path = header_path.with_name("header-large.png")
    sheet_width = config.tile_grid_width()
    title_annotations = build_text_annotations(
        metadata.header_text,
        primary_font=config.header_font,
        cjk_font=config.cjk_font,
        cjk_font_scale=config.cjk_font_scale,
        point_size=config.header_point_size * scale,
        x=config.header_title_x * scale,
        y=config.header_title_y * scale,
        cjk_y_offset=config.cjk_y_offset * scale,
    )
    detail_annotations = build_text_annotations(
        metadata.detail_text,
        primary_font=config.font,
        cjk_font=config.cjk_font,
        cjk_font_scale=config.cjk_font_scale,
        point_size=config.detail_point_size * scale,
        x=config.header_detail_x * scale,
        y=config.header_detail_y * scale,
        cjk_y_offset=config.cjk_y_offset * scale,
    )
    cmd = [
        "magick",
        "-size",
        f"{sheet_width * scale}x{config.header_height * scale}",
        "xc:white",
        "-fill",
        "black",
        "-stroke",
        "black",
        "-strokewidth",
        str(config.header_stroke_width),
        "-gravity",
        "northwest",
        *title_annotations,
        *detail_annotations,
        "-filter",
        "Lanczos",
        "-resize",
        f"{sheet_width}x{config.header_height}!",
        str(working_path),
    ]
    subprocess.run(cmd, check=True)
    working_path.replace(header_path)


def append_header(header_path: Path, tile_path: Path, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["magick", str(header_path), str(tile_path), "-append", str(output_path)]
    subprocess.run(cmd, check=True)


def create_contact_sheet(input_path: Path, output_path: Path, config: SheetConfig) -> VideoMetadata:
    metadata = probe_metadata(input_path)
    with tempfile.TemporaryDirectory(prefix="contact-sheet-") as tmp_name:
        tmp = Path(tmp_name)
        header_path = tmp / "header.png"
        tile_path = tmp / "tile.png"
        create_tile(input_path, tile_path, metadata, config)
        create_header(header_path, metadata, config)
        append_header(header_path, tile_path, output_path)
    return metadata


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        validate_args(args)
        config = config_from_args(args)
        outputs: list[tuple[Path, VideoMetadata]] = []
        show_progress = len(args.inputs) > 1
        total = len(args.inputs)
        for index, input_path in enumerate(args.inputs, start=1):
            if show_progress:
                print(format_progress(index, total, input_path), flush=True)
            output_path = resolve_output_path(input_path, args.output, args.output_dir)
            metadata = create_contact_sheet(input_path, output_path, config)
            outputs.append((output_path, metadata))
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"contact_sheet.py: error: {exc}")
        return 1

    for output_path, metadata in outputs:
        print(f"Wrote {output_path}")
        print(metadata.detail_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
