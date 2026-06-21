#!/usr/bin/env python3
import argparse
import csv
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont


def run(cmd):
    subprocess.run(cmd, check=True)


def read_pairs(path):
    pairs = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        ref, src = line.split("|")
        pairs.append((Path(ref.strip()), Path(src.strip())))
    return pairs


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


def extract_frame(video, frame_index, output_path):
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
            f"select=eq(n\\,{frame_index}),format=rgb24",
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


def crop_array(arr, left, right, top, bottom):
    h, w = arr.shape[:2]
    return arr[top : h - bottom, left : w - right]


def shifted_overlap(a, b, dx, dy):
    h, w = a.shape[:2]
    ax0 = max(0, dx)
    bx0 = max(0, -dx)
    ay0 = max(0, dy)
    by0 = max(0, -dy)
    width = min(w - ax0, w - bx0)
    height = min(h - ay0, h - by0)
    if width <= 4 or height <= 4:
        raise ValueError("Shift leaves too little overlap")
    return (
        a[ay0 : ay0 + height, ax0 : ax0 + width],
        b[by0 : by0 + height, bx0 : bx0 + width],
    )


def luma(arr):
    arr = arr.astype(np.float32) / 255.0
    return 0.299 * arr[..., 0] + 0.587 * arr[..., 1] + 0.114 * arr[..., 2]


def edge_map(rgb):
    y = luma(rgb)
    gx = y[1:-1, 2:] - y[1:-1, :-2]
    gy = y[2:, 1:-1] - y[:-2, 1:-1]
    edge = np.sqrt(gx * gx + gy * gy)
    std = float(edge.std())
    if std < 1e-6:
        return edge - float(edge.mean())
    return (edge - float(edge.mean())) / std


def score_shift(a_rgb, b_rgb, dx, dy):
    a_crop, b_crop = shifted_overlap(a_rgb, b_rgb, dx, dy)
    a_edge = edge_map(a_crop)
    b_edge = edge_map(b_crop)
    corr = float(np.mean(a_edge * b_edge))
    mae = float(np.mean(np.abs(a_edge - b_edge)))
    return corr, mae


def best_shift(a_rgb, b_rgb, dx_values, dy_values):
    best = None
    rows = []
    for dy in dy_values:
        for dx in dx_values:
            corr, mae = score_shift(a_rgb, b_rgb, dx, dy)
            row = {"dx": dx, "dy": dy, "edge_corr": corr, "edge_mae": mae}
            rows.append(row)
            if best is None or corr > best["edge_corr"]:
                best = row
    return best, rows


def labeled(image, label):
    label_height = 24
    out = Image.new("RGB", (image.width, image.height + label_height), "black")
    out.paste(image, (0, label_height))
    draw = ImageDraw.Draw(out)
    draw.text((5, 6), label, fill="white", font=ImageFont.load_default())
    return out


def blend_for_shift(a_rgb, b_rgb, dx, dy):
    a_crop, b_crop = shifted_overlap(a_rgb, b_rgb, dx, dy)
    return Image.blend(Image.fromarray(a_crop), Image.fromarray(b_crop), 0.5)


def save_sheet(rows, output_path):
    if not rows:
        return
    row_width = max(sum(cell.width for cell in row) for row in rows)
    row_height = max(max(cell.height for cell in row) for row in rows)
    sheet = Image.new("RGB", (row_width, row_height * len(rows)), (22, 22, 22))
    for row_index, row in enumerate(rows):
        x = 0
        for cell in row:
            sheet.paste(cell, (x, row_index * row_height))
            x += cell.width
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path)


def selected_pairs(all_pairs, selected_indices):
    if not selected_indices:
        selected_indices = list(range(1, min(len(all_pairs), 6) + 1))
    for one_based in selected_indices:
        yield one_based, all_pairs[one_based - 1]


def generate(args):
    pairs = read_pairs(args.pairs)
    csv_rows = []
    sheet_rows = []
    dx_values = range(-args.max_dx, args.max_dx + 1)
    dy_values = range(-args.max_dy, args.max_dy + 1)

    with tempfile.TemporaryDirectory() as tmp_text:
        tmp = Path(tmp_text)
        for pair_index, (a_path, b_path) in selected_pairs(pairs, args.pair):
            count = min(frame_count(a_path), frame_count(b_path))
            for frame_index in pick_frame_indices(count, args.frames):
                a_png = tmp / f"pair_{pair_index:03d}_{frame_index}_A.png"
                b_png = tmp / f"pair_{pair_index:03d}_{frame_index}_B.png"
                extract_frame(a_path, frame_index, a_png)
                extract_frame(b_path, frame_index, b_png)

                a_image = Image.open(a_png).convert("RGB")
                b_image = Image.open(b_png).convert("RGB").resize(
                    a_image.size,
                    Image.Resampling.BICUBIC,
                )
                a_rgb = np.asarray(a_image, dtype=np.uint8)
                b_rgb = np.asarray(b_image, dtype=np.uint8)

                if args.mask_for_score:
                    a_rgb = crop_array(
                        a_rgb,
                        args.mask_left,
                        args.mask_right,
                        args.mask_top,
                        args.mask_bottom,
                    )
                    b_rgb = crop_array(
                        b_rgb,
                        args.mask_left,
                        args.mask_right,
                        args.mask_top,
                        args.mask_bottom,
                    )

                best, _ = best_shift(a_rgb, b_rgb, dx_values, dy_values)
                csv_rows.append(
                    {
                        "pair": f"pair_{pair_index:03d}",
                        "frame": frame_index,
                        **best,
                    }
                )

                if args.out_sheet:
                    zero_blend = blend_for_shift(a_rgb, b_rgb, 0, 0)
                    best_blend = blend_for_shift(a_rgb, b_rgb, best["dx"], best["dy"])
                    sheet_rows.append(
                        [
                            labeled(Image.fromarray(a_rgb), f"pair_{pair_index:03d} f{frame_index} A"),
                            labeled(Image.fromarray(b_rgb), "B resized"),
                            labeled(zero_blend, "blend dx0 dy0"),
                            labeled(
                                best_blend,
                                f"best dx{best['dx']} dy{best['dy']} corr {best['edge_corr']:.3f}",
                            ),
                        ]
                    )

    args.out_csv.parent.mkdir(parents=True, exist_ok=True)
    with args.out_csv.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["pair", "frame", "dx", "dy", "edge_corr", "edge_mae"])
        writer.writeheader()
        writer.writerows(csv_rows)
    print(f"Wrote {args.out_csv}")

    if args.out_sheet:
        save_sheet(sheet_rows, args.out_sheet)
        print(f"Wrote {args.out_sheet}")

    by_pair = {}
    for row in csv_rows:
        by_pair.setdefault(row["pair"], []).append(row)
    print("Median best shifts:")
    for pair, rows in by_pair.items():
        dx = np.median([row["dx"] for row in rows])
        dy = np.median([row["dy"] for row in rows])
        corr = np.median([row["edge_corr"] for row in rows])
        print(f"  {pair}: dx={dx:g}, dy={dy:g}, edge_corr={corr:.3f}")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Diagnose A/B spatial shifts. Positive dy means the best score was obtained "
            "by moving B downward relative to A; negative dy means moving B upward."
        )
    )
    parser.add_argument("--pairs", type=Path, default=Path("generated_video_pairs/train_pairs.txt"))
    parser.add_argument("--out-csv", type=Path, default=Path("generated_video_pairs/alignment_overlays/shift_diagnostics.csv"))
    parser.add_argument("--out-sheet", type=Path, default=Path("generated_video_pairs/alignment_overlays/shift_diagnostics.png"))
    parser.add_argument("--pair", action="append", type=int, help="1-based pair index to include. May repeat.")
    parser.add_argument("--frames", type=int, default=3)
    parser.add_argument("--max-dx", type=int, default=2)
    parser.add_argument("--max-dy", type=int, default=8)
    parser.add_argument("--mask-for-score", action="store_true")
    parser.add_argument("--mask-left", type=int, default=56)
    parser.add_argument("--mask-right", type=int, default=56)
    parser.add_argument("--mask-top", type=int, default=40)
    parser.add_argument("--mask-bottom", type=int, default=96)
    args = parser.parse_args()
    generate(args)


if __name__ == "__main__":
    main()
