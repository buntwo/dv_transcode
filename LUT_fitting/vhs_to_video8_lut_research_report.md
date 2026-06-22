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

## Addendum: Moving Beyond LUTs

After the LUT work above, visual review showed a consistent limitation: the learned LUTs corrected strong yellow indoor casts well, but bright non-shadow scenes could still look washed out. Further LUT variants tried to protect highlights, including luma-gated corrections and monotonic luma gates, but the improvements were small and sometimes introduced banding or flatness. This suggested that a simpler, more interpretable ffmpeg filtergraph might be a better production tool than another learned 3D LUT.

We tested several built-in ffmpeg color filters on top of a luma gamma correction:

- `vibrance`
- `greyedge`
- `colorbalance`
- `colorcorrect`
- `normalize`
- `grayworld`

`grayworld` was rejected visually. `greyedge` looked promising but was about twice as slow as the simple alternatives and is frame-adaptive, which makes it less attractive for production encodes. The best practical candidate became a fixed `eq + colorcorrect` chain.

Important implementation findings:

- `g_opt`, originally represented as a LUT, can be approximated by `eq=gamma`.
- `eq=gamma=1.46` matched the previous `lutyuv` gamma reference better than `1 / 0.68`.
- Baking `eq + colorcorrect` into a 3D LUT was not faster. The pure filtergraph was fastest in real FFV1 encode tests.
- `colorcorrect` operates in YUV according to ffmpeg documentation. Earlier differences between YUV-only and GBR-roundtrip filtergraphs came from the extra YUV -> GBR -> YUV round trip before `colorcorrect`, not from `colorcorrect` switching to RGB semantics.

## Experiment 9F: YUV-Only Filtergraph Search

We then committed to a source-native YUV-only production model with no explicit color-space conversions inside the optimized filtergraph:

```text
eq=gamma=G,
colorcorrect=rl=-A:bl=Q*A:rh=-K*A:bh=K*Q*A:saturation=S
```

The optimization used the existing `tone_score`:

```text
delta_e76_mean + 0.05 * nonshadow_positive_luma_bias + 0.25 * new_clip_pct
```

Search method:

- Stage 1 coarse grid over `G`, `A`, and `saturation`.
- Stage 2 grid over `Q` and `K` around top Stage 1 candidates.
- Stage 3 deterministic jitter around top Stage 2 candidates.
- Full-resolution re-score of the top 25 on train and validation.

The search script is:

```text
run_expt9F_yuv_only_search.py
```

Best train-selected filtergraph:

```bash
-vf "eq=gamma=1.43214046,colorcorrect=rl=-0.004439:bl=0.012896:rh=-0.004175:bh=0.012128:saturation=0.880000"
```

Validation-best searched filtergraph:

```bash
-vf "eq=gamma=1.38885721,colorcorrect=rl=-0.004548:bl=0.013689:rh=-0.004332:bh=0.013040:saturation=0.881156"
```

Comparison:

| Candidate | Train tone | Train dE76 | Validation tone | Validation dE76 |
|---|---:|---:|---:|---:|
| Previous YUV-only `cc_opt` | 9.662 | 9.064 | 11.351 | 10.390 |
| Previous GBR-roundtrip pure filtergraph | 9.614 | 9.078 | 11.156 | 10.321 |
| New train-best YUV-only | 9.301 | 8.751 | 10.744 | 9.873 |
| New validation-best YUV-only | 9.362 | 8.873 | 10.487 | 9.716 |

We selected the train-best candidate for production review, while keeping the validation-best candidate as a close alternative.

The final selected filtergraph is saved at:

```text
generated_video_pairs/evaluations/expt9F_yuv_only_search/optimized_filtergraph_train_best.txt
```

## Master Clip Production Review

The 10-bit FFV1 tape masters live at:

```text
/Volumes/TU/tu.brian.2026.05.09/data/masters/tape/
```

A new script generates 12 equally spaced 10-second review clips from each master, writing both:

- `clip_NNN_control.mkv`
- `clip_NNN_optimized.mkv`

The script reads each sampled segment once and produces both outputs in one ffmpeg invocation:

```text
generate_expt9F_master_clips.py
```

Outputs are written under:

```text
generated_video_pairs/evaluations/expt9F_yuv_only_search/transformed_videos/<master name>/
```

The output clips are forced to `yuv422p10le` FFV1 so the 10-bit review path does not collapse to 8-bit.

## Addendum: Shadow-Toe Curves, Gamma Weight, and Shrinkage

Visual review of the expt9F train-best filtergraph on real master clips showed a recurring failure mode: many clips were already acceptable before correction, and the full correction often made midtones/highlights look washed out even when metrics improved. This pushed the work away from "fit a stronger transform" and toward "how much correction should survive?"

We investigated dark clips by parsing the native `yuv422p10le` Y plane directly. The dark values were not mostly clipped into a single black floor. Instead, useful shadow content was spread across low Y codes, with substantial mass around the deep-shadow and upper-shadow ranges. This supported a shadow-toe lift, but not a global black pedestal.

### Expt10: Curve-Based Shadow Toe

Expt10 tested three methods:

1. RGB `curves=interp=pchip` with a one-parameter shadow-toe lift.
2. Y-only `lutyuv` piecewise-linear approximation of the same curve.
3. A 50% output blend of the previous expt9F filtergraph.

The metrics selected aggressive curves, but visual review showed patchiness and contouring. The derivative analysis explained why: the selected Y-only curve had an almost-flat segment through part of the shadow range, effectively collapsing nearby dark values into patches. The lesson was that smoothness alone is not enough; local contrast preservation matters, and metric optimization was rewarding average luma matching while ignoring patchiness.

### Expt11: Gamma Weight Search

Expt11 returned to the expt9F joint optimization family, adding `eq=gamma_weight`:

```text
eq=gamma=G:gamma_weight=W,
colorcorrect=rl=-A:bl=Q*A:rh=-K*A:bh=K*Q*A:saturation=S
```

The search used the same staged structure:

- Stage 1: trimmed coarse grid over `G`, `W`, `A`, and `S`.
- Stage 2: grid over `Q` and `K` for top Stage 1 candidates.
- Stage 3: local random refinement.
- Full train/validation evaluation of top candidates plus baselines.

The best expt11 100% filtergraph was:

```bash
eq=gamma=1.54313576:gamma_weight=0.60436964,colorcorrect=rl=-0.006119:bl=0.014374:rh=-0.001099:bh=0.002580:saturation=0.850752
```

By metrics, this beat the previous 50% blend:

| Split | Candidate | Score | dE76 | RGB MAE | Shadow Lift | Mid Lift | High Lift | New Clip % |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Train | expt11 best | 12.447 | 9.654 | 20.663 | 26.502 | 26.014 | 17.974 | 0.099 |
| Train | previous 50% | 13.804 | 11.934 | 26.108 | 17.677 | 17.626 | 12.341 | 0.074 |
| Validation | expt11 best | 12.749 | 9.710 | 19.177 | 26.832 | 26.548 | 17.538 | 0.198 |
| Validation | previous 50% | 13.457 | 11.433 | 22.439 | 17.913 | 17.945 | 12.031 | 0.184 |

However, the metrics also show that expt11 still lifts mids/highs more than the 50% blend. Since the visual complaint was washout, the metric win did not settle the question.

### Focused Master-Clip Review

To avoid filling the disk, we generated clips only for the seven Access videos used for review, resolving Access names back to master filenames with:

```text
/Users/btu/scratch/Videos/access_name_map.csv
```

The generator is:

```text
generate_expt11_access_master_clips.py
```

For each of the seven Access videos, it writes 12 equally spaced 10-second clips from the original 10-bit master, with four variants:

- `ctrl`
- `previous_50pct`
- `expt11_best`
- `expt11_best_50pct`

Outputs:

```text
generated_video_pairs/evaluations/expt11_gamma_weight_search/transformed_videos/access_master_clips/
```

The review player was moved to:

```text
play_review_videos.sh
```

and the focused master review can be launched with:

```bash
./play_review_videos.sh expt11-access
```

### Current Visual Winner

Visual review found little meaningful difference between `previous_50pct` and `expt11_best_50pct`. This suggests shrinkage is the important tool: blend a too-strong correction back toward the original instead of trusting the full fitted direction.

The current visual winner is:

```bash
split=2[orig][work];[work]eq=gamma=1.43214046,colorcorrect=rl=-0.004439:bl=0.012896:rh=-0.004175:bh=0.012128:saturation=0.880000[filt];[orig][filt]blend=all_expr='0.500000*A+0.500000*B'
```

This final filtergraph is saved permanently at:

```text
LUTs/vhs_to_video8_previous50_visual_winner.ffmpeg-filtergraph
```

It is not a LUT. It is a pure ffmpeg filtergraph selected by visual review because it retains useful correction while avoiding the over-brightened, washed-out look of stronger metric winners.

### Final Transcode Placement and Scale Flags

The selected color graph is meant to be inserted into the production transcode chain, not used as a whole-chain replacement by itself. The recommended placement is:

```text
bwdif=mode=send_field:parity=auto:deint=all,
drawbox=x=0:y=0:w=iw:h=3:color=black:t=fill,
drawbox=x=0:y=ih-12:w=iw:h=12:color=black:t=fill,
hqdn3d=1.5:1.125:2.25:1.6875,
<final color graph>,
scale=trunc(ih*dar/2)*2:ih:flags=lanczos+accurate_rnd+full_chroma_int,
setsar=1,
setparams=range=limited,
format=yuv420p10le
```

Rationale:

- `bwdif` and the top/bottom masking should happen before color correction.
- `hqdn3d` should stay before the gamma/color move so the lift does not amplify as much analog noise.
- The color graph should run before final resize/output normalization.
- The final scale should use explicit `flags=lanczos+accurate_rnd+full_chroma_int`, which looked preferable in the review set and removes ambiguity from ffmpeg defaults.

The saved filtergraph file documents this placement:

```text
LUTs/vhs_to_video8_previous50_visual_winner.ffmpeg-filtergraph
```

The executable line in that file remains the color graph only, because the review scripts read it directly and splice it into larger chains.

### Final Review Outputs and Timing

All-master final winner clips were generated under:

```text
generated_video_pairs/evaluations/final_visual_winner/transformed_videos/masters/
```

This produced 300 control clips and 300 winner clips: 12 ten-second samples from each of 25 master tapes.

Train/validation pair review clips were generated under:

```text
generated_video_pairs/evaluations/final_visual_winner/transformed_videos/pairs/
```

This produced 63 clips total: for each of 14 train pairs and 7 validation pairs, `ctrl`, `winner`, and `video8`.

The timing results were:

| Dataset | Timing Mode | Clips | Control Median | Winner Median | Median Overhead | Mean Overhead |
|---|---:|---:|---:|---:|---:|---:|
| Master clips | sequential isolated sample | 92 | 2.068 s | 2.476 s | 18.7% | 22.2% |
| Train/validation pairs | sequential | 21 | 3.141 s | 4.136 s | 28.0% | 30.5% |

After collecting enough isolated timing data, the remaining master clips were filled in parallel. Those parallel timings are useful for throughput but were not used for the overhead estimate.

Playback commands:

```bash
./play_review_videos.sh final-masters
./play_review_videos.sh final-pairs
```

### Denoise and Scale Review

A focused review set was generated for the seven Access videos:

```text
generated_video_pairs/evaluations/expt12_denoise_workflow_review/transformed_videos/access_master_clips/
```

For each selected Access video, three equally spaced 10-second clips were generated from the original 10-bit master. Each clip has four variants:

- `ctrl`: existing workflow without the new color graph.
- `with_denoise`: workflow with `hqdn3d` plus the final color graph.
- `no_denoise`: same chain but with `hqdn3d` removed.
- `with_denoise_lanczos`: denoise plus final color graph plus explicit `scale` flags.

Playback command:

```bash
./play_review_videos.sh denoise-review
```

Median render times for this review set:

| Variant | Clips | Median Time |
|---|---:|---:|
| `ctrl` | 21 | 4.538 s |
| `with_denoise` | 21 | 5.643 s |
| `no_denoise` | 21 | 5.481 s |
| `with_denoise_lanczos` | 21 | 5.637 s |

The explicit scale flags were selected for the final placement. The denoise choice remains visual, but the recommended production placement keeps `hqdn3d` before the color graph.

### PAL/China Caveat

Some clips recorded in China may have originated on PAL tapes, or on PAL-adjacent capture/playback paths. This was not confirmed, and optimizing a PAL-specific pipeline was intentionally out of scope. However, the visual review suggested that the final correction can be less successful on some China-recorded clips than on US-recorded clips.

This is plausible because PAL and NTSC-derived home-video paths can differ in setup/black-level behavior and transfer assumptions. In particular, a gamma lift selected from mostly NTSC-derived evidence may be too aggressive for clips that did not have the same setup-level/crushed-shadow history. The final filtergraph file now records this caveat so future use does not treat the correction as equally validated for every tape standard.
