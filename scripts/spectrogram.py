#!/usr/bin/env python3
"""Generate audio spectrogram PNGs with ffmpeg."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from contact_sheet import probe_metadata
from utils import build_text_annotations
from utils import format_progress


DEFAULT_SIZE = "1920x1080"
DEFAULT_FONT = "Helvetica-Neue"
DEFAULT_CJK_FONT = "Heiti-SC-Medium"
DEFAULT_CJK_FONT_SCALE = 0.94
DEFAULT_CJK_Y_OFFSET = -2
DEFAULT_TITLE_POINT_SIZE = 16
DEFAULT_TITLE_X = 20
DEFAULT_TITLE_Y = 18
DEFAULT_FOOTER_COVER_WIDTH = 720
DEFAULT_FOOTER_COVER_HEIGHT = 34
DEFAULT_POSTPROCESS_SCALE = 4


@dataclass(frozen=True)
class SpectrogramConfig:
    duration_seconds: float | None
    size: str
    font: str
    cjk_font: str
    cjk_font_scale: float
    cjk_y_offset: int
    title_point_size: int
    title_x: int
    title_y: int
    footer_cover_width: int
    footer_cover_height: int
    postprocess_scale: int


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate audio spectrogram PNGs.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("inputs", type=Path, nargs="+", help="Input media file(s).")
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        help="Directory for <input filename>.spectrogram.png. Cannot be combined with --output.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output PNG path for one input. Defaults to <input>.spectrogram.png.",
    )
    parser.add_argument(
        "--duration",
        type=positive_float,
        help="Seconds of audio to include from the start of each file. Defaults to the full input.",
    )
    parser.add_argument("--size", default=DEFAULT_SIZE, help="Output image size.")
    return parser.parse_args(argv)


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
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
    if shutil.which("ffmpeg") is None:
        raise ValueError("ffmpeg is required")
    if shutil.which("ffprobe") is None:
        raise ValueError("ffprobe is required")
    if shutil.which("magick") is None:
        raise ValueError("ImageMagick 'magick' is required")


def config_from_args(args: argparse.Namespace) -> SpectrogramConfig:
    return SpectrogramConfig(
        duration_seconds=args.duration,
        size=args.size,
        font=DEFAULT_FONT,
        cjk_font=DEFAULT_CJK_FONT,
        cjk_font_scale=DEFAULT_CJK_FONT_SCALE,
        cjk_y_offset=DEFAULT_CJK_Y_OFFSET,
        title_point_size=DEFAULT_TITLE_POINT_SIZE,
        title_x=DEFAULT_TITLE_X,
        title_y=DEFAULT_TITLE_Y,
        footer_cover_width=DEFAULT_FOOTER_COVER_WIDTH,
        footer_cover_height=DEFAULT_FOOTER_COVER_HEIGHT,
        postprocess_scale=DEFAULT_POSTPROCESS_SCALE,
    )


def default_output_path(input_path: Path) -> Path:
    return input_path.with_name(f"{input_path.name}.spectrogram.png")


def resolve_output_path(input_path: Path, output_path: Path | None, output_dir: Path | None) -> Path:
    if output_path is not None:
        return output_path
    if output_dir is not None:
        return output_dir / f"{input_path.name}.spectrogram.png"
    return default_output_path(input_path)


def build_ffmpeg_command(input_path: Path, output_path: Path, config: SpectrogramConfig) -> list[str]:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        "00:00:00",
    ]
    if config.duration_seconds is not None:
        cmd.extend(["-t", format_duration_arg(config.duration_seconds)])
    cmd.extend(
        [
            "-i",
            str(input_path),
            "-filter_complex",
            f"[0:a]showspectrumpic=s={config.size}:mode=separate:legend=1:scale=log:fscale=lin",
            "-frames:v",
            "1",
            str(output_path),
        ]
    )
    return cmd


def format_duration_arg(seconds: float) -> str:
    if seconds.is_integer():
        return str(int(seconds))
    return f"{seconds:g}"


def identify_image_size(path: Path) -> tuple[int, int]:
    cmd = ["magick", "identify", "-format", "%w %h", str(path)]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    width, height = result.stdout.strip().split()
    return int(width), int(height)


def build_postprocess_command(
    title_text: str,
    raw_path: Path,
    output_path: Path,
    config: SpectrogramConfig,
) -> list[str]:
    width, height = identify_image_size(raw_path)
    scale = config.postprocess_scale
    scaled_width = width * scale
    scaled_height = height * scale
    footer_y = max(scaled_height - (config.footer_cover_height * scale), 0)
    text_annotations = build_text_annotations(
        title_text,
        primary_font=config.font,
        cjk_font=config.cjk_font,
        cjk_font_scale=config.cjk_font_scale,
        point_size=config.title_point_size * scale,
        x=config.title_x * scale,
        y=config.title_y * scale,
        cjk_y_offset=config.cjk_y_offset * scale,
    )
    return [
        "magick",
        str(raw_path),
        "-filter",
        "point",
        "-resize",
        f"{scaled_width}x{scaled_height}!",
        "-fill",
        "black",
        "-draw",
        f"rectangle 0,{footer_y} {config.footer_cover_width * scale},{scaled_height}",
        "-fill",
        "white",
        "-stroke",
        "none",
        "-gravity",
        "northwest",
        *text_annotations,
        "-filter",
        "Lanczos",
        "-resize",
        f"{width}x{height}!",
        str(output_path),
    ]


def create_spectrogram(input_path: Path, output_path: Path, config: SpectrogramConfig) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="spectrogram-") as tmp_name:
        raw_path = Path(tmp_name) / "raw.png"
        subprocess.run(build_ffmpeg_command(input_path, raw_path, config), check=True)
        metadata = probe_metadata(input_path)
        title_text = f"{metadata.header_text}  ·  {metadata.detail_text}"
        subprocess.run(build_postprocess_command(title_text, raw_path, output_path, config), check=True)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        validate_args(args)
        config = config_from_args(args)
        show_progress = len(args.inputs) > 1
        total = len(args.inputs)
        for index, input_path in enumerate(args.inputs, start=1):
            if show_progress:
                print(format_progress(index, total, input_path), flush=True)
            output_path = resolve_output_path(input_path, args.output, args.output_dir)
            create_spectrogram(input_path, output_path, config)
            if not show_progress:
                print(f"Wrote {output_path}")
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"spectrogram.py: error: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
