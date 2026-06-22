#!/usr/bin/env python3
"""Generate control/test clips from 10-bit tape masters using the expt9F filtergraph."""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import time
from pathlib import Path


MASTER_ROOT = Path("/Volumes/TU/tu.brian.2026.05.09/data/masters/tape")
OUT_ROOT = Path("generated_video_pairs/evaluations/expt9F_yuv_only_search/transformed_videos")
FILTERGRAPH = Path("generated_video_pairs/evaluations/expt9F_yuv_only_search/optimized_filtergraph_train_best.txt")


def run(cmd: list[str | Path]) -> None:
    subprocess.run([str(part) for part in cmd], check=True)


def run_capture(cmd: list[str | Path]) -> bytes:
    return subprocess.check_output([str(part) for part in cmd])


def discover_masters(root: Path) -> list[Path]:
    return sorted(path for path in root.glob("*.mkv") if path.is_file() and not path.name.startswith("._"))


def probe_duration(video: Path) -> float:
    raw = run_capture(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            video,
        ]
    )
    duration = float(json.loads(raw)["format"]["duration"])
    if duration <= 0:
        raise ValueError(f"Invalid duration for {video}: {duration}")
    return duration


def sample_starts(duration: float, count: int, clip_seconds: float) -> list[float]:
    max_start = max(0.0, duration - clip_seconds)
    step = max_start / (count + 1)
    return [step * index for index in range(1, count + 1)]


def output_dir_for(out_root: Path, master: Path) -> Path:
    return out_root / master.stem


def render_clip(master: Path, out_dir: Path, index: int, start: float, clip_seconds: float, vf: str, force: bool) -> float:
    control = out_dir / f"clip_{index:03d}_control.mkv"
    optimized = out_dir / f"clip_{index:03d}_optimized.mkv"
    if not force and control.exists() and optimized.exists():
        return 0.0

    out_dir.mkdir(parents=True, exist_ok=True)
    filter_complex = (
        f"[0:v]trim=duration={clip_seconds:.6f},setpts=PTS-STARTPTS,split=2[control][test_in];"
        f"[test_in]{vf}[test]"
    )
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start:.6f}",
        "-i",
        master,
        "-filter_complex",
        filter_complex,
        "-map",
        "[control]",
        "-an",
        "-c:v",
        "ffv1",
        "-pix_fmt",
        "yuv422p10le",
        "-level",
        "3",
        "-g",
        "1",
        "-slicecrc",
        "1",
        control,
        "-map",
        "[test]",
        "-an",
        "-c:v",
        "ffv1",
        "-pix_fmt",
        "yuv422p10le",
        "-level",
        "3",
        "-g",
        "1",
        "-slicecrc",
        "1",
        optimized,
    ]
    started = time.perf_counter()
    run(cmd)
    return time.perf_counter() - started


def write_manifest(rows: list[dict[str, object]], out_root: Path) -> None:
    out_root.mkdir(parents=True, exist_ok=True)
    with (out_root / "master_clip_manifest.csv").open("w", newline="") as f:
        fieldnames = [
            "master",
            "clip",
            "start_seconds",
            "control",
            "optimized",
            "elapsed_seconds",
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master-root", type=Path, default=MASTER_ROOT)
    parser.add_argument("--out-root", type=Path, default=OUT_ROOT)
    parser.add_argument("--filtergraph", type=Path, default=FILTERGRAPH)
    parser.add_argument("--clips-per-master", type=int, default=12)
    parser.add_argument("--clip-seconds", type=float, default=10.0)
    parser.add_argument("--limit", type=int, help="Limit number of masters for testing.")
    parser.add_argument("--force", action="store_true", help="Overwrite clips that already exist.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    vf = args.filtergraph.read_text().strip()
    if not vf:
        raise ValueError(f"Empty filtergraph file: {args.filtergraph}")

    masters = discover_masters(args.master_root)
    if args.limit is not None:
        masters = masters[: args.limit]
    rows: list[dict[str, object]] = []
    for master in masters:
        duration = probe_duration(master)
        out_dir = output_dir_for(args.out_root, master)
        for index, start in enumerate(sample_starts(duration, args.clips_per_master, args.clip_seconds), start=1):
            print(f"{master.name} clip_{index:03d} start={start:.3f}", flush=True)
            elapsed = render_clip(master, out_dir, index, start, args.clip_seconds, vf, args.force)
            rows.append(
                {
                    "master": str(master),
                    "clip": f"clip_{index:03d}",
                    "start_seconds": f"{start:.6f}",
                    "control": out_dir / f"clip_{index:03d}_control.mkv",
                    "optimized": out_dir / f"clip_{index:03d}_optimized.mkv",
                    "elapsed_seconds": f"{elapsed:.3f}",
                }
            )
            write_manifest(rows, args.out_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
