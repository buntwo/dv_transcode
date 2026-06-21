#!/usr/bin/env python3
import argparse
import subprocess
from pathlib import Path


GRID = (
    ("standard", True, "standard_i"),
    ("standard", False, "standard_noi"),
    ("root-poly", True, "rootpoly_i"),
    ("root-poly", False, "rootpoly_noi"),
)


def run(cmd):
    print(" ".join(str(part) for part in cmd), flush=True)
    subprocess.run(cmd, check=True)


def main():
    parser = argparse.ArgumentParser(
        description="Fit the VHS-to-Video8 model grid and emit 100%/85% LUTs."
    )
    parser.add_argument("--pairs", type=Path, default=Path("generated_video_pairs/train_pairs.txt"))
    parser.add_argument("--out-dir", type=Path, default=Path("LUTs/candidates"))
    parser.add_argument("--size", type=int, default=33)
    parser.add_argument("--degree", type=int, default=2)
    parser.add_argument("--fps", type=float, default=2.0)
    parser.add_argument("--sample-width", type=int)
    parser.add_argument("--sample-height", type=int)
    parser.add_argument("--mask-left", type=int, default=14)
    parser.add_argument("--mask-right", type=int, default=14)
    parser.add_argument("--mask-top", type=int, default=10)
    parser.add_argument("--mask-bottom", type=int, default=24)
    parser.add_argument("--max-samples", type=int, default=500000)
    parser.add_argument(
        "--sampling",
        choices=["random", "pair-balanced", "pair-luma-balanced"],
        default="pair-balanced",
    )
    parser.add_argument("--luma-bins", type=int, default=6)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    for model, intercept, name in GRID:
        cmd = [
            "uv",
            "run",
            "python",
            "fit_vhs_lut.py",
            "--pairs",
            str(args.pairs),
            "--out",
            str(args.out_dir / f"vhs_to_video8_{name}_d{args.degree}_{args.size}.cube"),
            "--model",
            model,
            "--degree",
            str(args.degree),
            "--size",
            str(args.size),
            "--strengths",
            "1.0",
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
            str(args.max_samples),
            "--sampling",
            args.sampling,
            "--luma-bins",
            str(args.luma_bins),
        ]
        if args.sample_width is not None or args.sample_height is not None:
            if args.sample_width is None or args.sample_height is None:
                raise SystemExit("--sample-width and --sample-height must be provided together")
            cmd.extend(
                [
                    "--sample-width",
                    str(args.sample_width),
                    "--sample-height",
                    str(args.sample_height),
                ]
            )
        cmd.append("--intercept" if intercept else "--no-intercept")
        run(cmd)


if __name__ == "__main__":
    main()
