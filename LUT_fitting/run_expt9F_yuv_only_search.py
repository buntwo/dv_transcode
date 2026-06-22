#!/usr/bin/env python3
"""Optimize native-YUV eq+colorcorrect parameters without conversion filters."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import subprocess
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

from evaluate_luts import collect_samples, compute_metrics, read_manifest
from generate_lut_review_sheets import calculate_sample_times, probe_metadata
from run_expt9_greyworld_review import Variant, compose_grid, render_frame, safe_name, tile_size


OUT_ROOT = Path("generated_video_pairs/evaluations/expt9F_yuv_only_search")
TRAIN_PAIRS = Path("generated_video_pairs/train_geometry_normalized_pairs.txt")
VALIDATION_PAIRS = Path("generated_video_pairs/validation_geometry_normalized_pairs.txt")
BASE_WIDTH = 640
BASE_HEIGHT = 480
BASE_MASKS = {"mask_left": 6, "mask_right": 6, "mask_top": 6, "mask_bottom": 20}
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


@dataclass(frozen=True)
class Candidate:
    label: str
    gamma: float
    amount: float
    q: float
    k: float
    saturation: float
    stage: str

    @property
    def vf(self) -> str:
        rl = -self.amount
        bl = self.q * self.amount
        rh = -self.k * self.amount
        bh = self.k * self.q * self.amount
        return (
            f"eq=gamma={self.gamma:.8f},"
            f"colorcorrect=rl={rl:.6f}:bl={bl:.6f}:rh={rh:.6f}:bh={bh:.6f}:saturation={self.saturation:.6f}"
        )

    @property
    def params(self) -> dict[str, float | str]:
        return {
            "label": self.label,
            "stage": self.stage,
            "gamma": self.gamma,
            "amount": self.amount,
            "q": self.q,
            "k": self.k,
            "saturation": self.saturation,
            "vf": self.vf,
        }


@dataclass(frozen=True)
class FilterCandidate:
    label: str
    vf: str
    stage: str

    @property
    def params(self) -> dict[str, float | str]:
        return {
            "label": self.label,
            "stage": self.stage,
            "gamma": "",
            "amount": "",
            "q": "",
            "k": "",
            "saturation": "",
            "vf": self.vf,
        }


@dataclass(frozen=True)
class PairCache:
    split: str
    pair: str
    ref_video: Path
    raw_video: Path
    width: int
    height: int
    masks: dict[str, int]


def run(cmd: list[str | Path]) -> None:
    subprocess.run([str(part) for part in cmd], check=True)


def run_capture(cmd: list[str | Path]) -> bytes:
    return subprocess.check_output([str(part) for part in cmd])


def label_float(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".").replace("-", "m").replace(".", "p")


def candidate_label(stage: str, gamma: float, amount: float, q: float, k: float, saturation: float) -> str:
    return (
        f"{stage}_g{label_float(gamma)}_a{label_float(amount)}_"
        f"q{label_float(q)}_k{label_float(k)}_s{label_float(saturation)}"
    )


def make_candidate(stage: str, gamma: float, amount: float, q: float, k: float, saturation: float) -> Candidate:
    gamma = round(float(gamma), 8)
    amount = round(float(np.clip(amount, 0.0, 0.03)), 8)
    q = round(float(np.clip(q, 0.75, 3.25)), 8)
    k = round(float(np.clip(k, 0.0, 1.0)), 8)
    saturation = round(float(np.clip(saturation, 0.88, 1.02)), 8)
    return Candidate(candidate_label(stage, gamma, amount, q, k, saturation), gamma, amount, q, k, saturation, stage)


def dedupe_candidates(candidates: list[Candidate]) -> list[Candidate]:
    seen = set()
    out = []
    for candidate in candidates:
        key = (candidate.gamma, candidate.amount, candidate.q, candidate.k, candidate.saturation)
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def stage1_candidates() -> list[Candidate]:
    gammas = [1.15, 1.20, 1.25, 1.30, 1.35, 1.40, 1.45, 1.50]
    amounts = [0.000, 0.004, 0.008, 0.012, 0.016, 0.020, 0.026]
    saturations = [0.90, 0.95, 1.00]
    return [
        make_candidate("stage1", gamma, amount, 2.0, 0.5, saturation)
        for gamma in gammas
        for amount in amounts
        for saturation in saturations
    ]


def stage2_candidates(top_stage1: list[Candidate]) -> list[Candidate]:
    qs = [1.0, 1.5, 2.0, 2.5, 3.0]
    ks = [0.0, 0.25, 0.5, 0.75, 1.0]
    return dedupe_candidates(
        [
            make_candidate("stage2", base.gamma, base.amount, q, k, base.saturation)
            for base in top_stage1
            for q in qs
            for k in ks
        ]
    )


def stage3_candidates(top_stage2: list[Candidate], count: int = 120, seed: int = 9009) -> list[Candidate]:
    rng = np.random.default_rng(seed)
    candidates = []
    seen = set()
    attempts = 0
    while len(candidates) < count and attempts < count * 30:
        attempts += 1
        base = top_stage2[int(rng.integers(0, len(top_stage2)))]
        candidate = make_candidate(
            "stage3",
            base.gamma + rng.uniform(-0.025, 0.025),
            base.amount + rng.uniform(-0.003, 0.003),
            base.q + rng.uniform(-0.25, 0.25),
            base.k + rng.uniform(-0.125, 0.125),
            base.saturation + rng.uniform(-0.025, 0.025),
        )
        key = (candidate.gamma, candidate.amount, candidate.q, candidate.k, candidate.saturation)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(candidate)
    return candidates


def smoke_candidates() -> list[Candidate]:
    return [
        make_candidate("smoke", 1.30, 0.000, 2.0, 0.5, 1.0),
        make_candidate("smoke", 1.40, 0.008, 2.0, 0.5, 0.95),
        make_candidate("smoke", 1.46, 0.010, 2.0, 0.5, 0.90),
    ]


def cache_video(src: Path, out_path: Path, fps: float, width: int | None, height: int | None) -> None:
    if out_path.exists():
        return
    out_path.parent.mkdir(parents=True, exist_ok=True)
    filters = [f"fps={fps}"]
    if width is not None or height is not None:
        if width is None or height is None:
            raise ValueError("width and height must be provided together")
        filters.append(f"scale={width}:{height}:flags=bicubic")
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            src,
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            ",".join(filters),
            "-c:v",
            "ffv1",
            "-level",
            "3",
            "-g",
            "1",
            "-slicecrc",
            "1",
            out_path,
        ]
    )


def probe_size(video: Path) -> tuple[int, int]:
    raw = run_capture(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
            video,
        ]
    )
    width, height = raw.decode().strip().split("x")
    return int(width), int(height)


def scaled_masks(width: int, height: int) -> dict[str, int]:
    x_scale = width / BASE_WIDTH
    y_scale = height / BASE_HEIGHT
    return {
        "mask_left": int(round(BASE_MASKS["mask_left"] * x_scale)),
        "mask_right": int(round(BASE_MASKS["mask_right"] * x_scale)),
        "mask_top": int(round(BASE_MASKS["mask_top"] * y_scale)),
        "mask_bottom": int(round(BASE_MASKS["mask_bottom"] * y_scale)),
    }


def make_cache(
    split: str,
    manifest: Path,
    cache_root: Path,
    fps: float,
    width: int | None,
    height: int | None,
    limit_pairs: int | None = None,
) -> list[PairCache]:
    pairs = read_manifest(manifest)
    if limit_pairs is not None:
        pairs = pairs[:limit_pairs]
    out = []
    for index, (ref_video, raw_video) in enumerate(pairs, start=1):
        pair = f"pair_{index:03d}"
        pair_root = cache_root / split / pair
        ref_cache = pair_root / "A.mkv"
        raw_cache = pair_root / "B.mkv"
        cache_video(ref_video, ref_cache, fps, width, height)
        cache_video(raw_video, raw_cache, fps, width, height)
        cache_width, cache_height = probe_size(raw_cache)
        out.append(PairCache(split, pair, ref_cache, raw_cache, cache_width, cache_height, scaled_masks(cache_width, cache_height)))
    return out


def load_rgb_frames(video: Path, width: int, height: int, vf: str | None = None) -> list[np.ndarray]:
    filters = []
    if vf:
        filters.append(vf)
    filters.append("format=rgb24")
    raw = run_capture(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            video,
            "-vf",
            ",".join(filters),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ]
    )
    frame_bytes = width * height * 3
    if len(raw) % frame_bytes:
        raise RuntimeError(f"Unexpected rawvideo byte count for {video}: {len(raw)} is not divisible by {frame_bytes}")
    frame_count = len(raw) // frame_bytes
    arr = np.frombuffer(raw, dtype=np.uint8).reshape((frame_count, height, width, 3)).astype(np.float32) / 255.0
    return [arr[index] for index in range(frame_count)]


def tone_score(metrics: dict[str, float]) -> float:
    return (
        float(metrics["delta_e76_mean"])
        + 0.05 * float(metrics["nonshadow_positive_luma_bias"])
        + 0.25 * float(metrics["new_clip_pct"])
    )


def aggregate_pair_rows(rows: list[dict[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {"candidate": rows[0]["candidate"], "stage": rows[0]["stage"], "vf": rows[0]["vf"]}
    for key in ("gamma", "amount", "q", "k", "saturation"):
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
    candidates: list[Candidate | FilterCandidate],
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
            metrics["tone_score"] = tone_score(metrics)
            per_pair_rows.append(
                {
                    "pair": pair.pair,
                    "candidate": "raw",
                    "stage": "baseline",
                    "gamma": "",
                    "amount": "",
                    "q": "",
                    "k": "",
                    "saturation": "",
                    "vf": "",
                    **metrics,
                }
            )

    def evaluate_one_candidate(candidate: Candidate | FilterCandidate) -> list[dict[str, object]]:
        candidate_rows = []
        for pair, ref_frames, raw_frames, _ref_samples_raw, _raw_samples_raw, _raw_test_samples in loaded_pairs:
            cand_frames = load_rgb_frames(pair.raw_video, pair.width, pair.height, candidate.vf)
            ref_samples, raw_samples, cand_samples = collect_samples(ref_frames, raw_frames, cand_frames, pair.masks)
            metrics = compute_metrics(ref_samples, cand_samples, raw_samples)
            metrics["tone_score"] = tone_score(metrics)
            params = candidate.params
            candidate_rows.append(
                {
                    "pair": pair.pair,
                    "candidate": candidate.label,
                    "stage": candidate.stage,
                    "gamma": params["gamma"],
                    "amount": params["amount"],
                    "q": params["q"],
                    "k": params["k"],
                    "saturation": params["saturation"],
                    "vf": candidate.vf,
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
    fieldnames = [
        "pair",
        "candidate",
        "stage",
        "gamma",
        "amount",
        "q",
        "k",
        "saturation",
        "vf",
        *METRIC_FIELDS,
    ]
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
    fieldnames = ["candidate", "stage", "gamma", "amount", "q", "k", "saturation", "vf", *METRIC_FIELDS]
    with out_csv.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def write_candidate_manifest(candidates: list[Candidate | FilterCandidate], out_path: Path) -> None:
    rows = [candidate.params for candidate in candidates]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["label", "stage", "gamma", "amount", "q", "k", "saturation", "vf"])
        writer.writeheader()
        writer.writerows(rows)


def select_rows(rows: list[dict[str, object]], count: int, candidate_map: dict[str, Candidate]) -> list[Candidate]:
    selected = []
    for row in rows:
        label = str(row["candidate"])
        candidate = candidate_map.get(label)
        if candidate is None:
            continue
        selected.append(candidate)
        if len(selected) >= count:
            break
    return selected


def baseline_candidates() -> list[FilterCandidate]:
    return [
        FilterCandidate("eq_yuv_only", "eq=gamma=1.46", "baseline"),
        FilterCandidate(
            "current_eq_yuv_only_cc_opt",
            "eq=gamma=1.46,colorcorrect=rl=-0.010000:bl=0.020000:rh=-0.005000:bh=0.010000:saturation=0.900000",
            "baseline",
        ),
        FilterCandidate(
            "current_pure_filtergraph_cc_opt",
            (
                "format=yuv444p10le,eq=gamma=1.46,format=gbrp10le,"
                "colorcorrect=rl=-0.010:bl=0.020:rh=-0.005:bh=0.010:saturation=0.90,"
                "format=yuv422p10le"
            ),
            "baseline",
        ),
    ]


def write_best(best: Candidate, out_root: Path) -> None:
    (out_root / "best_filtergraph.txt").write_text(best.vf + "\n")
    (out_root / "best_params.json").write_text(json.dumps(best.params, indent=2) + "\n")


def make_review_sheets(
    split: str,
    manifest: Path,
    candidates: list[Candidate],
    out_root: Path,
    frames: int,
    tile_width_arg: int,
) -> None:
    out_dir = out_root / "review_sheets" / split
    frames_root = out_root / "review_frames" / split
    variants = [Variant("A", None), Variant("B", None), Variant("current_eq_yuv_only_cc_opt", baseline_candidates()[1].vf)]
    variants.extend(Variant(f"top{index}_{candidate.label}", candidate.vf) for index, candidate in enumerate(candidates, start=1))
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "column_manifest.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["column", "label", "vf"])
        writer.writeheader()
        for index, variant in enumerate(variants, start=1):
            writer.writerow({"column": index, "label": variant.label, "vf": variant.vf or ""})

    for pair_index, (ref_video, raw_video) in enumerate(read_manifest(manifest), start=1):
        pair = f"pair_{pair_index:03d}"
        ref_meta = probe_metadata(ref_video)
        raw_meta = probe_metadata(raw_video)
        duration = min(ref_meta.duration_seconds, raw_meta.duration_seconds)
        tile_width, tile_height = tile_size(raw_video, tile_width_arg)
        frame_paths = []
        for row_index, time_s in enumerate(calculate_sample_times(duration, frames), start=1):
            for variant in variants:
                source = ref_video if variant.label == "A" else raw_video
                frame_path = frames_root / pair / f"r{row_index:03d}_{safe_name(variant.label)}.png"
                render_frame(source, frame_path, time_s, tile_width, tile_height, variant)
                frame_paths.append(frame_path)
        compose_grid(
            frame_paths,
            frames,
            len(variants),
            tile_width,
            tile_height,
            f"expt9F {split} {pair}",
            out_dir / f"{pair}.expt9F_grid.png",
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-root", type=Path, default=OUT_ROOT)
    parser.add_argument("--train-pairs", type=Path, default=TRAIN_PAIRS)
    parser.add_argument("--validation-pairs", type=Path, default=VALIDATION_PAIRS)
    parser.add_argument("--coarse-fps", type=float, default=0.5)
    parser.add_argument("--coarse-width", type=int, default=320)
    parser.add_argument("--coarse-height", type=int, default=240)
    parser.add_argument("--full-fps", type=float, default=2.0)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--skip-review-sheets", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.out_root.mkdir(parents=True, exist_ok=True)
    cache_root = args.out_root / "frame_cache"
    limit_pairs = 1 if args.smoke else None

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
        candidates = smoke_candidates()
        write_candidate_manifest(candidates, args.out_root / "candidate_manifest.csv")
        rows = evaluate_candidates(coarse_train, candidates, args.out_root / "smoke_pair_metrics.csv", jobs=args.jobs)
        write_summary(rows, args.out_root / "smoke_summary.csv")
        return 0

    stage1 = stage1_candidates()
    write_candidate_manifest(stage1, args.out_root / "candidate_manifest.csv")
    stage1_summary = args.out_root / "stage1_coarse_train.csv"
    rows1 = read_summary(stage1_summary)
    if rows1:
        print(f"Reusing {stage1_summary}", flush=True)
    else:
        rows1 = write_summary(
            evaluate_candidates(coarse_train, stage1, args.out_root / "stage1_coarse_train_pair_metrics.csv", jobs=args.jobs),
            stage1_summary,
        )
    map1 = {candidate.label: candidate for candidate in stage1}
    top1 = select_rows(rows1, 6, map1)

    stage2 = stage2_candidates(top1)
    stage2_summary = args.out_root / "stage2_shape_train.csv"
    rows2 = read_summary(stage2_summary)
    if rows2:
        print(f"Reusing {stage2_summary}", flush=True)
    else:
        rows2 = write_summary(
            evaluate_candidates(
                coarse_train,
                stage2,
                args.out_root / "stage2_shape_train_pair_metrics.csv",
                include_raw=False,
                jobs=args.jobs,
            ),
            stage2_summary,
        )
    map2 = {candidate.label: candidate for candidate in stage2}
    top2 = select_rows(rows2, 8, map2)

    stage3 = stage3_candidates(top2)
    all_search = dedupe_candidates([*stage1, *stage2, *stage3])
    write_candidate_manifest(all_search, args.out_root / "candidate_manifest.csv")
    rows3 = write_summary(
        evaluate_candidates(
            coarse_train,
            stage3,
            args.out_root / "stage3_refine_train_pair_metrics.csv",
            include_raw=False,
            jobs=args.jobs,
        ),
        args.out_root / "stage3_refine_train.csv",
    )

    coarse_union = write_summary([*rows1, *rows2, *rows3], args.out_root / "coarse_union_train.csv")
    all_map = {candidate.label: candidate for candidate in all_search}
    top_full = select_rows(coarse_union, 25, all_map)
    full_candidates: list[Candidate | FilterCandidate] = [*baseline_candidates(), *top_full]
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

    top_review = select_rows(full_train_rows, 5, all_map)
    if not args.skip_review_sheets:
        make_review_sheets("train", args.train_pairs, top_review, args.out_root, frames=3, tile_width_arg=360)
        make_review_sheets("validation", args.validation_pairs, top_review, args.out_root, frames=3, tile_width_arg=360)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
