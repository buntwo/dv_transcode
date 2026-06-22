#!/usr/bin/env python3
"""Generate Access-master clips to review denoise placement in the final workflow."""

from __future__ import annotations

import argparse
import csv
import shutil
import subprocess
import time
from pathlib import Path

from generate_expt11_access_master_clips import (
    ACCESS_NAME_MAP,
    ACCESS_PREFIXES,
    ACCESS_ROOT,
    MASTER_ROOT,
    access_inputs,
    master_for_access,
    probe_duration,
    read_name_map,
    safe_name,
    sample_starts,
)
from generate_final_winner_master_clips import read_filtergraph


OUT_ROOT = Path("generated_video_pairs/evaluations/expt12_denoise_workflow_review/transformed_videos/access_master_clips")
FILTERGRAPH = Path("LUTs/vhs_to_video8_previous50_visual_winner.ffmpeg-filtergraph")

PRE = [
    "bwdif=mode=send_field:parity=auto:deint=all",
    "drawbox=x=0:y=0:w=iw:h=3:color=black:t=fill",
    "drawbox=x=0:y=ih-12:w=iw:h=12:color=black:t=fill",
]
DENOISE = "hqdn3d=1.5:1.125:2.25:1.6875"
POST_DEFAULT = [
    "scale=trunc(ih*dar/2)*2:ih",
    "setsar=1",
    "setparams=range=limited",
    "format=yuv420p10le",
]
POST_LANCZOS = [
    "scale=trunc(ih*dar/2)*2:ih:flags=lanczos+accurate_rnd+full_chroma_int",
    "setsar=1",
    "setparams=range=limited",
    "format=yuv420p10le",
]


def run(cmd: list[str | Path]) -> None:
    subprocess.run([str(part) for part in cmd], check=True)


def nearest_existing_parent(path: Path) -> Path:
    current = path
    while not current.exists():
        if current.parent == current:
            raise FileNotFoundError(f"No existing parent found for {path}")
        current = current.parent
    return current


def check_free_space(path: Path, min_free_gb: float) -> None:
    usage = shutil.disk_usage(nearest_existing_parent(path))
    free_gb = usage.free / 1024**3
    if free_gb < min_free_gb:
        raise SystemExit(f"Stopping: only {free_gb:.1f} GB free under {nearest_existing_parent(path)}")


def chain(parts: list[str]) -> str:
    return ",".join(part for part in parts if part)


def variants(winner_graph: str) -> dict[str, str]:
    return {
        "ctrl": chain([*PRE, DENOISE, *POST_DEFAULT]),
        "with_denoise": chain([*PRE, DENOISE, winner_graph, *POST_DEFAULT]),
        "no_denoise": chain([*PRE, winner_graph, *POST_DEFAULT]),
        "with_denoise_lanczos": chain([*PRE, DENOISE, winner_graph, *POST_LANCZOS]),
    }


def render_clip(master: Path, start: float, clip_seconds: float, vf: str, output: Path, force: bool) -> float | None:
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
        "-map",
        "0:v:0",
        "-an",
        "-c:v",
        "ffv1",
        "-pix_fmt",
        "yuv420p10le",
        "-level",
        "3",
        "-g",
        "1",
        "-slicecrc",
        "1",
        output,
    ]
    started = time.perf_counter()
    run(cmd)
    return time.perf_counter() - started


def write_manifest(rows: list[dict[str, object]], out_root: Path) -> None:
    fieldnames = [
        "access_video",
        "master",
        "clip",
        "start_seconds",
        "clip_seconds",
        "variant",
        "output",
        "elapsed_seconds",
    ]
    out_root.mkdir(parents=True, exist_ok=True)
    with (out_root / "denoise_review_manifest.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def elapsed_text(value: float | None) -> str:
    return "" if value is None else f"{value:.3f}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master-root", type=Path, default=MASTER_ROOT)
    parser.add_argument("--access-root", type=Path, default=ACCESS_ROOT)
    parser.add_argument("--access-name-map", type=Path, default=ACCESS_NAME_MAP)
    parser.add_argument("--out-root", type=Path, default=OUT_ROOT)
    parser.add_argument("--filtergraph", type=Path, default=FILTERGRAPH)
    parser.add_argument("--clips-per-access-video", type=int, default=3)
    parser.add_argument("--clip-seconds", type=float, default=10.0)
    parser.add_argument("--limit", type=int, help="Limit Access videos for testing.")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--min-free-gb", type=float, default=20.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    winner_graph = read_filtergraph(args.filtergraph)
    review_variants = variants(winner_graph)
    name_map = read_name_map(args.access_name_map)
    access_videos = access_inputs(args.access_root)
    if args.limit is not None:
        access_videos = access_videos[: args.limit]

    rows: list[dict[str, object]] = []
    for access_video in access_videos:
        master = master_for_access(access_video, name_map, args.master_root)
        duration = probe_duration(master)
        out_dir = args.out_root / safe_name(access_video.stem)
        starts = sample_starts(duration, args.clips_per_access_video, args.clip_seconds)
        for clip_index, start in enumerate(starts, start=1):
            clip = f"clip_{clip_index:03d}"
            print(f"{access_video.name} -> {master.name} {clip} start={start:.3f}", flush=True)
            for variant, vf in review_variants.items():
                check_free_space(args.out_root, args.min_free_gb)
                output = out_dir / f"{clip}_{variant}.mkv"
                elapsed = render_clip(master, start, args.clip_seconds, vf, output, args.force)
                rows.append(
                    {
                        "access_video": access_video,
                        "master": master,
                        "clip": clip,
                        "start_seconds": f"{start:.6f}",
                        "clip_seconds": f"{args.clip_seconds:.6f}",
                        "variant": variant,
                        "output": output,
                        "elapsed_seconds": elapsed_text(elapsed),
                    }
                )
                write_manifest(rows, args.out_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
