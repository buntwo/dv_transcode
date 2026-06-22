#!/usr/bin/env python3
"""Generate/evaluate a monotone source-luma-gated variant of the previous best LUT."""

from __future__ import annotations

import argparse
import csv
import subprocess
from collections import defaultdict
from pathlib import Path

import numpy as np


BASE_LUT = Path("LUTs/vhs_to_video8_standard_i_native_1M_random_seed2002_strength85.cube")
VALIDATION_PAIRS = Path("generated_video_pairs/validation_geometry_normalized_pairs.txt")
OUT_ROOT = Path("generated_video_pairs/evaluations/expt8_monotone_luma_gate")
LUMA_COEF = np.array([0.299, 0.587, 0.114], dtype=np.float32)


def run(cmd):
    print(" ".join(str(part) for part in cmd), flush=True)
    subprocess.run(cmd, check=True)


def rgb_luma(rgb):
    return rgb @ LUMA_COEF


def rgb_to_ycbcr(rgb):
    y = rgb_luma(rgb)
    cb = (rgb[:, 2] - y) / 1.772
    cr = (rgb[:, 0] - y) / 1.402
    return np.stack([y, cb, cr], axis=1)


def ycbcr_to_rgb(ycbcr):
    y = ycbcr[:, 0]
    cb = ycbcr[:, 1]
    cr = ycbcr[:, 2]
    r = y + 1.402 * cr
    b = y + 1.772 * cb
    g = (y - 0.299 * r - 0.114 * b) / 0.587
    return np.stack([r, g, b], axis=1)


def smoothstep(x, edge0, edge1):
    t = np.clip((x - edge0) / (edge1 - edge0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def parse_cube(path):
    header = []
    rows = []
    size = None
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line:
            header.append(raw_line)
            continue
        parts = line.split()
        keyword = parts[0].upper()
        if keyword == "LUT_3D_SIZE":
            size = int(parts[1])
            header.append(raw_line)
        elif keyword in {"TITLE", "DOMAIN_MIN", "DOMAIN_MAX"} or line.startswith("#"):
            header.append(raw_line)
        else:
            if len(parts) != 3:
                raise ValueError(f"Unexpected LUT row: {raw_line}")
            rows.append([float(value) for value in parts])
    if size is None:
        raise ValueError(f"Missing LUT_3D_SIZE in {path}")
    if len(rows) != size ** 3:
        raise ValueError(f"Expected {size ** 3} rows, found {len(rows)}")
    return header, np.asarray(rows, dtype=np.float32), size


def cube_inputs(size):
    grid = np.linspace(0.0, 1.0, size, dtype=np.float32)
    values = []
    for b in grid:
        for g in grid:
            for r in grid:
                values.append([r, g, b])
    return np.asarray(values, dtype=np.float32)


def write_cube(path, header, rows, title):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        wrote_title = False
        for line in header:
            if line.strip().upper().startswith("TITLE"):
                f.write(f'TITLE "{title}"\n')
                wrote_title = True
            else:
                f.write(f"{line}\n")
        if not wrote_title:
            f.write(f'TITLE "{title}"\n')
        for row in rows:
            f.write(f"{row[0]:.8f} {row[1]:.8f} {row[2]:.8f}\n")


def pava(values, weights):
    blocks = []
    for value, weight in zip(values, weights):
        blocks.append([float(value), float(weight), 1])
        while len(blocks) >= 2 and blocks[-2][0] > blocks[-1][0]:
            prev = blocks.pop()
            cur = blocks.pop()
            weight_sum = cur[1] + prev[1]
            merged_value = (cur[0] * cur[1] + prev[0] * prev[1]) / weight_sum
            blocks.append([merged_value, weight_sum, cur[2] + prev[2]])

    fitted = []
    for value, _, count in blocks:
        fitted.extend([value] * count)
    return np.asarray(fitted, dtype=np.float32)


def fit_monotone_luma_curve(source_y, desired_y, bins):
    edges = np.linspace(0.0, 1.0, bins + 1, dtype=np.float32)
    centers = (edges[:-1] + edges[1:]) / 2.0
    binned = []
    weights = []
    used_centers = []

    for index, center in enumerate(centers):
        if index == len(centers) - 1:
            mask = (source_y >= edges[index]) & (source_y <= edges[index + 1])
        else:
            mask = (source_y >= edges[index]) & (source_y < edges[index + 1])
        if not np.any(mask):
            continue
        used_centers.append(center)
        binned.append(float(np.mean(desired_y[mask])))
        weights.append(float(np.count_nonzero(mask)))

    x = np.asarray(used_centers, dtype=np.float32)
    y = pava(np.asarray(binned, dtype=np.float32), np.asarray(weights, dtype=np.float32))
    y = np.clip(y, 0.0, 1.0)
    return x, y


def apply_monotone_curve(source_y, curve_x, curve_y):
    return np.interp(source_y, curve_x, curve_y).astype(np.float32)


def gamut_fit_chroma(y, cb, cr, max_iters=18):
    lo = np.zeros_like(y, dtype=np.float32)
    hi = np.ones_like(y, dtype=np.float32)
    scale = hi.copy()
    for _ in range(max_iters):
        mid = (lo + hi) * 0.5
        rgb = ycbcr_to_rgb(np.stack([y, cb * mid, cr * mid], axis=1))
        inside = (rgb.min(axis=1) >= 0.0) & (rgb.max(axis=1) <= 1.0)
        lo = np.where(inside, mid, lo)
        hi = np.where(inside, hi, mid)
        scale = lo
    return cb * scale, cr * scale


def make_variant(base_lut, out_path, threshold, fade_width, bins):
    header, best_rgb, size = parse_cube(base_lut)
    inp_rgb = cube_inputs(size)
    inp_ycc = rgb_to_ycbcr(inp_rgb)
    best_ycc = rgb_to_ycbcr(best_rgb)

    source_y = inp_ycc[:, 0]
    y_gate = 1.0 - smoothstep(source_y, threshold, threshold + fade_width)
    desired_y = inp_ycc[:, 0] + y_gate * (best_ycc[:, 0] - inp_ycc[:, 0])

    curve_x, curve_y = fit_monotone_luma_curve(source_y, desired_y, bins)
    out_y = apply_monotone_curve(source_y, curve_x, curve_y)

    # Chroma correction remains fully active; only luma is gated/monotone.
    out_cb = best_ycc[:, 1]
    out_cr = best_ycc[:, 2]
    out_cb, out_cr = gamut_fit_chroma(out_y, out_cb, out_cr)
    out_rgb = np.clip(ycbcr_to_rgb(np.stack([out_y, out_cb, out_cr], axis=1)), 0.0, 1.0)

    write_cube(out_path, header, out_rgb, f"expt8_monotone_luma_T{threshold:.2f}_chroma_only")
    return curve_x, curve_y


def evaluate(args, lut):
    eval_dir = args.out_root / "evaluation"
    cmd = [
        "uv",
        "run",
        "python",
        "evaluate_luts.py",
        "--pairs",
        str(args.validation_pairs),
        "--out-dir",
        str(eval_dir),
        "--sanity-chart",
        str(args.sanity_chart),
        "--contact-sheet",
        "--contact-frames",
        "3",
        "--lut",
        str(args.base_lut),
        "--lut",
        str(lut),
    ]
    run(cmd)
    return eval_dir / "validation_metrics.csv"


def to_float(row, key):
    value = row.get(key, "")
    if value == "" or value.lower() == "nan":
        return float("nan")
    return float(value)


def nanmean(values):
    finite = [value for value in values if np.isfinite(value)]
    if not finite:
        return float("nan")
    return sum(finite) / len(finite)


def summarize(metrics_csv, out_csv):
    grouped = defaultdict(list)
    with metrics_csv.open() as f:
        for row in csv.DictReader(f):
            grouped[row["candidate"]].append(row)

    fields = [
        "candidate",
        "rgb_mae",
        "delta_e76_mean",
        "luma_mae",
        "shadow_luma_lift",
        "mid_luma_lift",
        "high_luma_lift",
        "nonshadow_positive_luma_bias",
        "nonwarm_mid_high_luma_bias",
        "nonshadow_luma_over_p95",
        "new_clip_pct",
        "warm_yellow_delta_e76_mean",
        "tone_protection_score",
    ]
    rows = []
    for candidate, candidate_rows in grouped.items():
        row = {"candidate": candidate}
        for key in fields[1:-1]:
            row[key] = nanmean([to_float(item, key) for item in candidate_rows])
        row["tone_protection_score"] = (
            row["delta_e76_mean"]
            + 0.10 * row["nonshadow_positive_luma_bias"]
            + 0.02 * row["nonshadow_luma_over_p95"]
            + 0.35 * row["new_clip_pct"]
        )
        rows.append(row)

    rows.sort(key=lambda row: row["tone_protection_score"])
    with out_csv.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {out_csv}")
    for row in rows:
        print(
            f"  {row['candidate']}: score {row['tone_protection_score']:.3f}, "
            f"dE {row['delta_e76_mean']:.3f}, RGB {row['rgb_mae']:.3f}, "
            f"nonshadow+Y {row['nonshadow_positive_luma_bias']:.3f}, "
            f"nonwarm mid/high {row['nonwarm_mid_high_luma_bias']:.3f}",
            flush=True,
        )


def write_curve(path, curve_x, curve_y):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["source_y", "output_y"])
        writer.writerows(zip(curve_x, curve_y))


def main():
    parser = argparse.ArgumentParser(
        description="Run monotone-luma-gated BEST-LUT experiment."
    )
    parser.add_argument("--base-lut", type=Path, default=BASE_LUT)
    parser.add_argument("--validation-pairs", type=Path, default=VALIDATION_PAIRS)
    parser.add_argument("--out-root", type=Path, default=OUT_ROOT)
    parser.add_argument("--threshold", type=float, default=0.50)
    parser.add_argument("--fade-width", type=float, default=0.10)
    parser.add_argument("--bins", type=int, default=512)
    parser.add_argument("--sanity-chart", type=Path, default=Path("lut_sanity_chart.png"))
    parser.add_argument("--skip-eval", action="store_true")
    args = parser.parse_args()

    lut_dir = args.out_root / "luts"
    lut_dir.mkdir(parents=True, exist_ok=True)
    out_lut = lut_dir / f"expt8_monotone_T{int(round(args.threshold * 100)):02d}_chroma_only.cube"
    curve_x, curve_y = make_variant(args.base_lut, out_lut, args.threshold, args.fade_width, args.bins)
    write_curve(args.out_root / "monotone_luma_curve.csv", curve_x, curve_y)
    print(f"Wrote {out_lut}")
    print(f"Wrote {args.out_root / 'monotone_luma_curve.csv'}")

    if not args.skip_eval:
        metrics_csv = evaluate(args, out_lut)
        summarize(metrics_csv, args.out_root / "experiment_summary.csv")


if __name__ == "__main__":
    main()
