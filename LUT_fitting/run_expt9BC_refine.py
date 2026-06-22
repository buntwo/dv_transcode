#!/usr/bin/env python3
"""Narrow refinement around expt9B/C first-pass winners."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from run_expt9BC_filters import (
    OUT_ROOT as FIRST_PASS_ROOT,
    Candidate,
    evaluate_candidates,
    fixed_base_luts,
    lut_filter,
    make_review_grids,
    select_winners,
    suffix_filter,
    summarize,
    write_candidates_manifest,
)
from run_expt9_luma_only import ACCESS_ROOT, VALIDATION_PAIRS


OUT_ROOT = Path("generated_video_pairs/evaluations/expt9BC_refine")


def make_candidates() -> list[Candidate]:
    candidates = []
    for base, lut in fixed_base_luts().items():
        candidates.append(Candidate(label=base, part="A", base=base, vf=lut_filter(lut), note="fixed A baseline"))

        for intensity in (-0.04, -0.08, -0.12, -0.16, -0.20, -0.28):
            label = f"vib_{abs(int(round(intensity * 100))):02d}"
            suffix = f"vibrance=intensity={intensity:.2f}"
            candidates.append(
                Candidate(
                    label=f"{base}_B_{label}",
                    part="B",
                    base=base,
                    vf=suffix_filter(lut, suffix),
                    note=f"manual vibrance intensity {intensity:.2f}",
                )
            )

        for minknorm in (3, 5, 8):
            for sigma in (0.5, 1.0, 2.0):
                label = f"ge_d2_n{minknorm}_s{str(sigma).replace('.', 'p')}"
                suffix = f"greyedge=difford=2:minknorm={minknorm}:sigma={sigma}"
                candidates.append(
                    Candidate(
                        label=f"{base}_C_{label}",
                        part="C",
                        base=base,
                        vf=suffix_filter(lut, suffix),
                        note=f"greyedge difford 2 norm {minknorm} sigma {sigma}",
                    )
                )
    return candidates


def write_winners(winners: dict[tuple[str, str], dict[str, object]], out_path: Path) -> None:
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["part", "base", "candidate", "tone_score", "delta_e76_mean", "rgb_mae", "note"])
        writer.writeheader()
        for (part, base), row in winners.items():
            writer.writerow(
                {
                    "part": part,
                    "base": base,
                    "candidate": row["candidate"],
                    "tone_score": row["tone_score"],
                    "delta_e76_mean": row["delta_e76_mean"],
                    "rgb_mae": row["rgb_mae"],
                    "note": row["note"],
                }
            )


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
    winners = select_winners(summary_rows)
    write_winners(winners, args.out_root / "winners.csv")
    if not args.skip_grids:
        make_review_grids(args, candidates, winners)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
