#!/usr/bin/env python3
"""Generate final-winner review clips for the train/validation pair datasets."""

from __future__ import annotations

import argparse
import csv
import shutil
import subprocess
import time
from pathlib import Path


OUT_ROOT = Path("generated_video_pairs/evaluations/final_visual_winner/transformed_videos/pairs")
FILTERGRAPH = Path("LUTs/vhs_to_video8_previous50_visual_winner.ffmpeg-filtergraph")
MANIFESTS = {
    "train": Path("generated_video_pairs/train_geometry_normalized_pairs.txt"),
    "validation": Path("generated_video_pairs/validation_geometry_normalized_pairs.txt"),
}


def run(cmd: list[str | Path]) -> None:
    subprocess.run([str(part) for part in cmd], check=True)


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


def read_manifest(path: Path) -> list[tuple[Path, Path]]:
    pairs: list[tuple[Path, Path]] = []
    with path.open() as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            left, right = stripped.split("|", 1)
            pairs.append((Path(left), Path(right)))
    return pairs


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
            f"Stopping before next pair: only {free_gb:.1f} GB free under {nearest_existing_parent(path)} "
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


def render_one(source: Path, vf: str, output: Path, force: bool) -> float | None:
    if output.exists() and not force:
        return None
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd: list[str | Path] = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        source,
        "-vf",
        vf,
        *output_args(output),
    ]
    started = time.perf_counter()
    run(cmd)
    return time.perf_counter() - started


def size_bytes(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


def elapsed_text(value: float | None) -> str:
    return "" if value is None else f"{value:.3f}"


def pct_text(ctrl_elapsed: float | None, winner_elapsed: float | None) -> str:
    if ctrl_elapsed is None or winner_elapsed is None or ctrl_elapsed <= 0:
        return ""
    return f"{(winner_elapsed / ctrl_elapsed - 1.0) * 100.0:.1f}"


def write_rows(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "split",
        "pair",
        "video8_source",
        "ctrl_source",
        "ctrl",
        "winner",
        "video8",
        "ctrl_elapsed_seconds",
        "winner_elapsed_seconds",
        "video8_elapsed_seconds",
        "filter_overhead_seconds",
        "filter_overhead_pct",
        "ctrl_bytes",
        "winner_bytes",
        "video8_bytes",
    ]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-root", type=Path, default=OUT_ROOT)
    parser.add_argument("--filtergraph", type=Path, default=FILTERGRAPH)
    parser.add_argument("--train-manifest", type=Path, default=MANIFESTS["train"])
    parser.add_argument("--validation-manifest", type=Path, default=MANIFESTS["validation"])
    parser.add_argument("--limit-pairs", type=int, help="Limit pairs per split for testing.")
    parser.add_argument("--force", action="store_true", help="Overwrite existing outputs and remeasure timings.")
    parser.add_argument("--min-free-gb", type=float, default=20.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    winner_graph = read_filtergraph(args.filtergraph)
    ctrl_vf = "setpts=PTS-STARTPTS"
    winner_vf = f"setpts=PTS-STARTPTS,{winner_graph}"

    manifests = {"train": args.train_manifest, "validation": args.validation_manifest}
    rows: list[dict[str, object]] = []
    timing_csv = args.out_root / "pair_clip_timing.csv"
    for split, manifest in manifests.items():
        pairs = read_manifest(manifest)
        if args.limit_pairs is not None:
            pairs = pairs[: args.limit_pairs]
        for index, (video8_source, ctrl_source) in enumerate(pairs, start=1):
            pair = f"pair_{index:03d}"
            out_dir = args.out_root / split
            ctrl = out_dir / f"{pair}_ctrl.mkv"
            winner = out_dir / f"{pair}_winner.mkv"
            video8 = out_dir / f"{pair}_video8.mkv"
            print(f"{split} {pair}", flush=True)

            check_free_space(args.out_root, args.min_free_gb)
            ctrl_elapsed = render_one(ctrl_source, ctrl_vf, ctrl, args.force)
            check_free_space(args.out_root, args.min_free_gb)
            winner_elapsed = render_one(ctrl_source, winner_vf, winner, args.force)
            check_free_space(args.out_root, args.min_free_gb)
            video8_elapsed = render_one(video8_source, ctrl_vf, video8, args.force)

            overhead = ""
            if ctrl_elapsed is not None and winner_elapsed is not None:
                overhead = f"{winner_elapsed - ctrl_elapsed:.3f}"
            rows.append(
                {
                    "split": split,
                    "pair": pair,
                    "video8_source": video8_source,
                    "ctrl_source": ctrl_source,
                    "ctrl": ctrl,
                    "winner": winner,
                    "video8": video8,
                    "ctrl_elapsed_seconds": elapsed_text(ctrl_elapsed),
                    "winner_elapsed_seconds": elapsed_text(winner_elapsed),
                    "video8_elapsed_seconds": elapsed_text(video8_elapsed),
                    "filter_overhead_seconds": overhead,
                    "filter_overhead_pct": pct_text(ctrl_elapsed, winner_elapsed),
                    "ctrl_bytes": size_bytes(ctrl),
                    "winner_bytes": size_bytes(winner),
                    "video8_bytes": size_bytes(video8),
                }
            )
            write_rows(timing_csv, rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
