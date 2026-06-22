#!/usr/bin/env python3

import argparse
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image


LUMA_COEF = np.array([0.299, 0.587, 0.114], dtype=np.float32)


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


def rgb_luma(rgb):
    return rgb @ LUMA_COEF


def rgb_to_ycbcr(rgb):
    y = rgb_luma(rgb)
    cb = (rgb[:, 2] - y) / 1.772
    cr = (rgb[:, 0] - y) / 1.402
    return np.stack([y, cb, cr], axis=1)


def ycbcr_to_rgb(ycbcr):
    y = ycbcr[:, 0]
    cb = ycbcr[:, 1]
    cr = ycbcr[:, 2]
    r = y + 1.402 * cr
    b = y + 1.772 * cb
    g = (y - 0.299 * r - 0.114 * b) / 0.587
    return np.stack([r, g, b], axis=1)


def smoothstep(x, edge0, edge1):
    t = np.clip((x - edge0) / (edge1 - edge0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def allowed_luma_lift(src_y, shadow_lift, mid_lift, highlight_lift):
    return np.interp(
        src_y,
        [0.0, 0.20, 0.55, 0.82, 1.0],
        [shadow_lift, shadow_lift, mid_lift, highlight_lift, highlight_lift],
    )


def controlled_luma_target(
    src_y,
    ref_y,
    shadow_lift=0.18,
    mid_lift=0.045,
    highlight_lift=0.01,
    max_darken=0.22,
):
    lift_limit = allowed_luma_lift(src_y, shadow_lift, mid_lift, highlight_lift)
    delta = np.clip(ref_y - src_y, -max_darken, lift_limit)
    return np.clip(src_y + delta, 0.0, 1.0)


def solve_rgb_coefficients(X, Y, weights, intercept, ridge):
    sw = np.sqrt(weights)[:, None]
    Xw = X * sw
    Yw = Y * sw

    A = Xw.T @ Xw
    B = Xw.T @ Yw

    reg = np.eye(A.shape[0], dtype=np.float32) * ridge
    if intercept:
        # Do not regularize intercept heavily.
        reg[0, 0] = 0.0

    return np.linalg.solve(A + reg, B)


def solve_rgb_luma_regularized(
    X,
    Y,
    src_rgb,
    ref_rgb,
    weights,
    intercept,
    ridge,
    luma_regularization,
    shadow_lift,
    mid_lift,
    highlight_lift,
    max_darken,
):
    feature_count = X.shape[1]
    system_size = feature_count * 3
    A = np.zeros((system_size, system_size), dtype=np.float64)
    B = np.zeros(system_size, dtype=np.float64)

    weighted_xtx = X.T @ (weights[:, None] * X)
    for channel in range(3):
        start = channel * feature_count
        end = start + feature_count
        A[start:end, start:end] += weighted_xtx
        B[start:end] += X.T @ (weights * Y[:, channel])

        reg = np.full(feature_count, ridge, dtype=np.float64)
        if intercept:
            reg[0] = 0.0
        A[start:end, start:end] += np.diag(reg)

    if luma_regularization > 0.0:
        src_y = rgb_luma(src_rgb)
        ref_y = rgb_luma(ref_rgb)
        target_y = controlled_luma_target(
            src_y,
            ref_y,
            shadow_lift=shadow_lift,
            mid_lift=mid_lift,
            highlight_lift=highlight_lift,
            max_darken=max_darken,
        )
        mid_high_weight = 0.35 + 0.65 * smoothstep(src_y, 0.25, 0.78)
        luma_weights = weights * luma_regularization * mid_high_weight
        luma_xtx = X.T @ (luma_weights[:, None] * X)
        luma_rhs = X.T @ (luma_weights * target_y)

        for out_channel in range(3):
            out_start = out_channel * feature_count
            out_end = out_start + feature_count
            B[out_start:out_end] += LUMA_COEF[out_channel] * luma_rhs
            for in_channel in range(3):
                in_start = in_channel * feature_count
                in_end = in_start + feature_count
                A[out_start:out_end, in_start:in_end] += (
                    LUMA_COEF[out_channel] * LUMA_COEF[in_channel] * luma_xtx
                )

    coef_vector = np.linalg.solve(A, B)
    return np.stack(
        [
            coef_vector[0:feature_count],
            coef_vector[feature_count:2 * feature_count],
            coef_vector[2 * feature_count:3 * feature_count],
        ],
        axis=1,
    ).astype(np.float32)


def fit_rgb_transform(
    src_rgb,
    ref_rgb,
    degree,
    feature_model,
    intercept,
    ridge,
    robust_iters,
    luma_regularization=0.0,
    shadow_lift=0.18,
    mid_lift=0.045,
    highlight_lift=0.01,
    max_darken=0.22,
):
    X = feature_matrix(src_rgb, degree=degree, model=feature_model, intercept=intercept)
    Y = ref_rgb

    weights = np.ones(X.shape[0], dtype=np.float32)

    for _ in range(robust_iters):
        if luma_regularization > 0.0:
            coef = solve_rgb_luma_regularized(
                X,
                Y,
                src_rgb,
                ref_rgb,
                weights,
                intercept,
                ridge,
                luma_regularization,
                shadow_lift,
                mid_lift,
                highlight_lift,
                max_darken,
            )
        else:
            coef = solve_rgb_coefficients(X, Y, weights, intercept, ridge)

        pred = np.clip(X @ coef, 0.0, 1.0)
        err = np.sqrt(np.mean((pred - Y) ** 2, axis=1))
        weights = robust_weights(err)

    return {
        "kind": "rgb-poly",
        "coef": coef,
        "degree": degree,
        "feature_model": feature_model,
        "intercept": intercept,
    }


def robust_weights(err):
    med = np.median(err)
    mad = np.median(np.abs(err - med)) + 1e-6
    sigma = 1.4826 * mad + 1e-6
    # Cauchy-style robust weighting.
    c = 3.0 * sigma
    return 1.0 / (1.0 + (err / c) ** 2)


def fit_ycbcr_transform(src_rgb, ref_rgb, degree, intercept, ridge, robust_iters):
    src_ycc = rgb_to_ycbcr(src_rgb)
    ref_ycc = rgb_to_ycbcr(ref_rgb)
    X = feature_matrix(src_ycc, degree=degree, model="standard", intercept=intercept)
    Y = ref_ycc

    weights = np.ones(X.shape[0], dtype=np.float32)
    for _ in range(robust_iters):
        coef = solve_rgb_coefficients(X, Y, weights, intercept, ridge)
        pred_rgb = np.clip(ycbcr_to_rgb(X @ coef), 0.0, 1.0)
        err = np.sqrt(np.mean((pred_rgb - ref_rgb) ** 2, axis=1))
        weights = robust_weights(err)

    return {
        "kind": "ycbcr-poly",
        "coef": coef,
        "degree": degree,
        "intercept": intercept,
    }


def fit_tone_curve(
    src_y,
    ref_y,
    bins,
    shadow_lift,
    mid_lift,
    highlight_lift,
    max_darken,
):
    edges = np.linspace(0.0, 1.0, bins + 1, dtype=np.float32)
    centers = (edges[:-1] + edges[1:]) / 2.0
    targets = []

    for index, center in enumerate(centers):
        if index == len(centers) - 1:
            mask = (src_y >= edges[index]) & (src_y <= edges[index + 1])
        else:
            mask = (src_y >= edges[index]) & (src_y < edges[index + 1])

        if np.any(mask):
            ref_value = np.median(ref_y[mask])
        else:
            ref_value = center

        target = controlled_luma_target(
            np.array([center], dtype=np.float32),
            np.array([ref_value], dtype=np.float32),
            shadow_lift=shadow_lift,
            mid_lift=mid_lift,
            highlight_lift=highlight_lift,
            max_darken=max_darken,
        )[0]
        targets.append(target)

    x = np.concatenate([[0.0], centers, [1.0]]).astype(np.float32)
    y = np.concatenate([[0.0], np.array(targets, dtype=np.float32), [1.0]])

    # Keep the tone mapping ordered so shadow lift does not create reversals.
    y = np.maximum.accumulate(y)
    y = np.minimum(y, 1.0)
    return x, y


def apply_tone_curve(y, tone_x, tone_y):
    return np.interp(y, tone_x, tone_y).astype(np.float32)


def fit_chroma_transform(src_ycc, ref_ycc, degree, intercept, ridge, robust_iters):
    X = feature_matrix(src_ycc, degree=degree, model="standard", intercept=intercept)
    Y = ref_ycc[:, 1:3]

    weights = np.ones(X.shape[0], dtype=np.float32)
    for _ in range(robust_iters):
        coef = solve_rgb_coefficients(X, Y, weights, intercept, ridge)
        pred = X @ coef
        err = np.sqrt(np.mean((pred - Y) ** 2, axis=1))
        weights = robust_weights(err)

    return coef


def fit_hybrid_ycbcr_transform(
    src_rgb,
    ref_rgb,
    degree,
    intercept,
    ridge,
    robust_iters,
    tone_bins,
    shadow_lift,
    mid_lift,
    highlight_lift,
    max_darken,
):
    src_ycc = rgb_to_ycbcr(src_rgb)
    ref_ycc = rgb_to_ycbcr(ref_rgb)
    tone_x, tone_y = fit_tone_curve(
        src_ycc[:, 0],
        ref_ycc[:, 0],
        tone_bins,
        shadow_lift,
        mid_lift,
        highlight_lift,
        max_darken,
    )
    chroma_coef = fit_chroma_transform(
        src_ycc,
        ref_ycc,
        degree=degree,
        intercept=intercept,
        ridge=ridge,
        robust_iters=robust_iters,
    )
    return {
        "kind": "hybrid-ycbcr",
        "tone_x": tone_x,
        "tone_y": tone_y,
        "chroma_coef": chroma_coef,
        "degree": degree,
        "intercept": intercept,
    }


def fit_transform(
    src_rgb,
    ref_rgb,
    degree=2,
    model="standard",
    intercept=True,
    ridge=1e-4,
    robust_iters=5,
    luma_regularization=1.0,
    shadow_lift=0.18,
    mid_lift=0.045,
    highlight_lift=0.01,
    max_darken=0.22,
    tone_bins=32,
):
    if model == "rgb-luma-reg":
        return fit_rgb_transform(
            src_rgb,
            ref_rgb,
            degree=degree,
            feature_model="standard",
            intercept=intercept,
            ridge=ridge,
            robust_iters=robust_iters,
            luma_regularization=luma_regularization,
            shadow_lift=shadow_lift,
            mid_lift=mid_lift,
            highlight_lift=highlight_lift,
            max_darken=max_darken,
        )

    if model == "ycbcr":
        return fit_ycbcr_transform(
            src_rgb,
            ref_rgb,
            degree=degree,
            intercept=intercept,
            ridge=ridge,
            robust_iters=robust_iters,
        )

    if model == "hybrid-ycbcr":
        return fit_hybrid_ycbcr_transform(
            src_rgb,
            ref_rgb,
            degree=degree,
            intercept=intercept,
            ridge=ridge,
            robust_iters=robust_iters,
            tone_bins=tone_bins,
            shadow_lift=shadow_lift,
            mid_lift=mid_lift,
            highlight_lift=highlight_lift,
            max_darken=max_darken,
        )

    return fit_rgb_transform(
        src_rgb,
        ref_rgb,
        degree=degree,
        feature_model=model,
        intercept=intercept,
        ridge=ridge,
        robust_iters=robust_iters,
    )


def apply_transform(rgb, transform, degree=2, model="standard", intercept=True):
    if not isinstance(transform, dict):
        X = feature_matrix(rgb, degree=degree, model=model, intercept=intercept)
        return np.clip(X @ transform, 0.0, 1.0)

    if transform["kind"] == "rgb-poly":
        X = feature_matrix(
            rgb,
            degree=transform["degree"],
            model=transform["feature_model"],
            intercept=transform["intercept"],
        )
        return np.clip(X @ transform["coef"], 0.0, 1.0)

    if transform["kind"] == "ycbcr-poly":
        ycc = rgb_to_ycbcr(rgb)
        X = feature_matrix(
            ycc,
            degree=transform["degree"],
            model="standard",
            intercept=transform["intercept"],
        )
        return np.clip(ycbcr_to_rgb(X @ transform["coef"]), 0.0, 1.0)

    if transform["kind"] == "hybrid-ycbcr":
        ycc = rgb_to_ycbcr(rgb)
        X = feature_matrix(
            ycc,
            degree=transform["degree"],
            model="standard",
            intercept=transform["intercept"],
        )
        out_ycc = np.empty_like(ycc)
        out_ycc[:, 0] = apply_tone_curve(ycc[:, 0], transform["tone_x"], transform["tone_y"])
        out_ycc[:, 1:3] = X @ transform["chroma_coef"]
        return np.clip(ycbcr_to_rgb(out_ycc), 0.0, 1.0)

    raise ValueError(f"Unknown transform kind: {transform['kind']}")


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
    ap.add_argument(
        "--model",
        choices=["standard", "root-poly", "rgb-luma-reg", "ycbcr", "hybrid-ycbcr"],
        default="standard",
    )
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
    ap.add_argument(
        "--luma-regularization",
        type=float,
        default=1.0,
        help="Mode A only: weight for luma-control regularization.",
    )
    ap.add_argument(
        "--shadow-lift",
        type=float,
        default=0.18,
        help="Modes A/C: maximum positive luma lift allowed in shadows.",
    )
    ap.add_argument(
        "--mid-lift",
        type=float,
        default=0.045,
        help="Modes A/C: maximum positive luma lift encouraged in midtones.",
    )
    ap.add_argument(
        "--highlight-lift",
        type=float,
        default=0.01,
        help="Modes A/C: maximum positive luma lift encouraged in highlights.",
    )
    ap.add_argument(
        "--max-darken",
        type=float,
        default=0.22,
        help="Modes A/C: maximum negative luma movement allowed in the controlled target.",
    )
    ap.add_argument(
        "--tone-bins",
        type=int,
        default=32,
        help="Mode C only: number of bins for the controlled luma tone curve.",
    )
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
        luma_regularization=args.luma_regularization,
        shadow_lift=args.shadow_lift,
        mid_lift=args.mid_lift,
        highlight_lift=args.highlight_lift,
        max_darken=args.max_darken,
        tone_bins=args.tone_bins,
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
