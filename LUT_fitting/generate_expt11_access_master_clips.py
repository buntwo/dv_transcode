#!/usr/bin/env python3
"""Generate focused 10-bit master review clips for the selected Access videos."""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import time
from pathlib import Path

from run_expt11_gamma_weight_search import PREVIOUS_FILTERGRAPH


MASTER_ROOT = Path("/Volumes/TU/tu.brian.2026.05.09/data/masters/tape")
ACCESS_ROOT = Path("../../Access")
ACCESS_NAME_MAP = Path("/Users/btu/scratch/Videos/access_name_map.csv")
OUT_ROOT = Path("generated_video_pairs/evaluations/expt11_gamma_weight_search/transformed_videos/access_master_clips")
EXPT11_BEST = Path("generated_video_pairs/evaluations/expt11_gamma_weight_search/best_filtergraph.txt")
ACCESS_PREFIXES = ("06 ", "07 ", "08 ", "09 ", "10 ", "11 ", "16 ")


def run(cmd: list[str | Path]) -> None:
    subprocess.run([str(part) for part in cmd], check=True)


def run_capture(cmd: list[str | Path]) -> bytes:
    return subprocess.check_output([str(part) for part in cmd])


def normalize_name(value: str) -> str:
    return re.sub(r"[\W_]+", "", value, flags=re.UNICODE).casefold()


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")


def access_inputs(root: Path) -> list[Path]:
    videos = []
    for prefix in ACCESS_PREFIXES:
        matches = sorted(path for path in root.glob(f"{prefix}*") if path.is_file())
        if not matches:
            raise FileNotFoundError(f"No Access video found for prefix {prefix!r} under {root}")
        videos.append(matches[0])
    return videos


def strip_access_prefix(stem: str) -> str:
    return re.sub(r"^\d+\s+", "", stem)


def read_name_map(path: Path) -> dict[str, dict[str, str]]:
    mapping: dict[str, dict[str, str]] = {}
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            for field in ("renamed_stem", "without_prefix", "original_stem"):
                value = row.get(field, "")
                if value:
                    mapping[normalize_name(value)] = row
    return mapping


def master_for_access(access_video: Path, name_map: dict[str, dict[str, str]], master_root: Path) -> Path:
    key = normalize_name(strip_access_prefix(access_video.stem))
    row = name_map.get(key)
    if row is None:
        raise KeyError(f"No master mapping found for Access video {access_video.name} with key {key!r}")
    master = master_root / f"{row['original_stem']}.mkv"
    if not master.exists():
        raise FileNotFoundError(f"Mapped master does not exist for {access_video.name}: {master}")
    return master


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


def blend_filter(input_label: str, filtergraph: str, output_label: str, strength: float) -> str:
    original = f"{output_label}_orig"
    work = f"{output_label}_work"
    filtered = f"{output_label}_filt"
    return (
        f"[{input_label}]split=2[{original}][{work}];"
        f"[{work}]{filtergraph}[{filtered}];"
        f"[{original}][{filtered}]blend=all_expr='{1.0 - strength:.6f}*A+{strength:.6f}*B'[{output_label}]"
    )


def output_args(label: str, path: Path) -> list[str | Path]:
    return [
        "-map",
        f"[{label}]",
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
        path,
    ]


def render_clip(
    master: Path,
    out_dir: Path,
    index: int,
    start: float,
    clip_seconds: float,
    expt11_best: str,
    force: bool,
) -> tuple[float, dict[str, Path]]:
    outputs = {
        "ctrl": out_dir / f"clip_{index:03d}_ctrl.mkv",
        "previous_50pct": out_dir / f"clip_{index:03d}_previous_50pct.mkv",
        "expt11_best": out_dir / f"clip_{index:03d}_expt11_best.mkv",
        "expt11_best_50pct": out_dir / f"clip_{index:03d}_expt11_best_50pct.mkv",
    }
    if not force and all(path.exists() for path in outputs.values()):
        return 0.0, outputs

    out_dir.mkdir(parents=True, exist_ok=True)
    filter_complex = ";".join(
        [
            f"[0:v]trim=duration={clip_seconds:.6f},setpts=PTS-STARTPTS,split=4[ctrl][prev50_in][best_in][best50_in]",
            blend_filter("prev50_in", PREVIOUS_FILTERGRAPH, "previous_50pct", 0.5),
            f"[best_in]{expt11_best}[expt11_best]",
            blend_filter("best50_in", expt11_best, "expt11_best_50pct", 0.5),
        ]
    )
    cmd: list[str | Path] = [
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
        *output_args("ctrl", outputs["ctrl"]),
        *output_args("previous_50pct", outputs["previous_50pct"]),
        *output_args("expt11_best", outputs["expt11_best"]),
        *output_args("expt11_best_50pct", outputs["expt11_best_50pct"]),
    ]
    started = time.perf_counter()
    run(cmd)
    return time.perf_counter() - started, outputs


def write_manifest(rows: list[dict[str, object]], out_root: Path) -> None:
    out_root.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "access_video",
        "master",
        "clip",
        "start_seconds",
        "ctrl",
        "previous_50pct",
        "expt11_best",
        "expt11_best_50pct",
        "elapsed_seconds",
    ]
    with (out_root / "access_master_clip_manifest.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master-root", type=Path, default=MASTER_ROOT)
    parser.add_argument("--access-root", type=Path, default=ACCESS_ROOT)
    parser.add_argument("--access-name-map", type=Path, default=ACCESS_NAME_MAP)
    parser.add_argument("--out-root", type=Path, default=OUT_ROOT)
    parser.add_argument("--expt11-best", type=Path, default=EXPT11_BEST)
    parser.add_argument("--clips-per-access-video", type=int, default=12)
    parser.add_argument("--clip-seconds", type=float, default=10.0)
    parser.add_argument("--limit", type=int, help="Limit number of Access videos for testing.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing outputs.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    expt11_best = args.expt11_best.read_text().strip()
    if not expt11_best:
        raise ValueError(f"Empty expt11 best filtergraph: {args.expt11_best}")

    name_map = read_name_map(args.access_name_map)
    access_videos = access_inputs(args.access_root)
    if args.limit is not None:
        access_videos = access_videos[: args.limit]

    rows: list[dict[str, object]] = []
    for access_video in access_videos:
        master = master_for_access(access_video, name_map, args.master_root)
        duration = probe_duration(master)
        out_dir = args.out_root / safe_name(access_video.stem)
        for index, start in enumerate(sample_starts(duration, args.clips_per_access_video, args.clip_seconds), start=1):
            print(f"{access_video.name} -> {master.name} clip_{index:03d} start={start:.3f}", flush=True)
            elapsed, outputs = render_clip(master, out_dir, index, start, args.clip_seconds, expt11_best, args.force)
            rows.append(
                {
                    "access_video": access_video,
                    "master": master,
                    "clip": f"clip_{index:03d}",
                    "start_seconds": f"{start:.6f}",
                    "ctrl": outputs["ctrl"],
                    "previous_50pct": outputs["previous_50pct"],
                    "expt11_best": outputs["expt11_best"],
                    "expt11_best_50pct": outputs["expt11_best_50pct"],
                    "elapsed_seconds": f"{elapsed:.3f}",
                }
            )
            write_manifest(rows, args.out_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
