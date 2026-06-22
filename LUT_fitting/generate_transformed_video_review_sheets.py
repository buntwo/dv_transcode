#!/usr/bin/env python3
"""Generate train/validation contact sheets for transformed full-clip outputs."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from evaluate_luts import read_manifest
from generate_lut_review_sheets import calculate_sample_times, probe_metadata
from run_expt9_greyworld_review import Variant, compose_grid, render_frame, safe_name, tile_size


TRANSFORMED_ROOT = Path("generated_video_pairs/evaluations/expt9D_builtin_filters/transformed_videos")
TRAIN_PAIRS = Path("generated_video_pairs/train_geometry_normalized_pairs.txt")
VALIDATION_PAIRS = Path("generated_video_pairs/validation_geometry_normalized_pairs.txt")


def write_column_manifest(out_dir: Path, variants: list[Variant]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "column_manifest.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["column", "label", "source"])
        writer.writeheader()
        for index, variant in enumerate(variants, start=1):
            writer.writerow({"column": index, "label": variant.label, "source": variant.vf or ""})


def make_split(
    split: str,
    manifest: Path,
    transformed_root: Path,
    pipeline: str,
    compare_pipeline: str | None,
    out_root: Path,
    frames: int,
    tile_width_arg: int,
) -> None:
    out_dir = out_root / split
    frames_root = out_root / "frames" / split
    variants = [Variant("A", None), Variant("B", None)]
    if compare_pipeline:
        variants.append(Variant(compare_pipeline, str(transformed_root / compare_pipeline / split)))
    variants.append(Variant(pipeline, str(transformed_root / pipeline / split)))
    write_column_manifest(out_dir, variants)

    for index, (video_a, video_b) in enumerate(read_manifest(manifest), start=1):
        pair_key = f"pair_{index:03d}"
        transformed_b = transformed_root / pipeline / split / f"{pair_key}_B.mkv"
        compare_b = transformed_root / compare_pipeline / split / f"{pair_key}_B.mkv" if compare_pipeline else None
        sources = [("A", video_a), ("B", video_b)]
        if compare_b:
            sources.append((compare_pipeline, compare_b))
        sources.append((pipeline, transformed_b))
        for label, path in sources:
            if not path.exists():
                raise FileNotFoundError(f"Missing {label} video for {split} {pair_key}: {path}")

        src_meta = probe_metadata(video_b)
        ref_meta = probe_metadata(video_a)
        duration = min(src_meta.duration_seconds, ref_meta.duration_seconds)
        tile_width, tile_height = tile_size(video_b, tile_width_arg)
        frame_paths = []
        for row_index, time_s in enumerate(calculate_sample_times(duration, frames), start=1):
            for label, source in sources:
                frame_path = frames_root / pair_key / f"r{row_index:03d}_{safe_name(label)}.png"
                render_frame(source, frame_path, time_s, tile_width, tile_height, Variant(label, None))
                frame_paths.append(frame_path)
        compose_grid(
            frame_paths,
            frames,
            len(sources),
            tile_width,
            tile_height,
            f"{split} {pair_key}: {', '.join(label for label, _source in sources)}",
            out_dir / f"{pair_key}.{pipeline}.grid.png",
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transformed-root", type=Path, default=TRANSFORMED_ROOT)
    parser.add_argument("--train-pairs", type=Path, default=TRAIN_PAIRS)
    parser.add_argument("--validation-pairs", type=Path, default=VALIDATION_PAIRS)
    parser.add_argument("--pipeline", default="pure_filtergraph_cc_opt")
    parser.add_argument("--compare-pipeline", default="g_opt_cc_opt")
    parser.add_argument("--out-root", type=Path, default=TRANSFORMED_ROOT / "review_sheets" / "pure_filtergraph_cc_opt")
    parser.add_argument("--frames", type=int, default=3)
    parser.add_argument("--tile-width", type=int, default=360)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    make_split(
        "train",
        args.train_pairs,
        args.transformed_root,
        args.pipeline,
        args.compare_pipeline,
        args.out_root,
        args.frames,
        args.tile_width,
    )
    make_split(
        "validation",
        args.validation_pairs,
        args.transformed_root,
        args.pipeline,
        args.compare_pipeline,
        args.out_root,
        args.frames,
        args.tile_width,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
