#!/usr/bin/env python3
import argparse
import csv
import subprocess
from collections import defaultdict
from pathlib import Path

import numpy as np


BASE_LUT = Path("LUTs/vhs_to_video8_standard_i_native_1M_random_seed2002_strength85.cube")
TRAIN_PAIRS = Path("generated_video_pairs/train_geometry_normalized_pairs.txt")
VALIDATION_PAIRS = Path("generated_video_pairs/validation_geometry_normalized_pairs.txt")
OUT_ROOT = Path("generated_video_pairs/evaluations/expt6_tone_protection")

LUMA_COEF = np.array([0.299, 0.587, 0.114], dtype=np.float32)


GATED_VARIANTS = [
    {
        "name": "expt6a_final_strength75",
        "y": (0.882, 0.882, 0.882),
        "c": (0.882, 0.882, 0.882),
        "yellow_y": 0.0,
        "yellow_c": 0.0,
    },
    {
        "name": "expt6b_final_strength70",
        "y": (0.824, 0.824, 0.824),
        "c": (0.824, 0.824, 0.824),
        "yellow_y": 0.0,
        "yellow_c": 0.0,
    },
    {
        "name": "expt6c_y_damp_high_chroma_full",
        "y": (1.0, 0.45, 0.15),
        "c": (1.0, 1.0, 1.0),
        "yellow_y": 0.10,
        "yellow_c": 0.0,
    },
    {
        "name": "expt6d_no_high_luma",
        "y": (1.0, 0.55, 0.0),
        "c": (1.0, 1.0, 0.90),
        "yellow_y": 0.12,
        "yellow_c": 0.0,
    },
    {
        "name": "expt6e_chroma_only_mid_high",
        "y": (1.0, 0.15, 0.0),
        "c": (1.0, 0.90, 0.80),
        "yellow_y": 0.08,
        "yellow_c": 0.0,
    },
    {
        "name": "expt6f_shadow_yellow_gate",
        "y": (1.0, 0.25, 0.05),
        "c": (0.90, 0.45, 0.25),
        "yellow_y": 0.35,
        "yellow_c": 0.60,
    },
    {
        "name": "expt6g_nonshadow_identity",
        "y": (1.0, 0.20, 0.05),
        "c": (0.90, 0.25, 0.10),
        "yellow_y": 0.45,
        "yellow_c": 0.75,
    },
    {
        "name": "expt6h_yellow_chroma_only",
        "y": (0.80, 0.20, 0.0),
        "c": (0.50, 0.20, 0.10),
        "yellow_y": 0.25,
        "yellow_c": 0.90,
    },
    {
        "name": "expt6i_highlight_protect_soft",
        "y": (1.0, 0.65, 0.25),
        "c": (1.0, 0.90, 0.75),
        "yellow_y": 0.10,
        "yellow_c": 0.10,
    },
    {
        "name": "expt6j_shadow_luma_full_color",
        "y": (1.0, 0.05, 0.0),
        "c": (1.0, 1.0, 1.0),
        "yellow_y": 0.05,
        "yellow_c": 0.0,
    },
    {
        "name": "expt6k_nonwarm_tone_protect",
        "y": (1.0, 0.30, 0.05),
        "c": (1.0, 0.55, 0.35),
        "yellow_y": 0.25,
        "yellow_c": 0.50,
    },
    {
        "name": "expt6l_light_luma_damp",
        "y": (1.0, 0.75, 0.50),
        "c": (1.0, 1.0, 1.0),
        "yellow_y": 0.0,
        "yellow_c": 0.0,
    },
]


FIT_VARIANTS = [
    {
        "name": "expt6m_rgb_luma_reg_strong",
        "args": [
            "--model",
            "rgb-luma-reg",
            "--luma-regularization",
            "3.0",
            "--shadow-lift",
            "0.20",
            "--mid-lift",
            "0.025",
            "--highlight-lift",
            "0.0",
        ],
    },
    {
        "name": "expt6n_rgb_luma_reg_midzero",
        "args": [
            "--model",
            "rgb-luma-reg",
            "--luma-regularization",
            "2.0",
            "--shadow-lift",
            "0.20",
            "--mid-lift",
            "0.0",
            "--highlight-lift",
            "0.0",
        ],
    },
    {
        "name": "expt6o_rgb_luma_reg_mild",
        "args": [
            "--model",
            "rgb-luma-reg",
            "--luma-regularization",
            "1.5",
            "--shadow-lift",
            "0.18",
            "--mid-lift",
            "0.025",
            "--highlight-lift",
            "0.005",
        ],
    },
    {
        "name": "expt6p_ycbcr_affine",
        "args": ["--model", "ycbcr", "--degree", "1"],
    },
    {
        "name": "expt6q_hybrid_ycbcr_loose",
        "args": [
            "--model",
            "hybrid-ycbcr",
            "--shadow-lift",
            "0.22",
            "--mid-lift",
            "0.08",
            "--highlight-lift",
            "0.02",
        ],
    },
    {
        "name": "expt6r_hybrid_ycbcr_strict",
        "args": [
            "--model",
            "hybrid-ycbcr",
            "--shadow-lift",
            "0.20",
            "--mid-lift",
            "0.025",
            "--highlight-lift",
            "0.0",
        ],
    },
]


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
        raise ValueError(f"Expected {size ** 3} LUT rows, found {len(rows)}")
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


def luma_gate(y, strengths):
    shadow, mid, high = strengths
    return np.interp(y, [0.0, 0.25, 0.65, 1.0], [shadow, shadow, mid, high])


def yellow_gate(rgb):
    score = 0.5 * (rgb[:, 0] + rgb[:, 1]) - rgb[:, 2]
    return smoothstep(score, 0.06, 0.18)


def make_gated_variant(base_lut, out_path, variant):
    header, lut_rows, size = parse_cube(base_lut)
    inp = cube_inputs(size)
    inp_ycc = rgb_to_ycbcr(inp)
    out_ycc = rgb_to_ycbcr(lut_rows)

    yellow = yellow_gate(inp)
    y_gate = np.clip(luma_gate(inp_ycc[:, 0], variant["y"]) + variant["yellow_y"] * yellow, 0.0, 1.0)
    c_gate = np.clip(luma_gate(inp_ycc[:, 0], variant["c"]) + variant["yellow_c"] * yellow, 0.0, 1.0)

    new_ycc = np.empty_like(inp_ycc)
    new_ycc[:, 0] = inp_ycc[:, 0] + y_gate * (out_ycc[:, 0] - inp_ycc[:, 0])
    new_ycc[:, 1] = inp_ycc[:, 1] + c_gate * (out_ycc[:, 1] - inp_ycc[:, 1])
    new_ycc[:, 2] = inp_ycc[:, 2] + c_gate * (out_ycc[:, 2] - inp_ycc[:, 2])
    new_rows = np.clip(ycbcr_to_rgb(new_ycc), 0.0, 1.0)
    write_cube(out_path, header, new_rows, variant["name"])


def fit_variant(args, variant, out_path):
    if out_path.exists() and not args.refit:
        print(f"Skipping existing {out_path}", flush=True)
        return
    cmd = [
        "uv",
        "run",
        "python",
        "fit_vhs_lut.py",
        "--pairs",
        str(args.train_pairs),
        "--out",
        str(out_path),
        "--degree",
        "2",
        "--intercept",
        "--strength",
        "0.85",
        "--max-samples",
        str(args.max_samples),
        "--sampling",
        "random",
        "--seed",
        "2002",
    ]
    cmd.extend(variant["args"])
    run(cmd)


def evaluate(args, luts):
    eval_dir = args.out_root / "evaluation_all"
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
        "--fps",
        str(args.fps),
    ]
    for lut in luts:
        cmd.extend(["--lut", str(lut)])
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
        "mid_luma_bias",
        "high_luma_bias",
        "nonshadow_positive_luma_bias",
        "nonwarm_mid_high_luma_bias",
        "nonshadow_luma_over_p95",
        "new_clip_pct",
        "warm_yellow_delta_e76_mean",
        "warm_yellow_luma_bias",
        "tone_protection_score",
    ]

    summary = []
    for candidate, rows in grouped.items():
        out = {"candidate": candidate}
        for key in fields[1:-1]:
            out[key] = nanmean([to_float(row, key) for row in rows])
        out["tone_protection_score"] = (
            out["delta_e76_mean"]
            + 0.10 * out["nonshadow_positive_luma_bias"]
            + 0.02 * out["nonshadow_luma_over_p95"]
            + 0.35 * out["new_clip_pct"]
        )
        summary.append(out)

    summary.sort(key=lambda row: row["tone_protection_score"])
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(summary)

    print(f"Wrote {out_csv}")
    print("Top tone-protection score:")
    for row in summary[:8]:
        print(
            f"  {row['candidate']}: score {row['tone_protection_score']:.3f}, "
            f"dE {row['delta_e76_mean']:.3f}, nonshadow+Y {row['nonshadow_positive_luma_bias']:.3f}, "
            f"new clip {row['new_clip_pct']:.3f}",
            flush=True,
        )


def main():
    parser = argparse.ArgumentParser(
        description="Run tone/highlight-protection experiments from the final VHS LUT."
    )
    parser.add_argument("--base-lut", type=Path, default=BASE_LUT)
    parser.add_argument("--train-pairs", type=Path, default=TRAIN_PAIRS)
    parser.add_argument("--validation-pairs", type=Path, default=VALIDATION_PAIRS)
    parser.add_argument("--out-root", type=Path, default=OUT_ROOT)
    parser.add_argument("--max-samples", type=int, default=1000000)
    parser.add_argument("--fps", type=float, default=2.0)
    parser.add_argument("--sanity-chart", type=Path, default=Path("lut_sanity_chart.png"))
    parser.add_argument("--refit", action="store_true")
    parser.add_argument("--skip-eval", action="store_true")
    args = parser.parse_args()

    lut_dir = args.out_root / "luts"
    lut_dir.mkdir(parents=True, exist_ok=True)

    luts = [args.base_lut]
    for variant in GATED_VARIANTS:
        out_path = lut_dir / f"{variant['name']}.cube"
        make_gated_variant(args.base_lut, out_path, variant)
        luts.append(out_path)
        print(f"Wrote {out_path}", flush=True)

    for variant in FIT_VARIANTS:
        out_path = lut_dir / f"{variant['name']}.cube"
        fit_variant(args, variant, out_path)
        luts.append(out_path)

    if not args.skip_eval:
        metrics_csv = evaluate(args, luts)
        summarize(metrics_csv, args.out_root / "experiment_summary.csv")


if __name__ == "__main__":
    main()
