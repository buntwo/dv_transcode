#!/usr/bin/env python3
"""Benchmark pure ffmpeg g_opt+cc_opt filters against baked LUTs on validation clips."""

from __future__ import annotations

import argparse
import csv
import statistics
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from evaluate_luts import read_manifest


OUT_ROOT = Path("generated_video_pairs/evaluations/expt9D_builtin_filters/filter_vs_baked_lut_benchmark")
VALIDATION_PAIRS = Path("generated_video_pairs/validation_geometry_normalized_pairs.txt")
BAKED_ROOT = Path("generated_video_pairs/evaluations/expt9D_builtin_filters/bake_gopt_cc_opt_test")
CC_OPT = "colorcorrect=rl=-0.010:bl=0.020:rh=-0.005:bh=0.010:saturation=0.90"
PURE_FILTER = f"format=yuv444p,lutyuv=y=pow(val/255\\,0.68)*255,format=rgb24,{CC_OPT},format=gbrp"


@dataclass(frozen=True)
class Pipeline:
    label: str
    vf: str


def pipelines(args: argparse.Namespace) -> list[Pipeline]:
    return [
        Pipeline("pure_filtergraph", PURE_FILTER),
        Pipeline("baked_lut_size65", f"format=gbrp,lut3d={args.lut65}:interp=tetrahedral,format=gbrp"),
        Pipeline("baked_lut_size129", f"format=gbrp,lut3d={args.lut129}:interp=tetrahedral,format=gbrp"),
    ]


def run_timed(video: Path, vf: str) -> float:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        video,
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        f"{vf},setpts=PTS-STARTPTS",
        "-c:v",
        "ffv1",
        "-level",
        "3",
        "-g",
        "1",
        "-slicecrc",
        "1",
        "-f",
        "matroska",
        "/dev/null",
    ]
    start = time.perf_counter()
    subprocess.run(cmd, check=True)
    return time.perf_counter() - start


def validation_videos(manifest: Path) -> list[tuple[str, Path]]:
    return [
        (f"pair_{index:03d}", src_video)
        for index, (_ref_video, src_video) in enumerate(read_manifest(manifest), start=1)
    ]


def write_outputs(rows: list[dict[str, object]], out_root: Path) -> None:
    out_root.mkdir(parents=True, exist_ok=True)
    csv_path = out_root / "timings.csv"
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["pipeline", "repeat", "pair", "source", "seconds"])
        writer.writeheader()
        writer.writerows(rows)

    by_pipeline: dict[str, list[float]] = {}
    for row in rows:
        by_pipeline.setdefault(str(row["pipeline"]), []).append(float(row["seconds"]))

    md_path = out_root / "summary.md"
    baseline = statistics.mean(by_pipeline["pure_filtergraph"])
    with md_path.open("w") as f:
        f.write("# g_opt+cc_opt Pure Filtergraph vs Baked LUT Benchmark\n\n")
        f.write("Validation B clips were encoded to FFV1/Matroska and written to `/dev/null`.\n\n")
        f.write("| Pipeline | Runs | Total s | Mean s/clip | Median s/clip | Min s | Max s | Relative mean |\n")
        f.write("|---|---:|---:|---:|---:|---:|---:|---:|\n")
        for label, values in sorted(by_pipeline.items()):
            mean = statistics.mean(values)
            f.write(
                f"| {label} | {len(values)} | {sum(values):.2f} | {mean:.3f} | "
                f"{statistics.median(values):.3f} | {min(values):.3f} | {max(values):.3f} | {mean / baseline:.3f}x |\n"
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-root", type=Path, default=OUT_ROOT)
    parser.add_argument("--validation-pairs", type=Path, default=VALIDATION_PAIRS)
    parser.add_argument("--lut65", type=Path, default=BAKED_ROOT / "g_opt_cc_opt_ffmpeg_baked_size65.cube")
    parser.add_argument("--lut129", type=Path, default=BAKED_ROOT / "g_opt_cc_opt_ffmpeg_baked_size129.cube")
    parser.add_argument("--repeats", type=int, default=2)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows: list[dict[str, object]] = []
    videos = validation_videos(args.validation_pairs)
    for repeat in range(1, args.repeats + 1):
        for pipeline in pipelines(args):
            for pair, video in videos:
                print(f"repeat {repeat} {pipeline.label} {pair}", flush=True)
                elapsed = run_timed(video, pipeline.vf)
                rows.append(
                    {
                        "pipeline": pipeline.label,
                        "repeat": repeat,
                        "pair": pair,
                        "source": video,
                        "seconds": f"{elapsed:.3f}",
                    }
                )
    write_outputs(rows, args.out_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
