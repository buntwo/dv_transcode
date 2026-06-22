#!/usr/bin/env python3
"""Generate per-video grids comparing one control frame row against many LUTs."""

from __future__ import annotations

import argparse
import csv
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from generate_lut_review_sheets import (
    DEFAULT_CJK_FONT,
    DEFAULT_CJK_FONT_SCALE,
    DEFAULT_CJK_Y_OFFSET,
    DEFAULT_FONT,
    DEFAULT_HEADER_FONT,
    DEFAULT_HEADER_SCALE,
    DEFAULT_HEADER_STROKE_WIDTH,
    DEFAULT_HEADER_TITLE_X,
    DEFAULT_HEADER_TITLE_Y,
    DEFAULT_PADDING,
    DETAIL_SEPARATOR,
    VideoMetadata,
    build_text_annotations,
    calculate_sample_times,
    create_frame,
    display_aspect_ratio,
    format_progress,
    probe_metadata,
)


@dataclass(frozen=True)
class LutSpec:
    label: str
    path: Path


@dataclass(frozen=True)
class GridConfig:
    tile_width: int
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
    columns: int
    sample_count: int

    def tile_height_for(self, metadata: VideoMetadata) -> int:
        width_ratio, height_ratio = display_aspect_ratio(metadata)
        return round(self.tile_width * height_ratio / width_ratio)

    def tile_grid_width(self) -> int:
        return self.columns * self.tile_width + (2 * self.margin) + ((self.columns - 1) * self.padding)


def parse_lut(value: str) -> LutSpec:
    if "=" not in value:
        raise argparse.ArgumentTypeError("--lut must be LABEL=PATH")
    label, path_text = value.split("=", 1)
    label = label.strip()
    if not label:
        raise argparse.ArgumentTypeError("LUT label must not be empty")
    path = Path(path_text.strip())
    return LutSpec(label=label, path=path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate 12-row by N+1-column LUT comparison grids.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("inputs", type=Path, nargs="+")
    parser.add_argument("--lut", type=parse_lut, action="append", required=True, help="LABEL=PATH; may repeat")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--control-label", default="CTRL")
    parser.add_argument("--sample-count", type=int, default=12)
    parser.add_argument("--tile-width", type=int, default=300)
    parser.add_argument("--margin", type=int, default=20)
    parser.add_argument("--padding", type=int, default=5)
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    for input_path in args.inputs:
        if not input_path.exists() or not input_path.is_file():
            raise ValueError(f"input does not exist or is not a file: {input_path}")
    for lut in args.lut:
        if not lut.path.exists() or not lut.path.is_file():
            raise ValueError(f"LUT does not exist: {lut.path}")
    if shutil.which("ffmpeg") is None:
        raise ValueError("ffmpeg is required")
    if shutil.which("ffprobe") is None:
        raise ValueError("ffprobe is required")
    if shutil.which("magick") is None:
        raise ValueError("ImageMagick 'magick' is required")


def config_from_args(args: argparse.Namespace) -> GridConfig:
    return GridConfig(
        tile_width=args.tile_width,
        header_height=126,
        margin=args.margin,
        padding=args.padding,
        font=DEFAULT_FONT,
        header_font=DEFAULT_HEADER_FONT,
        cjk_font=DEFAULT_CJK_FONT,
        cjk_font_scale=DEFAULT_CJK_FONT_SCALE,
        cjk_y_offset=DEFAULT_CJK_Y_OFFSET,
        header_scale=DEFAULT_HEADER_SCALE,
        header_stroke_width=DEFAULT_HEADER_STROKE_WIDTH,
        header_title_x=DEFAULT_HEADER_TITLE_X,
        header_title_y=DEFAULT_HEADER_TITLE_Y,
        header_detail_x=36,
        header_detail_y=77,
        point_size=18,
        header_point_size=34,
        detail_point_size=19,
        columns=1 + len(args.lut),
        sample_count=args.sample_count,
    )


def output_path_for(input_path: Path, output_dir: Path) -> Path:
    return output_dir / f"{input_path.name}.lut_grid.png"


def create_grid(input_path: Path, output_path: Path, metadata: VideoMetadata, config: GridConfig, luts: list[LutSpec], control_label: str) -> None:
    with tempfile.TemporaryDirectory(prefix="lut-comparison-grid-") as tmp_name:
        tmp = Path(tmp_name)
        frame_paths: list[Path] = []
        variants: list[tuple[str, Path | None]] = [(control_label, None)] + [(lut.label, lut.path) for lut in luts]

        for row_index, sample_time in enumerate(calculate_sample_times(metadata.duration_seconds, config.sample_count), start=1):
            for column_index, (label, lut_path) in enumerate(variants):
                frame_path = tmp / f"r{row_index:03d}_c{column_index:03d}.png"
                create_frame(
                    input_path,
                    frame_path,
                    sample_time,
                    metadata,
                    config,
                    label,
                    lut_path,
                )
                frame_paths.append(frame_path)

        tile_path = tmp / "grid.png"
        header_path = tmp / "header.png"
        compose_grid(frame_paths, tile_path, metadata, config)
        create_header(header_path, metadata, config, control_label, luts)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["magick", str(header_path), str(tile_path), "-append", str(output_path)], check=True)


def compose_grid(frame_paths: list[Path], tile_path: Path, metadata: VideoMetadata, config: GridConfig) -> None:
    tile_width = config.tile_width
    tile_height = config.tile_height_for(metadata)
    grid_width = config.tile_grid_width()
    grid_height = config.sample_count * tile_height + (2 * config.margin) + ((config.sample_count - 1) * config.padding)
    cmd = ["magick", "-size", f"{grid_width}x{grid_height}", "xc:white"]
    for index, frame_path in enumerate(frame_paths):
        row = index // config.columns
        column = index % config.columns
        x = config.margin + column * (tile_width + config.padding)
        y = config.margin + row * (tile_height + config.padding)
        cmd.extend([str(frame_path), "-geometry", f"+{x}+{y}", "-composite"])
    cmd.append(str(tile_path))
    subprocess.run(cmd, check=True)


def create_header(header_path: Path, metadata: VideoMetadata, config: GridConfig, control_label: str, luts: list[LutSpec]) -> None:
    scale = config.header_scale
    working_path = header_path.with_name("header-large.png")
    sheet_width = config.tile_grid_width()
    column_text = "Columns: " + ", ".join([control_label] + [lut.label for lut in luts])
    detail_text = f"{metadata.detail_text}{DETAIL_SEPARATOR}{config.sample_count} rows x {config.columns} cols{DETAIL_SEPARATOR}{column_text}"

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


def write_manifest(output_dir: Path, luts: list[LutSpec], control_label: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "column_manifest.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["column", "label", "lut"])
        writer.writeheader()
        writer.writerow({"column": 1, "label": control_label, "lut": ""})
        for index, lut in enumerate(luts, start=2):
            writer.writerow({"column": index, "label": lut.label, "lut": str(lut.path)})


def main() -> int:
    args = parse_args()
    try:
        validate_args(args)
        config = config_from_args(args)
        write_manifest(args.output_dir, args.lut, args.control_label)
        total = len(args.inputs)
        for index, input_path in enumerate(args.inputs, start=1):
            if total > 1:
                print(format_progress(index, total, input_path), flush=True)
            metadata = probe_metadata(input_path)
            create_grid(
                input_path,
                output_path_for(input_path, args.output_dir),
                metadata,
                config,
                args.lut,
                args.control_label,
            )
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"generate_lut_comparison_grid.py: error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
