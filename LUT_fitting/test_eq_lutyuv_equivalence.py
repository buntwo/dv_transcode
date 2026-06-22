#!/usr/bin/env python3
"""Compare eq gamma against the lutyuv gamma graph on cached review frames."""

from __future__ import annotations

import argparse
import csv
import subprocess
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image


OUT_ROOT = Path("generated_video_pairs/evaluations/expt9D_builtin_filters/eq_lutyuv_equivalence")
FRAMES_ROOT = Path("generated_video_pairs/evaluations/expt9D_builtin_filters/frames")
CC_OPT = "colorcorrect=rl=-0.010:bl=0.020:rh=-0.005:bh=0.010:saturation=0.90"
LUTYUV_GAMMA = "format=yuv444p,lutyuv=y=pow(val/255\\,0.68)*255,format=rgb24"


@dataclass
class Stats:
    pixels: int = 0
    channels: int = 0
    abs_sum: int = 0
    channel_hist: np.ndarray = field(default_factory=lambda: np.zeros(256, dtype=np.int64))
    pixel_max_hist: np.ndarray = field(default_factory=lambda: np.zeros(256, dtype=np.int64))
    max_abs: int = 0

    def add(self, diff: np.ndarray) -> None:
        pixel_max = diff.max(axis=2)
        self.pixels += int(pixel_max.size)
        self.channels += int(diff.size)
        self.abs_sum += int(diff.sum())
        self.channel_hist += np.bincount(diff.reshape(-1), minlength=256)
        self.pixel_max_hist += np.bincount(pixel_max.reshape(-1), minlength=256)
        self.max_abs = max(self.max_abs, int(diff.max()))

    def percentile(self, hist: np.ndarray, total: int, pct: float) -> int:
        if total == 0:
            return 0
        threshold = int(np.ceil(total * pct / 100.0))
        return int(np.searchsorted(np.cumsum(hist), threshold))

    def row(self) -> dict[str, float | int]:
        return {
            "frames": "",
            "pixels": self.pixels,
            "mean_abs_rgb": self.abs_sum / self.channels if self.channels else 0.0,
            "p95_abs_rgb": self.percentile(self.channel_hist, self.channels, 95),
            "p99_abs_rgb": self.percentile(self.channel_hist, self.channels, 99),
            "max_abs_rgb": self.max_abs,
            "exact_pixel_percent": 100.0 * self.pixel_max_hist[0] / self.pixels if self.pixels else 0.0,
            "within_1_pixel_percent": 100.0 * self.pixel_max_hist[:2].sum() / self.pixels if self.pixels else 0.0,
            "within_2_pixel_percent": 100.0 * self.pixel_max_hist[:3].sum() / self.pixels if self.pixels else 0.0,
            "within_3_pixel_percent": 100.0 * self.pixel_max_hist[:4].sum() / self.pixels if self.pixels else 0.0,
        }


def run(cmd: list[str | Path]) -> None:
    subprocess.run([str(part) for part in cmd], check=True)


def render_frame(frame: Path, out_path: Path, vf: str) -> None:
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            frame,
            "-vf",
            vf,
            "-frames:v",
            "1",
            out_path,
        ]
    )


def diff_images(a: Path, b: Path) -> np.ndarray:
    image_a = np.asarray(Image.open(a).convert("RGB"), dtype=np.int16)
    image_b = np.asarray(Image.open(b).convert("RGB"), dtype=np.int16)
    if image_a.shape != image_b.shape:
        raise ValueError(f"Shape mismatch: {a} {image_a.shape} vs {b} {image_b.shape}")
    return np.abs(image_a - image_b).astype(np.uint8)


def frame_group(path: Path) -> str:
    parts = path.parts
    if path.name == "lut_sanity_chart.png":
        return "sanity"
    if "frames" in parts:
        idx = parts.index("frames")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return "other"


def collect_frames(root: Path) -> list[Path]:
    frames = []
    sanity = Path("lut_sanity_chart.png")
    if sanity.exists():
        frames.append(sanity)
    frames.extend(sorted(root.glob("**/*_CTRL.png")))
    return frames


def write_rows(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def compare_variant(
    frames: list[Path],
    label: str,
    reference_vf: str,
    candidate_vf: str,
    tmp: Path,
) -> tuple[list[dict[str, object]], dict[str, Stats]]:
    rows = []
    stats = defaultdict(Stats)
    for index, frame in enumerate(frames, start=1):
        ref = tmp / f"{index:04d}_{label}_ref.png"
        cand = tmp / f"{index:04d}_{label}_cand.png"
        render_frame(frame, ref, reference_vf)
        render_frame(frame, cand, candidate_vf)
        diff = diff_images(ref, cand)
        group = frame_group(frame)
        stats["all"].add(diff)
        stats[group].add(diff)
        row_stats = Stats()
        row_stats.add(diff)
        row = row_stats.row()
        rows.append({"label": label, "group": group, "frame": str(frame), **row})
    return rows, stats


def summarize_stats(label: str, stats: dict[str, Stats]) -> list[dict[str, object]]:
    rows = []
    for group in sorted(stats):
        row = stats[group].row()
        rows.append({"label": label, "group": group, **row})
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-root", type=Path, default=OUT_ROOT)
    parser.add_argument("--frames-root", type=Path, default=FRAMES_ROOT)
    parser.add_argument(
        "--gamma",
        type=float,
        action="append",
        default=[1.44, 1.46, 1.47058824, 1.48, 1.50],
        help="eq gamma value to test. Repeatable.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    frames = collect_frames(args.frames_root)
    if not frames:
        raise SystemExit("No frames found.")

    all_summary_rows = []
    all_frame_rows = []
    with tempfile.TemporaryDirectory(prefix="eq-lutyuv-test-") as tmp_name:
        tmp = Path(tmp_name)
        gamma_summaries = []
        for gamma in args.gamma:
            label = f"gamma_only_eq_{gamma:.8f}".replace(".", "p")
            rows, stats = compare_variant(
                frames,
                label,
                LUTYUV_GAMMA,
                f"eq=gamma={gamma:.8f},format=rgb24",
                tmp,
            )
            all_frame_rows.extend(rows)
            summary_rows = summarize_stats(label, stats)
            all_summary_rows.extend(summary_rows)
            gamma_summaries.extend(row for row in summary_rows if row["group"] == "all")

        best = min(gamma_summaries, key=lambda row: (float(row["mean_abs_rgb"]), int(row["max_abs_rgb"])))
        best_gamma = float(str(best["label"]).removeprefix("gamma_only_eq_").replace("p", "."))
        full_label = f"full_chain_eq_{best_gamma:.8f}".replace(".", "p")
        rows, stats = compare_variant(
            frames,
            full_label,
            f"{LUTYUV_GAMMA},{CC_OPT},format=rgb24",
            f"eq=gamma={best_gamma:.8f},format=rgb24,{CC_OPT},format=rgb24",
            tmp,
        )
        all_frame_rows.extend(rows)
        all_summary_rows.extend(summarize_stats(full_label, stats))

    write_rows(args.out_root / "summary.csv", all_summary_rows)
    write_rows(args.out_root / "per_frame.csv", all_frame_rows)

    md_path = args.out_root / "summary.md"
    args.out_root.mkdir(parents=True, exist_ok=True)
    with md_path.open("w") as f:
        f.write("# eq vs lutyuv Equivalence Test\n\n")
        f.write(f"Frames tested: {len(frames)} cached CTRL/sanity frames\n\n")
        f.write(f"Reference gamma filter: `{LUTYUV_GAMMA}`\n\n")
        f.write(f"Best eq gamma by mean RGB error: `{best_gamma:.8f}`\n\n")
        f.write("| Label | Group | Pixels | Mean RGB abs | P99 RGB abs | Max RGB abs | Within 1 px | Within 2 px | Within 3 px |\n")
        f.write("|---|---|---:|---:|---:|---:|---:|---:|---:|\n")
        for row in all_summary_rows:
            if row["group"] == "all" or str(row["label"]).startswith("full_chain"):
                f.write(
                    f"| {row['label']} | {row['group']} | {row['pixels']} | "
                    f"{float(row['mean_abs_rgb']):.4f} | {row['p99_abs_rgb']} | {row['max_abs_rgb']} | "
                    f"{float(row['within_1_pixel_percent']):.2f}% | "
                    f"{float(row['within_2_pixel_percent']):.2f}% | "
                    f"{float(row['within_3_pixel_percent']):.2f}% |\n"
                )
    print(f"Wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
