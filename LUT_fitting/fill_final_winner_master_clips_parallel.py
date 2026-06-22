#!/usr/bin/env python3
"""Parallel-fill final-winner master clips after collecting sequential timing data."""

from __future__ import annotations

import argparse
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from generate_final_winner_master_clips import (
    FILTERGRAPH,
    MASTER_ROOT,
    OUT_ROOT,
    check_free_space,
    discover_masters,
    elapsed_text,
    pct_text,
    probe_duration,
    read_filtergraph,
    render_one,
    sample_starts,
    size_bytes,
    speed_text,
)


def completed_from_timing(path: Path) -> set[tuple[str, str]]:
    completed: set[tuple[str, str]] = set()
    if not path.exists():
        return completed
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            ctrl = Path(row["ctrl"])
            winner = Path(row["winner"])
            if ctrl.exists() and winner.exists():
                completed.add((Path(row["master"]).stem, row["clip"]))
    return completed


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
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
        "parallel_jobs",
    ]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def render_task(task: dict[str, object]) -> dict[str, object]:
    master = Path(task["master"])
    start = float(task["start"])
    clip_seconds = float(task["clip_seconds"])
    ctrl_vf = str(task["ctrl_vf"])
    winner_vf = str(task["winner_vf"])
    ctrl = Path(task["ctrl"])
    winner = Path(task["winner"])
    min_free_gb = float(task["min_free_gb"])
    jobs = int(task["jobs"])

    check_free_space(ctrl.parent, min_free_gb)
    ctrl_elapsed = render_one(master, start, clip_seconds, ctrl_vf, ctrl, force=True)
    check_free_space(winner.parent, min_free_gb)
    winner_elapsed = render_one(master, start, clip_seconds, winner_vf, winner, force=True)

    overhead = ""
    if ctrl_elapsed is not None and winner_elapsed is not None:
        overhead = f"{winner_elapsed - ctrl_elapsed:.3f}"
    return {
        "master": master,
        "clip": task["clip"],
        "start_seconds": f"{start:.6f}",
        "clip_seconds": f"{clip_seconds:.6f}",
        "ctrl": ctrl,
        "winner": winner,
        "ctrl_elapsed_seconds": elapsed_text(ctrl_elapsed),
        "winner_elapsed_seconds": elapsed_text(winner_elapsed),
        "filter_overhead_seconds": overhead,
        "filter_overhead_pct": pct_text(ctrl_elapsed, winner_elapsed),
        "ctrl_realtime_speed": speed_text(clip_seconds, ctrl_elapsed),
        "winner_realtime_speed": speed_text(clip_seconds, winner_elapsed),
        "ctrl_bytes": size_bytes(ctrl),
        "winner_bytes": size_bytes(winner),
        "parallel_jobs": jobs,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master-root", type=Path, default=MASTER_ROOT)
    parser.add_argument("--out-root", type=Path, default=OUT_ROOT)
    parser.add_argument("--filtergraph", type=Path, default=FILTERGRAPH)
    parser.add_argument("--clips-per-master", type=int, default=12)
    parser.add_argument("--clip-seconds", type=float, default=10.0)
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--limit", type=int, help="Limit number of masters for testing.")
    parser.add_argument("--min-free-gb", type=float, default=20.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.jobs < 1:
        raise ValueError("--jobs must be at least 1")

    winner_graph = read_filtergraph(args.filtergraph)
    ctrl_vf = "setpts=PTS-STARTPTS"
    winner_vf = f"setpts=PTS-STARTPTS,{winner_graph}"
    timing_csv = args.out_root / "master_clip_timing.csv"
    fill_csv = args.out_root / "master_clip_parallel_fill_timing.csv"
    completed = completed_from_timing(timing_csv)

    masters = discover_masters(args.master_root)
    if args.limit is not None:
        masters = masters[: args.limit]
    tasks: list[dict[str, object]] = []
    for master in masters:
        duration = probe_duration(master)
        out_dir = args.out_root / master.stem
        for index, start in enumerate(sample_starts(duration, args.clips_per_master, args.clip_seconds), start=1):
            clip = f"clip_{index:03d}"
            if (master.stem, clip) in completed:
                continue
            tasks.append(
                {
                    "master": master,
                    "clip": clip,
                    "start": start,
                    "clip_seconds": args.clip_seconds,
                    "ctrl_vf": ctrl_vf,
                    "winner_vf": winner_vf,
                    "ctrl": out_dir / f"{clip}_ctrl.mkv",
                    "winner": out_dir / f"{clip}_winner.mkv",
                    "min_free_gb": args.min_free_gb,
                    "jobs": args.jobs,
                }
            )

    print(f"Completed from sequential timing: {len(completed)}")
    print(f"Parallel fill tasks: {len(tasks)} with jobs={args.jobs}", flush=True)
    rows: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(render_task, task): task for task in tasks}
        for future in as_completed(futures):
            row = future.result()
            rows.append(row)
            rows.sort(key=lambda item: (Path(str(item["master"])).name, str(item["clip"])))
            write_csv(fill_csv, rows)
            print(f"filled {Path(str(row['master'])).name} {row['clip']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
