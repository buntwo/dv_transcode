#!/usr/bin/env python3
import argparse
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


def extract_frame(video, frame_index, output_path, sample_width=None, sample_height=None):
    filters = [f"select=eq(n\\,{frame_index})"]
    if sample_width and sample_height:
        filters.append(f"scale={sample_width}:{sample_height}:flags=bicubic")
    filters.append("format=rgb24")
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
            ",".join(filters),
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


def resize_to_match(image, size):
    return image.resize(size, Image.Resampling.BICUBIC)


def crop_mask(image, left, right, top, bottom):
    width, height = image.size
    x0 = left
    y0 = top
    x1 = width - right
    y1 = height - bottom
    if x1 <= x0 or y1 <= y0:
        raise ValueError("Mask is too aggressive for image size")
    return image.crop((x0, y0, x1, y1))


def checkerboard(a, b, tile):
    arr_a = np.asarray(a, dtype=np.uint8)
    arr_b = np.asarray(b, dtype=np.uint8)
    h, w, _ = arr_a.shape
    yy, xx = np.indices((h, w))
    mask = ((xx // tile) + (yy // tile)) % 2 == 0
    out = np.where(mask[..., None], arr_a, arr_b)
    return Image.fromarray(out, "RGB")


def boosted_difference(a, b, scale):
    arr_a = np.asarray(a, dtype=np.int16)
    arr_b = np.asarray(b, dtype=np.int16)
    diff = np.clip(np.abs(arr_a - arr_b) * scale, 0, 255).astype(np.uint8)
    return Image.fromarray(diff, "RGB")


def labeled(image, label):
    label_height = 24
    out = Image.new("RGB", (image.width, image.height + label_height), "black")
    out.paste(image, (0, label_height))
    draw = ImageDraw.Draw(out)
    draw.text((5, 6), label, fill="white", font=ImageFont.load_default())
    return out


def make_row(pair_label, frame_index, a_image, b_image, diff_scale, checker_tile):
    b_matched = resize_to_match(b_image, a_image.size)
    blend = Image.blend(a_image, b_matched, 0.5)
    diff = boosted_difference(a_image, b_matched, diff_scale)
    checker = checkerboard(a_image, b_matched, checker_tile)
    return [
        labeled(a_image, f"{pair_label} frame {frame_index} A"),
        labeled(b_matched, "B resized"),
        labeled(blend, "50/50 blend"),
        labeled(checker, "checker"),
        labeled(diff, f"diff x{diff_scale:g}"),
    ]


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
    rows = []

    with tempfile.TemporaryDirectory() as tmp_text:
        tmp = Path(tmp_text)
        for pair_index, (a_path, b_path) in selected_pairs(pairs, args.pair):
            count = min(frame_count(a_path), frame_count(b_path))
            pair_label = f"pair_{pair_index:03d}"
            for frame_index in pick_frame_indices(count, args.frames):
                a_png = tmp / f"{pair_label}_{frame_index}_A.png"
                b_png = tmp / f"{pair_label}_{frame_index}_B.png"
                extract_frame(
                    a_path,
                    frame_index,
                    a_png,
                    sample_width=args.sample_width,
                    sample_height=args.sample_height,
                )
                extract_frame(
                    b_path,
                    frame_index,
                    b_png,
                    sample_width=args.sample_width,
                    sample_height=args.sample_height,
                )
                a_image = Image.open(a_png).convert("RGB")
                b_image = Image.open(b_png).convert("RGB")

                if args.mask_for_review:
                    a_image = crop_mask(
                        a_image,
                        args.mask_left,
                        args.mask_right,
                        args.mask_top,
                        args.mask_bottom,
                    )
                    b_image = crop_mask(
                        b_image,
                        args.mask_left,
                        args.mask_right,
                        args.mask_top,
                        args.mask_bottom,
                    )

                rows.append(
                    make_row(
                        pair_label,
                        frame_index,
                        a_image,
                        b_image,
                        args.diff_scale,
                        args.checker_tile,
                    )
                )

    save_sheet(rows, args.out)
    print(f"Wrote {args.out}")


def main():
    parser = argparse.ArgumentParser(
        description="Generate visual alignment overlays from A/B pair clips."
    )
    parser.add_argument("--pairs", type=Path, default=Path("generated_video_pairs/train_pairs.txt"))
    parser.add_argument("--out", type=Path, default=Path("generated_video_pairs/alignment_overlays/train_alignment_overlay.png"))
    parser.add_argument("--pair", action="append", type=int, help="1-based pair index to include. May repeat.")
    parser.add_argument("--frames", type=int, default=3)
    parser.add_argument("--sample-width", type=int)
    parser.add_argument("--sample-height", type=int)
    parser.add_argument("--diff-scale", type=float, default=4.0)
    parser.add_argument("--checker-tile", type=int, default=24)
    parser.add_argument("--mask-for-review", action="store_true")
    parser.add_argument("--mask-left", type=int, default=0)
    parser.add_argument("--mask-right", type=int, default=0)
    parser.add_argument("--mask-top", type=int, default=0)
    parser.add_argument("--mask-bottom", type=int, default=0)
    args = parser.parse_args()
    generate(args)


if __name__ == "__main__":
    main()
