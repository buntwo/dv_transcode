#!/usr/bin/env python3
"""Generate luma-only ffmpeg/LUT candidates for staged color correction experiment A."""

from __future__ import annotations

import argparse
import csv
import subprocess
from pathlib import Path

import numpy as np


OUT_ROOT = Path("generated_video_pairs/evaluations/expt9A_luma_only")
VALIDATION_PAIRS = Path("generated_video_pairs/validation_geometry_normalized_pairs.txt")
ACCESS_ROOT = Path("../../Access")
ACCESS_PREFIXES = ("06 ", "07 ", "08 ", "09 ", "10 ", "11 ", "16 ")
LUMA_COEF = np.array([0.299, 0.587, 0.114], dtype=np.float32)


def run(cmd: list[str | Path]) -> None:
    print(" ".join(str(part) for part in cmd), flush=True)
    subprocess.run([str(part) for part in cmd], check=True)


def rgb_luma(rgb: np.ndarray) -> np.ndarray:
    return rgb @ LUMA_COEF


def rgb_to_ycbcr(rgb: np.ndarray) -> np.ndarray:
    y = rgb_luma(rgb)
    cb = (rgb[:, 2] - y) / 1.772
    cr = (rgb[:, 0] - y) / 1.402
    return np.stack([y, cb, cr], axis=1)


def ycbcr_to_rgb(ycbcr: np.ndarray) -> np.ndarray:
    y = ycbcr[:, 0]
    cb = ycbcr[:, 1]
    cr = ycbcr[:, 2]
    r = y + 1.402 * cr
    b = y + 1.772 * cb
    g = (y - 0.299 * r - 0.114 * b) / 0.587
    return np.stack([r, g, b], axis=1)


def gamut_fit_chroma(y: np.ndarray, cb: np.ndarray, cr: np.ndarray, max_iters: int = 18) -> tuple[np.ndarray, np.ndarray]:
    lo = np.zeros_like(y, dtype=np.float32)
    hi = np.ones_like(y, dtype=np.float32)
    for _ in range(max_iters):
        mid = (lo + hi) * 0.5
        rgb = ycbcr_to_rgb(np.stack([y, cb * mid, cr * mid], axis=1))
        inside = (rgb.min(axis=1) >= 0.0) & (rgb.max(axis=1) <= 1.0)
        lo = np.where(inside, mid, lo)
        hi = np.where(inside, hi, mid)
    return cb * lo, cr * lo


def cube_inputs(size: int) -> np.ndarray:
    grid = np.linspace(0.0, 1.0, size, dtype=np.float32)
    values = []
    for b in grid:
        for g in grid:
            for r in grid:
                values.append([r, g, b])
    return np.asarray(values, dtype=np.float32)


def write_cube(path: Path, rows: np.ndarray, size: int, title: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        f.write(f'TITLE "{title}"\n')
        f.write(f"LUT_3D_SIZE {size}\n")
        f.write("DOMAIN_MIN 0.0 0.0 0.0\n")
        f.write("DOMAIN_MAX 1.0 1.0 1.0\n")
        for row in rows:
            f.write(f"{row[0]:.8f} {row[1]:.8f} {row[2]:.8f}\n")


def gamma_curve(gamma: float):
    def apply(y: np.ndarray) -> np.ndarray:
        return np.power(np.clip(y, 0.0, 1.0), gamma).astype(np.float32)

    return apply


def control_point_curve(points: tuple[tuple[float, float], ...]):
    x = np.asarray([point[0] for point in points], dtype=np.float32)
    y = np.asarray([point[1] for point in points], dtype=np.float32)
    if np.any(np.diff(x) <= 0.0):
        raise ValueError("control point x values must be strictly increasing")
    if np.any(np.diff(y) < 0.0):
        raise ValueError("control point y values must be monotonic")

    def apply(values: np.ndarray) -> np.ndarray:
        return np.interp(values, x, y).astype(np.float32)

    return apply


def candidate_specs() -> list[dict[str, object]]:
    return [
        {"label": "g90", "kind": "gamma", "gamma": 0.90, "curve": gamma_curve(0.90)},
        {"label": "g82", "kind": "gamma", "gamma": 0.82, "curve": gamma_curve(0.82)},
        {"label": "g74", "kind": "gamma", "gamma": 0.74, "curve": gamma_curve(0.74)},
        {"label": "g62", "kind": "gamma", "gamma": 0.62, "curve": gamma_curve(0.62)},
        {"label": "g50", "kind": "gamma", "gamma": 0.50, "curve": gamma_curve(0.50)},
        {
            "label": "cp_mild",
            "kind": "control_points",
            "points": ((0.0, 0.0), (0.10, 0.13), (0.25, 0.29), (0.50, 0.51), (0.75, 0.75), (1.0, 1.0)),
            "curve": control_point_curve(((0.0, 0.0), (0.10, 0.13), (0.25, 0.29), (0.50, 0.51), (0.75, 0.75), (1.0, 1.0))),
        },
        {
            "label": "cp_base",
            "kind": "control_points",
            "points": ((0.0, 0.0), (0.10, 0.16), (0.25, 0.31), (0.50, 0.52), (0.75, 0.75), (1.0, 1.0)),
            "curve": control_point_curve(((0.0, 0.0), (0.10, 0.16), (0.25, 0.31), (0.50, 0.52), (0.75, 0.75), (1.0, 1.0))),
        },
        {
            "label": "cp_strong",
            "kind": "control_points",
            "points": ((0.0, 0.0), (0.10, 0.19), (0.25, 0.35), (0.50, 0.54), (0.75, 0.75), (1.0, 1.0)),
            "curve": control_point_curve(((0.0, 0.0), (0.10, 0.19), (0.25, 0.35), (0.50, 0.54), (0.75, 0.75), (1.0, 1.0))),
        },
    ]


def make_lut(spec: dict[str, object], size: int, lut_dir: Path) -> Path:
    inp_rgb = cube_inputs(size)
    inp_ycc = rgb_to_ycbcr(inp_rgb)
    curve = spec["curve"]
    assert callable(curve)
    out_y = np.clip(curve(inp_ycc[:, 0]), 0.0, 1.0)
    out_cb, out_cr = gamut_fit_chroma(out_y, inp_ycc[:, 1], inp_ycc[:, 2])
    out_rgb = np.clip(ycbcr_to_rgb(np.stack([out_y, out_cb, out_cr], axis=1)), 0.0, 1.0)
    out_path = lut_dir / f"expt9a_luma_{spec['label']}.cube"
    write_cube(out_path, out_rgb, size, f"expt9a_luma_{spec['label']}")
    return out_path


def write_curve_csv(specs: list[dict[str, object]], out_path: Path) -> None:
    ys = np.linspace(0.0, 1.0, 101, dtype=np.float32)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        fieldnames = ["source_y"] + [str(spec["label"]) for spec in specs]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for y in ys:
            row = {"source_y": f"{y:.4f}"}
            for spec in specs:
                curve = spec["curve"]
                assert callable(curve)
                row[str(spec["label"])] = f"{float(curve(np.asarray([y], dtype=np.float32))[0]):.6f}"
            writer.writerow(row)


def write_manifest(specs: list[dict[str, object]], lut_paths: list[Path], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        fieldnames = ["label", "kind", "gamma", "points", "lut"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for spec, path in zip(specs, lut_paths):
            writer.writerow(
                {
                    "label": spec["label"],
                    "kind": spec["kind"],
                    "gamma": spec.get("gamma", ""),
                    "points": spec.get("points", ""),
                    "lut": path,
                }
            )


def access_inputs(root: Path) -> list[Path]:
    found = []
    for prefix in ACCESS_PREFIXES:
        matches = sorted(path for path in root.glob(f"{prefix}*") if path.is_file())
        if not matches:
            raise FileNotFoundError(f"No Access video found for prefix {prefix!r} under {root}")
        found.append(matches[0])
    return found


def evaluate(args: argparse.Namespace, lut_paths: list[Path]) -> None:
    cmd = [
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
    ]
    for lut in lut_paths:
        cmd.extend(["--lut", lut])
    run(cmd)


def make_access_grid(args: argparse.Namespace, lut_paths: list[Path], specs: list[dict[str, object]]) -> None:
    cmd = [
        "uv",
        "run",
        "python",
        "generate_lut_comparison_grid.py",
        "--output-dir",
        args.out_root / "access_grid",
        "--sample-count",
        "12",
        "--tile-width",
        "220",
    ]
    for spec, lut in zip(specs, lut_paths):
        cmd.extend(["--lut", f"{spec['label']}={lut}"])
    cmd.extend(access_inputs(args.access_root))
    run(cmd)


def make_validation_grid(args: argparse.Namespace, lut_paths: list[Path], specs: list[dict[str, object]]) -> None:
    cmd = [
        "uv",
        "run",
        "python",
        "generate_pair_lut_comparison_grid.py",
        "--pairs",
        args.validation_pairs,
        "--output-dir",
        args.out_root / "validation_pair_grid",
        "--sample-count",
        "12",
        "--tile-width",
        "200",
    ]
    for spec, lut in zip(specs, lut_paths):
        cmd.extend(["--lut", f"{spec['label']}={lut}"])
    run(cmd)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-root", type=Path, default=OUT_ROOT)
    parser.add_argument("--validation-pairs", type=Path, default=VALIDATION_PAIRS)
    parser.add_argument("--access-root", type=Path, default=ACCESS_ROOT)
    parser.add_argument("--size", type=int, default=33)
    parser.add_argument("--skip-eval", action="store_true")
    parser.add_argument("--skip-access-grid", action="store_true")
    parser.add_argument("--skip-validation-grid", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    specs = candidate_specs()
    lut_dir = args.out_root / "luts"
    lut_paths = [make_lut(spec, args.size, lut_dir) for spec in specs]
    write_manifest(specs, lut_paths, args.out_root / "candidate_manifest.csv")
    write_curve_csv(specs, args.out_root / "luma_curves.csv")

    if not args.skip_eval:
        evaluate(args, lut_paths)
    if not args.skip_access_grid:
        make_access_grid(args, lut_paths, specs)
    if not args.skip_validation_grid:
        make_validation_grid(args, lut_paths, specs)

    print(f"Wrote {args.out_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
