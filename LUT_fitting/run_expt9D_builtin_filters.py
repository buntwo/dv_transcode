#!/usr/bin/env python3
"""Optimize built-in ffmpeg color filters on top of g_opt, excluding grayworld/greyedge."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from run_expt9BC_filters import Candidate, evaluate_candidates, lut_filter, suffix_filter, summarize, write_candidates_manifest
from run_expt9_greyworld_review import Variant, compose_grid, render_frame, safe_name, tile_size
from run_expt9_luma_only import ACCESS_ROOT, VALIDATION_PAIRS, access_inputs
from evaluate_luts import read_manifest
from generate_lut_review_sheets import calculate_sample_times, display_aspect_ratio, probe_metadata
from run_expt9BC_filters import GOPT_LUT


OUT_ROOT = Path("generated_video_pairs/evaluations/expt9D_builtin_filters")
TRAIN_PAIRS = Path("generated_video_pairs/train_geometry_normalized_pairs.txt")


def candidates() -> list[Candidate]:
    out = [
        Candidate("g_opt", "A", "g_opt", lut_filter(GOPT_LUT), "fixed A baseline"),
        Candidate("g_opt_vibrance_28", "reference", "g_opt", suffix_filter(GOPT_LUT, "vibrance=intensity=-0.28"), "current B reference"),
    ]

    for blue in (0.02, 0.04, 0.06, 0.08):
        out.append(
            Candidate(
                f"cb_blue_{int(blue * 100):02d}",
                "colorbalance",
                "g_opt",
                suffix_filter(GOPT_LUT, f"colorbalance=bs={blue:.3f}:bm={blue * 0.875:.3f}:bh={blue * 0.20:.3f}:pl=1"),
                f"blue lift {blue:.2f}, preserve lightness",
            )
        )
        out.append(
            Candidate(
                f"cb_cool_{int(blue * 100):02d}",
                "colorbalance",
                "g_opt",
                suffix_filter(
                    GOPT_LUT,
                    (
                        f"colorbalance=rs={-0.50 * blue:.3f}:gs={-0.25 * blue:.3f}:bs={blue:.3f}:"
                        f"rm={-0.45 * blue:.3f}:gm={-0.20 * blue:.3f}:bm={0.875 * blue:.3f}:"
                        f"rh={-0.10 * blue:.3f}:bh={0.20 * blue:.3f}:pl=1"
                    ),
                ),
                f"cool red/green down and blue up {blue:.2f}, preserve lightness",
            )
        )

    for amount in (0.01, 0.02, 0.04, 0.06, 0.08):
        for saturation in (1.00, 0.95, 0.90):
            sat_label = int(round(saturation * 100))
            out.append(
                Candidate(
                    f"cc_manual_{int(amount * 100):02d}_sat{sat_label}",
                    "colorcorrect",
                    "g_opt",
                    suffix_filter(
                        GOPT_LUT,
                        (
                            f"colorcorrect=rl={-amount:.3f}:bl={2 * amount:.3f}:"
                            f"rh={-0.50 * amount:.3f}:bh={amount:.3f}:saturation={saturation:.2f}"
                        ),
                    ),
                    f"manual colorcorrect amount {amount:.2f}, saturation {saturation:.2f}",
                )
            )

    for analyze in ("average", "median", "minmax"):
        for saturation in (1.00, 0.95, 0.90):
            out.append(
                Candidate(
                    f"cc_{analyze}_sat{int(round(saturation * 100))}",
                    "colorcorrect",
                    "g_opt",
                    suffix_filter(GOPT_LUT, f"colorcorrect=analyze={analyze}:saturation={saturation:.2f}"),
                    f"auto colorcorrect {analyze}, saturation {saturation:.2f}",
                )
            )

    for strength in (0.05, 0.10, 0.15, 0.20):
        for independence in (0.00, 0.25, 0.50, 1.00):
            out.append(
                Candidate(
                    f"norm_s{int(strength * 100):02d}_i{int(independence * 100):03d}",
                    "normalize",
                    "g_opt",
                    suffix_filter(GOPT_LUT, f"normalize=strength={strength:.2f}:independence={independence:.2f}"),
                    f"normalize strength {strength:.2f}, independence {independence:.2f}",
                )
            )

    return out


def select_winners(summary_rows: list[dict[str, object]]) -> list[dict[str, object]]:
    winners = []
    for part in ("colorbalance", "colorcorrect", "normalize"):
        rows = [row for row in summary_rows if row["part"] == part]
        rows.sort(key=lambda row: (row["tone_score"], row["delta_e76_mean"]))
        winners.append(rows[0])
    return winners


def write_winners(rows: list[dict[str, object]], out_path: Path) -> None:
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["part", "candidate", "tone_score", "delta_e76_mean", "rgb_mae", "note"])
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "part": row["part"],
                    "candidate": row["candidate"],
                    "tone_score": row["tone_score"],
                    "delta_e76_mean": row["delta_e76_mean"],
                    "rgb_mae": row["rgb_mae"],
                    "note": row["note"],
                }
            )


def make_variants(all_candidates: list[Candidate], winners: list[dict[str, object]]) -> list[Variant]:
    by_label = {candidate.label: candidate for candidate in all_candidates}
    labels = ["CTRL", "g_opt", "g_opt_greyedge", "g_opt_vibrance_28"] + [str(row["candidate"]) for row in winners]
    variants = []
    for label in labels:
        if label == "CTRL":
            variants.append(Variant(label, None))
        elif label == "g_opt_greyedge":
            variants.append(Variant(label, suffix_filter(GOPT_LUT, "greyedge=difford=2:minknorm=5:sigma=1.0")))
        else:
            variants.append(Variant(label, by_label[label].vf))
    return variants


def make_access(args: argparse.Namespace, vars_: list[Variant]) -> None:
    frames_root = args.out_root / "frames" / "access"
    out_dir = args.out_root / "access_grid_winners"
    write_column_manifest(out_dir, vars_)
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
            args.access_frames,
            len(vars_),
            tile_width,
            tile_height,
            f"Expt9D built-in filter winners: {video.name}",
            out_dir / f"{video.name}.builtin_grid.png",
        )


def make_validation(args: argparse.Namespace, vars_: list[Variant]) -> None:
    frames_root = args.out_root / "frames" / "validation"
    out_dir = args.out_root / "validation_grid_winners"
    validation_variants = [Variant("Video8", None), *vars_]
    write_column_manifest(out_dir, validation_variants)
    for index, (ref_video, src_video) in enumerate(read_manifest(args.validation_pairs), start=1):
        src_meta = probe_metadata(src_video)
        ref_meta = probe_metadata(ref_video)
        duration = min(src_meta.duration_seconds, ref_meta.duration_seconds)
        tile_width, tile_height = tile_size(src_video, args.validation_tile_width)
        pair_key = f"pair_{index:03d}"
        frame_paths = []
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
            args.validation_frames,
            len(validation_variants),
            tile_width,
            tile_height,
            f"Expt9D validation {pair_key}",
            out_dir / f"{pair_key}.builtin_grid.png",
        )


def make_training(args: argparse.Namespace, vars_: list[Variant]) -> None:
    frames_root = args.out_root / "frames" / "training"
    out_dir = args.out_root / "training_grid_winners"
    training_variants = [Variant("Video8", None), *vars_]
    write_column_manifest(out_dir, training_variants)
    for index, (ref_video, src_video) in enumerate(read_manifest(args.train_pairs), start=1):
        src_meta = probe_metadata(src_video)
        ref_meta = probe_metadata(ref_video)
        duration = min(src_meta.duration_seconds, ref_meta.duration_seconds)
        tile_width, tile_height = tile_size(src_video, args.training_tile_width)
        pair_key = f"pair_{index:03d}"
        frame_paths = []
        for row_index, time_s in enumerate(calculate_sample_times(duration, args.training_frames), start=1):
            ref_frame = frames_root / pair_key / f"r{row_index:03d}_Video8.png"
            render_frame(ref_video, ref_frame, time_s, tile_width, tile_height, Variant("Video8", None))
            frame_paths.append(ref_frame)
            for variant in vars_:
                frame_path = frames_root / pair_key / f"r{row_index:03d}_{safe_name(variant.label)}.png"
                render_frame(src_video, frame_path, time_s, tile_width, tile_height, variant)
                frame_paths.append(frame_path)
        compose_grid(
            frame_paths,
            args.training_frames,
            len(training_variants),
            tile_width,
            tile_height,
            f"Expt9D training {pair_key}",
            out_dir / f"{pair_key}.builtin_grid.png",
        )


def write_column_manifest(out_dir: Path, vars_: list[Variant]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "column_manifest.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["column", "label", "vf"])
        writer.writeheader()
        for index, variant in enumerate(vars_, start=1):
            writer.writerow({"column": index, "label": variant.label, "vf": variant.vf or ""})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-root", type=Path, default=OUT_ROOT)
    parser.add_argument("--train-pairs", type=Path, default=TRAIN_PAIRS)
    parser.add_argument("--validation-pairs", type=Path, default=VALIDATION_PAIRS)
    parser.add_argument("--access-root", type=Path, default=ACCESS_ROOT)
    parser.add_argument("--fps", type=float, default=2.0)
    parser.add_argument("--sample-width", type=int)
    parser.add_argument("--sample-height", type=int)
    parser.add_argument("--access-frames", type=int, default=12)
    parser.add_argument("--validation-frames", type=int, default=3)
    parser.add_argument("--training-frames", type=int, default=3)
    parser.add_argument("--access-tile-width", type=int, default=420)
    parser.add_argument("--validation-tile-width", type=int, default=360)
    parser.add_argument("--training-tile-width", type=int, default=360)
    parser.add_argument("--skip-eval", action="store_true")
    parser.add_argument("--skip-grids", action="store_true")
    parser.add_argument("--skip-access", action="store_true")
    parser.add_argument("--skip-validation", action="store_true")
    parser.add_argument("--skip-training", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    all_candidates = candidates()
    write_candidates_manifest(all_candidates, args.out_root / "candidate_manifest.csv")
    metrics_csv = args.out_root / "evaluation" / "validation_metrics.csv"
    if not args.skip_eval:
        metrics_csv = evaluate_candidates(args, all_candidates)
    summary = summarize(metrics_csv, all_candidates, args.out_root / "experiment_summary.csv")
    winners = select_winners(summary)
    write_winners(winners, args.out_root / "winners.csv")
    if not args.skip_grids:
        vars_ = make_variants(all_candidates, winners)
        if not args.skip_access:
            make_access(args, vars_)
        if not args.skip_validation:
            make_validation(args, vars_)
        if not args.skip_training:
            make_training(args, vars_)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
