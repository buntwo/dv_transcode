#!/usr/bin/env python3
"""Compare YUV-only colorcorrect against the validated RGB-ish chain."""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from evaluate_luts import read_manifest
from generate_lut_review_sheets import calculate_sample_times
from run_expt9_luma_only import access_inputs


OUT_ROOT = Path("generated_video_pairs/evaluations/expt9D_builtin_filters/yuv_only_vs_rgb_chain")
VALIDATION_PAIRS = Path("generated_video_pairs/validation_geometry_normalized_pairs.txt")
ACCESS_ROOT = Path("../../Access")
CC_OPT = "colorcorrect=rl=-0.010:bl=0.020:rh=-0.005:bh=0.010:saturation=0.90"
RGB_CHAIN = f"format=yuv444p10le,eq=gamma=1.46,format=gbrp10le,{CC_OPT},format=yuv422p10le"
YUV_ONLY_CHAIN = f"eq=gamma=1.46,{CC_OPT},format=yuv422p10le"


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    duration: float
    pix_fmt: str


def run(cmd: list[str | Path]) -> bytes:
    return subprocess.check_output([str(part) for part in cmd])


def probe(video: Path) -> VideoInfo:
    raw = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,duration,pix_fmt",
            "-of",
            "json",
            video,
        ]
    )
    stream = json.loads(raw)["streams"][0]
    return VideoInfo(
        width=int(stream["width"]),
        height=int(stream["height"]),
        duration=float(stream.get("duration") or 10.0),
        pix_fmt=str(stream["pix_fmt"]),
    )


def render_raw_frame(video: Path, time_s: float, vf: str, info: VideoInfo) -> np.ndarray:
    raw = run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{time_s:.6f}",
            "-i",
            video,
            "-vf",
            vf,
            "-frames:v",
            "1",
            "-pix_fmt",
            "yuv422p10le",
            "-f",
            "rawvideo",
            "-",
        ]
    )
    expected_samples = info.width * info.height * 2
    arr = np.frombuffer(raw, dtype="<u2")
    if arr.size != expected_samples:
        raise ValueError(f"Expected {expected_samples} samples from {video}, got {arr.size}")
    return arr.astype(np.int32)


def compare_frame(video: Path, time_s: float, label: str) -> dict[str, object]:
    info = probe(video)
    rgb = render_raw_frame(video, time_s, RGB_CHAIN, info)
    yuv = render_raw_frame(video, time_s, YUV_ONLY_CHAIN, info)
    diff = np.abs(rgb - yuv)
    return {
        "label": label,
        "video": str(video),
        "source_pix_fmt": info.pix_fmt,
        "time_s": f"{time_s:.3f}",
        "samples": int(diff.size),
        "mean_abs_10bit_code": f"{float(diff.mean()):.4f}",
        "p95_abs_10bit_code": f"{float(np.percentile(diff, 95)):.4f}",
        "p99_abs_10bit_code": f"{float(np.percentile(diff, 99)):.4f}",
        "max_abs_10bit_code": int(diff.max()),
        "exact_sample_percent": f"{float((diff == 0).mean() * 100.0):.4f}",
        "within_1_code_percent": f"{float((diff <= 1).mean() * 100.0):.4f}",
        "within_2_code_percent": f"{float((diff <= 2).mean() * 100.0):.4f}",
        "within_4_code_percent": f"{float((diff <= 4).mean() * 100.0):.4f}",
        "within_8_code_percent": f"{float((diff <= 8).mean() * 100.0):.4f}",
    }


def collect_test_videos(args: argparse.Namespace) -> list[tuple[str, Path, int]]:
    videos: list[tuple[str, Path, int]] = []
    for index, (_ref, src) in enumerate(read_manifest(args.validation_pairs), start=1):
        videos.append((f"validation_pair_{index:03d}", src, args.validation_frames))
    if args.include_access:
        for video in access_inputs(args.access_root):
            videos.append((f"access_{video.stem}", video, args.access_frames))
    return videos


def write_outputs(rows: list[dict[str, object]], out_root: Path) -> None:
    out_root.mkdir(parents=True, exist_ok=True)
    csv_path = out_root / "frame_metrics.csv"
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    values = {
        key: np.asarray([float(row[key]) for row in rows], dtype=np.float64)
        for key in (
            "mean_abs_10bit_code",
            "p95_abs_10bit_code",
            "p99_abs_10bit_code",
            "max_abs_10bit_code",
            "within_1_code_percent",
            "within_2_code_percent",
            "within_4_code_percent",
            "within_8_code_percent",
        )
    }
    md_path = out_root / "summary.md"
    with md_path.open("w") as f:
        f.write("# YUV-only vs RGB-ish Chain Test\n\n")
        f.write(f"RGB-ish reference: `{RGB_CHAIN}`\n\n")
        f.write(f"YUV-only candidate: `{YUV_ONLY_CHAIN}`\n\n")
        f.write(f"Frames tested: {len(rows)}\n\n")
        f.write("| Metric | Mean across frames | Worst frame |\n")
        f.write("|---|---:|---:|\n")
        for key, arr in values.items():
            if key.startswith("within_"):
                f.write(f"| {key} | {arr.mean():.4f}% | {arr.min():.4f}% |\n")
            else:
                f.write(f"| {key} | {arr.mean():.4f} | {arr.max():.4f} |\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-root", type=Path, default=OUT_ROOT)
    parser.add_argument("--validation-pairs", type=Path, default=VALIDATION_PAIRS)
    parser.add_argument("--validation-frames", type=int, default=3)
    parser.add_argument("--include-access", action="store_true")
    parser.add_argument("--access-root", type=Path, default=ACCESS_ROOT)
    parser.add_argument("--access-frames", type=int, default=2)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows: list[dict[str, object]] = []
    for label, video, frame_count in collect_test_videos(args):
        info = probe(video)
        duration = info.duration
        for time_s in calculate_sample_times(duration, frame_count):
            print(f"{label} {time_s:.3f}", flush=True)
            rows.append(compare_frame(video, time_s, label))
    write_outputs(rows, args.out_root)
    print(f"Wrote {args.out_root / 'summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
