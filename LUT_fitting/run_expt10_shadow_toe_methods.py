#!/usr/bin/env python3
"""Search shadow-toe correction methods and generate frame-only review grids."""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from evaluate_luts import read_manifest
from generate_lut_review_sheets import calculate_sample_times, probe_metadata
from run_expt9F_yuv_only_search import (
    FilterCandidate,
    evaluate_candidates,
    make_cache,
    read_summary,
    write_summary,
)
from run_expt9_greyworld_review import Variant, compose_grid, render_frame, safe_name, tile_size
from run_expt9_luma_only import ACCESS_ROOT, access_inputs


OUT_ROOT = Path("generated_video_pairs/evaluations/expt10_shadow_toe_methods")
TRAIN_PAIRS = Path("generated_video_pairs/train_geometry_normalized_pairs.txt")
VALIDATION_PAIRS = Path("generated_video_pairs/validation_geometry_normalized_pairs.txt")
PREVIOUS_FILTERGRAPH = (
    "eq=gamma=1.43214046,"
    "colorcorrect=rl=-0.004439:bl=0.012896:rh=-0.004175:bh=0.012128:saturation=0.880000"
)


@dataclass(frozen=True)
class ToeCandidate:
    label: str
    method: str
    lift: float | None
    vf: str

    @property
    def as_filter_candidate(self) -> FilterCandidate:
        return FilterCandidate(self.label, self.vf, self.method)


def run(cmd: list[str | Path]) -> None:
    subprocess.run([str(part) for part in cmd], check=True)


def label_lift(lift: float) -> str:
    return f"l{int(round(lift * 1000)):03d}"


def toe_points(lift: float) -> tuple[tuple[float, float], ...]:
    return (
        (0.0, 0.0),
        (0.10, 0.10 + lift),
        (0.20, 0.20 + 0.45 * lift),
        (0.50, 0.50),
        (1.0, 1.0),
    )


def curves_filter(lift: float) -> str:
    points = " ".join(f"{x:.2f}/{y:.6f}" for x, y in toe_points(lift))
    return f"curves=interp=pchip:master='{points}'"


def lutyuv_filter(lift: float) -> str:
    y1 = 0.10 + lift
    y2 = 0.20 + 0.45 * lift
    slope01 = y1 / 0.10
    slope12 = (y2 - y1) / 0.10
    slope23 = (0.50 - y2) / 0.30
    expr = (
        f"if(lt(val,0.10*maxval),val*{slope01:.10f},"
        f"if(lt(val,0.20*maxval),{y1:.10f}*maxval+(val-0.10*maxval)*{slope12:.10f},"
        f"if(lt(val,0.50*maxval),{y2:.10f}*maxval+(val-0.20*maxval)*{slope23:.10f},val)))"
    )
    return f"lutyuv=y='{expr}'"


def previous_strength_filter(strength: float) -> str:
    strength = float(strength)
    original_weight = 1.0 - strength
    return (
        f"split=2[orig][work];"
        f"[work]{PREVIOUS_FILTERGRAPH}[filt];"
        f"[orig][filt]blend=all_expr='{original_weight:.6f}*A+{strength:.6f}*B'"
    )


def lift_grid(start: float, stop: float, step: float) -> list[float]:
    values = []
    current = start
    while current <= stop + (step / 2):
        values.append(round(current, 6))
        current += step
    return values


def search_candidates(lifts: list[float]) -> list[ToeCandidate]:
    candidates: list[ToeCandidate] = []
    for lift in lifts:
        suffix = label_lift(lift)
        candidates.append(ToeCandidate(f"m1_curves_{suffix}", "method1_curves_pchip", lift, curves_filter(lift)))
        candidates.append(ToeCandidate(f"m2_y_lutyuv_{suffix}", "method2_lutyuv_linear", lift, lutyuv_filter(lift)))
    return candidates


def fixed_candidates(strength: float) -> list[ToeCandidate]:
    return [ToeCandidate(f"m3_previous_strength_{int(round(strength * 100)):03d}", "method3_previous_blend", strength, previous_strength_filter(strength))]


def write_candidate_manifest(candidates: list[ToeCandidate], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["label", "method", "lift_or_strength", "vf"])
        writer.writeheader()
        for candidate in candidates:
            writer.writerow(
                {
                    "label": candidate.label,
                    "method": candidate.method,
                    "lift_or_strength": "" if candidate.lift is None else f"{candidate.lift:.6f}",
                    "vf": candidate.vf,
                }
            )


def method_best(summary_rows: list[dict[str, object]], candidates: list[ToeCandidate], method: str) -> ToeCandidate:
    labels = {candidate.label for candidate in candidates if candidate.method == method}
    for row in summary_rows:
        if row["candidate"] in labels:
            return next(candidate for candidate in candidates if candidate.label == row["candidate"])
    raise RuntimeError(f"No best candidate found for {method}")


def write_selected(selected: list[ToeCandidate], out_root: Path) -> None:
    data = [
        {
            "label": candidate.label,
            "method": candidate.method,
            "lift_or_strength": candidate.lift,
            "vf": candidate.vf,
        }
        for candidate in selected
    ]
    (out_root / "selected_methods.json").write_text(json.dumps(data, indent=2) + "\n")
    with (out_root / "selected_filtergraphs.txt").open("w") as f:
        for candidate in selected:
            f.write(f"[{candidate.label}]\n{candidate.vf}\n\n")


def write_column_manifest(out_dir: Path, variants: list[Variant]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "column_manifest.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["column", "label", "vf"])
        writer.writeheader()
        for index, variant in enumerate(variants, start=1):
            writer.writerow({"column": index, "label": variant.label, "vf": variant.vf or ""})


def render_pair_grid(
    split: str,
    manifest: Path,
    out_root: Path,
    variants: list[Variant],
    frames: int,
    tile_width_arg: int,
) -> None:
    out_dir = out_root / f"{split}_grid"
    frames_root = out_root / "frames" / split
    columns = [Variant("Video8", None), Variant("CTRL", None), *variants]
    write_column_manifest(out_dir, columns)
    for index, (ref_video, src_video) in enumerate(read_manifest(manifest), start=1):
        ref_meta = probe_metadata(ref_video)
        src_meta = probe_metadata(src_video)
        duration = min(ref_meta.duration_seconds, src_meta.duration_seconds)
        tile_width, tile_height = tile_size(src_video, tile_width_arg)
        pair_key = f"pair_{index:03d}"
        frame_paths = []
        for row_index, time_s in enumerate(calculate_sample_times(duration, frames), start=1):
            for variant in columns:
                source = ref_video if variant.label == "Video8" else src_video
                frame_path = frames_root / pair_key / f"r{row_index:03d}_{safe_name(variant.label)}.png"
                render_frame(source, frame_path, time_s, tile_width, tile_height, variant)
                frame_paths.append(frame_path)
        compose_grid(
            frame_paths,
            frames,
            len(columns),
            tile_width,
            tile_height,
            f"Expt10 {split} {pair_key}: {', '.join(variant.label for variant in columns)}",
            out_dir / f"{pair_key}.expt10_grid.png",
        )


def render_access_grid(
    access_root: Path,
    out_root: Path,
    variants: list[Variant],
    frames: int,
    tile_width_arg: int,
) -> None:
    out_dir = out_root / "access_grid"
    frames_root = out_root / "frames" / "access"
    columns = [Variant("CTRL", None), *variants]
    write_column_manifest(out_dir, columns)
    for video in access_inputs(access_root):
        metadata = probe_metadata(video)
        tile_width, tile_height = tile_size(video, tile_width_arg)
        video_key = safe_name(video.stem)
        frame_paths = []
        for row_index, time_s in enumerate(calculate_sample_times(metadata.duration_seconds, frames), start=1):
            for variant in columns:
                frame_path = frames_root / video_key / f"r{row_index:03d}_{safe_name(variant.label)}.png"
                render_frame(video, frame_path, time_s, tile_width, tile_height, variant)
                frame_paths.append(frame_path)
        compose_grid(
            frame_paths,
            frames,
            len(columns),
            tile_width,
            tile_height,
            f"Expt10 Access {video.name}: {', '.join(variant.label for variant in columns)}",
            out_dir / f"{video.name}.expt10_grid.png",
        )


def read_selected(path: Path) -> list[ToeCandidate]:
    data = json.loads(path.read_text())
    return [
        ToeCandidate(
            label=str(item["label"]),
            method=str(item["method"]),
            lift=None if item["lift_or_strength"] is None else float(item["lift_or_strength"]),
            vf=str(item["vf"]),
        )
        for item in data
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-root", type=Path, default=OUT_ROOT)
    parser.add_argument("--train-pairs", type=Path, default=TRAIN_PAIRS)
    parser.add_argument("--validation-pairs", type=Path, default=VALIDATION_PAIRS)
    parser.add_argument("--access-root", type=Path, default=ACCESS_ROOT)
    parser.add_argument("--fps", type=float, default=2.0)
    parser.add_argument("--lift-start", type=float, default=0.0)
    parser.add_argument("--lift-stop", type=float, default=0.18)
    parser.add_argument("--lift-step", type=float, default=0.01)
    parser.add_argument("--previous-strength", type=float, default=0.5)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--skip-eval", action="store_true")
    parser.add_argument("--skip-grids", action="store_true")
    parser.add_argument("--skip-train-grid", action="store_true")
    parser.add_argument("--skip-validation-grid", action="store_true")
    parser.add_argument("--skip-access-grid", action="store_true")
    parser.add_argument("--train-frames", type=int, default=3)
    parser.add_argument("--validation-frames", type=int, default=3)
    parser.add_argument("--access-frames", type=int, default=12)
    parser.add_argument("--pair-tile-width", type=int, default=360)
    parser.add_argument("--access-tile-width", type=int, default=420)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.out_root.mkdir(parents=True, exist_ok=True)
    cache_root = args.out_root / "frame_cache"

    search = search_candidates(lift_grid(args.lift_start, args.lift_stop, args.lift_step))
    fixed = fixed_candidates(args.previous_strength)
    all_candidates = [*search, *fixed]
    write_candidate_manifest(all_candidates, args.out_root / "candidate_manifest.csv")

    selected_path = args.out_root / "selected_methods.json"
    if args.skip_eval:
        selected = read_selected(selected_path)
    else:
        train_pairs = make_cache("train_full", args.train_pairs, cache_root, args.fps, None, None)
        train_rows_path = args.out_root / "train_search_metrics.csv"
        train_rows = read_summary(train_rows_path)
        expected_labels = {"raw", *(candidate.label for candidate in all_candidates)}
        existing_labels = {str(row["candidate"]) for row in train_rows}
        if train_rows and expected_labels.issubset(existing_labels):
            print(f"Reusing {train_rows_path}", flush=True)
        else:
            train_rows = write_summary(
                evaluate_candidates(
                    train_pairs,
                    [candidate.as_filter_candidate for candidate in all_candidates],
                    args.out_root / "train_search_pair_metrics.csv",
                    jobs=args.jobs,
                ),
                train_rows_path,
            )

        best_m1 = method_best(train_rows, search, "method1_curves_pchip")
        best_m2 = method_best(train_rows, search, "method2_lutyuv_linear")
        selected = [best_m1, best_m2, *fixed]
        write_selected(selected, args.out_root)
        write_candidate_manifest(selected, args.out_root / "selected_manifest.csv")

        validation_pairs = make_cache("validation_full", args.validation_pairs, cache_root, args.fps, None, None)
        write_summary(
            evaluate_candidates(
                validation_pairs,
                [candidate.as_filter_candidate for candidate in selected],
                args.out_root / "validation_selected_pair_metrics.csv",
                jobs=args.jobs,
            ),
            args.out_root / "validation_selected_metrics.csv",
        )

    if not args.skip_grids:
        variants = [Variant(candidate.label, candidate.vf) for candidate in selected]
        if not args.skip_train_grid:
            render_pair_grid("train", args.train_pairs, args.out_root, variants, args.train_frames, args.pair_tile_width)
        if not args.skip_validation_grid:
            render_pair_grid(
                "validation",
                args.validation_pairs,
                args.out_root,
                variants,
                args.validation_frames,
                args.pair_tile_width,
            )
        if not args.skip_access_grid:
            render_access_grid(args.access_root, args.out_root, variants, args.access_frames, args.access_tile_width)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
