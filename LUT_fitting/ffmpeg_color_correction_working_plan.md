# FFmpeg Color Correction Working Plan

Goal: explore color-only correction for VHS-to-Video8 restoration without tape cleanup filters.

## Strategy

Use an incremental workflow first, then a narrow joint tune:

1. Lock A: luma-only tone shaping.
   - Must lift crushed shadows while protecting midtones/highlights.
   - Must be monotonic and avoid banding.
   - Keep color/chroma untouched.
2. Add B: manual chroma/yellow correction on top of fixed A.
   - Reduce indoor yellow cast.
   - Avoid blue/magenta skin drift and preserve non-yellow scenes.
3. Test C: automatic white balance on top of fixed A.
   - Try grayworld/greyedge as candidates only; likely higher risk for home video.
4. Narrow joint tune after A and B/C are understood.

## A Candidate

Prefer a monotonic control-point luma curve over a single gamma curve.

Initial control points:

- 0.00 -> 0.00
- 0.10 -> 0.16
- 0.25 -> 0.31
- 0.50 -> 0.52
- 0.75 -> 0.75
- 1.00 -> 1.00

The curve should be applied to luma only, preserving chroma as much as practical.

## Expt9A / A: Luma-Only Candidates

Script: `run_expt9_luma_only.py`

Output root: `generated_video_pairs/evaluations/expt9A_luma_only`

Candidate set:

- `g90`: luma gamma 0.90
- `g82`: luma gamma 0.82
- `g74`: luma gamma 0.74
- `g62`: luma gamma 0.62
- `g50`: luma gamma 0.50
- `cp_mild`: control points `(0.00,0.00) (0.10,0.13) (0.25,0.29) (0.50,0.51) (0.75,0.75) (1.00,1.00)`
- `cp_base`: control points `(0.00,0.00) (0.10,0.16) (0.25,0.31) (0.50,0.52) (0.75,0.75) (1.00,1.00)`
- `cp_strong`: control points `(0.00,0.00) (0.10,0.19) (0.25,0.35) (0.50,0.54) (0.75,0.75) (1.00,1.00)`

Implementation:

- Export each candidate as a 33^3 `.cube`.
- Convert RGB to Rec.601-style YCbCr, transform only Y, preserve Cb/Cr.
- Reduce chroma only when needed to keep the transformed RGB in gamut.
- Generate validation metrics plus Access and validation-pair review grids.

Completed outputs:

- Candidate LUTs: `generated_video_pairs/evaluations/expt9A_luma_only/luts/`
- Curve samples: `generated_video_pairs/evaluations/expt9A_luma_only/luma_curves.csv`
- Candidate manifest: `generated_video_pairs/evaluations/expt9A_luma_only/candidate_manifest.csv`
- Validation metrics: `generated_video_pairs/evaluations/expt9A_luma_only/evaluation/validation_metrics.csv`
- Access review grids: `generated_video_pairs/evaluations/expt9A_luma_only/access_grid/`
- Validation pair grids: `generated_video_pairs/evaluations/expt9A_luma_only/validation_pair_grid/`
- Access review grids with previous best as second column: `generated_video_pairs/evaluations/expt9A_luma_only/access_grid_with_best/`
- Validation pair grids with previous best after VHS/Video8: `generated_video_pairs/evaluations/expt9A_luma_only/validation_pair_grid_with_best/`
- Focused deeper-gamma Access grids: `generated_video_pairs/evaluations/expt9A_luma_only/access_grid_gamma_deeper_with_best/`
- Focused deeper-gamma validation grids, 3 frames per 10-second clip: `generated_video_pairs/evaluations/expt9A_luma_only/validation_pair_grid_gamma_deeper_with_best/`

Validation metric snapshot:

| candidate | RGB MAE | dE76 | luma MAE | shadow lift | mid lift | high lift | nonshadow +Y bias | new clip % |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| raw | 35.19 | 16.56 | 33.52 | 0.00 | 0.00 | 0.00 | 3.27 | 0.000 |
| g90 | 28.87 | 14.48 | 27.27 | 7.57 | 8.79 | 5.47 | 4.51 | 0.079 |
| g82 | 23.88 | 12.86 | 22.49 | 15.03 | 17.25 | 10.40 | 6.36 | 0.213 |
| g74 | 20.20 | 11.71 | 19.11 | 23.79 | 26.13 | 15.45 | 9.20 | 0.408 |
| g62 | 20.31 | 11.80 | 19.95 | 39.52 | 40.86 | 23.31 | 16.52 | 0.971 |
| g50 | 29.74 | 14.98 | 30.37 | 59.17 | 57.35 | 31.47 | 28.91 | 2.230 |
| cp_mild | 30.35 | 14.89 | 28.69 | 7.96 | 5.29 | -0.05 | 3.76 | 0.002 |
| cp_base | 27.19 | 13.82 | 25.55 | 14.27 | 8.84 | 0.41 | 4.26 | 0.009 |
| cp_strong | 23.10 | 12.51 | 21.60 | 22.65 | 15.94 | 1.27 | 5.74 | 0.036 |

Initial read: gamma curves reduce aggregate error, but they lift high luma substantially. Control-point curves protect highlights much better; `cp_strong` is the most interesting first visual candidate because it gets close to `g74` metrics while keeping high-luma lift near 1.3 levels instead of 15.4.

## Expt9A Gamma Grid

Script: `run_expt9A_gamma_grid.py`

Grid searched luma gamma values from 0.60 through 0.78 in 0.02 increments. Objective was lowest mean validation dE76, with RGB MAE as a secondary check.

Best grid value:

- `g68`: gamma 0.68
- RGB MAE 19.19
- dE76 11.40
- luma MAE 18.37

Completed outputs:

- LUTs: `generated_video_pairs/evaluations/expt9A_gamma_grid/luts/`
- Summary: `generated_video_pairs/evaluations/expt9A_gamma_grid/gamma_grid_summary.csv`
- Best marker: `generated_video_pairs/evaluations/expt9A_gamma_grid/best_gamma.txt`
- Access grids: `generated_video_pairs/evaluations/expt9A_gamma_grid/access_grid_best_gamma/`
- Validation grids, 3 frames per 10-second clip: `generated_video_pairs/evaluations/expt9A_gamma_grid/validation_pair_grid_best_gamma/`

## Expt9A cp_gammaopt

Script: `run_expt9A_cp_gammaopt.py`

Purpose: match the optimal gamma curve in the lower luma region, then taper like the control-point configs to protect highlights.

Control points, derived from gamma 0.68 at the low end:

- 0.00 -> 0.00
- 0.10 -> 0.2089
- 0.25 -> 0.3896
- 0.50 -> 0.56
- 0.75 -> 0.75
- 1.00 -> 1.00

Completed outputs:

- LUT: `generated_video_pairs/evaluations/expt9A_cp_gammaopt/luts/expt9a_luma_cp_gammaopt.cube`
- Candidate manifest: `generated_video_pairs/evaluations/expt9A_cp_gammaopt/candidate_manifest.csv`
- Curve samples: `generated_video_pairs/evaluations/expt9A_cp_gammaopt/luma_curves.csv`
- Validation metrics: `generated_video_pairs/evaluations/expt9A_cp_gammaopt/evaluation/validation_metrics.csv`
- Access grids: `generated_video_pairs/evaluations/expt9A_cp_gammaopt/access_grid/`
- Validation grids, 3 frames per 10-second clip: `generated_video_pairs/evaluations/expt9A_cp_gammaopt/validation_pair_grid/`

Metric comparison:

| candidate | RGB MAE | dE76 | luma MAE | shadow lift | mid lift | high lift | nonshadow +Y bias | new clip % |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| BEST | 18.19 | 8.88 | 17.79 | 28.82 | 33.37 | 21.30 | 12.15 | 0.245 |
| g68 | 19.19 | 11.40 | 18.37 | 31.25 | 33.12 | 19.35 | 12.21 | 0.639 |
| cp_gammaopt | 21.03 | 11.88 | 19.79 | 29.66 | 22.97 | 2.11 | 7.89 | 0.088 |
| cp_strong | 23.10 | 12.51 | 21.60 | 22.65 | 15.94 | 1.27 | 5.74 | 0.036 |

Initial read: `cp_gammaopt` sacrifices some aggregate RGB/dE versus `g68`, but it keeps most of the low-end lift while sharply reducing high-luma lift and new clipping. This is a good visual-review candidate if gamma-opt looks too washed out.

## Expt9B / B: Manual Color-Balance Knobs

Scripts:

- `run_expt9BC_filters.py`
- `run_expt9BC_refine.py`
- `run_expt9B_vibrance_deep.py`

Fixed A bases:

- `g_opt`: alias for `g68`; LUT copied to `generated_video_pairs/evaluations/expt9A_gamma_grid/luts/expt9a_luma_g_opt.cube`
- `cp_gammaopt`: `generated_video_pairs/evaluations/expt9A_cp_gammaopt/luts/expt9a_luma_cp_gammaopt.cube`

First pass tested manual `colorbalance`, manual `colorcorrect`, `hue=s=0.94`, and `vibrance=intensity=-0.12`. Manual `vibrance` was the best B family for both A bases.

Refinement tested `vibrance` intensities from `-0.04` to `-0.28`; because `-0.28` won or tied at the boundary, a final B-only extension tested `-0.20`, `-0.28`, `-0.36`, `-0.44`, `-0.52`, `-0.60`, and `-0.72`.

Final B winners:

| base | candidate | filter suffix | RGB MAE | dE76 | tone score |
|---|---|---|---:|---:|---:|
| g_opt | g_opt_B_vib_28 | `vibrance=intensity=-0.28` | 18.84 | 9.49 | 10.08 |
| cp_gammaopt | cp_gammaopt_B_vib_36 | `vibrance=intensity=-0.36` | 20.65 | 9.99 | 10.37 |

Completed outputs:

- Broad B/C first pass: `generated_video_pairs/evaluations/expt9BC_color_filters/`
- B/C refinement: `generated_video_pairs/evaluations/expt9BC_refine/`
- Final B vibrance extension: `generated_video_pairs/evaluations/expt9B_vibrance_deep/`
- Final B Access grids: `generated_video_pairs/evaluations/expt9B_vibrance_deep/access_grid_winners/`
- Final B validation grids, 3 frames per 10-second clip: `generated_video_pairs/evaluations/expt9B_vibrance_deep/validation_grid_winners/`

Initial read: manual vibrance reduction improves aggregate validation metrics materially. This is not a yellow-specific correction; it appears to work by reducing excess chroma/saturation after luma lift. Visual review is important because too much vibrance reduction can make home-video colors feel dead even when dE improves.

## Expt9C / C: Automatic White Balance

Scripts:

- `run_expt9BC_filters.py`
- `run_expt9BC_refine.py`

First pass tested `grayworld`, `greyedge` variants, and `colorcorrect=analyze={average,median,minmax}` variants. Refinement searched around the best greyedge family with `difford=2`, `minknorm={3,5,8}`, and `sigma={0.5,1,2}`.

Final C winners:

| base | candidate | filter suffix | RGB MAE | dE76 | tone score |
|---|---|---|---:|---:|---:|
| g_opt | g_opt_C_ge_d2_n5_s1p0 | `greyedge=difford=2:minknorm=5:sigma=1.0` | 18.71 | 10.26 | 10.85 |
| cp_gammaopt | cp_gammaopt_C_ge_d2_n5_s2p0 | `greyedge=difford=2:minknorm=5:sigma=2.0` | 21.04 | 11.03 | 11.41 |

Completed outputs:

- C Access grids from refinement: `generated_video_pairs/evaluations/expt9BC_refine/expt9C_access_grid_winners/`
- C validation grids, 3 frames per 10-second clip: `generated_video_pairs/evaluations/expt9BC_refine/expt9C_validation_grid_winners/`

Initial read: automatic greyedge improves over A-only but is weaker than the final B vibrance reduction by the validation metrics. It remains worth visual review because it may correct some indoor color casts differently from simple desaturation, but it is more scene-dependent.

No joint tuning has been done yet. B and C were optimized independently on top of fixed A bases.

## Expt9C Greyworld Review

Script: `run_expt9_greyworld_review.py`

Purpose: create a focused higher-resolution visual review for `grayworld` alongside the current fixed-A and B/C winners. `cp_gammaopt` is intentionally excluded because visual review suggested it often looks flat from high-end compression.

Access columns:

- `CTRL`
- `g_opt`
- `g_opt_vibrance`: `g_opt,vibrance=intensity=-0.28`
- `g_opt_greyedge`: `g_opt,greyedge=difford=2:minknorm=5:sigma=1.0`
- `g_opt_greyworld`: `g_opt,grayworld`

Validation columns:

- `Video8`
- `CTRL`
- `g_opt`
- `g_opt_vibrance`
- `g_opt_greyedge`
- `g_opt_greyworld`

Completed outputs:

- High-resolution Access grids: `generated_video_pairs/evaluations/expt9C_greyworld_review/access_grid/`
- High-resolution validation grids, 3 frames per 10-second clip: `generated_video_pairs/evaluations/expt9C_greyworld_review/validation_grid/`
- Cached individual frames: `generated_video_pairs/evaluations/expt9C_greyworld_review/frames/`

The grid generator now saves every individual rendered cell. This should make future recomposition requests cheaper when the same frames/columns are reused.

## Current Visual Preference

After reviewing the greyworld comparison grids, the current visual leader is:

`g_opt + greyedge=difford=2:minknorm=5:sigma=1.0`

This is preferred visually over `g_opt + vibrance=-0.28`, despite vibrance winning the validation metric. Treat greyedge as the current candidate to beat.

The partially started expt9D built-in-only optimization for `colorbalance`, `colorcorrect`, and `normalize` was interrupted and should be considered incomplete.

## Expt9D / Built-In Filter Alternatives

Script: `run_expt9D_builtin_filters.py`

Purpose: optimize built-in ffmpeg alternatives on top of fixed `g_opt`, excluding `grayworld` and `greyedge`.

Candidate families:

- `colorbalance` blue-only and cool-push variants with `pl=1`
- manual `colorcorrect` variants with red-down/blue-up and saturation levels
- auto `colorcorrect=analyze={average,median,minmax}` variants
- conservative `normalize` variants over strength and independence

Best candidates by family:

| family | candidate | filter suffix | RGB MAE | dE76 | tone score |
|---|---|---|---:|---:|---:|
| colorbalance | `cb_blue_04` | `colorbalance=bs=0.040:bm=0.035:bh=0.008:pl=1` | 19.07 | 11.16 | 11.86 |
| colorcorrect | `cc_manual_01_sat90` | `colorcorrect=rl=-0.010:bl=0.020:rh=-0.005:bh=0.010:saturation=0.90` | 18.85 | 10.05 | 10.71 |
| normalize | `norm_s20_i100` | `normalize=strength=0.20:independence=1.00` | 19.17 | 11.37 | 12.15 |

Completed outputs:

- Summary: `generated_video_pairs/evaluations/expt9D_builtin_filters/experiment_summary.csv`
- Winners: `generated_video_pairs/evaluations/expt9D_builtin_filters/winners.csv`
- High-resolution Access grids: `generated_video_pairs/evaluations/expt9D_builtin_filters/access_grid_winners/`
- High-resolution validation grids: `generated_video_pairs/evaluations/expt9D_builtin_filters/validation_grid_winners/`
- Cached individual frames: `generated_video_pairs/evaluations/expt9D_builtin_filters/frames/`

The expt9D grids were overwritten to include the current visual leader as a direct comparison. Access columns are now:

- `CTRL`
- `g_opt`
- `g_opt_greyedge`
- `g_opt_vibrance_28`
- `cb_blue_04`
- `cc_manual_01_sat90`
- `norm_s20_i100`

Validation grids add `Video8` as the first column before the same candidates.

Initial read: `colorcorrect` manual is the best of these built-in alternatives by metrics. It is cheaper and safer than greyedge, but the current visual leader remains `g_opt + greyedge=difford=2:minknorm=5:sigma=1.0` unless visual review of expt9D says otherwise.
