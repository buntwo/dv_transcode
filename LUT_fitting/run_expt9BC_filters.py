#!/usr/bin/env python3
"""Run part B/C color-correction filter experiments on fixed expt9A bases."""

from __future__ import annotations

import argparse
import csv
import re
import subprocess
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from evaluate_luts import collect_samples, compute_metrics, extract_frames, load_frames, read_manifest
from generate_lut_review_sheets import calculate_sample_times, display_aspect_ratio, format_duration, probe_metadata
from run_expt9_luma_only import ACCESS_ROOT, VALIDATION_PAIRS, access_inputs


OUT_ROOT = Path("generated_video_pairs/evaluations/expt9BC_color_filters")
BEST_LUT = Path("LUTs/vhs_to_video8_standard_i_native_1M_random_seed2002_strength85.cube")
GOPT_LUT = Path("generated_video_pairs/evaluations/expt9A_gamma_grid/luts/expt9a_luma_g68.cube")
CP_GAMMAOPT_LUT = Path("generated_video_pairs/evaluations/expt9A_cp_gammaopt/luts/expt9a_luma_cp_gammaopt.cube")


@dataclass(frozen=True)
class Candidate:
    label: str
    part: str
    base: str
    vf: str
    note: str


def run(cmd: list[str | Path]) -> None:
    subprocess.run([str(part) for part in cmd], check=True)


def lut_filter(lut: Path) -> str:
    return f"format=gbrp,lut3d={lut}:interp=tetrahedral,format=rgb24"


def suffix_filter(base_lut: Path, suffix: str) -> str:
    if not suffix:
        return lut_filter(base_lut)
    return f"{lut_filter(base_lut)},{suffix},format=rgb24"


def safe_label(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")


def fixed_base_luts() -> dict[str, Path]:
    return {
        "g_opt": GOPT_LUT,
        "cp_gammaopt": CP_GAMMAOPT_LUT,
    }


def manual_suffixes() -> list[tuple[str, str, str]]:
    return [
        (
            "cb_cool_mild",
            "colorbalance=rs=-0.010:gs=-0.005:bs=0.025:rm=-0.010:gm=-0.005:bm=0.025:pl=1",
            "manual colorbalance, mild blue/cool push with lightness preserve",
        ),
        (
            "cb_cool_med",
            "colorbalance=rs=-0.020:gs=-0.010:bs=0.045:rm=-0.018:gm=-0.008:bm=0.040:pl=1",
            "manual colorbalance, medium blue/cool push with lightness preserve",
        ),
        (
            "cb_cool_strong",
            "colorbalance=rs=-0.030:gs=-0.015:bs=0.065:rm=-0.025:gm=-0.012:bm=0.055:rh=-0.005:bh=0.010:pl=1",
            "manual colorbalance, strong blue/cool push with lightness preserve",
        ),
        (
            "cb_blue_only",
            "colorbalance=bs=0.040:bm=0.035:bh=0.005:pl=1",
            "manual colorbalance, blue-only lift",
        ),
        (
            "cc_mild",
            "colorcorrect=rl=-0.010:bl=0.020:rh=-0.005:bh=0.010:saturation=1.00",
            "manual colorcorrect, mild red down / blue up",
        ),
        (
            "cc_med",
            "colorcorrect=rl=-0.020:bl=0.040:rh=-0.010:bh=0.020:saturation=1.00",
            "manual colorcorrect, medium red down / blue up",
        ),
        (
            "cc_med_sat",
            "colorcorrect=rl=-0.020:bl=0.040:rh=-0.010:bh=0.020:saturation=0.95",
            "manual colorcorrect plus slight desaturation",
        ),
        (
            "hue_desat",
            "hue=s=0.94",
            "manual hue saturation reduction",
        ),
        (
            "vibrance_down",
            "vibrance=intensity=-0.12",
            "manual vibrance reduction",
        ),
    ]


def auto_suffixes() -> list[tuple[str, str, str]]:
    return [
        ("grayworld", "grayworld", "LAB gray-world white balance"),
        ("greyedge_d0_n1_s1", "greyedge=difford=0:minknorm=1:sigma=1", "grey-edge difford 0 norm 1 sigma 1"),
        ("greyedge_d1_n1_s1", "greyedge=difford=1:minknorm=1:sigma=1", "grey-edge difford 1 norm 1 sigma 1"),
        ("greyedge_d1_n5_s1", "greyedge=difford=1:minknorm=5:sigma=1", "grey-edge difford 1 norm 5 sigma 1"),
        ("greyedge_d2_n5_s1", "greyedge=difford=2:minknorm=5:sigma=1", "grey-edge difford 2 norm 5 sigma 1"),
        ("cc_avg", "colorcorrect=analyze=average:saturation=1.00", "auto colorcorrect average"),
        ("cc_med", "colorcorrect=analyze=median:saturation=1.00", "auto colorcorrect median"),
        ("cc_med_sat", "colorcorrect=analyze=median:saturation=0.95", "auto colorcorrect median with slight desaturation"),
        ("cc_minmax", "colorcorrect=analyze=minmax:saturation=1.00", "auto colorcorrect minmax"),
    ]


def make_candidates() -> list[Candidate]:
    candidates = []
    for base, lut in fixed_base_luts().items():
        candidates.append(Candidate(label=base, part="A", base=base, vf=suffix_filter(lut, ""), note="fixed A baseline"))
        for label, suffix, note in manual_suffixes():
            candidates.append(
                Candidate(label=f"{base}_B_{label}", part="B", base=base, vf=suffix_filter(lut, suffix), note=note)
            )
        for label, suffix, note in auto_suffixes():
            candidates.append(
                Candidate(label=f"{base}_C_{label}", part="C", base=base, vf=suffix_filter(lut, suffix), note=note)
            )
    candidates.append(Candidate(label="BEST", part="reference", base="BEST", vf=lut_filter(BEST_LUT), note="previous best LUT"))
    return candidates


def extract_frames_filter(video: Path, out_dir: Path, fps: float, width: int | None, height: int | None, vf: str) -> None:
    out_pattern = str(out_dir / "frame_%05d.png")
    filters = []
    if width is not None or height is not None:
        if width is None or height is None:
            raise ValueError("--sample-width and --sample-height must be provided together")
        filters.append(f"scale={width}:{height}:flags=bicubic")
    filters.append(vf)
    filters.append("format=rgb24")
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            video,
            "-vf",
            f"fps={fps}," + ",".join(filters),
            "-vsync",
            "0",
            out_pattern,
        ]
    )


def evaluate_candidates(args: argparse.Namespace, candidates: list[Candidate]) -> Path:
    pairs = read_manifest(args.validation_pairs)
    masks = {"mask_left": 6, "mask_right": 6, "mask_top": 6, "mask_bottom": 20}
    rows = []
    out_csv = args.out_root / "evaluation" / "validation_metrics.csv"

    with tempfile.TemporaryDirectory(prefix="expt9bc-eval-") as tmp_name:
        tmp = Path(tmp_name)
        for pair_index, (ref_video, src_video) in enumerate(pairs, start=1):
            pair_label = ref_video.stem.replace("_A", "")
            print(f"Evaluating {pair_label}", flush=True)
            ref_dir = tmp / f"{pair_index:03d}_ref"
            raw_dir = tmp / f"{pair_index:03d}_raw"
            ref_dir.mkdir()
            raw_dir.mkdir()
            extract_frames(ref_video, ref_dir, args.fps, args.sample_width, args.sample_height)
            extract_frames(src_video, raw_dir, args.fps, args.sample_width, args.sample_height)
            ref_frames = load_frames(ref_dir)
            raw_frames = load_frames(raw_dir)

            ref_samples, raw_samples, raw_test_samples = collect_samples(ref_frames, raw_frames, raw_frames, masks)
            rows.append({"pair": pair_label, "candidate": "raw", "part": "raw", "base": "raw", **compute_metrics(ref_samples, raw_test_samples, raw_samples)})

            for candidate in candidates:
                cand_dir = tmp / f"{pair_index:03d}_{safe_label(candidate.label)}"
                cand_dir.mkdir()
                extract_frames_filter(src_video, cand_dir, args.fps, args.sample_width, args.sample_height, candidate.vf)
                cand_frames = load_frames(cand_dir)
                ref_samples, raw_samples, cand_samples = collect_samples(ref_frames, raw_frames, cand_frames, masks)
                rows.append(
                    {
                        "pair": pair_label,
                        "candidate": candidate.label,
                        "part": candidate.part,
                        "base": candidate.base,
                        **compute_metrics(ref_samples, cand_samples, raw_samples),
                    }
                )

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "pair",
        "candidate",
        "part",
        "base",
        "sample_count",
        "rgb_mae",
        "rgb_rmse",
        "luma_mae",
        "delta_e76_mean",
        "delta_e76_p95",
        "shadow_luma_lift",
        "mid_luma_lift",
        "high_luma_lift",
        "mid_luma_bias",
        "high_luma_bias",
        "nonshadow_positive_luma_bias",
        "nonwarm_mid_high_luma_bias",
        "nonshadow_luma_over_p95",
        "clip_pct",
        "new_clip_pct",
        "warm_yellow_delta_e76_mean",
        "warm_yellow_luma_bias",
    ]
    with out_csv.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return out_csv


def to_float(row: dict[str, str], key: str) -> float:
    value = row.get(key, "")
    if value == "" or value.lower() == "nan":
        return float("nan")
    return float(value)


def summarize(metrics_csv: Path, candidates: list[Candidate], out_csv: Path) -> list[dict[str, object]]:
    metadata = {candidate.label: candidate for candidate in candidates}
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    with metrics_csv.open() as f:
        for row in csv.DictReader(f):
            grouped[row["candidate"]].append(row)

    metric_fields = [
        "rgb_mae",
        "delta_e76_mean",
        "luma_mae",
        "shadow_luma_lift",
        "mid_luma_lift",
        "high_luma_lift",
        "nonshadow_positive_luma_bias",
        "new_clip_pct",
        "warm_yellow_delta_e76_mean",
        "warm_yellow_luma_bias",
    ]
    rows = []
    for candidate_label, candidate_rows in grouped.items():
        candidate = metadata.get(candidate_label)
        part = candidate.part if candidate else candidate_rows[0]["part"]
        base = candidate.base if candidate else candidate_rows[0]["base"]
        row: dict[str, object] = {
            "candidate": candidate_label,
            "part": part,
            "base": base,
            "note": candidate.note if candidate else "",
        }
        for field in metric_fields:
            values = [to_float(candidate_row, field) for candidate_row in candidate_rows]
            finite = [value for value in values if np.isfinite(value)]
            row[field] = float(np.mean(finite)) if finite else float("nan")
        row["tone_score"] = (
            float(row["delta_e76_mean"])
            + 0.05 * float(row["nonshadow_positive_luma_bias"])
            + 0.25 * float(row["new_clip_pct"])
        )
        rows.append(row)

    rows.sort(key=lambda row: (row["part"], row["tone_score"], row["delta_e76_mean"]))
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["candidate", "part", "base", "note", *metric_fields, "tone_score"]
    with out_csv.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def select_winners(summary_rows: list[dict[str, object]]) -> dict[tuple[str, str], dict[str, object]]:
    winners = {}
    for part in ("B", "C"):
        for base in ("g_opt", "cp_gammaopt"):
            candidates = [row for row in summary_rows if row["part"] == part and row["base"] == base]
            candidates.sort(key=lambda row: (row["tone_score"], row["delta_e76_mean"]))
            winners[(part, base)] = candidates[0]
    return winners


def write_candidates_manifest(candidates: list[Candidate], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["label", "part", "base", "vf", "note"])
        writer.writeheader()
        for candidate in candidates:
            writer.writerow(candidate.__dict__)


def draw_label(image: Image.Image, label: str, timestamp: str) -> Image.Image:
    out = image.convert("RGB")
    draw = ImageDraw.Draw(out)
    font = ImageFont.load_default()
    for text, xy, anchor in [(label, (6, 6), "left"), (timestamp, (out.width - 6, out.height - 18), "right")]:
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        x, y = xy
        if anchor == "right":
            x = x - tw
        draw.rectangle((x - 4, y - 3, x + tw + 4, y + th + 3), fill=(0, 0, 0))
        draw.text((x, y), text, fill=(255, 255, 255), font=font)
    return out


def make_frame(input_path: Path, output_path: Path, time_s: float, tile_width: int, tile_height: int, vf: str | None, label: str) -> None:
    filters = []
    if vf:
        filters.append(vf)
    filters.extend(
        [
            f"scale={tile_width}:{tile_height}:force_original_aspect_ratio=decrease",
            f"pad={tile_width}:{tile_height}:(ow-iw)/2:(oh-ih)/2:white",
            "format=rgb24",
        ]
    )
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{time_s:.6f}",
            "-i",
            input_path,
            "-vf",
            ",".join(filters),
            "-frames:v",
            "1",
            output_path,
        ]
    )
    image = Image.open(output_path)
    draw_label(image, label, format_duration(time_s)).save(output_path)


def compose_grid(frame_paths: list[Path], rows: int, cols: int, tile_width: int, tile_height: int, output_path: Path, title: str) -> None:
    margin = 20
    padding = 5
    header_h = 70
    width = cols * tile_width + 2 * margin + (cols - 1) * padding
    height = header_h + rows * tile_height + 2 * margin + (rows - 1) * padding
    sheet = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    draw.text((margin, 18), title, fill=(0, 0, 0), font=font)
    for index, frame_path in enumerate(frame_paths):
        row = index // cols
        col = index % cols
        x = margin + col * (tile_width + padding)
        y = header_h + margin + row * (tile_height + padding)
        sheet.paste(Image.open(frame_path).convert("RGB"), (x, y))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path)


def tile_size(video: Path, tile_width: int) -> tuple[int, int]:
    metadata = probe_metadata(video)
    width_ratio, height_ratio = display_aspect_ratio(metadata)
    return tile_width, round(tile_width * height_ratio / width_ratio)


def access_grid(args: argparse.Namespace, part: str, variants: list[tuple[str, str | None]], out_dir: Path) -> None:
    videos = access_inputs(args.access_root)
    with tempfile.TemporaryDirectory(prefix=f"expt9bc-{part}-access-") as tmp_name:
        tmp = Path(tmp_name)
        for video in videos:
            metadata = probe_metadata(video)
            tile_width, tile_height = tile_size(video, args.grid_tile_width)
            frame_paths = []
            for row_index, time_s in enumerate(calculate_sample_times(metadata.duration_seconds, 12), start=1):
                for label, vf in variants:
                    frame_path = tmp / f"{safe_label(video.stem)}_r{row_index:03d}_{safe_label(label)}.png"
                    make_frame(video, frame_path, time_s, tile_width, tile_height, vf, label)
                    frame_paths.append(frame_path)
            compose_grid(
                frame_paths,
                rows=12,
                cols=len(variants),
                tile_width=tile_width,
                tile_height=tile_height,
                output_path=out_dir / f"{video.name}.filter_grid.png",
                title=f"Expt9{part} columns: {', '.join(label for label, _ in variants)}",
            )


def validation_grid(args: argparse.Namespace, part: str, variants: list[tuple[str, str | None]], out_dir: Path) -> None:
    pairs = read_manifest(args.validation_pairs)
    with tempfile.TemporaryDirectory(prefix=f"expt9bc-{part}-validation-") as tmp_name:
        tmp = Path(tmp_name)
        for index, (ref_video, src_video) in enumerate(pairs, start=1):
            metadata = probe_metadata(src_video)
            tile_width, tile_height = tile_size(src_video, args.grid_tile_width)
            frame_paths = []
            duration = min(metadata.duration_seconds, probe_metadata(ref_video).duration_seconds)
            for row_index, time_s in enumerate(calculate_sample_times(duration, 3), start=1):
                src_path = tmp / f"pair{index:03d}_r{row_index:03d}_vhs.png"
                ref_path = tmp / f"pair{index:03d}_r{row_index:03d}_video8.png"
                make_frame(src_video, src_path, time_s, tile_width, tile_height, None, "VHS")
                make_frame(ref_video, ref_path, time_s, tile_width, tile_height, None, "Video8")
                frame_paths.extend([src_path, ref_path])
                for label, vf in variants:
                    frame_path = tmp / f"pair{index:03d}_r{row_index:03d}_{safe_label(label)}.png"
                    make_frame(src_video, frame_path, time_s, tile_width, tile_height, vf, label)
                    frame_paths.append(frame_path)
            compose_grid(
                frame_paths,
                rows=3,
                cols=2 + len(variants),
                tile_width=tile_width,
                tile_height=tile_height,
                output_path=out_dir / f"pair_{index:03d}.filter_grid.png",
                title=f"Expt9{part} columns: VHS, Video8, {', '.join(label for label, _ in variants)}",
            )


def make_review_grids(args: argparse.Namespace, candidates: list[Candidate], winners: dict[tuple[str, str], dict[str, object]]) -> None:
    by_label = {candidate.label: candidate for candidate in candidates}
    common = [
        ("BEST", lut_filter(BEST_LUT)),
        ("g_opt", lut_filter(GOPT_LUT)),
        ("cp_gammaopt", lut_filter(CP_GAMMAOPT_LUT)),
    ]
    for part in ("B", "C"):
        variants = list(common)
        for base in ("g_opt", "cp_gammaopt"):
            winner_label = str(winners[(part, base)]["candidate"])
            variants.append((winner_label, by_label[winner_label].vf))
        access_grid(args, part, variants, args.out_root / f"expt9{part}_access_grid_winners")
        validation_grid(args, part, variants, args.out_root / f"expt9{part}_validation_grid_winners")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-root", type=Path, default=OUT_ROOT)
    parser.add_argument("--validation-pairs", type=Path, default=VALIDATION_PAIRS)
    parser.add_argument("--access-root", type=Path, default=ACCESS_ROOT)
    parser.add_argument("--fps", type=float, default=2.0)
    parser.add_argument("--sample-width", type=int)
    parser.add_argument("--sample-height", type=int)
    parser.add_argument("--grid-tile-width", type=int, default=260)
    parser.add_argument("--skip-eval", action="store_true")
    parser.add_argument("--skip-grids", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    candidates = make_candidates()
    write_candidates_manifest(candidates, args.out_root / "candidate_manifest.csv")
    metrics_csv = args.out_root / "evaluation" / "validation_metrics.csv"
    if not args.skip_eval:
        metrics_csv = evaluate_candidates(args, candidates)
    summary_rows = summarize(metrics_csv, candidates, args.out_root / "experiment_summary.csv")
    winners = select_winners(summary_rows)
    with (args.out_root / "winners.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["part", "base", "candidate", "tone_score", "delta_e76_mean", "rgb_mae", "note"])
        writer.writeheader()
        for (part, base), row in winners.items():
            writer.writerow(
                {
                    "part": part,
                    "base": base,
                    "candidate": row["candidate"],
                    "tone_score": row["tone_score"],
                    "delta_e76_mean": row["delta_e76_mean"],
                    "rgb_mae": row["rgb_mae"],
                    "note": row["note"],
                }
            )
    if not args.skip_grids:
        make_review_grids(args, candidates, winners)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
