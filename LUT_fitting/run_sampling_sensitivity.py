#!/usr/bin/env python3
import argparse
import csv
import subprocess
from collections import defaultdict
from pathlib import Path


def run(cmd):
    print(" ".join(str(part) for part in cmd), flush=True)
    subprocess.run(cmd, check=True)


def lut_path(out_dir, samples, seed):
    return out_dir / "luts" / f"standard_i_random_{samples}_seed{seed}_strength85.cube"


def fit_luts(args):
    lut_dir = args.out_dir / "luts"
    lut_dir.mkdir(parents=True, exist_ok=True)
    luts = []

    for samples in args.samples:
        for seed in args.seeds:
            out = lut_path(args.out_dir, samples, seed)
            luts.append(out)
            if out.exists() and not args.refit:
                print(f"Skipping existing {out}", flush=True)
                continue

            run(
                [
                    "uv",
                    "run",
                    "python",
                    "fit_vhs_lut.py",
                    "--pairs",
                    str(args.train_pairs),
                    "--out",
                    str(out),
                    "--model",
                    "standard",
                    "--degree",
                    "2",
                    "--intercept",
                    "--size",
                    str(args.lut_size),
                    "--strength",
                    "0.85",
                    "--fps",
                    str(args.fps),
                    "--mask-left",
                    str(args.mask_left),
                    "--mask-right",
                    str(args.mask_right),
                    "--mask-top",
                    str(args.mask_top),
                    "--mask-bottom",
                    str(args.mask_bottom),
                    "--max-samples",
                    str(samples),
                    "--sampling",
                    "random",
                    "--seed",
                    str(seed),
                ]
            )

    return luts


def evaluate_luts(args, luts):
    eval_dir = args.out_dir / "evaluation"
    cmd = [
        "uv",
        "run",
        "python",
        "evaluate_luts.py",
        "--pairs",
        str(args.validation_pairs),
        "--out-dir",
        str(eval_dir),
        "--mask-left",
        str(args.mask_left),
        "--mask-right",
        str(args.mask_right),
        "--mask-top",
        str(args.mask_top),
        "--mask-bottom",
        str(args.mask_bottom),
        "--sanity-chart",
        str(args.sanity_chart),
    ]
    for lut in luts:
        cmd.extend(["--lut", str(lut)])
    run(cmd)
    return eval_dir / "validation_metrics.csv"


def parse_candidate(candidate):
    # standard_i_random_2000000_seed1001_strength85
    parts = candidate.split("_")
    samples = None
    seed = None
    for part in parts:
        if part.startswith("seed"):
            seed = int(part.removeprefix("seed"))
        elif part.isdigit():
            samples = int(part)
    if samples is None or seed is None:
        raise ValueError(f"Could not parse candidate name: {candidate}")
    return samples, seed


def summarize(metrics_csv, out_csv):
    rows = []
    grouped = defaultdict(list)

    with metrics_csv.open() as f:
        for row in csv.DictReader(f):
            candidate = row["candidate"]
            if candidate == "raw":
                continue
            samples, seed = parse_candidate(candidate)
            row["samples"] = samples
            row["seed"] = seed
            rows.append(row)

    per_trial = []
    by_trial = defaultdict(list)
    for row in rows:
        by_trial[(row["samples"], row["seed"])].append(row)

    for (samples, seed), trial_rows in sorted(by_trial.items()):
        rgb = sum(float(row["rgb_mae"]) for row in trial_rows) / len(trial_rows)
        de = sum(float(row["delta_e76_mean"]) for row in trial_rows) / len(trial_rows)
        luma = sum(float(row["luma_mae"]) for row in trial_rows) / len(trial_rows)
        per_trial.append(
            {
                "samples": samples,
                "seed": seed,
                "rgb_mae": rgb,
                "delta_e76_mean": de,
                "luma_mae": luma,
            }
        )
        grouped[samples].append(per_trial[-1])

    summary_rows = []
    for samples, trial_rows in sorted(grouped.items()):
        n = len(trial_rows)
        rgb_values = [row["rgb_mae"] for row in trial_rows]
        de_values = [row["delta_e76_mean"] for row in trial_rows]
        luma_values = [row["luma_mae"] for row in trial_rows]
        summary_rows.append(
            {
                "samples": samples,
                "trials": n,
                "rgb_mae_mean": mean(rgb_values),
                "rgb_mae_std": std(rgb_values),
                "delta_e76_mean": mean(de_values),
                "delta_e76_std": std(de_values),
                "luma_mae_mean": mean(luma_values),
                "luma_mae_std": std(luma_values),
            }
        )

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with (out_csv.parent / "trial_metrics.csv").open("w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["samples", "seed", "rgb_mae", "delta_e76_mean", "luma_mae"],
        )
        writer.writeheader()
        writer.writerows(per_trial)

    with out_csv.open("w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "samples",
                "trials",
                "rgb_mae_mean",
                "rgb_mae_std",
                "delta_e76_mean",
                "delta_e76_std",
                "luma_mae_mean",
                "luma_mae_std",
            ],
        )
        writer.writeheader()
        writer.writerows(summary_rows)

    print(f"Wrote {out_csv.parent / 'trial_metrics.csv'}")
    print(f"Wrote {out_csv}")
    print("Summary:")
    for row in summary_rows:
        print(
            f"  {row['samples']}: RGB {row['rgb_mae_mean']:.3f} +/- {row['rgb_mae_std']:.3f}, "
            f"dE {row['delta_e76_mean']:.3f} +/- {row['delta_e76_std']:.3f}",
            flush=True,
        )


def mean(values):
    return sum(values) / len(values)


def std(values):
    if len(values) < 2:
        return 0.0
    m = mean(values)
    return (sum((value - m) ** 2 for value in values) / (len(values) - 1)) ** 0.5


def main():
    parser = argparse.ArgumentParser(
        description="Run random-sampling sensitivity trials for the selected LUT model."
    )
    parser.add_argument("--train-pairs", type=Path, default=Path("generated_video_pairs/train_geometry_normalized_pairs.txt"))
    parser.add_argument("--validation-pairs", type=Path, default=Path("generated_video_pairs/validation_geometry_normalized_pairs.txt"))
    parser.add_argument("--out-dir", type=Path, default=Path("generated_video_pairs/evaluations/expt3_sampling_sensitivity"))
    parser.add_argument("--samples", type=int, nargs="+", default=[500000, 1000000, 2000000, 5000000])
    parser.add_argument("--seeds", type=int, nargs="+", default=[1001, 2002, 3003])
    parser.add_argument("--fps", type=float, default=2.0)
    parser.add_argument("--lut-size", type=int, default=33)
    parser.add_argument("--mask-left", type=int, default=56)
    parser.add_argument("--mask-right", type=int, default=56)
    parser.add_argument("--mask-top", type=int, default=40)
    parser.add_argument("--mask-bottom", type=int, default=96)
    parser.add_argument("--sanity-chart", type=Path, default=Path("lut_sanity_chart.png"))
    parser.add_argument("--refit", action="store_true")
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    luts = fit_luts(args)
    metrics_csv = evaluate_luts(args, luts)
    summarize(metrics_csv, args.out_dir / "sampling_sensitivity_summary.csv")


if __name__ == "__main__":
    main()
