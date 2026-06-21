# VHS to Video8 LUT Fitting Research Report

## Objective

The goal of this work was to build a general color correction LUT that makes VHS dub footage look closer to matching Video8 direct captures. The target correction is not restoration of lost VHS detail. It is a correction of systematic transfer-chain differences:

- VHS dubs are darker.
- VHS shadows are more crushed.
- VHS highlights are compressed.
- VHS is warmer/redder/yellower.
- VHS is more saturated.
- VHS has suppressed blue contribution after RGB decode.
- VHS has more noise, chroma smear, ringing, and border artifacts.

The intended production use remains:

```text
raw VHS master
-> standard cleanup / preprocessing
-> VHS-to-Video8 LUT
-> final encode
```

## Source Data

The initial clip manifests were:

```text
video_pairs_raw_training
video_pairs_raw_validation
```

These contained row-wise clip-selection notes with paired `-ss1` / `-ss2` offsets. We generated actual 10-second FFV1 pair clips from those manifests:

```text
generated_video_pairs/train
generated_video_pairs/validation
```

Each pair uses:

```text
pair_NNN_A.mkv = Video8 reference
pair_NNN_B.mkv = VHS source
```

The original generated clips were preserved as:

```text
generated_video_pairs/train_bad
generated_video_pairs/validation_bad
```

after manual frame-offset corrections were applied from:

```text
generated_video_pairs/generated_pairs_finetune
```

The correction script is:

```text
apply_pair_finetune.py
```

## Initial Fitting Problem

Early LUT candidates produced severe color errors, including skin tones mapping toward blue. The validation metrics were also suspicious: newly fit LUTs were worse than raw VHS, while an older known-good LUT still improved validation strongly.

The key diagnostic was fitting the current script against the two original known-good source pairs:

```text
/Users/btu/scratch/Videos/for_gpt_color_analysis
/Users/btu/scratch/Videos/for_gpt_color_analysis2
```

The in-memory polynomial fit improved training error, but the exported `.cube` performed badly in FFmpeg. This isolated the bug to LUT export/application rather than the fitting math.

## Cube Export Bug

The existing exporter wrote `.cube` entries in this order:

```python
for r in grid:
    for g in grid:
        for b in grid:
```

FFmpeg `lut3d` expects red to vary fastest, so the correct line order is:

```python
for b in grid:
    for g in grid:
        for r in grid:
```

An identity LUT exported in the old order mapped red to blue. After fixing the exporter, refitting the two known-good pairs matched the existing known-good LUT:

| Candidate | RGB MAE | dE76 |
|---|---:|---:|
| Raw | 34.24 | 16.14 |
| Fixed refit 85% | 15.55 | 8.19 |
| Old known-good 85% | 15.57 | 8.35 |

Conclusion: the polynomial fitting methodology was sound enough to reproduce the known-good LUT once cube export order was fixed.

## Geometry Issue

A/B clip dimensions were not initially the same:

```text
A: 640x480
B: 648x486
```

Both were tagged/displayed as 4:3, but the active image areas did not align perfectly. B also had top/bottom black masking and side noise. Fitting with naive resizing and loose border masks risked pairing colors from different image content.

We generated visual alignment overlays:

```text
generate_alignment_overlays.py
```

and numerical shift diagnostics:

```text
diagnose_pair_shift.py
```

The shift diagnostic found that B generally needed to move slightly upward after resizing:

```text
pair_001: dx=1, dy=-3
pair_002: dx=1, dy=-3
pair_006: dx=1, dy=-1
pair_010: dx=1, dy=-3
pair_014: dx=1, dy=-2
```

We then implemented per-pair geometry normalization:

```text
normalize_pair_geometry.py
```

This optimizes a center-scale plus translation transform for B against A using luma-edge correlation, then writes normalized FFV1 pairs.

Normalized output directories:

```text
generated_video_pairs/train_geometry_normalized
generated_video_pairs/validation_geometry_normalized
```

Manifests:

```text
generated_video_pairs/train_geometry_normalized_pairs.txt
generated_video_pairs/validation_geometry_normalized_pairs.txt
```

The optimizer found a consistent pattern:

- `sx` mostly `1.000`
- `sy` mostly `1.011-1.017`
- `dx_full` mostly `0-2 px`
- `dy_full` mostly `-1 to -2 px`

Edge correlation improved for every pair.

## Fitter Defaults and Methodology

The fitter now supports:

- native-size extraction with no `scale=` unless `--sample-width` / `--sample-height` are passed
- standard polynomial and root-polynomial models
- intercept or no-intercept variants
- multiple output strengths from one fit
- fixed FFmpeg-compatible `.cube` order
- explicit side masks
- random, pair-balanced, and pair-luma-balanced sampling
- deterministic seed control

The locked default model is now:

```text
model: standard polynomial
degree: 2
intercept: yes
strength: 0.85
native extraction: yes
mask: left 56, right 56, top 40, bottom 96
max samples: 1,000,000
sampling: random
seed: 2002
LUT size: 33
fps: 2
```

Default command:

```bash
uv run python fit_vhs_lut.py \
  --pairs generated_video_pairs/train_geometry_normalized_pairs.txt \
  --out LUTs/final_vhs_to_video8_standard_i_85.cube
```

## Experiment 1: Model Selection

Setup:

- geometry-normalized train/validation
- native-size extraction
- 2M random samples
- full-res mask: `56/56/40/96`
- strength variants: 100%, 85%
- models:
  - standard degree 2, intercept
  - standard degree 2, no intercept
  - root-poly degree 2, intercept
  - root-poly degree 2, no intercept

Top results:

| Candidate | RGB MAE | dE76 |
|---|---:|---:|
| Raw | 35.44 | 16.99 |
| Standard + intercept 85% | 18.74 | 9.13 |
| Standard + intercept 100% | 19.13 | 9.19 |
| Root-poly + intercept 85% | 19.23 | 9.43 |
| Standard no-intercept 85% | 19.45 | 9.42 |

Conclusion: standard degree-2 polynomial with intercept was the best aggregate model. 85% strength was better than 100% by metrics and safer visually.

## Experiment 2: Sampling Strategy

Fixed model:

```text
standard degree-2 + intercept
```

Compared:

- random
- pair-balanced
- pair-luma-balanced

Each at 2M samples, 70% and 85% strengths.

Results:

| Candidate | RGB MAE | dE76 |
|---|---:|---:|
| Raw | 35.44 | 16.99 |
| Pair-balanced 85% | 18.70 | 9.24 |
| Random 85% | 18.74 | 9.13 |
| Pair-luma-balanced 85% | 19.09 | 9.44 |
| Pair-balanced 70% | 19.57 | 9.74 |
| Random 70% | 19.75 | 9.70 |
| Pair-luma-balanced 70% | 19.80 | 9.89 |

Conclusion: pair-balanced was marginally best by RGB MAE, random was marginally best by dE76, and visual review favored random 85%. Pair-luma-balanced did not help in the current implementation.

## Experiment 3: Random Sampling Sensitivity

Fixed:

```text
standard degree-2 + intercept
random sampling
85% strength
native-size normalized data
```

Compared sample counts with three seeds each:

| Samples | RGB MAE mean +/- std | dE76 mean +/- std |
|---:|---:|---:|
| 500k | 18.498 +/- 0.017 | 9.091 +/- 0.011 |
| 1M | 18.426 +/- 0.016 | 9.026 +/- 0.011 |
| 2M | 18.760 +/- 0.014 | 9.148 +/- 0.013 |
| 5M | 20.470 +/- 0.219 | 10.054 +/- 0.158 |

Conclusion:

- seed-to-seed variation is tiny at 500k-2M
- 1M random is best by validation metrics
- more samples are not necessarily better
- 5M and 20M random degrade, likely because more hard/outlier/misaligned/noisy pixels enter the fit

Locked choice:

```text
1M random samples
seed 2002
```

## Experiment 4: Downscaling the Fitting Input

Compared native fitting against downscaled fitting. Sample counts were scaled by pixel count:

```text
640x480 native: 1M samples
320x240:        250k samples
160x120:        64k samples
```

Results:

| Candidate | RGB MAE | dE76 |
|---|---:|---:|
| Raw | 35.44 | 16.99 |
| Native 1M | 18.41 | 9.02 |
| 320x240 250k | 18.67 | 9.17 |
| 160x120 64k | 18.99 | 9.28 |

Conclusion: after geometry normalization, native-size extraction with masks is best by metrics. Downscaling is still viable, especially 320x240, but not preferred.

## Current Best LUT

Best current candidate:

```text
generated_video_pairs/evaluations/expt3_sampling_sensitivity/luts/standard_i_random_1000000_seed2002_strength85.cube
```

The fitter defaults are now set so this configuration can be reproduced directly.

## Key Lessons

1. `.cube` export order matters. The old exporter produced channel-permuted LUTs under FFmpeg.
2. Geometry must be normalized before fitting. Color fitting assumes corresponding sample locations represent the same image content.
3. Native-size fitting works best once geometry is fixed.
4. More samples are not always better. Random sampling beyond about 1M increased sensitivity to hard/outlier pixels.
5. Metrics and visual review must both be used. RGB MAE and dE76 are useful for shortlisting, but washed-out highlights and skin tone acceptability require visual inspection.

## Recommended Next Steps

1. Generate a final LUT using the new fitter defaults.
2. Review the final LUT on representative VHS-only clips, especially bright outdoor scenes and skin tones.
3. Consider a final strength sweep around the selected model if washout remains visible:

```text
70%, 75%, 80%, 85%
```

4. Preserve geometry-normalized train/validation data and current experiment outputs as the reproducible basis for future work.

