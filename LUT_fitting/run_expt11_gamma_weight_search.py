#!/usr/bin/env python3
"""Optimize eq gamma_weight plus colorcorrect with staged search."""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from evaluate_luts import collect_samples, compute_metrics, read_manifest
from generate_lut_review_sheets import calculate_sample_times, probe_metadata
from run_expt9F_yuv_only_search import PairCache, load_rgb_frames, make_cache
from run_expt9_greyworld_review import Variant, compose_grid, render_frame, safe_name, tile_size
from run_expt9_luma_only import ACCESS_ROOT, access_inputs


OUT_ROOT = Path("generated_video_pairs/evaluations/expt11_gamma_weight_search")
TRAIN_PAIRS = Path("generated_video_pairs/train_geometry_normalized_pairs.txt")
VALIDATION_PAIRS = Path("generated_video_pairs/validation_geometry_normalized_pairs.txt")
PREVIOUS_FILTERGRAPH = (
    "eq=gamma=1.43214046,"
    "colorcorrect=rl=-0.004439:bl=0.012896:rh=-0.004175:bh=0.012128:saturation=0.880000"
)

METRIC_FIELDS = [
    "sample_count",
    "rgb_mae",
    "rgb_rmse",
    "luma_mae",
    "delta_e76_mean",
    "delta_e76_p95",
    "shadow_luma_lift",
    "mid_luma_lift",
    "high_luma_lift",
    "mid_luma_bias",
    "high_luma_bias",
    "nonshadow_positive_luma_bias",
    "nonwarm_mid_high_luma_bias",
    "nonshadow_luma_over_p95",
    "clip_pct",
    "new_clip_pct",
    "warm_yellow_delta_e76_mean",
    "warm_yellow_luma_bias",
    "tone_score",
]

PARAM_FIELDS = ["gamma", "gamma_weight", "amount", "q", "k", "saturation"]


@dataclass(frozen=True)
class Candidate:
    label: str
    stage: str
    gamma: float | None
    gamma_weight: float | None
    amount: float | None
    q: float | None
    k: float | None
    saturation: float | None
    vf: str
    note: str = ""

    @property
    def params(self) -> dict[str, object]:
        return {
            "label": self.label,
            "stage": self.stage,
            "gamma": "" if self.gamma is None else self.gamma,
            "gamma_weight": "" if self.gamma_weight is None else self.gamma_weight,
            "amount": "" if self.amount is None else self.amount,
            "q": "" if self.q is None else self.q,
            "k": "" if self.k is None else self.k,
            "saturation": "" if self.saturation is None else self.saturation,
            "vf": self.vf,
            "note": self.note,
        }


def label_float(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".").replace("-", "m").replace(".", "p")


def previous_strength_filter(strength: float) -> str:
    return (
        f"split=2[orig][work];"
        f"[work]{PREVIOUS_FILTERGRAPH}[filt];"
        f"[orig][filt]blend=all_expr='{1.0 - strength:.6f}*A+{strength:.6f}*B'"
    )


def candidate_label(stage: str, gamma: float, gamma_weight: float, amount: float, q: float, k: float, saturation: float) -> str:
    return (
        f"{stage}_g{label_float(gamma)}_w{label_float(gamma_weight)}_a{label_float(amount)}_"
        f"q{label_float(q)}_k{label_float(k)}_s{label_float(saturation)}"
    )


def make_candidate(
    stage: str,
    gamma: float,
    gamma_weight: float,
    amount: float,
    q: float,
    k: float,
    saturation: float,
) -> Candidate:
    gamma = round(float(np.clip(gamma, 1.00, 1.70)), 8)
    gamma_weight = round(float(np.clip(gamma_weight, 0.05, 1.00)), 8)
    amount = round(float(np.clip(amount, 0.0, 0.026)), 8)
    q = round(float(np.clip(q, 0.75, 3.25)), 8)
    k = round(float(np.clip(k, 0.0, 1.0)), 8)
    saturation = round(float(np.clip(saturation, 0.84, 1.04)), 8)
    rl = -amount
    bl = q * amount
    rh = -k * amount
    bh = k * q * amount
    vf = (
        f"eq=gamma={gamma:.8f}:gamma_weight={gamma_weight:.8f},"
        f"colorcorrect=rl={rl:.6f}:bl={bl:.6f}:rh={rh:.6f}:bh={bh:.6f}:saturation={saturation:.6f}"
    )
    return Candidate(
        candidate_label(stage, gamma, gamma_weight, amount, q, k, saturation),
        stage,
        gamma,
        gamma_weight,
        amount,
        q,
        k,
        saturation,
        vf,
    )


def dedupe_candidates(candidates: list[Candidate]) -> list[Candidate]:
    seen = set()
    out = []
    for candidate in candidates:
        key = (candidate.gamma, candidate.gamma_weight, candidate.amount, candidate.q, candidate.k, candidate.saturation)
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def stage1_candidates() -> list[Candidate]:
    gammas = [1.10, 1.25, 1.40, 1.55]
    weights = [0.20, 0.45, 0.70, 1.00]
    amounts = [0.000, 0.010, 0.020]
    saturations = [0.88, 0.94, 1.00]
    return [
        make_candidate("stage1", gamma, weight, amount, 2.0, 0.5, saturation)
        for gamma in gammas
        for weight in weights
        for amount in amounts
        for saturation in saturations
    ]


def stage2_candidates(top_stage1: list[Candidate]) -> list[Candidate]:
    qs = [1.0, 1.5, 2.0, 2.5, 3.0]
    ks = [0.0, 0.25, 0.5, 0.75, 1.0]
    return dedupe_candidates(
        [
            make_candidate("stage2", base.gamma or 1.0, base.gamma_weight or 1.0, base.amount or 0.0, q, k, base.saturation or 1.0)
            for base in top_stage1
            for q in qs
            for k in ks
        ]
    )


def stage3_candidates(top_stage2: list[Candidate], count: int = 120, seed: int = 11011) -> list[Candidate]:
    rng = np.random.default_rng(seed)
    candidates = []
    seen = set()
    attempts = 0
    while len(candidates) < count and attempts < count * 50:
        attempts += 1
        base = top_stage2[int(rng.integers(0, len(top_stage2)))]
        candidate = make_candidate(
            "stage3",
            (base.gamma or 1.0) + rng.uniform(-0.06, 0.06),
            (base.gamma_weight or 1.0) + rng.uniform(-0.12, 0.12),
            (base.amount or 0.0) + rng.uniform(-0.004, 0.004),
            (base.q or 2.0) + rng.uniform(-0.30, 0.30),
            (base.k or 0.5) + rng.uniform(-0.15, 0.15),
            (base.saturation or 1.0) + rng.uniform(-0.03, 0.03),
        )
        key = (candidate.gamma, candidate.gamma_weight, candidate.amount, candidate.q, candidate.k, candidate.saturation)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
    return candidates


def baseline_candidates() -> list[Candidate]:
    return [
        Candidate("previous_full_expt9f", "baseline", None, None, None, None, None, None, PREVIOUS_FILTERGRAPH, "expt9F train-best filtergraph"),
        Candidate("previous_50pct_expt9f", "baseline", None, None, None, None, None, None, previous_strength_filter(0.5), "50% output blend of expt9F"),
    ]


def score_value(metrics: dict[str, float]) -> float:
    d_e = float(metrics["delta_e76_mean"])
    nonshadow_pos = float(metrics["nonshadow_positive_luma_bias"])
    high_lift = max(float(metrics["high_luma_lift"]), 0.0)
    mid_over = max(float(metrics["mid_luma_lift"]) - 6.0, 0.0)
    new_clip = float(metrics["new_clip_pct"])
    return d_e + 0.08 * nonshadow_pos + 0.08 * high_lift + 0.04 * mid_over + 0.30 * new_clip


def aggregate_pair_rows(rows: list[dict[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {"candidate": rows[0]["candidate"], "stage": rows[0]["stage"], "vf": rows[0]["vf"], "note": rows[0].get("note", "")}
    for key in PARAM_FIELDS:
        out[key] = rows[0].get(key, "")
    for field in METRIC_FIELDS:
        if field == "sample_count":
            out[field] = int(sum(float(row[field]) for row in rows))
            continue
        values = [float(row[field]) for row in rows if np.isfinite(float(row[field]))]
        out[field] = float(np.mean(values)) if values else float("nan")
    return out


def evaluate_candidates(
    pairs: list[PairCache],
    candidates: list[Candidate],
    out_csv: Path,
    include_raw: bool = True,
    jobs: int = 1,
) -> list[dict[str, object]]:
    per_pair_rows = []
    loaded_pairs = []
    for pair in pairs:
        print(f"Loading {pair.split} {pair.pair}", flush=True)
        ref_frames = load_rgb_frames(pair.ref_video, pair.width, pair.height)
        raw_frames = load_rgb_frames(pair.raw_video, pair.width, pair.height)
        ref_samples, raw_samples, raw_test_samples = collect_samples(ref_frames, raw_frames, raw_frames, pair.masks)
        loaded_pairs.append((pair, ref_frames, raw_frames, ref_samples, raw_samples, raw_test_samples))
        if include_raw:
            metrics = compute_metrics(ref_samples, raw_test_samples, raw_samples)
            metrics["tone_score"] = score_value(metrics)
            per_pair_rows.append(
                {
                    "pair": pair.pair,
                    "candidate": "raw",
                    "stage": "baseline",
                    "gamma": "",
                    "gamma_weight": "",
                    "amount": "",
                    "q": "",
                    "k": "",
                    "saturation": "",
                    "vf": "",
                    "note": "",
                    **metrics,
                }
            )

    def evaluate_one_candidate(candidate: Candidate) -> list[dict[str, object]]:
        candidate_rows = []
        for pair, ref_frames, raw_frames, _ref_samples_raw, _raw_samples_raw, _raw_test_samples in loaded_pairs:
            cand_frames = load_rgb_frames(pair.raw_video, pair.width, pair.height, candidate.vf)
            ref_samples, raw_samples, cand_samples = collect_samples(ref_frames, raw_frames, cand_frames, pair.masks)
            metrics = compute_metrics(ref_samples, cand_samples, raw_samples)
            metrics["tone_score"] = score_value(metrics)
            params = candidate.params
            candidate_rows.append(
                {
                    "pair": pair.pair,
                    "candidate": candidate.label,
                    "stage": candidate.stage,
                    "gamma": params["gamma"],
                    "gamma_weight": params["gamma_weight"],
                    "amount": params["amount"],
                    "q": params["q"],
                    "k": params["k"],
                    "saturation": params["saturation"],
                    "vf": candidate.vf,
                    "note": candidate.note,
                    **metrics,
                }
            )
        return candidate_rows

    if jobs <= 1 or len(candidates) <= 1:
        for candidate in candidates:
            print(f"Evaluating {candidate.label}", flush=True)
            per_pair_rows.extend(evaluate_one_candidate(candidate))
    else:
        with ThreadPoolExecutor(max_workers=jobs) as executor:
            futures = {executor.submit(evaluate_one_candidate, candidate): candidate for candidate in candidates}
            for future in as_completed(futures):
                candidate = futures[future]
                print(f"Finished {candidate.label}", flush=True)
                per_pair_rows.extend(future.result())

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["pair", "candidate", "stage", *PARAM_FIELDS, "vf", "note", *METRIC_FIELDS]
    with out_csv.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(per_pair_rows)

    grouped: dict[str, list[dict[str, object]]] = {}
    for row in per_pair_rows:
        grouped.setdefault(str(row["candidate"]), []).append(row)
    return [aggregate_pair_rows(rows) for rows in grouped.values()]


def read_summary(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def write_summary(rows: list[dict[str, object]], out_csv: Path) -> list[dict[str, object]]:
    rows = sorted(rows, key=lambda row: (float(row["tone_score"]), float(row["delta_e76_mean"])))
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["candidate", "stage", *PARAM_FIELDS, "vf", "note", *METRIC_FIELDS]
    with out_csv.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def write_candidate_manifest(candidates: list[Candidate], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["label", "stage", *PARAM_FIELDS, "vf", "note"])
        writer.writeheader()
        writer.writerows([candidate.params for candidate in candidates])


def select_rows(rows: list[dict[str, object]], count: int, candidate_map: dict[str, Candidate]) -> list[Candidate]:
    selected = []
    for row in rows:
        candidate = candidate_map.get(str(row["candidate"]))
        if candidate is None:
            continue
        selected.append(candidate)
        if len(selected) >= count:
            break
    return selected


def write_best(best: Candidate, out_root: Path) -> None:
    (out_root / "best_filtergraph.txt").write_text(best.vf + "\n")
    (out_root / "best_params.json").write_text(json.dumps(best.params, indent=2) + "\n")


def write_column_manifest(out_dir: Path, variants: list[Variant]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "column_manifest.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["column", "label", "vf"])
        writer.writeheader()
        for index, variant in enumerate(variants, start=1):
            writer.writerow({"column": index, "label": variant.label, "vf": variant.vf or ""})


def make_review_variants(candidates: list[Candidate]) -> list[Variant]:
    variants = [Variant("previous_full", PREVIOUS_FILTERGRAPH), Variant("previous_50pct", previous_strength_filter(0.5))]
    variants.extend(Variant(f"top{index}_{candidate.label}", candidate.vf) for index, candidate in enumerate(candidates, start=1))
    return variants


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
            f"Expt11 {split} {pair_key}: {', '.join(variant.label for variant in columns)}",
            out_dir / f"{pair_key}.expt11_grid.png",
        )


def render_access_grid(access_root: Path, out_root: Path, variants: list[Variant], frames: int, tile_width_arg: int) -> None:
    out_dir = out_root / "access_grid"
    frames_root = out_root / "frames" / "access"
    columns = [Variant("CTRL", None), *variants]
    write_column_manifest(out_dir, columns)
    for video in access_inputs(access_root):
        metadata = probe_metadata(video)
        tile_width, tile_height = tile_size(video, tile_width_arg)
        frame_paths = []
        video_key = safe_name(video.stem)
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
            f"Expt11 Access {video.name}: {', '.join(variant.label for variant in columns)}",
            out_dir / f"{video.name}.expt11_grid.png",
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-root", type=Path, default=OUT_ROOT)
    parser.add_argument("--train-pairs", type=Path, default=TRAIN_PAIRS)
    parser.add_argument("--validation-pairs", type=Path, default=VALIDATION_PAIRS)
    parser.add_argument("--access-root", type=Path, default=ACCESS_ROOT)
    parser.add_argument("--coarse-fps", type=float, default=0.5)
    parser.add_argument("--coarse-width", type=int, default=320)
    parser.add_argument("--coarse-height", type=int, default=240)
    parser.add_argument("--full-fps", type=float, default=2.0)
    parser.add_argument("--stage1-top", type=int, default=8)
    parser.add_argument("--stage2-top", type=int, default=8)
    parser.add_argument("--stage3-count", type=int, default=120)
    parser.add_argument("--full-top", type=int, default=25)
    parser.add_argument("--review-top", type=int, default=5)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--skip-eval", action="store_true")
    parser.add_argument("--skip-grids", action="store_true")
    parser.add_argument("--skip-train-grid", action="store_true")
    parser.add_argument("--skip-validation-grid", action="store_true")
    parser.add_argument("--skip-access-grid", action="store_true")
    parser.add_argument("--train-frames", type=int, default=3)
    parser.add_argument("--validation-frames", type=int, default=3)
    parser.add_argument("--access-frames", type=int, default=12)
    parser.add_argument("--pair-tile-width", type=int, default=300)
    parser.add_argument("--access-tile-width", type=int, default=300)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.out_root.mkdir(parents=True, exist_ok=True)
    cache_root = args.out_root / "frame_cache"
    limit_pairs = 1 if args.smoke else None

    if not args.skip_eval:
        coarse_train = make_cache(
            "train_coarse",
            args.train_pairs,
            cache_root,
            args.coarse_fps,
            args.coarse_width,
            args.coarse_height,
            limit_pairs=limit_pairs,
        )
        if args.smoke:
            candidates = [make_candidate("smoke", 1.25, 0.45, 0.010, 2.0, 0.5, 0.94), *baseline_candidates()]
            write_candidate_manifest(candidates, args.out_root / "candidate_manifest.csv")
            rows = evaluate_candidates(coarse_train, candidates, args.out_root / "smoke_pair_metrics.csv", jobs=args.jobs)
            write_summary(rows, args.out_root / "smoke_summary.csv")
            return 0

        stage1 = stage1_candidates()
        write_candidate_manifest(stage1, args.out_root / "candidate_manifest.csv")
        rows1 = write_summary(
            evaluate_candidates(coarse_train, stage1, args.out_root / "stage1_coarse_train_pair_metrics.csv", jobs=args.jobs),
            args.out_root / "stage1_coarse_train.csv",
        )
        map1 = {candidate.label: candidate for candidate in stage1}
        top1 = select_rows(rows1, args.stage1_top, map1)

        stage2 = stage2_candidates(top1)
        rows2 = write_summary(
            evaluate_candidates(coarse_train, stage2, args.out_root / "stage2_shape_train_pair_metrics.csv", include_raw=False, jobs=args.jobs),
            args.out_root / "stage2_shape_train.csv",
        )
        map2 = {candidate.label: candidate for candidate in stage2}
        top2 = select_rows(rows2, args.stage2_top, map2)

        stage3 = stage3_candidates(top2, count=args.stage3_count)
        rows3 = write_summary(
            evaluate_candidates(coarse_train, stage3, args.out_root / "stage3_refine_train_pair_metrics.csv", include_raw=False, jobs=args.jobs),
            args.out_root / "stage3_refine_train.csv",
        )

        all_search = dedupe_candidates([*stage1, *stage2, *stage3])
        write_candidate_manifest(all_search, args.out_root / "candidate_manifest.csv")
        coarse_union = write_summary([*rows1, *rows2, *rows3], args.out_root / "coarse_union_train.csv")
        all_map = {candidate.label: candidate for candidate in all_search}
        top_full = select_rows(coarse_union, args.full_top, all_map)

        full_candidates = [*baseline_candidates(), *top_full]
        write_candidate_manifest(full_candidates, args.out_root / "full_candidate_manifest.csv")

        full_train = make_cache("train_full", args.train_pairs, cache_root, args.full_fps, None, None)
        full_validation = make_cache("validation_full", args.validation_pairs, cache_root, args.full_fps, None, None)
        full_train_rows = write_summary(
            evaluate_candidates(full_train, full_candidates, args.out_root / "full_train_pair_metrics.csv", jobs=args.jobs),
            args.out_root / "full_train_metrics.csv",
        )
        write_summary(
            evaluate_candidates(full_validation, full_candidates, args.out_root / "full_validation_pair_metrics.csv", jobs=args.jobs),
            args.out_root / "full_validation_metrics.csv",
        )

        best = select_rows(full_train_rows, 1, all_map)[0]
        write_best(best, args.out_root)
        top_review = select_rows(full_train_rows, args.review_top, all_map)
        write_candidate_manifest(top_review, args.out_root / "review_candidate_manifest.csv")
    else:
        top_review = []
        with (args.out_root / "review_candidate_manifest.csv").open(newline="") as f:
            for row in csv.DictReader(f):
                top_review.append(
                    Candidate(
                        str(row["label"]),
                        str(row["stage"]),
                        float(row["gamma"]),
                        float(row["gamma_weight"]),
                        float(row["amount"]),
                        float(row["q"]),
                        float(row["k"]),
                        float(row["saturation"]),
                        str(row["vf"]),
                        str(row.get("note", "")),
                    )
                )

    if not args.skip_grids:
        variants = make_review_variants(top_review)
        if not args.skip_train_grid:
            render_pair_grid("train", args.train_pairs, args.out_root, variants, args.train_frames, args.pair_tile_width)
        if not args.skip_validation_grid:
            render_pair_grid("validation", args.validation_pairs, args.out_root, variants, args.validation_frames, args.pair_tile_width)
        if not args.skip_access_grid:
            render_access_grid(args.access_root, args.out_root, variants, args.access_frames, args.access_tile_width)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
