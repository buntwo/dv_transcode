#!/usr/bin/env python3
"""Generate final-winner review clips from every 10-bit tape master.

Each sampled master segment is encoded twice as a separate ffmpeg run:
control first, then the visual-winner filtergraph. That makes the timing CSV
useful for estimating the practical overhead of adding the filtergraph.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import time
from pathlib import Path


MASTER_ROOT = Path("/Volumes/TU/tu.brian.2026.05.09/data/masters/tape")
OUT_ROOT = Path("generated_video_pairs/evaluations/final_visual_winner/transformed_videos/masters")
FILTERGRAPH = Path("LUTs/vhs_to_video8_previous50_visual_winner.ffmpeg-filtergraph")


def run(cmd: list[str | Path]) -> None:
    subprocess.run([str(part) for part in cmd], check=True)


def run_capture(cmd: list[str | Path]) -> bytes:
    return subprocess.check_output([str(part) for part in cmd])


def read_filtergraph(path: Path) -> str:
    lines = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            lines.append(stripped)
    graph = " ".join(lines)
    if not graph:
        raise ValueError(f"Empty filtergraph file: {path}")
    return graph


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


def nearest_existing_parent(path: Path) -> Path:
    current = path
    while not current.exists():
        if current.parent == current:
            raise FileNotFoundError(f"No existing parent found for {path}")
        current = current.parent
    return current


def free_gb_for(path: Path) -> float:
    usage = shutil.disk_usage(nearest_existing_parent(path))
    return usage.free / 1024**3


def check_free_space(path: Path, min_free_gb: float) -> None:
    free_gb = free_gb_for(path)
    if free_gb < min_free_gb:
        raise SystemExit(
            f"Stopping before next clip: only {free_gb:.1f} GB free under {nearest_existing_parent(path)} "
            f"(minimum {min_free_gb:.1f} GB)."
        )


def output_args(path: Path) -> list[str | Path]:
    return [
        "-map",
        "0:v:0",
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


def render_one(master: Path, start: float, clip_seconds: float, vf: str, output: Path, force: bool) -> float | None:
    if output.exists() and not force:
        return None
    output.parent.mkdir(parents=True, exist_ok=True)
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
        "-t",
        f"{clip_seconds:.6f}",
        "-vf",
        vf,
        *output_args(output),
    ]
    started = time.perf_counter()
    run(cmd)
    return time.perf_counter() - started


def size_bytes(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


def write_rows(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "master",
        "clip",
        "start_seconds",
        "clip_seconds",
        "ctrl",
        "winner",
        "ctrl_elapsed_seconds",
        "winner_elapsed_seconds",
        "filter_overhead_seconds",
        "filter_overhead_pct",
        "ctrl_realtime_speed",
        "winner_realtime_speed",
        "ctrl_bytes",
        "winner_bytes",
    ]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def elapsed_text(value: float | None) -> str:
    return "" if value is None else f"{value:.3f}"


def pct_text(ctrl_elapsed: float | None, winner_elapsed: float | None) -> str:
    if ctrl_elapsed is None or winner_elapsed is None or ctrl_elapsed <= 0:
        return ""
    return f"{(winner_elapsed / ctrl_elapsed - 1.0) * 100.0:.1f}"


def speed_text(clip_seconds: float, elapsed: float | None) -> str:
    if elapsed is None or elapsed <= 0:
        return ""
    return f"{clip_seconds / elapsed:.3f}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master-root", type=Path, default=MASTER_ROOT)
    parser.add_argument("--out-root", type=Path, default=OUT_ROOT)
    parser.add_argument("--filtergraph", type=Path, default=FILTERGRAPH)
    parser.add_argument("--clips-per-master", type=int, default=12)
    parser.add_argument("--clip-seconds", type=float, default=10.0)
    parser.add_argument("--limit", type=int, help="Limit number of masters for testing.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing outputs and remeasure timings.")
    parser.add_argument("--min-free-gb", type=float, default=20.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    winner_graph = read_filtergraph(args.filtergraph)
    ctrl_vf = "setpts=PTS-STARTPTS"
    winner_vf = f"setpts=PTS-STARTPTS,{winner_graph}"

    masters = discover_masters(args.master_root)
    if args.limit is not None:
        masters = masters[: args.limit]
    if not masters:
        raise FileNotFoundError(f"No .mkv masters found under {args.master_root}")

    rows: list[dict[str, object]] = []
    timing_csv = args.out_root / "master_clip_timing.csv"
    for master in masters:
        duration = probe_duration(master)
        out_dir = args.out_root / master.stem
        for index, start in enumerate(sample_starts(duration, args.clips_per_master, args.clip_seconds), start=1):
            clip = f"clip_{index:03d}"
            ctrl = out_dir / f"{clip}_ctrl.mkv"
            winner = out_dir / f"{clip}_winner.mkv"
            print(f"{master.name} {clip} start={start:.3f}", flush=True)

            check_free_space(args.out_root, args.min_free_gb)
            ctrl_elapsed = render_one(master, start, args.clip_seconds, ctrl_vf, ctrl, args.force)
            check_free_space(args.out_root, args.min_free_gb)
            winner_elapsed = render_one(master, start, args.clip_seconds, winner_vf, winner, args.force)

            overhead = ""
            if ctrl_elapsed is not None and winner_elapsed is not None:
                overhead = f"{winner_elapsed - ctrl_elapsed:.3f}"
            row = {
                "master": master,
                "clip": clip,
                "start_seconds": f"{start:.6f}",
                "clip_seconds": f"{args.clip_seconds:.6f}",
                "ctrl": ctrl,
                "winner": winner,
                "ctrl_elapsed_seconds": elapsed_text(ctrl_elapsed),
                "winner_elapsed_seconds": elapsed_text(winner_elapsed),
                "filter_overhead_seconds": overhead,
                "filter_overhead_pct": pct_text(ctrl_elapsed, winner_elapsed),
                "ctrl_realtime_speed": speed_text(args.clip_seconds, ctrl_elapsed),
                "winner_realtime_speed": speed_text(args.clip_seconds, winner_elapsed),
                "ctrl_bytes": size_bytes(ctrl),
                "winner_bytes": size_bytes(winner),
            }
            rows.append(row)
            write_rows(timing_csv, rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
