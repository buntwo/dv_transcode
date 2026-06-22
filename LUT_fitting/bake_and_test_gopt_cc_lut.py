#!/usr/bin/env python3
"""Bake the pure ffmpeg g_opt+cc_opt filter chain into a cube and compare on PNG frames."""

from __future__ import annotations

import argparse
import csv
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image


OUT_ROOT = Path("generated_video_pairs/evaluations/expt9D_builtin_filters/bake_gopt_cc_opt_test")
CC_OPT = "colorcorrect=rl=-0.010:bl=0.020:rh=-0.005:bh=0.010:saturation=0.90"
GOPT_FILTER = "format=yuv444p,lutyuv=y=pow(val/255\\,0.68)*255,format=rgb24"
DIRECT_FILTER = f"{GOPT_FILTER},{CC_OPT},format=rgb24"


def run(cmd: list[str | Path]) -> None:
    subprocess.run([str(part) for part in cmd], check=True)


def cube_grid(size: int) -> np.ndarray:
    levels = np.linspace(0, 255, size, dtype=np.float32).round().astype(np.uint8)
    rows = []
    for b in levels:
        for g in levels:
            for r in levels:
                rows.append((r, g, b))
    return np.asarray(rows, dtype=np.uint8)


def write_identity_image(path: Path, size: int) -> None:
    pixels = cube_grid(size)
    image = Image.fromarray(pixels.reshape((size, size * size, 3)), mode="RGB")
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def apply_filter(input_path: Path, output_path: Path, vf: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            input_path,
            "-vf",
            vf,
            "-frames:v",
            "1",
            output_path,
        ]
    )


def write_cube(path: Path, baked_image: Path, size: int) -> None:
    pixels = np.asarray(Image.open(baked_image).convert("RGB"), dtype=np.float32).reshape((-1, 3)) / 255.0
    expected = size**3
    if pixels.shape[0] != expected:
        raise ValueError(f"Expected {expected} pixels, got {pixels.shape[0]}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        f.write('TITLE "g_opt_cc_opt_ffmpeg_baked"\n')
        f.write(f"LUT_3D_SIZE {size}\n")
        f.write("DOMAIN_MIN 0.0 0.0 0.0\n")
        f.write("DOMAIN_MAX 1.0 1.0 1.0\n")
        for r, g, b in pixels:
            f.write(f"{r:.8f} {g:.8f} {b:.8f}\n")


def bake_lut(size: int, out_root: Path) -> Path:
    identity = out_root / "identity_cube_grid.png"
    baked = out_root / "identity_cube_grid.direct_filter.png"
    cube = out_root / f"g_opt_cc_opt_ffmpeg_baked_size{size}.cube"
    write_identity_image(identity, size)
    apply_filter(identity, baked, DIRECT_FILTER)
    write_cube(cube, baked, size)
    return cube


def default_test_frames() -> list[Path]:
    frames = [Path("lut_sanity_chart.png")]
    frames.extend(sorted(Path("generated_video_pairs/evaluations/expt9D_builtin_filters/frames/access").glob("*/r*_CTRL.png"))[:12])
    frames.extend(sorted(Path("generated_video_pairs/evaluations/expt9D_builtin_filters/frames/validation").glob("*/r*_CTRL.png"))[:12])
    return [frame for frame in frames if frame.exists()]


def compare_images(a_path: Path, b_path: Path) -> dict[str, float | int]:
    a = np.asarray(Image.open(a_path).convert("RGB"), dtype=np.int16)
    b = np.asarray(Image.open(b_path).convert("RGB"), dtype=np.int16)
    if a.shape != b.shape:
        raise ValueError(f"Shape mismatch: {a_path} {a.shape} vs {b_path} {b.shape}")
    diff = np.abs(a - b)
    per_pixel = diff.max(axis=2)
    return {
        "pixels": int(per_pixel.size),
        "mean_abs_rgb": float(diff.mean()),
        "p95_abs_rgb": float(np.percentile(diff, 95)),
        "p99_abs_rgb": float(np.percentile(diff, 99)),
        "max_abs_rgb": int(diff.max()),
        "mean_pixel_max_abs": float(per_pixel.mean()),
        "p99_pixel_max_abs": float(np.percentile(per_pixel, 99)),
        "max_pixel_max_abs": int(per_pixel.max()),
        "exact_pixel_percent": float((per_pixel == 0).mean() * 100.0),
        "within_1_pixel_percent": float((per_pixel <= 1).mean() * 100.0),
        "within_2_pixel_percent": float((per_pixel <= 2).mean() * 100.0),
    }


def test_frames(frames: list[Path], cube: Path, out_root: Path) -> Path:
    rows = []
    lut_filter = f"format=gbrp,lut3d={cube}:interp=tetrahedral,format=rgb24"
    rendered_root = out_root / "rendered_frames"
    for index, frame in enumerate(frames, start=1):
        stem = f"{index:03d}_{frame.stem}"
        direct = rendered_root / f"{stem}.direct.png"
        baked = rendered_root / f"{stem}.baked_lut.png"
        apply_filter(frame, direct, DIRECT_FILTER)
        apply_filter(frame, baked, lut_filter)
        metrics = compare_images(direct, baked)
        rows.append({"frame": str(frame), "direct": str(direct), "baked_lut": str(baked), **metrics})

    csv_path = out_root / "frame_comparison.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    summary_path = out_root / "summary.md"
    numeric_keys = [key for key in rows[0] if key not in {"frame", "direct", "baked_lut"}]
    with summary_path.open("w") as f:
        f.write("# Baked g_opt+cc_opt LUT Test\n\n")
        f.write(f"Direct filter chain: `{DIRECT_FILTER}`\n\n")
        f.write(f"Baked LUT: `{cube}`\n\n")
        f.write(f"Frames tested: {len(rows)}\n\n")
        f.write("| Metric | Mean | Max |\n")
        f.write("|---|---:|---:|\n")
        for key in numeric_keys:
            values = [float(row[key]) for row in rows]
            f.write(f"| {key} | {np.mean(values):.4f} | {np.max(values):.4f} |\n")
    return csv_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-root", type=Path, default=OUT_ROOT)
    parser.add_argument("--size", type=int, default=65)
    parser.add_argument("--frame", type=Path, action="append", help="PNG/JPEG frame to test. Defaults to sanity + cached CTRL frames.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    frames = args.frame or default_test_frames()
    if not frames:
        raise SystemExit("No test frames found.")
    with tempfile.TemporaryDirectory(prefix="bake-gopt-cc-") as _tmp:
        cube = bake_lut(args.size, args.out_root)
        csv_path = test_frames(frames, cube, args.out_root)
    print(f"Wrote {cube}")
    print(f"Wrote {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
