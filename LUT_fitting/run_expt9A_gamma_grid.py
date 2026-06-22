#!/usr/bin/env python3
"""Grid-search luma gamma for expt9A and generate focused review sheets."""

from __future__ import annotations

import argparse
import csv
import subprocess
from collections import defaultdict
from pathlib import Path

import numpy as np

from run_expt9_luma_only import ACCESS_ROOT, VALIDATION_PAIRS, access_inputs, gamma_curve, make_lut


OUT_ROOT = Path("generated_video_pairs/evaluations/expt9A_gamma_grid")
BEST_LUT = Path("LUTs/vhs_to_video8_standard_i_native_1M_random_seed2002_strength85.cube")
G74_LUT = Path("generated_video_pairs/evaluations/expt9A_luma_only/luts/expt9a_luma_g74.cube")
CP_STRONG_LUT = Path("generated_video_pairs/evaluations/expt9A_luma_only/luts/expt9a_luma_cp_strong.cube")


def run(cmd: list[str | Path]) -> None:
    print(" ".join(str(part) for part in cmd), flush=True)
    subprocess.run([str(part) for part in cmd], check=True)


def gamma_values(start: float, stop: float, step: float) -> list[float]:
    values = []
    current = start
    while current <= stop + (step / 2):
        values.append(round(current, 4))
        current += step
    return values


def spec_for_gamma(gamma: float) -> dict[str, object]:
    label = f"g{int(round(gamma * 100)):02d}"
    return {"label": label, "kind": "gamma", "gamma": gamma, "curve": gamma_curve(gamma)}


def evaluate(args: argparse.Namespace, specs: list[dict[str, object]], lut_paths: list[Path]) -> Path:
    eval_dir = args.out_root / "evaluation"
    cmd: list[str | Path] = [
        "uv",
        "run",
        "python",
        "evaluate_luts.py",
        "--pairs",
        args.validation_pairs,
        "--out-dir",
        eval_dir,
        "--contact-frames",
        "3",
    ]
    for lut in lut_paths:
        cmd.extend(["--lut", lut])
    run(cmd)
    return eval_dir / "validation_metrics.csv"


def summarize(metrics_csv: Path, out_csv: Path) -> dict[str, object]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    with metrics_csv.open() as f:
        for row in csv.DictReader(f):
            grouped[row["candidate"]].append(row)

    fields = [
        "candidate",
        "gamma",
        "rgb_mae",
        "delta_e76_mean",
        "luma_mae",
        "shadow_luma_lift",
        "mid_luma_lift",
        "high_luma_lift",
        "nonshadow_positive_luma_bias",
        "new_clip_pct",
    ]
    rows = []
    for candidate, candidate_rows in sorted(grouped.items()):
        if not candidate.startswith("expt9a_luma_g"):
            continue
        gamma = int(candidate.rsplit("_g", 1)[1]) / 100.0
        row = {"candidate": candidate, "gamma": gamma}
        for field in fields[2:]:
            values = [
                float(candidate_row[field])
                for candidate_row in candidate_rows
                if candidate_row[field] and candidate_row[field].lower() != "nan"
            ]
            row[field] = float(np.mean(values))
        rows.append(row)

    rows.sort(key=lambda row: (row["delta_e76_mean"], row["rgb_mae"]))
    best = rows[0]

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return best


def write_best_marker(best: dict[str, object], path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                f"candidate={best['candidate']}",
                f"gamma={best['gamma']:.2f}",
                f"delta_e76_mean={best['delta_e76_mean']:.6f}",
                f"rgb_mae={best['rgb_mae']:.6f}",
                f"luma_mae={best['luma_mae']:.6f}",
                "",
            ]
        )
    )


def make_access_grid(args: argparse.Namespace, best_lut: Path, best_label: str) -> None:
    cmd: list[str | Path] = [
        "uv",
        "run",
        "python",
        "generate_lut_comparison_grid.py",
        "--output-dir",
        args.out_root / "access_grid_best_gamma",
        "--sample-count",
        "12",
        "--tile-width",
        "300",
        "--lut",
        f"BEST={BEST_LUT}",
        "--lut",
        f"g74={G74_LUT}",
        "--lut",
        f"{best_label}={best_lut}",
        "--lut",
        f"cp_strong={CP_STRONG_LUT}",
    ]
    cmd.extend(access_inputs(args.access_root))
    run(cmd)


def make_validation_grid(args: argparse.Namespace, best_lut: Path, best_label: str) -> None:
    cmd: list[str | Path] = [
        "uv",
        "run",
        "python",
        "generate_pair_lut_comparison_grid.py",
        "--pairs",
        args.validation_pairs,
        "--output-dir",
        args.out_root / "validation_pair_grid_best_gamma",
        "--sample-count",
        "3",
        "--tile-width",
        "300",
        "--lut",
        f"BEST={BEST_LUT}",
        "--lut",
        f"g74={G74_LUT}",
        "--lut",
        f"{best_label}={best_lut}",
        "--lut",
        f"cp_strong={CP_STRONG_LUT}",
    ]
    run(cmd)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-root", type=Path, default=OUT_ROOT)
    parser.add_argument("--validation-pairs", type=Path, default=VALIDATION_PAIRS)
    parser.add_argument("--access-root", type=Path, default=ACCESS_ROOT)
    parser.add_argument("--size", type=int, default=33)
    parser.add_argument("--start", type=float, default=0.60)
    parser.add_argument("--stop", type=float, default=0.78)
    parser.add_argument("--step", type=float, default=0.02)
    parser.add_argument("--skip-grids", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    specs = [spec_for_gamma(gamma) for gamma in gamma_values(args.start, args.stop, args.step)]
    lut_paths = [make_lut(spec, args.size, args.out_root / "luts") for spec in specs]
    metrics_csv = evaluate(args, specs, lut_paths)
    best = summarize(metrics_csv, args.out_root / "gamma_grid_summary.csv")
    write_best_marker(best, args.out_root / "best_gamma.txt")

    best_gamma = float(best["gamma"])
    best_label = f"g{int(round(best_gamma * 100)):02d}"
    best_lut = args.out_root / "luts" / f"expt9a_luma_{best_label}.cube"
    if not args.skip_grids:
        make_access_grid(args, best_lut, best_label)
        make_validation_grid(args, best_lut, best_label)

    print(f"Best gamma: {best_gamma:.2f} ({best['candidate']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
