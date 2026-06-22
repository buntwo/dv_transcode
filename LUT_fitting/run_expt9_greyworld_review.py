#!/usr/bin/env python3
"""Generate high-resolution greyworld/greyedge/vibrance review grids with cached frames."""

from __future__ import annotations

import argparse
import csv
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from evaluate_luts import read_manifest
from generate_lut_review_sheets import calculate_sample_times, display_aspect_ratio, format_duration, probe_metadata
from run_expt9BC_filters import GOPT_LUT, suffix_filter
from run_expt9_luma_only import ACCESS_ROOT, VALIDATION_PAIRS, access_inputs


OUT_ROOT = Path("generated_video_pairs/evaluations/expt9C_greyworld_review")


@dataclass(frozen=True)
class Variant:
    label: str
    vf: str | None


def run(cmd: list[str | Path]) -> None:
    subprocess.run([str(part) for part in cmd], check=True)


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")


def variants() -> list[Variant]:
    return [
        Variant("CTRL", None),
        Variant("g_opt", suffix_filter(GOPT_LUT, "")),
        Variant("g_opt_vibrance", suffix_filter(GOPT_LUT, "vibrance=intensity=-0.28")),
        Variant("g_opt_greyedge", suffix_filter(GOPT_LUT, "greyedge=difford=2:minknorm=5:sigma=1.0")),
        Variant("g_opt_greyworld", suffix_filter(GOPT_LUT, "grayworld")),
    ]


def tile_size(video: Path, tile_width: int) -> tuple[int, int]:
    metadata = probe_metadata(video)
    width_ratio, height_ratio = display_aspect_ratio(metadata)
    return tile_width, round(tile_width * height_ratio / width_ratio)


def draw_label(path: Path, label: str, timestamp: str) -> None:
    image = Image.open(path).convert("RGB")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    for text, xy, anchor in [(label, (8, 8), "left"), (timestamp, (image.width - 8, image.height - 22), "right")]:
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        x, y = xy
        if anchor == "right":
            x -= tw
        draw.rectangle((x - 5, y - 4, x + tw + 5, y + th + 4), fill=(0, 0, 0))
        draw.text((x, y), text, fill=(255, 255, 255), font=font)
    image.save(path)


def render_frame(
    video: Path,
    frame_path: Path,
    time_s: float,
    tile_width: int,
    tile_height: int,
    variant: Variant,
) -> None:
    if frame_path.exists():
        return
    frame_path.parent.mkdir(parents=True, exist_ok=True)
    filters = []
    if variant.vf:
        filters.append(variant.vf)
    filters.extend(
        [
            f"scale={tile_width}:{tile_height}:force_original_aspect_ratio=decrease",
            f"pad={tile_width}:{tile_height}:(ow-iw)/2:(oh-ih)/2:white",
            "format=rgb24",
        ]
    )
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{time_s:.6f}",
            "-i",
            video,
            "-vf",
            ",".join(filters),
            "-frames:v",
            "1",
            frame_path,
        ]
    )
    draw_label(frame_path, variant.label, format_duration(time_s))


def compose_grid(
    frame_paths: list[Path],
    rows: int,
    columns: int,
    tile_width: int,
    tile_height: int,
    title: str,
    output_path: Path,
) -> None:
    margin = 24
    padding = 6
    header_height = 92
    width = columns * tile_width + 2 * margin + (columns - 1) * padding
    height = header_height + rows * tile_height + 2 * margin + (rows - 1) * padding
    sheet = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    draw.text((margin, 22), title, fill=(0, 0, 0), font=font)
    for index, frame_path in enumerate(frame_paths):
        row = index // columns
        column = index % columns
        x = margin + column * (tile_width + padding)
        y = header_height + margin + row * (tile_height + padding)
        sheet.paste(Image.open(frame_path).convert("RGB"), (x, y))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path)


def write_variant_manifest(out_dir: Path, vars_: list[Variant]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "column_manifest.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["column", "label", "vf"])
        writer.writeheader()
        for index, variant in enumerate(vars_, start=1):
            writer.writerow({"column": index, "label": variant.label, "vf": variant.vf or ""})


def make_access(args: argparse.Namespace, vars_: list[Variant]) -> None:
    out_dir = args.out_root / "access_grid"
    frames_root = args.out_root / "frames" / "access"
    write_variant_manifest(out_dir, vars_)
    for video in access_inputs(args.access_root):
        metadata = probe_metadata(video)
        tile_width, tile_height = tile_size(video, args.access_tile_width)
        frame_paths = []
        video_key = safe_name(video.stem)
        for row_index, time_s in enumerate(calculate_sample_times(metadata.duration_seconds, args.access_frames), start=1):
            for variant in vars_:
                frame_path = frames_root / video_key / f"r{row_index:03d}_{safe_name(variant.label)}.png"
                render_frame(video, frame_path, time_s, tile_width, tile_height, variant)
                frame_paths.append(frame_path)
        compose_grid(
            frame_paths,
            rows=args.access_frames,
            columns=len(vars_),
            tile_width=tile_width,
            tile_height=tile_height,
            title=f"Greyworld review: {video.name} | columns: {', '.join(v.label for v in vars_)}",
            output_path=out_dir / f"{video.name}.greyworld_grid.png",
        )


def make_validation(args: argparse.Namespace, vars_: list[Variant]) -> None:
    out_dir = args.out_root / "validation_grid"
    frames_root = args.out_root / "frames" / "validation"
    write_variant_manifest(out_dir, [Variant("Video8", None), *vars_])
    for index, (ref_video, src_video) in enumerate(read_manifest(args.validation_pairs), start=1):
        src_meta = probe_metadata(src_video)
        ref_meta = probe_metadata(ref_video)
        duration = min(src_meta.duration_seconds, ref_meta.duration_seconds)
        tile_width, tile_height = tile_size(src_video, args.validation_tile_width)
        frame_paths = []
        pair_key = f"pair_{index:03d}"
        for row_index, time_s in enumerate(calculate_sample_times(duration, args.validation_frames), start=1):
            ref_frame = frames_root / pair_key / f"r{row_index:03d}_Video8.png"
            render_frame(ref_video, ref_frame, time_s, tile_width, tile_height, Variant("Video8", None))
            frame_paths.append(ref_frame)
            for variant in vars_:
                frame_path = frames_root / pair_key / f"r{row_index:03d}_{safe_name(variant.label)}.png"
                render_frame(src_video, frame_path, time_s, tile_width, tile_height, variant)
                frame_paths.append(frame_path)
        compose_grid(
            frame_paths,
            rows=args.validation_frames,
            columns=1 + len(vars_),
            tile_width=tile_width,
            tile_height=tile_height,
            title=f"Greyworld validation {pair_key} | columns: Video8, {', '.join(v.label for v in vars_)}",
            output_path=out_dir / f"{pair_key}.greyworld_grid.png",
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-root", type=Path, default=OUT_ROOT)
    parser.add_argument("--access-root", type=Path, default=ACCESS_ROOT)
    parser.add_argument("--validation-pairs", type=Path, default=VALIDATION_PAIRS)
    parser.add_argument("--access-frames", type=int, default=12)
    parser.add_argument("--validation-frames", type=int, default=3)
    parser.add_argument("--access-tile-width", type=int, default=420)
    parser.add_argument("--validation-tile-width", type=int, default=360)
    parser.add_argument("--skip-access", action="store_true")
    parser.add_argument("--skip-validation", action="store_true")
    return parser.parse_args()


def main() -> int:
    vars_ = variants()
    args = parse_args()
    if not args.skip_access:
        make_access(args, vars_)
    if not args.skip_validation:
        make_validation(args, vars_)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
