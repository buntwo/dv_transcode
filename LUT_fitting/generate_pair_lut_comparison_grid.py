#!/usr/bin/env python3
"""Generate paired validation grids: source frame, reference frame, and LUT variants."""

from __future__ import annotations

import argparse
import csv
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from generate_lut_comparison_grid import LutSpec, parse_lut
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
    DETAIL_SEPARATOR,
    VideoMetadata,
    build_text_annotations,
    calculate_sample_times,
    create_frame,
    display_aspect_ratio,
    format_duration,
    format_progress,
    probe_metadata,
)


@dataclass(frozen=True)
class PairSpec:
    label: str
    ref_path: Path
    src_path: Path


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate 12-row paired validation LUT comparison grids.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--pairs", type=Path, required=True)
    parser.add_argument("--lut", type=parse_lut, action="append", required=True, help="LABEL=PATH; may repeat")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-label", default="VHS")
    parser.add_argument("--reference-label", default="Video8")
    parser.add_argument("--sample-count", type=int, default=12)
    parser.add_argument("--tile-width", type=int, default=300)
    parser.add_argument("--margin", type=int, default=20)
    parser.add_argument("--padding", type=int, default=5)
    return parser.parse_args()


def read_pairs(path: Path) -> list[PairSpec]:
    pairs = []
    for index, line in enumerate(path.read_text().splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        ref, src = line.split("|", 1)
        ref_path = Path(ref.strip())
        src_path = Path(src.strip())
        pairs.append(PairSpec(label=f"pair_{len(pairs) + 1:03d}", ref_path=ref_path, src_path=src_path))
    if not pairs:
        raise ValueError(f"No pairs found in {path}")
    return pairs


def validate_args(args: argparse.Namespace, pairs: list[PairSpec]) -> None:
    for pair in pairs:
        if not pair.ref_path.exists():
            raise ValueError(f"reference video does not exist: {pair.ref_path}")
        if not pair.src_path.exists():
            raise ValueError(f"source video does not exist: {pair.src_path}")
    for lut in args.lut:
        if not lut.path.exists() or not lut.path.is_file():
            raise ValueError(f"LUT does not exist: {lut.path}")


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
        columns=2 + len(args.lut),
        sample_count=args.sample_count,
    )


def output_path_for(pair: PairSpec, output_dir: Path) -> Path:
    return output_dir / f"{pair.label}.validation_lut_grid.png"


def create_grid(
    pair: PairSpec,
    output_path: Path,
    src_metadata: VideoMetadata,
    ref_metadata: VideoMetadata,
    config: GridConfig,
    luts: list[LutSpec],
    source_label: str,
    reference_label: str,
) -> None:
    duration = min(src_metadata.duration_seconds, ref_metadata.duration_seconds)
    sample_times = calculate_sample_times(duration, config.sample_count)
    with tempfile.TemporaryDirectory(prefix="pair-lut-comparison-grid-") as tmp_name:
        tmp = Path(tmp_name)
        frame_paths: list[Path] = []
        for row_index, sample_time in enumerate(sample_times, start=1):
            src_path = tmp / f"r{row_index:03d}_c000_src.png"
            ref_path = tmp / f"r{row_index:03d}_c001_ref.png"
            create_frame(pair.src_path, src_path, sample_time, src_metadata, config, source_label, None)
            create_frame(pair.ref_path, ref_path, sample_time, ref_metadata, config, reference_label, None)
            frame_paths.extend([src_path, ref_path])
            for column_index, lut in enumerate(luts, start=2):
                lut_path = tmp / f"r{row_index:03d}_c{column_index:03d}_{lut.label}.png"
                create_frame(pair.src_path, lut_path, sample_time, src_metadata, config, lut.label, lut.path)
                frame_paths.append(lut_path)

        tile_path = tmp / "grid.png"
        header_path = tmp / "header.png"
        compose_grid(frame_paths, tile_path, src_metadata, config)
        create_header(header_path, pair, src_metadata, ref_metadata, config, luts, source_label, reference_label)
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


def create_header(
    header_path: Path,
    pair: PairSpec,
    src_metadata: VideoMetadata,
    ref_metadata: VideoMetadata,
    config: GridConfig,
    luts: list[LutSpec],
    source_label: str,
    reference_label: str,
) -> None:
    scale = config.header_scale
    working_path = header_path.with_name("header-large.png")
    sheet_width = config.tile_grid_width()
    columns = [source_label, reference_label] + [lut.label for lut in luts]
    duration = min(src_metadata.duration_seconds, ref_metadata.duration_seconds)
    detail_text = (
        f"{format_duration(duration)}{DETAIL_SEPARATOR}"
        f"{config.sample_count} rows x {config.columns} cols{DETAIL_SEPARATOR}"
        f"Columns: {', '.join(columns)}"
    )

    title_annotations = build_text_annotations(
        f"{pair.label}: {pair.src_path.name} vs {pair.ref_path.name}",
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


def write_manifest(output_dir: Path, pairs: list[PairSpec], luts: list[LutSpec], source_label: str, reference_label: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "column_manifest.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["column", "label", "source"])
        writer.writeheader()
        writer.writerow({"column": 1, "label": source_label, "source": "source/VHS clip"})
        writer.writerow({"column": 2, "label": reference_label, "source": "reference/Video8 clip"})
        for index, lut in enumerate(luts, start=3):
            writer.writerow({"column": index, "label": lut.label, "source": str(lut.path)})
    with (output_dir / "pair_manifest.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["pair", "reference", "source"])
        writer.writeheader()
        for pair in pairs:
            writer.writerow({"pair": pair.label, "reference": str(pair.ref_path), "source": str(pair.src_path)})


def main() -> int:
    args = parse_args()
    try:
        pairs = read_pairs(args.pairs)
        validate_args(args, pairs)
        config = config_from_args(args)
        write_manifest(args.output_dir, pairs, args.lut, args.source_label, args.reference_label)
        total = len(pairs)
        for index, pair in enumerate(pairs, start=1):
            if total > 1:
                print(format_progress(index, total, pair.src_path), flush=True)
            src_metadata = probe_metadata(pair.src_path)
            ref_metadata = probe_metadata(pair.ref_path)
            create_grid(
                pair,
                output_path_for(pair, args.output_dir),
                src_metadata,
                ref_metadata,
                config,
                args.lut,
                args.source_label,
                args.reference_label,
            )
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"generate_pair_lut_comparison_grid.py: error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
