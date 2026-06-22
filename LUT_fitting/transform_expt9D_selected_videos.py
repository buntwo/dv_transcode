#!/usr/bin/env python3
"""Render full train/validation B clips through selected expt9D correction pipelines."""

from __future__ import annotations

import argparse
import csv
import statistics
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from evaluate_luts import read_manifest
from run_expt9BC_filters import BEST_LUT, GOPT_LUT, lut_filter, suffix_filter


OUT_ROOT = Path("generated_video_pairs/evaluations/expt9D_builtin_filters/transformed_videos")
TRAIN_PAIRS = Path("generated_video_pairs/train_geometry_normalized_pairs.txt")
VALIDATION_PAIRS = Path("generated_video_pairs/validation_geometry_normalized_pairs.txt")


@dataclass(frozen=True)
class Pipeline:
    label: str
    vf: str
    final_format: str | None = "gbrp"


def pipelines() -> list[Pipeline]:
    return [
        Pipeline("g_opt", suffix_filter(GOPT_LUT, "")),
        Pipeline("g_opt_greyedge", suffix_filter(GOPT_LUT, "greyedge=difford=2:minknorm=5:sigma=1.0")),
        Pipeline(
            "g_opt_cc_opt",
            suffix_filter(GOPT_LUT, "colorcorrect=rl=-0.010:bl=0.020:rh=-0.005:bh=0.010:saturation=0.90"),
        ),
        Pipeline("previous_best_lut", lut_filter(BEST_LUT)),
        Pipeline(
            "pure_filtergraph_cc_opt",
            (
                "format=yuv444p10le,eq=gamma=1.46,format=gbrp10le,"
                "colorcorrect=rl=-0.010:bl=0.020:rh=-0.005:bh=0.010:saturation=0.90,"
                "format=yuv422p10le"
            ),
            None,
        ),
        Pipeline(
            "pure_filtergraph_cc_nosat",
            (
                "format=yuv444p10le,eq=gamma=1.46,format=gbrp10le,"
                "colorcorrect=rl=-0.010:bl=0.020:rh=-0.005:bh=0.010,"
                "format=yuv422p10le"
            ),
            None,
        ),
    ]


def run(cmd: list[str | Path]) -> None:
    subprocess.run([str(part) for part in cmd], check=True)


def transform_video(src_video: Path, out_video: Path, pipeline: Pipeline) -> float:
    out_video.parent.mkdir(parents=True, exist_ok=True)
    filters = [pipeline.vf, "setpts=PTS-STARTPTS"]
    if pipeline.final_format:
        filters.append(f"format={pipeline.final_format}")
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        src_video,
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        ",".join(filters),
        "-c:v",
        "ffv1",
        "-level",
        "3",
        "-g",
        "1",
        "-slicecrc",
        "1",
        out_video,
    ]
    start = time.perf_counter()
    run(cmd)
    return time.perf_counter() - start


def pair_rows(manifest: Path) -> list[tuple[int, Path, Path]]:
    return [(index, ref_video, src_video) for index, (ref_video, src_video) in enumerate(read_manifest(manifest), start=1)]


def write_summary(rows: list[dict[str, object]], out_root: Path) -> None:
    csv_path = out_root / "timings.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="") as f:
        fieldnames = ["pipeline", "split", "pair", "source", "output", "seconds"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    by_pipeline: dict[str, list[float]] = {}
    by_pipeline_split: dict[tuple[str, str], list[float]] = {}
    for row in rows:
        pipeline = str(row["pipeline"])
        split = str(row["split"])
        seconds = float(row["seconds"])
        by_pipeline.setdefault(pipeline, []).append(seconds)
        by_pipeline_split.setdefault((pipeline, split), []).append(seconds)

    md_path = out_root / "timings_summary.md"
    with md_path.open("w") as f:
        f.write("# Expt9D Selected Video Transform Timings\n\n")
        f.write("Each timing is one full B-side clip rendered to FFV1.\n\n")
        f.write("| Pipeline | Split | Videos | Total s | Mean s/video | Median s/video | Min s | Max s |\n")
        f.write("|---|---:|---:|---:|---:|---:|---:|---:|\n")
        for (pipeline, split), values in sorted(by_pipeline_split.items()):
            f.write(
                f"| {pipeline} | {split} | {len(values)} | {sum(values):.2f} | "
                f"{statistics.mean(values):.2f} | {statistics.median(values):.2f} | {min(values):.2f} | {max(values):.2f} |\n"
            )
        f.write("\n| Pipeline | Videos | Total s | Mean s/video | Median s/video | Min s | Max s |\n")
        f.write("|---|---:|---:|---:|---:|---:|---:|\n")
        for pipeline, values in sorted(by_pipeline.items()):
            f.write(
                f"| {pipeline} | {len(values)} | {sum(values):.2f} | "
                f"{statistics.mean(values):.2f} | {statistics.median(values):.2f} | {min(values):.2f} | {max(values):.2f} |\n"
            )


def read_existing_timings(out_root: Path, replacing: set[str]) -> list[dict[str, object]]:
    csv_path = out_root / "timings.csv"
    if not csv_path.exists():
        return []
    with csv_path.open(newline="") as f:
        return [row for row in csv.DictReader(f) if row["pipeline"] not in replacing]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-root", type=Path, default=OUT_ROOT)
    parser.add_argument("--train-pairs", type=Path, default=TRAIN_PAIRS)
    parser.add_argument("--validation-pairs", type=Path, default=VALIDATION_PAIRS)
    parser.add_argument(
        "--pipeline",
        action="append",
        choices=[pipeline.label for pipeline in pipelines()],
        help="Render only the named pipeline. Repeat to render multiple selected pipelines.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    selected = pipelines()
    if args.pipeline:
        requested = set(args.pipeline)
        selected = [pipeline for pipeline in selected if pipeline.label in requested]
    rows: list[dict[str, object]] = read_existing_timings(args.out_root, {pipeline.label for pipeline in selected})
    manifests = [("train", args.train_pairs), ("validation", args.validation_pairs)]
    for pipeline in selected:
        for split, manifest in manifests:
            for pair_index, _ref_video, src_video in pair_rows(manifest):
                out_video = args.out_root / pipeline.label / split / f"pair_{pair_index:03d}_B.mkv"
                print(f"{pipeline.label} {split} pair_{pair_index:03d}", flush=True)
                elapsed = transform_video(src_video, out_video, pipeline)
                rows.append(
                    {
                        "pipeline": pipeline.label,
                        "split": split,
                        "pair": f"pair_{pair_index:03d}",
                        "source": src_video,
                        "output": out_video,
                        "seconds": f"{elapsed:.3f}",
                    }
                )
    write_summary(rows, args.out_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
