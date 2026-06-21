#!/usr/bin/env python3
import argparse
import csv
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage


def run(cmd):
    subprocess.run(cmd, check=True)


def probe_size(path):
    output = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0:s=x",
            str(path),
        ],
        text=True,
    ).strip()
    width, height = output.split("x")
    return int(width), int(height)


def frame_count(path):
    output = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=nb_read_frames",
            "-of",
            "default=nk=1:nw=1",
            str(path),
        ],
        text=True,
    ).strip()
    return int(output)


def extract_frame(video, frame_index, output_path, width, height):
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video),
            "-vf",
            f"select=eq(n\\,{frame_index}),scale={width}:{height}:flags=bicubic,format=rgb24",
            "-frames:v",
            "1",
            str(output_path),
        ]
    )


def pick_frame_indices(count, requested):
    if count <= 0:
        return []
    if requested <= 1:
        return [count // 2]
    return sorted(
        set(int(round(value)) for value in np.linspace(0, count - 1, requested + 2)[1:-1])
    )


def luma(rgb):
    rgb = rgb.astype(np.float32) / 255.0
    return 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]


def edge_map(rgb):
    y = luma(rgb)
    gx = y[1:-1, 2:] - y[1:-1, :-2]
    gy = y[2:, 1:-1] - y[:-2, 1:-1]
    edge = np.sqrt(gx * gx + gy * gy)
    std = float(edge.std())
    if std < 1e-6:
        return edge - float(edge.mean())
    return (edge - float(edge.mean())) / std


def transform_image(image, sx, sy, dx, dy, order=1):
    height, width = image.shape[:2]
    cy = (height - 1) / 2.0
    cx = (width - 1) / 2.0
    matrix = np.array([[1.0 / sy, 0.0], [0.0, 1.0 / sx]], dtype=np.float32)
    offset = np.array(
        [
            cy - (cy / sy) - (dy / sy),
            cx - (cx / sx) - (dx / sx),
        ],
        dtype=np.float32,
    )
    return ndimage.affine_transform(
        image,
        matrix,
        offset=offset,
        output_shape=image.shape,
        order=order,
        mode="constant",
        cval=0.0,
        prefilter=False,
    )


def crop(arr, left, right, top, bottom):
    height, width = arr.shape[:2]
    x0 = left
    x1 = width - right
    y0 = top
    y1 = height - bottom
    if x1 <= x0 or y1 <= y0:
        raise ValueError("Mask is too aggressive for score size")
    return arr[y0:y1, x0:x1]


def score_edges(a_edge, b_edge, masks, sx, sy, dx, dy):
    moved = transform_image(b_edge, sx, sy, dx, dy, order=1)
    a_crop = crop(a_edge, **masks)
    b_crop = crop(moved, **masks)
    corr = float(np.mean(a_crop * b_crop))
    mae = float(np.mean(np.abs(a_crop - b_crop)))
    return corr, mae


def optimize_frame(a_rgb, b_rgb, masks, coarse_scales, coarse_dx, coarse_dy):
    a_edge = edge_map(a_rgb)
    b_edge = edge_map(b_rgb)

    best = None
    before_corr, before_mae = score_edges(a_edge, b_edge, masks, 1.0, 1.0, 0.0, 0.0)

    for sx in coarse_scales:
        for sy in coarse_scales:
            for dx in coarse_dx:
                for dy in coarse_dy:
                    corr, mae = score_edges(a_edge, b_edge, masks, sx, sy, dx, dy)
                    row = {
                        "sx": sx,
                        "sy": sy,
                        "dx": dx,
                        "dy": dy,
                        "edge_corr": corr,
                        "edge_mae": mae,
                    }
                    if best is None or corr > best["edge_corr"]:
                        best = row

    refine_scales_x = np.linspace(best["sx"] - 0.004, best["sx"] + 0.004, 5)
    refine_scales_y = np.linspace(best["sy"] - 0.004, best["sy"] + 0.004, 5)
    refine_dx = np.linspace(best["dx"] - 1.5, best["dx"] + 1.5, 7)
    refine_dy = np.linspace(best["dy"] - 1.5, best["dy"] + 1.5, 7)
    for sx in refine_scales_x:
        for sy in refine_scales_y:
            for dx in refine_dx:
                for dy in refine_dy:
                    corr, mae = score_edges(a_edge, b_edge, masks, sx, sy, dx, dy)
                    row = {
                        "sx": float(sx),
                        "sy": float(sy),
                        "dx": float(dx),
                        "dy": float(dy),
                        "edge_corr": corr,
                        "edge_mae": mae,
                    }
                    if corr > best["edge_corr"]:
                        best = row

    best["before_edge_corr"] = before_corr
    best["before_edge_mae"] = before_mae
    return best


def ffmpeg_filter_for_transform(target_width, target_height, sx, sy, dx, dy):
    scaled_width = max(2, int(round(target_width * sx / 2.0) * 2))
    scaled_height = max(2, int(round(target_height * sy / 2.0) * 2))

    pad_width = max(target_width, scaled_width)
    pad_height = max(target_height, scaled_height)

    if scaled_width < target_width:
        pad_x = int(round((target_width - scaled_width) / 2.0 + dx))
        crop_x = 0
    else:
        pad_x = 0
        crop_x = int(round((scaled_width - target_width) / 2.0 - dx))

    if scaled_height < target_height:
        pad_y = int(round((target_height - scaled_height) / 2.0 + dy))
        crop_y = 0
    else:
        pad_y = 0
        crop_y = int(round((scaled_height - target_height) / 2.0 - dy))

    pad_x = max(0, min(pad_x, pad_width - scaled_width))
    pad_y = max(0, min(pad_y, pad_height - scaled_height))
    crop_x = max(0, min(crop_x, pad_width - target_width))
    crop_y = max(0, min(crop_y, pad_height - target_height))

    return (
        f"scale={scaled_width}:{scaled_height}:flags=bicubic,"
        f"pad={pad_width}:{pad_height}:{pad_x}:{pad_y}:color=black,"
        f"crop={target_width}:{target_height}:{crop_x}:{crop_y},"
        "setsar=1"
    )


def transform_b_video(input_path, output_path, target_width, target_height, sx, sy, dx, dy):
    vf = ffmpeg_filter_for_transform(target_width, target_height, sx, sy, dx, dy)
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            vf,
            "-c:v",
            "ffv1",
            "-level",
            "3",
            "-g",
            "1",
            "-slicecrc",
            "1",
            str(output_path),
        ]
    )


def parse_pair_files(files):
    if len(files) % 2:
        raise ValueError("Input files must be provided as A/B pairs")
    for index in range(0, len(files), 2):
        yield Path(files[index]), Path(files[index + 1])


def optimize_pair(a_path, b_path, args, tmp):
    a_width, a_height = probe_size(a_path)
    frame_indices = pick_frame_indices(min(frame_count(a_path), frame_count(b_path)), args.frames)
    frame_results = []
    masks = {
        "left": args.mask_left,
        "right": args.mask_right,
        "top": args.mask_top,
        "bottom": args.mask_bottom,
    }
    coarse_scales = np.arange(args.scale_min, args.scale_max + args.scale_step / 2, args.scale_step)
    coarse_dx = np.arange(-args.max_dx, args.max_dx + 0.5, args.shift_step)
    coarse_dy = np.arange(-args.max_dy, args.max_dy + 0.5, args.shift_step)

    for frame_index in frame_indices:
        a_png = tmp / f"{a_path.stem}_{frame_index}_A.png"
        b_png = tmp / f"{b_path.stem}_{frame_index}_B.png"
        extract_frame(a_path, frame_index, a_png, args.score_width, args.score_height)
        extract_frame(b_path, frame_index, b_png, args.score_width, args.score_height)
        a_rgb = np.asarray(Image.open(a_png).convert("RGB"), dtype=np.uint8)
        b_rgb = np.asarray(Image.open(b_png).convert("RGB"), dtype=np.uint8)
        result = optimize_frame(a_rgb, b_rgb, masks, coarse_scales, coarse_dx, coarse_dy)
        result["frame"] = frame_index
        frame_results.append(result)

    sx = float(np.median([row["sx"] for row in frame_results]))
    sy = float(np.median([row["sy"] for row in frame_results]))
    dx_score = float(np.median([row["dx"] for row in frame_results]))
    dy_score = float(np.median([row["dy"] for row in frame_results]))
    dx_full = dx_score * a_width / args.score_width
    dy_full = dy_score * a_height / args.score_height

    before_corr = float(np.median([row["before_edge_corr"] for row in frame_results]))
    after_corr = float(np.median([row["edge_corr"] for row in frame_results]))
    before_mae = float(np.median([row["before_edge_mae"] for row in frame_results]))
    after_mae = float(np.median([row["edge_mae"] for row in frame_results]))

    return {
        "pair": a_path.stem.removesuffix("_A"),
        "a_path": str(a_path),
        "b_path": str(b_path),
        "a_width": a_width,
        "a_height": a_height,
        "frames": ";".join(str(frame_index) for frame_index in frame_indices),
        "sx": sx,
        "sy": sy,
        "dx_score": dx_score,
        "dy_score": dy_score,
        "dx_full": dx_full,
        "dy_full": dy_full,
        "before_edge_corr": before_corr,
        "after_edge_corr": after_corr,
        "before_edge_mae": before_mae,
        "after_edge_mae": after_mae,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Optimize per-pair B geometry against A, then write normalized FFV1 pairs."
    )
    parser.add_argument("files", nargs="+", help="Input files as A B A B ...")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--csv-name", default="geometry_optimizations.csv")
    parser.add_argument("--frames", type=int, default=3)
    parser.add_argument("--score-width", type=int, default=320)
    parser.add_argument("--score-height", type=int, default=240)
    parser.add_argument("--mask-left", type=int, default=28)
    parser.add_argument("--mask-right", type=int, default=28)
    parser.add_argument("--mask-top", type=int, default=20)
    parser.add_argument("--mask-bottom", type=int, default=48)
    parser.add_argument("--scale-min", type=float, default=0.985)
    parser.add_argument("--scale-max", type=float, default=1.015)
    parser.add_argument("--scale-step", type=float, default=0.005)
    parser.add_argument("--max-dx", type=float, default=4.0)
    parser.add_argument("--max-dy", type=float, default=8.0)
    parser.add_argument("--shift-step", type=float, default=1.0)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.output_dir / args.csv_name
    rows = []

    with tempfile.TemporaryDirectory() as tmp_text:
        tmp = Path(tmp_text)
        for a_path, b_path in parse_pair_files(args.files):
            print(f"Optimizing {a_path.name} / {b_path.name}", flush=True)
            result = optimize_pair(a_path, b_path, args, tmp)
            out_a = args.output_dir / a_path.name
            out_b = args.output_dir / b_path.name
            shutil.copy2(a_path, out_a)
            transform_b_video(
                b_path,
                out_b,
                result["a_width"],
                result["a_height"],
                result["sx"],
                result["sy"],
                result["dx_full"],
                result["dy_full"],
            )
            rows.append(result)
            print(
                f"  sx={result['sx']:.4f} sy={result['sy']:.4f} "
                f"dx={result['dx_full']:.2f}px dy={result['dy_full']:.2f}px "
                f"corr {result['before_edge_corr']:.3f}->{result['after_edge_corr']:.3f}",
                flush=True,
            )

    fieldnames = [
        "pair",
        "a_path",
        "b_path",
        "a_width",
        "a_height",
        "frames",
        "sx",
        "sy",
        "dx_score",
        "dy_score",
        "dx_full",
        "dy_full",
        "before_edge_corr",
        "after_edge_corr",
        "before_edge_mae",
        "after_edge_mae",
    ]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {csv_path}")


if __name__ == "__main__":
    main()
