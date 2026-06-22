#!/usr/bin/env python3
"""Evaluate g_opt+cc_opt variants with and without the RGB/GBR round trip."""

from __future__ import annotations

import argparse
from pathlib import Path

from run_expt9BC_filters import Candidate, GOPT_LUT, evaluate_candidates, lut_filter, suffix_filter, summarize, write_candidates_manifest
from run_expt9_luma_only import VALIDATION_PAIRS


OUT_ROOT = Path("generated_video_pairs/evaluations/expt9E_yuv_only_metrics")
CC_OPT = "colorcorrect=rl=-0.010:bl=0.020:rh=-0.005:bh=0.010:saturation=0.90"


def candidates() -> list[Candidate]:
    return [
        Candidate(
            "g_opt_lut_cc_opt",
            "reference",
            "g_opt_lut",
            suffix_filter(GOPT_LUT, CC_OPT),
            "existing expt9D style: g_opt LUT then fixed colorcorrect",
        ),
        Candidate(
            "eq_rgb_roundtrip_cc_opt",
            "test",
            "eq",
            f"format=yuv444p10le,eq=gamma=1.46,format=gbrp10le,{CC_OPT},format=yuv422p10le",
            "eq gamma with explicit 10-bit GBR round trip before colorcorrect",
        ),
        Candidate(
            "eq_yuv_only_cc_opt",
            "test",
            "eq",
            f"eq=gamma=1.46,{CC_OPT}",
            "eq gamma and colorcorrect without RGB/GBR round trip",
        ),
        Candidate(
            "eq_yuv_only",
            "ablation",
            "eq",
            "eq=gamma=1.46",
            "eq gamma only, no colorcorrect",
        ),
        Candidate(
            "g_opt_lut",
            "ablation",
            "g_opt_lut",
            lut_filter(GOPT_LUT),
            "existing g_opt LUT only",
        ),
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-root", type=Path, default=OUT_ROOT)
    parser.add_argument("--validation-pairs", type=Path, default=VALIDATION_PAIRS)
    parser.add_argument("--fps", type=float, default=2.0)
    parser.add_argument("--sample-width", type=int)
    parser.add_argument("--sample-height", type=int)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cand = candidates()
    write_candidates_manifest(cand, args.out_root / "candidate_manifest.csv")
    metrics_csv = evaluate_candidates(args, cand)
    summarize(metrics_csv, cand, args.out_root / "experiment_summary.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
