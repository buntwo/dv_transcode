#!/usr/bin/env python3
import argparse
import csv
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from generate_pair_manifest import discover_pairs


def run(cmd):
    subprocess.run(cmd, check=True)


def lut_name(path):
    return Path(path).stem


def extract_frames(video, out_dir, fps, width, height, lut=None):
    out_pattern = str(Path(out_dir) / "frame_%05d.png")
    filters = [f"fps={fps}"]
    if width is not None or height is not None:
        if width is None or height is None:
            raise ValueError("--sample-width and --sample-height must be provided together")
        filters.append(f"scale={width}:{height}:flags=bicubic")
    if lut:
        filters += [
            "format=gbrp",
            f"lut3d={lut}:interp=tetrahedral",
        ]
    filters.append("format=rgb24")

    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video),
            "-vf",
            ",".join(filters),
            "-vsync",
            "0",
            out_pattern,
        ]
    )


def load_frames(frame_dir):
    frames = []
    for path in sorted(Path(frame_dir).glob("frame_*.png")):
        frames.append(np.asarray(Image.open(path).convert("RGB"), dtype=np.float32) / 255.0)
    return frames


def crop_pair(ref, src, mask_left, mask_right, mask_top, mask_bottom):
    h, w, _ = ref.shape
    y0 = mask_top
    y1 = h - mask_bottom
    x0 = mask_left
    x1 = w - mask_right
    if y1 <= y0 or x1 <= x0:
        raise RuntimeError("Mask too aggressive for sample size")
    return ref[y0:y1, x0:x1, :], src[y0:y1, x0:x1, :]


def validation_mask(ref_px, src_px):
    src_luma = 0.299 * src_px[:, 0] + 0.587 * src_px[:, 1] + 0.114 * src_px[:, 2]
    ref_luma = 0.299 * ref_px[:, 0] + 0.587 * ref_px[:, 1] + 0.114 * ref_px[:, 2]
    return (
        (src_luma > 0.04)
        & (src_luma < 0.94)
        & (ref_luma > 0.04)
        & (ref_luma < 0.94)
        & (src_px.min(axis=1) > 0.005)
        & (src_px.max(axis=1) < 0.995)
        & (ref_px.min(axis=1) > 0.005)
        & (ref_px.max(axis=1) < 0.995)
    )


def srgb_to_linear(rgb):
    return np.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)


def rgb_to_lab(rgb):
    linear = srgb_to_linear(np.clip(rgb, 0.0, 1.0))
    matrix = np.array(
        [
            [0.4124564, 0.3575761, 0.1804375],
            [0.2126729, 0.7151522, 0.0721750],
            [0.0193339, 0.1191920, 0.9503041],
        ],
        dtype=np.float32,
    )
    xyz = linear @ matrix.T
    xyz /= np.array([0.95047, 1.0, 1.08883], dtype=np.float32)
    delta = 6 / 29
    f = np.where(xyz > delta**3, np.cbrt(xyz), xyz / (3 * delta**2) + 4 / 29)
    lab = np.empty_like(f)
    lab[:, 0] = 116 * f[:, 1] - 16
    lab[:, 1] = 500 * (f[:, 0] - f[:, 1])
    lab[:, 2] = 200 * (f[:, 1] - f[:, 2])
    return lab


def compute_metrics(ref_samples, test_samples):
    diff = test_samples - ref_samples
    rgb_mae = np.mean(np.abs(diff)) * 255.0
    rgb_rmse = np.sqrt(np.mean(diff * diff)) * 255.0

    ref_luma = 0.299 * ref_samples[:, 0] + 0.587 * ref_samples[:, 1] + 0.114 * ref_samples[:, 2]
    test_luma = 0.299 * test_samples[:, 0] + 0.587 * test_samples[:, 1] + 0.114 * test_samples[:, 2]
    luma_mae = np.mean(np.abs(test_luma - ref_luma)) * 255.0

    ref_lab = rgb_to_lab(ref_samples)
    test_lab = rgb_to_lab(test_samples)
    delta_e76 = np.sqrt(np.sum((test_lab - ref_lab) ** 2, axis=1))

    return {
        "sample_count": int(len(ref_samples)),
        "rgb_mae": float(rgb_mae),
        "rgb_rmse": float(rgb_rmse),
        "luma_mae": float(luma_mae),
        "delta_e76_mean": float(np.mean(delta_e76)),
        "delta_e76_p95": float(np.percentile(delta_e76, 95)),
    }


def collect_samples(ref_frames, raw_frames, test_frames, masks):
    all_ref = []
    all_test = []
    n = min(len(ref_frames), len(raw_frames), len(test_frames))
    for index in range(n):
        if ref_frames[index].shape != raw_frames[index].shape:
            raise RuntimeError(
                "Reference and raw frame dimensions differ after extraction. "
                "Use --sample-width/--sample-height or normalize geometry first."
            )
        if ref_frames[index].shape != test_frames[index].shape:
            raise RuntimeError(
                "Reference and test frame dimensions differ after extraction. "
                "Use --sample-width/--sample-height or normalize geometry first."
            )
        ref_crop, raw_crop = crop_pair(ref_frames[index], raw_frames[index], **masks)
        _, test_crop = crop_pair(ref_frames[index], test_frames[index], **masks)

        ref_px = ref_crop.reshape(-1, 3)
        raw_px = raw_crop.reshape(-1, 3)
        test_px = test_crop.reshape(-1, 3)
        valid = validation_mask(ref_px, raw_px)

        if np.any(valid):
            all_ref.append(ref_px[valid])
            all_test.append(test_px[valid])

    if not all_ref:
        raise RuntimeError("No valid samples after masking")

    return np.concatenate(all_ref, axis=0), np.concatenate(all_test, axis=0)


def read_manifest(path):
    pairs = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        ref, src = line.split("|")
        pairs.append((Path(ref.strip()), Path(src.strip())))
    return pairs


def source_pairs(args):
    if args.pairs:
        return read_manifest(args.pairs)
    return discover_pairs(args.input_dir)


def draw_labeled_cell(image, label, width):
    label_h = 22
    out = Image.new("RGB", (width, image.height + label_h), "black")
    out.paste(image, (0, label_h))
    draw = ImageDraw.Draw(out)
    font = ImageFont.load_default()
    draw.text((4, 5), label, fill="white", font=font)
    return out


def save_contact_sheet(rows, path):
    if not rows:
        return
    row_width = max(sum(cell.width for cell in row) for row in rows)
    row_height = max(max(cell.height for cell in row) for row in rows)
    sheet = Image.new("RGB", (row_width, row_height * len(rows)), (24, 24, 24))

    for y_index, row in enumerate(rows):
        x = 0
        for cell in row:
            sheet.paste(cell, (x, y_index * row_height))
            x += cell.width

    path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(path)


def apply_lut_to_image(input_image, output_dir, luts):
    output_dir.mkdir(parents=True, exist_ok=True)
    written = []

    for lut in luts:
        output_path = output_dir / f"{input_image.stem}_{lut_name(lut)}.png"
        run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(input_image),
                "-vf",
                f"format=gbrp,lut3d={lut}:interp=tetrahedral,format=rgb24",
                "-frames:v",
                "1",
                str(output_path),
            ]
        )
        written.append(output_path)

    return written


def make_contact_row(pair_label, ref_frames, raw_frames, lut_frames_by_name, frame_index):
    n = min(len(ref_frames), len(raw_frames), *(len(frames) for frames in lut_frames_by_name.values()))
    if n == 0:
        return None
    idx = min(frame_index, n - 1)
    cells = [
        draw_labeled_cell(
            Image.fromarray((np.clip(ref_frames[idx], 0.0, 1.0) * 255).astype(np.uint8)),
            f"{pair_label} f{idx} A",
            ref_frames[idx].shape[1],
        ),
        draw_labeled_cell(
            Image.fromarray((np.clip(raw_frames[idx], 0.0, 1.0) * 255).astype(np.uint8)),
            "B raw",
            raw_frames[idx].shape[1],
        ),
    ]
    for name, frames in lut_frames_by_name.items():
        cells.append(
            draw_labeled_cell(
                Image.fromarray((np.clip(frames[idx], 0.0, 1.0) * 255).astype(np.uint8)),
                name,
                frames[idx].shape[1],
            )
        )
    return cells


def contact_indices(frame_count, requested_count):
    if frame_count <= 0:
        return []
    if requested_count <= 1:
        return [frame_count // 2]
    return sorted(
        set(
            int(round(value))
            for value in np.linspace(0, frame_count - 1, requested_count)
        )
    )


def evaluate(args):
    pairs = source_pairs(args)
    luts = [Path(lut) for lut in args.lut]
    legacy_margin = 6 if args.margin is None else args.margin
    legacy_bottom = 14 if args.bottom_mask is None else args.bottom_mask
    masks = {
        "mask_left": args.mask_left if args.mask_left is not None else legacy_margin,
        "mask_right": args.mask_right if args.mask_right is not None else legacy_margin,
        "mask_top": args.mask_top if args.mask_top is not None else legacy_margin,
        "mask_bottom": args.mask_bottom if args.mask_bottom is not None else legacy_margin + legacy_bottom,
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_csv = args.out_csv or (args.out_dir / "validation_metrics.csv")
    contact_sheet = args.contact_sheet
    if contact_sheet == Path("__default__"):
        contact_sheet = args.out_dir / "validation_lut_contact_sheet.png"
    rows = []
    contact_rows = []

    with tempfile.TemporaryDirectory() as tmp_text:
        tmp = Path(tmp_text)
        for pair_index, (ref_video, src_video) in enumerate(pairs, start=1):
            pair_label = ref_video.stem.replace("_A", "")
            print(f"Evaluating {pair_label}")

            ref_dir = tmp / f"pair_{pair_index:03d}_ref"
            raw_dir = tmp / f"pair_{pair_index:03d}_raw"
            ref_dir.mkdir()
            raw_dir.mkdir()
            extract_frames(ref_video, ref_dir, args.fps, args.sample_width, args.sample_height)
            extract_frames(src_video, raw_dir, args.fps, args.sample_width, args.sample_height)
            ref_frames = load_frames(ref_dir)
            raw_frames = load_frames(raw_dir)

            ref_samples, raw_samples = collect_samples(
                ref_frames,
                raw_frames,
                raw_frames,
                masks,
            )
            raw_metrics = compute_metrics(ref_samples, raw_samples)
            rows.append({"pair": pair_label, "candidate": "raw", **raw_metrics})

            lut_frames_by_name = {}
            for lut in luts:
                name = lut_name(lut)
                lut_dir = tmp / f"pair_{pair_index:03d}_{name}"
                lut_dir.mkdir()
                extract_frames(src_video, lut_dir, args.fps, args.sample_width, args.sample_height, lut=lut)
                lut_frames = load_frames(lut_dir)
                lut_frames_by_name[name] = lut_frames

                ref_samples, lut_samples = collect_samples(
                    ref_frames,
                    raw_frames,
                    lut_frames,
                    masks,
                )
                metrics = compute_metrics(ref_samples, lut_samples)
                rows.append({"pair": pair_label, "candidate": name, **metrics})

            if contact_sheet:
                frame_count = min(
                    len(ref_frames),
                    len(raw_frames),
                    *(len(frames) for frames in lut_frames_by_name.values()),
                )
                for frame_index in contact_indices(frame_count, args.contact_frames):
                    row = make_contact_row(
                        pair_label,
                        ref_frames,
                        raw_frames,
                        lut_frames_by_name,
                        frame_index,
                    )
                    if row:
                        contact_rows.append(row)

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "pair",
        "candidate",
        "sample_count",
        "rgb_mae",
        "rgb_rmse",
        "luma_mae",
        "delta_e76_mean",
        "delta_e76_p95",
    ]
    with out_csv.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {out_csv}")

    summary = {}
    for row in rows:
        summary.setdefault(row["candidate"], []).append(row)
    print("Candidate means:")
    for candidate, candidate_rows in summary.items():
        rgb_mae = np.mean([row["rgb_mae"] for row in candidate_rows])
        delta_e = np.mean([row["delta_e76_mean"] for row in candidate_rows])
        print(f"  {candidate}: RGB MAE {rgb_mae:.2f}, mean dE76 {delta_e:.2f}")

    if contact_sheet:
        save_contact_sheet(contact_rows, contact_sheet)
        print(f"Wrote {contact_sheet}")

    if args.sanity_chart:
        sanity_output_dir = args.sanity_output_dir or (args.out_dir / "sanity_charts")
        written = apply_lut_to_image(args.sanity_chart, sanity_output_dir, luts)
        for path in written:
            print(f"Wrote {path}")


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate LUT candidates against matched A/B validation clips."
    )
    pair_source = parser.add_mutually_exclusive_group(required=True)
    pair_source.add_argument("--input-dir", type=Path)
    pair_source.add_argument("--pairs", type=Path)
    parser.add_argument("--lut", action="append", required=True, help="Candidate .cube LUT. May repeat.")
    parser.add_argument("--out-dir", type=Path, default=Path("generated_video_pairs/evaluation"))
    parser.add_argument("--out-csv", type=Path)
    parser.add_argument(
        "--contact-sheet",
        nargs="?",
        const=Path("__default__"),
        type=Path,
        help="Optional contact sheet path. If passed without a path, writes into --out-dir.",
    )
    parser.add_argument("--contact-frames", type=int, default=3)
    parser.add_argument("--sanity-chart", type=Path, default=Path("lut_sanity_chart.png"))
    parser.add_argument("--sanity-output-dir", type=Path)
    parser.add_argument("--fps", type=float, default=2.0)
    parser.add_argument("--sample-width", type=int)
    parser.add_argument("--sample-height", type=int)
    parser.add_argument("--margin", type=int, default=None, help="Legacy shorthand for top/left/right masks.")
    parser.add_argument("--bottom-mask", type=int, default=None, help="Legacy extra bottom mask added to --margin.")
    parser.add_argument("--mask-left", type=int)
    parser.add_argument("--mask-right", type=int)
    parser.add_argument("--mask-top", type=int)
    parser.add_argument("--mask-bottom", type=int)
    args = parser.parse_args()
    evaluate(args)


if __name__ == "__main__":
    main()
