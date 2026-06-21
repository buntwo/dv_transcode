#!/usr/bin/env python3

import argparse
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image


def run(cmd):
    subprocess.run(cmd, check=True)


def extract_frames(video, out_dir, fps, width, height):
    out_pattern = str(Path(out_dir) / "frame_%05d.png")

    filters = [f"fps={fps}"]
    if width is not None or height is not None:
        if width is None or height is None:
            raise ValueError("--sample-width and --sample-height must be provided together")
        filters.append(f"scale={width}:{height}:flags=bicubic")
    filters.append("format=rgb24")
    vf = ",".join(filters)

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", video,
        "-vf", vf,
        "-vsync", "0",
        out_pattern,
    ]
    run(cmd)


def load_frames(frame_dir):
    files = sorted(Path(frame_dir).glob("frame_*.png"))
    frames = []
    for f in files:
        img = Image.open(f).convert("RGB")
        frames.append(np.asarray(img, dtype=np.float32) / 255.0)
    return frames


def feature_matrix(rgb, degree=2, model="standard", intercept=True):
    r = rgb[:, 0]
    g = rgb[:, 1]
    b = rgb[:, 2]

    feats = []
    if intercept:
        feats.append(np.ones_like(r))

    feats += [r, g, b]

    if model == "root-poly":
        if degree != 2:
            raise ValueError("root-poly currently supports degree 2 only")
        feats += [
            np.sqrt(np.clip(r * g, 0.0, None)),
            np.sqrt(np.clip(r * b, 0.0, None)),
            np.sqrt(np.clip(g * b, 0.0, None)),
        ]
        return np.stack(feats, axis=1)

    if model != "standard":
        raise ValueError(f"Unknown model: {model}")

    if degree >= 2:
        feats += [
            r * r, g * g, b * b,
            r * g, r * b, g * b,
        ]

    if degree >= 3:
        feats += [
            r ** 3, g ** 3, b ** 3,
            (r * r) * g, (r * r) * b,
            (g * g) * r, (g * g) * b,
            (b * b) * r, (b * b) * g,
            r * g * b,
        ]

    return np.stack(feats, axis=1)


def fit_transform(
    src_rgb,
    ref_rgb,
    degree=2,
    model="standard",
    intercept=True,
    ridge=1e-4,
    robust_iters=5,
):
    X = feature_matrix(src_rgb, degree=degree, model=model, intercept=intercept)
    Y = ref_rgb

    weights = np.ones(X.shape[0], dtype=np.float32)

    for _ in range(robust_iters):
        sw = np.sqrt(weights)[:, None]
        Xw = X * sw
        Yw = Y * sw

        A = Xw.T @ Xw
        B = Xw.T @ Yw

        reg = np.eye(A.shape[0], dtype=np.float32) * ridge
        if intercept:
            # Do not regularize intercept heavily.
            reg[0, 0] = 0.0

        coef = np.linalg.solve(A + reg, B)

        pred = np.clip(X @ coef, 0.0, 1.0)
        err = np.sqrt(np.mean((pred - Y) ** 2, axis=1))

        med = np.median(err)
        mad = np.median(np.abs(err - med)) + 1e-6
        sigma = 1.4826 * mad + 1e-6

        # Cauchy-style robust weighting.
        c = 3.0 * sigma
        weights = 1.0 / (1.0 + (err / c) ** 2)

    return coef


def apply_transform(rgb, coef, degree=2, model="standard", intercept=True):
    X = feature_matrix(rgb, degree=degree, model=model, intercept=intercept)
    return np.clip(X @ coef, 0.0, 1.0)


def collect_samples(
    pairs,
    fps,
    sample_width,
    sample_height,
    mask_left,
    mask_right,
    mask_top,
    mask_bottom,
    max_samples,
    sampling,
    luma_bins,
    seed,
):
    pair_samples = []

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        for idx, (ref_video, src_video) in enumerate(pairs, start=1):
            ref_dir = tmp / f"ref_{idx}"
            src_dir = tmp / f"src_{idx}"
            ref_dir.mkdir()
            src_dir.mkdir()

            extract_frames(ref_video, ref_dir, fps, sample_width, sample_height)
            extract_frames(src_video, src_dir, fps, sample_width, sample_height)

            ref_frames = load_frames(ref_dir)
            src_frames = load_frames(src_dir)

            n = min(len(ref_frames), len(src_frames))
            if n == 0:
                raise RuntimeError(f"No frames extracted for pair {idx}")

            pair_src = []
            pair_ref = []

            for ref, src in zip(ref_frames[:n], src_frames[:n]):
                if ref.shape != src.shape:
                    raise RuntimeError(
                        f"Pair {idx} frame dimensions differ after extraction: "
                        f"ref {ref.shape[1]}x{ref.shape[0]}, src {src.shape[1]}x{src.shape[0]}. "
                        "Use --sample-width/--sample-height or normalize geometry first."
                    )
                h, w, _ = src.shape

                y0 = mask_top
                y1 = h - mask_bottom
                x0 = mask_left
                x1 = w - mask_right

                if y1 <= y0 or x1 <= x0:
                    raise RuntimeError("Mask too aggressive for sample size")

                ref_crop = ref[y0:y1, x0:x1, :]
                src_crop = src[y0:y1, x0:x1, :]

                src_px = src_crop.reshape(-1, 3)
                ref_px = ref_crop.reshape(-1, 3)

                # Exclude clipped/crushed pixels in either source or target.
                src_luma = 0.299 * src_px[:, 0] + 0.587 * src_px[:, 1] + 0.114 * src_px[:, 2]
                ref_luma = 0.299 * ref_px[:, 0] + 0.587 * ref_px[:, 1] + 0.114 * ref_px[:, 2]

                valid = (
                    (src_luma > 0.04) & (src_luma < 0.94) &
                    (ref_luma > 0.04) & (ref_luma < 0.94) &
                    (src_px.min(axis=1) > 0.005) & (src_px.max(axis=1) < 0.995) &
                    (ref_px.min(axis=1) > 0.005) & (ref_px.max(axis=1) < 0.995)
                )

                src_px = src_px[valid]
                ref_px = ref_px[valid]

                if len(src_px):
                    pair_src.append(src_px)
                    pair_ref.append(ref_px)

            if pair_src:
                pair_samples.append(
                    (
                        np.concatenate(pair_src, axis=0),
                        np.concatenate(pair_ref, axis=0),
                    )
                )

    if not pair_samples:
        raise RuntimeError("No valid samples collected")

    src, ref = sample_pairs(pair_samples, max_samples, sampling, luma_bins, seed)

    return src, ref


def random_subset(src, ref, max_samples, rng):
    if len(src) <= max_samples:
        return src, ref
    keep = rng.choice(len(src), size=max_samples, replace=False)
    return src[keep], ref[keep]


def sample_luma_balanced(src, ref, budget, luma_bins, rng):
    if len(src) <= budget:
        return src, ref

    luma = 0.299 * src[:, 0] + 0.587 * src[:, 1] + 0.114 * src[:, 2]
    edges = np.linspace(0.04, 0.94, luma_bins + 1)
    bin_indices = []
    selected = []

    for index in range(luma_bins):
        if index == luma_bins - 1:
            mask = (luma >= edges[index]) & (luma <= edges[index + 1])
        else:
            mask = (luma >= edges[index]) & (luma < edges[index + 1])
        indices = np.flatnonzero(mask)
        if len(indices):
            bin_indices.append(indices)

    if not bin_indices:
        return random_subset(src, ref, budget, rng)

    base = max(1, budget // len(bin_indices))
    remaining_bins = []
    for indices in bin_indices:
        take = min(len(indices), base)
        selected.append(rng.choice(indices, size=take, replace=False))
        if len(indices) > take:
            remaining_bins.append(np.setdiff1d(indices, selected[-1], assume_unique=False))

    selected_count = sum(len(indices) for indices in selected)
    remaining_budget = budget - selected_count
    if remaining_budget > 0 and remaining_bins:
        remaining = np.concatenate(remaining_bins)
        take = min(len(remaining), remaining_budget)
        selected.append(rng.choice(remaining, size=take, replace=False))

    keep = np.concatenate(selected)
    if len(keep) > budget:
        keep = rng.choice(keep, size=budget, replace=False)
    return src[keep], ref[keep]


def sample_pairs(pair_samples, max_samples, sampling, luma_bins, seed):
    rng = np.random.default_rng(seed)

    if sampling == "random":
        src = np.concatenate([pair[0] for pair in pair_samples], axis=0)
        ref = np.concatenate([pair[1] for pair in pair_samples], axis=0)
        return random_subset(src, ref, max_samples, rng)

    per_pair_budget = max(1, max_samples // len(pair_samples))
    sampled_src = []
    sampled_ref = []

    for src, ref in pair_samples:
        if sampling == "pair-balanced":
            src_keep, ref_keep = random_subset(src, ref, per_pair_budget, rng)
        elif sampling == "pair-luma-balanced":
            src_keep, ref_keep = sample_luma_balanced(src, ref, per_pair_budget, luma_bins, rng)
        else:
            raise ValueError(f"Unknown sampling mode: {sampling}")
        sampled_src.append(src_keep)
        sampled_ref.append(ref_keep)

    src = np.concatenate(sampled_src, axis=0)
    ref = np.concatenate(sampled_ref, axis=0)

    if len(src) > max_samples:
        keep = rng.choice(len(src), size=max_samples, replace=False)
        src = src[keep]
        ref = ref[keep]

    return src, ref


def write_cube(path, coef, degree, model, intercept, size, strength):
    path = Path(path)

    with path.open("w") as f:
        f.write(
            f'TITLE "VHS to Video8 {model} degree {degree} '
            f'intercept {intercept} strength {strength}"\n'
        )
        f.write(f"LUT_3D_SIZE {size}\n")
        f.write("DOMAIN_MIN 0.0 0.0 0.0\n")
        f.write("DOMAIN_MAX 1.0 1.0 1.0\n")

        grid = np.linspace(0.0, 1.0, size, dtype=np.float32)

        # FFmpeg's .cube reader expects red to vary fastest.
        for b in grid:
            for g in grid:
                for r in grid:
                    inp = np.array([[r, g, b]], dtype=np.float32)
                    out = apply_transform(
                        inp,
                        coef,
                        degree=degree,
                        model=model,
                        intercept=intercept,
                    )[0]
                    out = inp[0] + strength * (out - inp[0])
                    out = np.clip(out, 0.0, 1.0)
                    f.write(f"{out[0]:.8f} {out[1]:.8f} {out[2]:.8f}\n")


def strength_label(strength):
    return f"{int(round(strength * 100)):03d}".rstrip("0").rstrip(".")


def output_path_for_strength(out, strength, multiple):
    path_text = str(out)
    percent = int(round(strength * 100))
    if "{strength}" in path_text or "{pct}" in path_text:
        return Path(
            path_text.format(
                strength=f"{strength:g}",
                pct=f"{percent}",
            )
        )

    path = Path(out)
    if not multiple:
        return path

    return path.with_name(f"{path.stem}_strength{percent}{path.suffix}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", required=True, help="Text file: video8|vhs per line")
    ap.add_argument("--out", required=True, help="Output .cube")
    ap.add_argument("--size", type=int, default=33)
    ap.add_argument(
        "--strength",
        type=float,
        action="append",
        help="Output strength. May be repeated. Defaults to 0.85.",
    )
    ap.add_argument("--strengths", type=float, nargs="+")
    ap.add_argument("--degree", type=int, default=2, choices=[1, 2, 3])
    ap.add_argument("--model", choices=["standard", "root-poly"], default="standard")
    ap.add_argument("--intercept", dest="intercept", action="store_true", default=True)
    ap.add_argument("--no-intercept", dest="intercept", action="store_false")
    ap.add_argument("--fps", type=float, default=2.0)
    ap.add_argument("--sample-width", type=int)
    ap.add_argument("--sample-height", type=int)
    ap.add_argument("--margin", type=int, default=None, help="Legacy shorthand for top/left/right masks.")
    ap.add_argument("--bottom-mask", type=int, default=None, help="Legacy extra bottom mask added to --margin.")
    ap.add_argument("--mask-left", type=int)
    ap.add_argument("--mask-right", type=int)
    ap.add_argument("--mask-top", type=int)
    ap.add_argument("--mask-bottom", type=int)
    ap.add_argument("--max-samples", type=int, default=1000000)
    ap.add_argument("--seed", type=int, default=2002)
    ap.add_argument(
        "--sampling",
        choices=["random", "pair-balanced", "pair-luma-balanced"],
        default="random",
    )
    ap.add_argument("--luma-bins", type=int, default=6)
    args = ap.parse_args()
    if args.model == "root-poly" and args.degree != 2:
        raise SystemExit("root-poly currently supports --degree 2 only")

    strengths = args.strengths or args.strength or [0.85]

    pairs = []
    with open(args.pairs, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            ref, src = line.split("|")
            pairs.append((ref.strip(), src.strip()))

    if args.margin is not None or args.bottom_mask is not None:
        legacy_margin = 6 if args.margin is None else args.margin
        legacy_bottom = 14 if args.bottom_mask is None else args.bottom_mask
        default_mask_left = legacy_margin
        default_mask_right = legacy_margin
        default_mask_top = legacy_margin
        default_mask_bottom = legacy_margin + legacy_bottom
    else:
        default_mask_left = 56
        default_mask_right = 56
        default_mask_top = 40
        default_mask_bottom = 96

    mask_left = args.mask_left if args.mask_left is not None else default_mask_left
    mask_right = args.mask_right if args.mask_right is not None else default_mask_right
    mask_top = args.mask_top if args.mask_top is not None else default_mask_top
    mask_bottom = args.mask_bottom if args.mask_bottom is not None else default_mask_bottom

    src, ref = collect_samples(
        pairs=pairs,
        fps=args.fps,
        sample_width=args.sample_width,
        sample_height=args.sample_height,
        mask_left=mask_left,
        mask_right=mask_right,
        mask_top=mask_top,
        mask_bottom=mask_bottom,
        max_samples=args.max_samples,
        sampling=args.sampling,
        luma_bins=args.luma_bins,
        seed=args.seed,
    )

    print(f"Collected samples: {len(src):,}")

    coef = fit_transform(
        src,
        ref,
        degree=args.degree,
        model=args.model,
        intercept=args.intercept,
    )

    pred = apply_transform(
        src,
        coef,
        degree=args.degree,
        model=args.model,
        intercept=args.intercept,
    )
    mae_before = np.mean(np.abs(src - ref)) * 255
    mae_after = np.mean(np.abs(pred - ref)) * 255

    print(f"RGB MAE before: {mae_before:.2f}")
    print(f"RGB MAE after:  {mae_after:.2f}")

    for strength in strengths:
        out_path = output_path_for_strength(args.out, strength, len(strengths) > 1)
        write_cube(
            out_path,
            coef,
            args.degree,
            args.model,
            args.intercept,
            args.size,
            strength,
        )
        print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
