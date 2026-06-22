#!/usr/bin/env python3
"""Extend expt9B vibrance-only search after the optimum hit the boundary."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from run_expt9BC_filters import (
    Candidate,
    access_grid,
    evaluate_candidates,
    fixed_base_luts,
    lut_filter,
    suffix_filter,
    summarize,
    validation_grid,
    write_candidates_manifest,
    BEST_LUT,
    GOPT_LUT,
    CP_GAMMAOPT_LUT,
)
from run_expt9_luma_only import ACCESS_ROOT, VALIDATION_PAIRS


OUT_ROOT = Path("generated_video_pairs/evaluations/expt9B_vibrance_deep")


def make_candidates() -> list[Candidate]:
    candidates = []
    for base, lut in fixed_base_luts().items():
        candidates.append(Candidate(label=base, part="A", base=base, vf=lut_filter(lut), note="fixed A baseline"))
        for intensity in (-0.20, -0.28, -0.36, -0.44, -0.52, -0.60, -0.72):
            label = f"vib_{abs(int(round(intensity * 100))):02d}"
            candidates.append(
                Candidate(
                    label=f"{base}_B_{label}",
                    part="B",
                    base=base,
                    vf=suffix_filter(lut, f"vibrance=intensity={intensity:.2f}"),
                    note=f"manual vibrance intensity {intensity:.2f}",
                )
            )
    return candidates


def select_b_winners(summary_rows: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    winners = {}
    for base in ("g_opt", "cp_gammaopt"):
        rows = [row for row in summary_rows if row["part"] == "B" and row["base"] == base]
        rows.sort(key=lambda row: (row["tone_score"], row["delta_e76_mean"]))
        winners[base] = rows[0]
    return winners


def write_winners(winners: dict[str, dict[str, object]], out_path: Path) -> None:
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["base", "candidate", "tone_score", "delta_e76_mean", "rgb_mae", "note"])
        writer.writeheader()
        for base, row in winners.items():
            writer.writerow(
                {
                    "base": base,
                    "candidate": row["candidate"],
                    "tone_score": row["tone_score"],
                    "delta_e76_mean": row["delta_e76_mean"],
                    "rgb_mae": row["rgb_mae"],
                    "note": row["note"],
                }
            )


def make_grids(args: argparse.Namespace, candidates: list[Candidate], winners: dict[str, dict[str, object]]) -> None:
    by_label = {candidate.label: candidate for candidate in candidates}
    variants = [
        ("BEST", lut_filter(BEST_LUT)),
        ("g_opt", lut_filter(GOPT_LUT)),
        ("cp_gammaopt", lut_filter(CP_GAMMAOPT_LUT)),
    ]
    for base in ("g_opt", "cp_gammaopt"):
        label = str(winners[base]["candidate"])
        variants.append((label, by_label[label].vf))
    access_grid(args, "B", variants, args.out_root / "access_grid_winners")
    validation_grid(args, "B", variants, args.out_root / "validation_grid_winners")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-root", type=Path, default=OUT_ROOT)
    parser.add_argument("--validation-pairs", type=Path, default=VALIDATION_PAIRS)
    parser.add_argument("--access-root", type=Path, default=ACCESS_ROOT)
    parser.add_argument("--fps", type=float, default=2.0)
    parser.add_argument("--sample-width", type=int)
    parser.add_argument("--sample-height", type=int)
    parser.add_argument("--grid-tile-width", type=int, default=260)
    parser.add_argument("--skip-eval", action="store_true")
    parser.add_argument("--skip-grids", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    candidates = make_candidates()
    write_candidates_manifest(candidates, args.out_root / "candidate_manifest.csv")
    metrics_csv = args.out_root / "evaluation" / "validation_metrics.csv"
    if not args.skip_eval:
        metrics_csv = evaluate_candidates(args, candidates)
    summary_rows = summarize(metrics_csv, candidates, args.out_root / "experiment_summary.csv")
    winners = select_b_winners(summary_rows)
    write_winners(winners, args.out_root / "winners.csv")
    if not args.skip_grids:
        make_grids(args, candidates, winners)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
