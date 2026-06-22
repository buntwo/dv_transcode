#!/usr/bin/env python3
"""Create/evaluate cp_gammaopt from the best expt9A gamma."""

from __future__ import annotations

import argparse
import csv
import subprocess
from pathlib import Path

import numpy as np

from run_expt9_luma_only import (
    ACCESS_ROOT,
    VALIDATION_PAIRS,
    access_inputs,
    control_point_curve,
    make_lut,
)


OUT_ROOT = Path("generated_video_pairs/evaluations/expt9A_cp_gammaopt")
GAMMA_GRID_ROOT = Path("generated_video_pairs/evaluations/expt9A_gamma_grid")
BEST_LUT = Path("LUTs/vhs_to_video8_standard_i_native_1M_random_seed2002_strength85.cube")
G74_LUT = Path("generated_video_pairs/evaluations/expt9A_luma_only/luts/expt9a_luma_g74.cube")
CP_STRONG_LUT = Path("generated_video_pairs/evaluations/expt9A_luma_only/luts/expt9a_luma_cp_strong.cube")


def run(cmd: list[str | Path]) -> None:
    print(" ".join(str(part) for part in cmd), flush=True)
    subprocess.run([str(part) for part in cmd], check=True)


def read_best_gamma(path: Path) -> float:
    values = {}
    for line in path.read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return float(values["gamma"])


def gamma_label(gamma: float) -> str:
    return f"g{int(round(gamma * 100)):02d}"


def gamma_lut_path(gamma: float) -> Path:
    label = gamma_label(gamma)
    return GAMMA_GRID_ROOT / "luts" / f"expt9a_luma_{label}.cube"


def cp_gammaopt_points(gamma: float) -> tuple[tuple[float, float], ...]:
    return (
        (0.00, 0.00),
        (0.10, float(0.10**gamma)),
        (0.25, float(0.25**gamma)),
        (0.50, 0.56),
        (0.75, 0.75),
        (1.00, 1.00),
    )


def cp_gammaopt_spec(gamma: float) -> dict[str, object]:
    points = cp_gammaopt_points(gamma)
    return {
        "label": "cp_gammaopt",
        "kind": "control_points",
        "gamma": gamma,
        "points": points,
        "curve": control_point_curve(points),
    }


def write_manifest(spec: dict[str, object], lut_path: Path, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["label", "kind", "gamma", "points", "lut"])
        writer.writeheader()
        writer.writerow(
            {
                "label": spec["label"],
                "kind": spec["kind"],
                "gamma": spec["gamma"],
                "points": spec["points"],
                "lut": lut_path,
            }
        )


def write_curve_csv(spec: dict[str, object], gamma: float, out_path: Path) -> None:
    ys = np.linspace(0.0, 1.0, 101, dtype=np.float32)
    curve = spec["curve"]
    assert callable(curve)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["source_y", gamma_label(gamma), "cp_gammaopt"])
        writer.writeheader()
        for y in ys:
            writer.writerow(
                {
                    "source_y": f"{float(y):.4f}",
                    gamma_label(gamma): f"{float(y**gamma):.6f}",
                    "cp_gammaopt": f"{float(curve(np.asarray([y], dtype=np.float32))[0]):.6f}",
                }
            )


def evaluate(args: argparse.Namespace, gamma: float, cp_lut: Path) -> Path:
    cmd: list[str | Path] = [
        "uv",
        "run",
        "python",
        "evaluate_luts.py",
        "--pairs",
        args.validation_pairs,
        "--out-dir",
        args.out_root / "evaluation",
        "--contact-frames",
        "3",
        "--lut",
        BEST_LUT,
        "--lut",
        gamma_lut_path(gamma),
        "--lut",
        cp_lut,
        "--lut",
        CP_STRONG_LUT,
    ]
    run(cmd)
    return args.out_root / "evaluation" / "validation_metrics.csv"


def make_access_grid(args: argparse.Namespace, gamma: float, cp_lut: Path) -> None:
    label = gamma_label(gamma)
    cmd: list[str | Path] = [
        "uv",
        "run",
        "python",
        "generate_lut_comparison_grid.py",
        "--output-dir",
        args.out_root / "access_grid",
        "--sample-count",
        "12",
        "--tile-width",
        "300",
        "--lut",
        f"BEST={BEST_LUT}",
        "--lut",
        f"g74={G74_LUT}",
        "--lut",
        f"{label}={gamma_lut_path(gamma)}",
        "--lut",
        f"cp_gammaopt={cp_lut}",
        "--lut",
        f"cp_strong={CP_STRONG_LUT}",
    ]
    cmd.extend(access_inputs(args.access_root))
    run(cmd)


def make_validation_grid(args: argparse.Namespace, gamma: float, cp_lut: Path) -> None:
    label = gamma_label(gamma)
    cmd: list[str | Path] = [
        "uv",
        "run",
        "python",
        "generate_pair_lut_comparison_grid.py",
        "--pairs",
        args.validation_pairs,
        "--output-dir",
        args.out_root / "validation_pair_grid",
        "--sample-count",
        "3",
        "--tile-width",
        "300",
        "--lut",
        f"BEST={BEST_LUT}",
        "--lut",
        f"g74={G74_LUT}",
        "--lut",
        f"{label}={gamma_lut_path(gamma)}",
        "--lut",
        f"cp_gammaopt={cp_lut}",
        "--lut",
        f"cp_strong={CP_STRONG_LUT}",
    ]
    run(cmd)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-root", type=Path, default=OUT_ROOT)
    parser.add_argument("--validation-pairs", type=Path, default=VALIDATION_PAIRS)
    parser.add_argument("--access-root", type=Path, default=ACCESS_ROOT)
    parser.add_argument("--gamma-grid-root", type=Path, default=GAMMA_GRID_ROOT)
    parser.add_argument("--size", type=int, default=33)
    parser.add_argument("--skip-grids", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    gamma = read_best_gamma(args.gamma_grid_root / "best_gamma.txt")
    spec = cp_gammaopt_spec(gamma)
    cp_lut = make_lut(spec, args.size, args.out_root / "luts")
    write_manifest(spec, cp_lut, args.out_root / "candidate_manifest.csv")
    write_curve_csv(spec, gamma, args.out_root / "luma_curves.csv")
    evaluate(args, gamma, cp_lut)
    if not args.skip_grids:
        make_access_grid(args, gamma, cp_lut)
        make_validation_grid(args, gamma, cp_lut)
    print(f"Wrote {cp_lut}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
