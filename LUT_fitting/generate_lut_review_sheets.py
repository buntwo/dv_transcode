#!/usr/bin/env python3
"""Generate original-vs-LUT contact sheets for quick VHS LUT review."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path


SCRIPT_UTILS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_UTILS))

from utils import build_text_annotations  # noqa: E402
from utils import format_progress  # noqa: E402


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
    def columns(self) -> int:
        return 6

    @property
    def rows(self) -> int:
        return 4

    @property
    def sample_count(self) -> int:
        return 12

    @property
    def tile_width(self) -> int:
        grid_width = self.sheet_width - (2 * self.margin) - ((self.columns - 1) * self.padding)
        return grid_width // self.columns

    def tile_height_for(self, metadata: VideoMetadata) -> int:
        width_ratio, height_ratio = display_aspect_ratio(metadata)
        return round(self.tile_width * height_ratio / width_ratio)

    def tile_grid_width(self) -> int:
        return self.columns * self.tile_width + (2 * self.margin) + ((self.columns - 1) * self.padding)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate 4x6 original/LUT side-by-side review sheets.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("inputs", type=Path, nargs="+")
    parser.add_argument("--lut", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("lut_review_sheets"))
    parser.add_argument("--sheet-width", type=int, default=DEFAULT_SHEET_WIDTH)
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    for input_path in args.inputs:
        if not input_path.exists() or not input_path.is_file():
            raise ValueError(f"input does not exist or is not a file: {input_path}")
    if not args.lut.exists() or not args.lut.is_file():
        raise ValueError(f"LUT does not exist: {args.lut}")
    if shutil.which("ffmpeg") is None:
        raise ValueError("ffmpeg is required")
    if shutil.which("ffprobe") is None:
        raise ValueError("ffprobe is required")
    if shutil.which("magick") is None:
        raise ValueError("ImageMagick 'magick' is required")


def config_from_args(args: argparse.Namespace) -> SheetConfig:
    return SheetConfig(
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


def output_path_for(input_path: Path, output_dir: Path) -> Path:
    return output_dir / f"{input_path.name}.lut_review.png"


def probe_metadata(input_path: Path) -> VideoMetadata:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(input_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
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


def calculate_sample_times(duration_seconds: float, sample_count: int) -> list[float]:
    step = duration_seconds / (sample_count + 1)
    return [step * index for index in range(1, sample_count + 1)]


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


def build_label_drawtext(label: str, config: SheetConfig) -> str:
    drawtext = ":".join(
        [
            f"font={escape_drawtext(config.font)}",
            f"text='{escape_drawtext(label)}'",
            "fontcolor=white",
            f"fontsize={config.point_size}",
            "box=1",
            "boxcolor=black@0.75",
            "boxborderw=4",
            "x=6",
            "y=6",
        ]
    )
    return f"drawtext={drawtext}"


def tile_filter(metadata: VideoMetadata, config: SheetConfig, timestamp_text: str, label: str, lut: Path | None) -> str:
    tile_width = config.tile_width
    tile_height = config.tile_height_for(metadata)
    filters = [
        f"scale={tile_width}:{tile_height}:force_original_aspect_ratio=decrease",
        f"pad={tile_width}:{tile_height}:(ow-iw)/2:(oh-ih)/2:white",
    ]
    if lut is not None:
        filters.extend(["format=gbrp", f"lut3d={lut}:interp=tetrahedral", "format=rgb24"])
    filters.extend(
        [
            build_label_drawtext(label, config),
            build_timestamp_drawtext(timestamp_text, config),
        ]
    )
    return ",".join(filters)


def create_frame(
    input_path: Path,
    frame_path: Path,
    sample_time: float,
    metadata: VideoMetadata,
    config: SheetConfig,
    label: str,
    lut: Path | None,
) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{sample_time:.6f}",
            "-i",
            str(input_path),
            "-filter:v",
            tile_filter(metadata, config, format_duration(sample_time), label, lut),
            "-frames:v",
            "1",
            str(frame_path),
        ],
        check=True,
    )


def create_tile(input_path: Path, tile_path: Path, metadata: VideoMetadata, config: SheetConfig, lut: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="lut-review-frames-") as tmp_name:
        tmp = Path(tmp_name)
        frame_paths: list[Path] = []
        for index, sample_time in enumerate(calculate_sample_times(metadata.duration_seconds, config.sample_count), start=1):
            original_path = tmp / f"frame-{index:03d}-a.png"
            corrected_path = tmp / f"frame-{index:03d}-b.png"
            create_frame(input_path, original_path, sample_time, metadata, config, "A", None)
            create_frame(input_path, corrected_path, sample_time, metadata, config, "B", lut)
            frame_paths.extend([original_path, corrected_path])
        compose_tile(frame_paths, tile_path, metadata, config)


def compose_tile(frame_paths: list[Path], tile_path: Path, metadata: VideoMetadata, config: SheetConfig) -> None:
    tile_width = config.tile_width
    tile_height = config.tile_height_for(metadata)
    grid_width = config.tile_grid_width()
    grid_height = config.rows * tile_height + (2 * config.margin) + ((config.rows - 1) * config.padding)
    cmd = ["magick", "-size", f"{grid_width}x{grid_height}", "xc:white"]
    for index, frame_path in enumerate(frame_paths):
        row = index // config.columns
        column = index % config.columns
        x = config.margin + column * (tile_width + config.padding)
        y = config.margin + row * (tile_height + config.padding)
        cmd.extend([str(frame_path), "-geometry", f"+{x}+{y}", "-composite"])
    cmd.append(str(tile_path))
    subprocess.run(cmd, check=True)


def create_header(header_path: Path, metadata: VideoMetadata, config: SheetConfig, lut: Path) -> None:
    scale = config.header_scale
    working_path = header_path.with_name("header-large.png")
    sheet_width = config.tile_grid_width()
    detail_text = f"{metadata.detail_text}{DETAIL_SEPARATOR}LUT {lut.name}"
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
        detail_text,
        primary_font=config.font,
        cjk_font=config.cjk_font,
        cjk_font_scale=config.cjk_font_scale,
        point_size=config.detail_point_size * scale,
        x=config.header_detail_x * scale,
        y=config.header_detail_y * scale,
        cjk_y_offset=config.cjk_y_offset * scale,
    )
    subprocess.run(
        [
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
        ],
        check=True,
    )
    working_path.replace(header_path)


def append_header(header_path: Path, tile_path: Path, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["magick", str(header_path), str(tile_path), "-append", str(output_path)], check=True)


def create_review_sheet(input_path: Path, output_path: Path, config: SheetConfig, lut: Path) -> VideoMetadata:
    metadata = probe_metadata(input_path)
    with tempfile.TemporaryDirectory(prefix="lut-review-sheet-") as tmp_name:
        tmp = Path(tmp_name)
        header_path = tmp / "header.png"
        tile_path = tmp / "tile.png"
        create_tile(input_path, tile_path, metadata, config, lut)
        create_header(header_path, metadata, config, lut)
        append_header(header_path, tile_path, output_path)
    return metadata


def escape_drawtext(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def main() -> int:
    args = parse_args()
    try:
        validate_args(args)
        config = config_from_args(args)
        total = len(args.inputs)
        for index, input_path in enumerate(args.inputs, start=1):
            if total > 1:
                print(format_progress(index, total, input_path), flush=True)
            output_path = output_path_for(input_path, args.output_dir)
            create_review_sheet(input_path, output_path, config, args.lut)
            if total == 1:
                print(f"Wrote {output_path}")
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"generate_lut_review_sheets.py: error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
