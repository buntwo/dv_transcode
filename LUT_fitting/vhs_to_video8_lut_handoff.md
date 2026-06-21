# VHS → Video8 Look-Matching LUT Pipeline Handoff

This document summarizes the workflow we have iterated on for correcting VHS dubs so they visually sit closer to the corresponding Video8 direct captures. It is intended as a handoff for Codex to continue building tooling around the LUT fitting and validation pipeline.

The user has already saved an earlier LUT fitting script. Do **not** re-create that script verbatim unless needed. Treat it as the current v1 fitter and extend around it.

---

## 1. Project goal

The working hypothesis is:

```text
The VHS tapes are analog dubs made from original Video8 tapes.
```

The observed VHS dub characteristics, relative to the Video8 direct captures, are consistent across tested pairs:

- VHS is darker overall.
- VHS shadows are more crushed.
- VHS highlights are compressed.
- VHS is more saturated.
- VHS is warmer / redder / yellower.
- VHS has suppressed blue contribution after decoding to RGB.
- VHS has more noise and chroma smear.
- VHS has some true fine-detail loss, but a lot of high-frequency energy is noise/ringing rather than real detail.

The goal is **not** to recover information lost in the VHS generation. The goal is to correct the systematic transfer-chain look:

```text
VHS dub look → closer to Video8 direct-capture look
```

Expected use:

```text
raw VHS master
→ standard preprocessing
→ general VHS-to-Video8 LUT
→ final 10-bit HEVC encode
```

---

## 2. Current empirical findings

### 2.1 Raw VHS vs filtered/transcoded VHS

A raw VHS sample was compared against the user’s previously filtered/transcoded VHS. After raw VHS was deinterlaced with `bwdif=mode=send_field`, the raw and filtered/transcoded VHS were very similar in broad color/level behavior.

Important prior conclusion:

```text
The user’s filter/transcode chain is not the main source of the Video8-vs-VHS color difference.
The color/look problem is already present in the raw VHS dub.
```

The filtered/transcoded VHS had roughly similar color stats to raw VHS, but with less noise and slight softening. Therefore, for convenience, fitting LUTs from the VHS transcodes is acceptable, as long as final application happens at a comparable processing stage.

### 2.2 Generated LUTs so far

Previously generated LUTs included pair-specific and combined LUTs such as:

```text
raw_vhs_to_video8_look_poly33.cube
general_vhs_transcode_to_video8_2pair_poly33.cube
general_vhs_transcode_to_video8_2pair_poly33_strength85.cube
newpair_vhs8_to_video8_look_poly33.cube
general_vhs_to_video8_channel_curves33.cube
general_vhs_to_video8_channel_curves33_strength85.cube
```

The most useful current candidate is:

```text
general_vhs_transcode_to_video8_2pair_poly33_strength85.cube
```

But this was trained on only two matched pairs, so it should be treated as a promising early general correction, not a final universal LUT.

### 2.3 Prior two-pair combined LUT result

From two matched pairs, the combined LUT improved both scenes and generalized better than the one-pair LUT.

Prior rough metrics:

| Test | Uncorrected VHS | Previous one-scene LUT | New two-pair LUT |
|---|---:|---:|---:|
| Overall mean ΔE | ~15.0 | ~8.4 | ~6.5 |
| Pair 1 mean ΔE | ~15.7 | ~6.1 | ~6.2 |
| Pair 2 mean ΔE | ~14.3 | ~10.7 | ~6.7 |

Interpretation:

```text
The correction direction is stable across at least two scenes.
A general LUT is viable.
More matched pairs are needed before batch use.
```

---

## 3. Core principle: train at the same stage where the LUT will be applied

This is the most important technical rule.

If the final chain is:

```text
raw VHS
→ bwdif
→ hqdn3d
→ scale/SAR
→ LUT
→ encode
```

then the training source should represent:

```text
raw VHS
→ bwdif
→ hqdn3d
→ scale/SAR
```

not raw interlaced VHS, and not a differently processed version.

Since the previous tests showed raw and transcoded VHS lead to similar LUTs, the current practical compromise is:

```text
Train LUT from already-transcoded VHS clips.
Apply LUT during the raw VHS final transcode after equivalent preprocessing.
```

This is acceptable because the dominant VHS color bias appears to originate in the dub/capture path, not in the user’s current filtering.

---

## 4. Recommended production filter order

The LUT should be placed near the end of the `-vf` chain, after the standard VHS cleanup/preprocessing, but before final output pixel format conversion.

Recommended order:

```text
bwdif send_field
→ border masking/cropping if needed
→ denoise / cleanup
→ scale / SAR correction
→ RGB working format
→ LUT
→ final 10-bit YUV format
→ final black border drawbox if desired
→ range/color tags
```

Typical command shape:

```bash
ffmpeg -i raw_vhs.mkv \
-vf "bwdif=mode=send_field:parity=auto:deint=all,
     hqdn3d=1.5:1.125:2.25:1.6875,
     scale='trunc(ih*dar/2)*2:ih':flags=lanczos,
     setsar=1,
     format=gbrp,
     lut3d=LUTs/general_vhs_transcode_to_video8_2pair_poly33_strength85.cube:interp=tetrahedral,
     format=yuv420p10le,
     drawbox=x=0:y=0:w=iw:h=3:color=black:t=fill,
     drawbox=x=0:y=ih-12:w=iw:h=12:color=black:t=fill,
     setparams=range=limited" \
-c:v libx265 -crf 18 -preset slow \
-pix_fmt yuv420p10le \
-color_range tv -colorspace smpte170m -color_primaries smpte170m -color_trc smpte170m \
-c:a copy output.mkv
```

Notes:

- `bwdif=mode=send_field` preserves field-rate motion: NTSC 29.97i becomes 59.94p.
- `hqdn3d` should usually come before the LUT so color correction does not lift/amplify VHS noise before cleanup.
- `format=gbrp` should be immediately before `lut3d`.
- `format=yuv420p10le` should be immediately after `lut3d` for 10-bit HEVC output.
- `lut3d=...:interp=tetrahedral` should be used; tetrahedral interpolation is preferred.
- `setparams=range=limited` is tagging, not a levels remap.
- Add explicit encoder/container tags for SD NTSC video: `smpte170m` / limited range.
- Do not apply another strong `eq`, `curves`, or `colorbalance` after the LUT unless intentionally doing per-scene grading.

### 4.1 Drawbox placement

The preferred final output order puts `drawbox` near the end so black borders remain clean black and do not influence color fitting/LUT behavior.

However, if the head-switching/border noise is extreme and contaminates denoising, placing the masking before denoise is defensible.

Preferred for clean output:

```text
preprocess → LUT → format → drawbox
```

Defensible if border noise is disruptive:

```text
drawbox → denoise → scale → LUT
```

---

## 5. Choosing matched pairs

The goal is to build a general correction for the VHS transfer chain, not a scene-specific grade. Use several short, diverse, matched Video8/VHS pairs.

Recommended count:

```text
5–10 matched pairs
10–20 seconds each
```

More diverse clips are better than longer clips.

Prefer:

```text
8 clips × 10 seconds
```

over:

```text
2 clips × 60 seconds
```

### 5.1 Required pair properties

Each pair should contain:

```text
Video8 direct clip | corresponding VHS dub/transcode clip
```

Requirements:

- Same scene.
- Same approximate time range.
- Same moment if possible.
- Minimal scene cuts inside the selected clip.
- No large mismatch in temporal offset.
- Similar framing, or at least enough shared image area to sample from.
- Avoid very large camera motion if possible.

Small alignment errors are tolerable because the fitter downsamples/blurs samples, but large offsets will corrupt the fit.

### 5.2 What types of scenes to select

Select clips that span the actual visual range of the tapes.

Priority list:

1. **Indoor people / skin tones**  
   Most important. Human skin makes color errors obvious.

2. **Outdoor daylight**  
   Prevents a LUT that is overly optimized for indoor warm lighting.

3. **Neutral walls / white clothing / paper / tablecloth**  
   Crucial for identifying yellow/blue/green casts.

4. **Dark indoor scene**  
   Tests shadow lift and black crush handling.

5. **Bright scene with windows / sky / highlights**  
   Tests highlight rolloff and clipping behavior.

6. **Saturated objects**  
   Toys, flowers, colorful clothes, party decorations.

7. **Foliage / grass**  
   Useful because greens can become yellow/cyan if overcorrected.

8. **Mixed lighting**  
   Example: daylight from window plus tungsten lamps.

9. **Faces plus background colors**  
   Good for detecting whether walls improve at the expense of skin.

10. **Ordinary random representative clip**  
   A normal scene from the tape, to avoid optimizing only extreme cases.

### 5.3 What to avoid or downweight

Avoid or downweight clips with:

- scene cuts
- fade transitions
- heavy camera motion
- zooms
- severe VHS mistracking
- dropouts
- large exposure changes
- large framing mismatch
- mismatched moments
- lots of date text / graphics / borders
- head-switching noise in the sample region
- sources from a different deck/capture path unless intentionally included

---

## 6. Train/validation split

If possible, use matched pairs for both training and validation.

Example with 8 pairs:

```text
6 pairs for training
2 pairs held out for validation
```

Why:

- VHS-only held-out clips can show whether corrected footage looks natural.
- Matched Video8/VHS held-out pairs can show whether the LUT actually moves VHS closer to Video8.

Unreferenced VHS-only validation is still useful, but it cannot answer “did this match the original Video8 better?”

---

## 7. LUT fitting approach

Do **not** average `.cube` LUTs.

Correct method:

```text
pool matched VHS→Video8 color samples across all training pairs
→ fit one shared transform
→ export a single `.cube` LUT
```

The v1 fitter already saved by the user does approximately:

```text
matched clips
→ decode frames to RGB
→ downsample/blur via extraction scale
→ exclude borders/date/clipped pixels
→ collect VHS RGB samples and Video8 RGB targets
→ fit RGB→RGB polynomial transform
→ export 33³ `.cube`
```

Current recommended default:

```text
ordinary degree-2 polynomial + intercept + robust fitting + ridge regularization
```

Export both:

```text
full strength LUT
85% strength LUT
```

The 85% LUT is safer for batch use when training data is limited.

---

## 8. Candidate fit types to compare

For future tooling, Codex should support fitting and comparing multiple model variants:

```text
1. Ordinary degree 1 / linear matrix, with intercept
2. Ordinary degree 1 / linear matrix, no intercept
3. Ordinary degree 2, with intercept
4. Ordinary degree 2, no intercept
5. Root-polynomial degree 2, with intercept
6. Root-polynomial degree 2, no intercept
7. Optional ordinary degree 3, only if enough training/validation data exists
```

Avoid degree 4+ unless heavily constrained and well validated.

### 8.1 Ordinary degree-2 features

With intercept:

```text
[1, R, G, B, R², G², B², RG, RB, GB]
```

Without intercept:

```text
[R, G, B, R², G², B², RG, RB, GB]
```

Advantages:

- More flexible.
- Can model brightness-dependent tone curve problems.
- Better suited to VHS look correction because the VHS issue includes black-level/pedestal, shadow crush, highlight compression, and gamma-encoded nonlinearities.

Disadvantages:

- More overfit risk.
- Exposure-dependent behavior.
- Can learn scene-specific artifacts if training data is weak.

### 8.2 Root-polynomial degree-2 features

Without intercept:

```text
[R, G, B, sqrt(RG), sqrt(RB), sqrt(GB)]
```

With intercept:

```text
[1, R, G, B, sqrt(RG), sqrt(RB), sqrt(GB)]
```

Root-polynomial correction is intended to make all terms scale linearly with exposure. For example:

```text
sqrt((kR)(kG)) = k sqrt(RG)
```

Advantages:

- More exposure-stable.
- Cleaner camera-profile-style color correction.
- Lower risk of strange exposure-dependent hue/saturation changes.

Disadvantages for this project:

- Less flexible for tone-curve correction.
- Degree-2 root-polynomial has no independent pure `R²`, `G²`, or `B²` curvature, because `sqrt(R²) = R` for nonnegative RGB.
- May be too constrained for VHS tone/level nonlinearities.

### 8.3 Current prior

Expected best candidate for this VHS project:

```text
ordinary degree 2 + intercept, exported at 85% and 100% strength
```

But this should be validated against held-out pairs. Do not rely only on prior expectations.

---

## 9. Why 85% strength exists

A strength-blended LUT is:

```text
output_strength = input + strength × (full_lut_output - input)
```

Example:

```text
85% output = original + 0.85 × (full correction - original)
```

Use 85% when training data is limited or when a batch LUT must handle unknown scenes.

Benefits:

- Reduces overcorrection.
- Safer for unusual lighting.
- Less likely to make shadows milky.
- Less likely to make whites blue/cyan.
- Less likely to make skin gray/magenta/cyan.

When full strength is acceptable:

- Many diverse matched pairs were used.
- Held-out matched validation improves cleanly.
- VHS-only review clips look natural.
- 85% is visibly undercorrected.

Current batch recommendation:

```text
Default: 85%
Use 100% selectively if a specific clip remains too VHS-warm/dark/saturated.
```

---

## 10. 8-bit vs 10-bit VHS transcodes for fitting

Preference order for fitting:

```text
raw 10-bit 4:2:2 VHS at pre-LUT stage
> 10-bit 4:2:0 VHS transcode
> 8-bit 4:2:0 VHS transcode
```

But based on prior tests:

```text
8-bit-derived LUT ≈ 10-bit-derived LUT
```

Expected difference is likely modest because:

- VHS chroma is already noisy and low-resolution.
- Fitting uses downsampled/blurred samples.
- The dominant correction is broad color/level bias.

However:

- 10-bit sources are better if easily available.
- 10-bit helps avoid quantization artifacts in subtle shadow/blue regions.
- Final application should happen during raw → final 10-bit encode when possible.

Do not apply LUT to already-encoded HEVC and re-encode unless necessary. Applying a LUT requires decode → filter → re-encode.

Best:

```text
raw VHS master → preprocessing → LUT → final 10-bit HEVC
```

Less ideal:

```text
HEVC transcode → LUT → HEVC re-encode
```

---

## 11. Validation strategy

Validation should combine:

```text
1. Synthetic sanity chart
2. Matched held-out pair metrics
3. Contact sheets
4. Live video review
5. Scopes
```

### 11.1 Synthetic sanity chart

Synthetic charts do not prove the LUT is visually correct, but they catch broken mappings.

Test chart should include:

- neutral gray ramp
- red ramp
- green ramp
- blue ramp
- black/dark gray/mid gray/white patches
- red/green/blue/yellow/cyan/magenta patches
- skin-ish patches

Apply LUT to chart:

```bash
ffmpeg -y -i lut_sanity_chart.png \
-vf "format=gbrp,lut3d=LUTs/candidate.cube:interp=tetrahedral,format=rgb24" \
lut_sanity_chart_candidate.png
```

Make before/after:

```bash
ffmpeg -y -i lut_sanity_chart.png -i lut_sanity_chart_candidate.png \
-filter_complex "[0:v][1:v]hstack=inputs=2" \
lut_sanity_chart_before_after.png
```

Compare original / 85% / 100%:

```bash
ffmpeg -y \
-i lut_sanity_chart.png \
-i lut_sanity_chart_lut85.png \
-i lut_sanity_chart_lut100.png \
-filter_complex "[0:v][1:v][2:v]hstack=inputs=3" \
lut_sanity_chart_3way.png
```

Synthetic chart warning:

```text
This is full-range RGB synthetic data, not VHS footage.
Use it to catch insane behavior, not to choose final look.
```

### 11.2 Neutral ramp mapping

For every fitted transform, print or plot mappings for neutral grays:

```text
[0.00, 0.00, 0.00] → ?
[0.05, 0.05, 0.05] → ?
[0.10, 0.10, 0.10] → ?
[0.18, 0.18, 0.18] → ?
[0.50, 0.50, 0.50] → ?
[0.75, 0.75, 0.75] → ?
[0.90, 0.90, 0.90] → ?
[1.00, 1.00, 1.00] → ?
```

Sane behavior for current VHS correction may look roughly like:

```text
0.00 → 0.01–0.04
0.05 → 0.08–0.12
0.18 → 0.25–0.32
0.50 → 0.55–0.65
0.75 → 0.78–0.88
0.90 → 0.90–0.98
1.00 → <= 1.00
```

Suspicious behavior:

```text
0.00 → 0.10+            # milky blacks
0.75 → 1.00+            # highlight clipping risk
0.90 → 1.10+            # definite clipping before clamp
neutral gray → strong color cast
ramp is non-monotonic
```

Do not judge the intercept alone. Judge the actual mapping.

### 11.3 Clipping diagnostics

For every candidate, compute clipping before final clamp:

1. LUT grid clipping:

```text
% of .cube grid entries where any channel < 0
% of .cube grid entries where any channel > 1
```

2. Actual video pixel clipping:

```text
% of sampled video pixels where any transformed channel < 0
% of sampled video pixels where any transformed channel > 1
```

Actual video pixel clipping matters more than full cube-grid clipping because many RGB combinations never occur in VHS footage.

Useful rough thresholds:

```text
<0.1–0.5% clipped actual pixels: usually good
several percent clipped: concerning
large chunks clipped: bad
```

### 11.4 Matched held-out pair validation

For held-out Video8/VHS pairs, generate:

```text
Video8 reference | VHS original | VHS + candidate LUT
```

If comparing 85% and 100%:

```text
Video8 reference | VHS original | LUT 85% | LUT 100%
```

Metrics to compute:

- RGB MAE in 8-bit units.
- ΔE-ish mean/median/percentiles.
- Luma mean and percentile differences.
- Saturation mean and percentile differences.
- RGB channel means.
- Clipping rate after candidate transform.

Important:

```text
The best LUT is not necessarily the one with lowest training error.
Choose the LUT that improves held-out pairs without weird visual side effects.
```

### 11.5 VHS-only held-out validation

For VHS-only clips without Video8 reference, do QC-style validation:

```text
original VHS | LUT 85% | LUT 100%
```

Look for:

| Check | Good sign | Bad sign |
|---|---|---|
| Skin tones | less orange/yellow, still natural | gray, magenta, cyan, waxy |
| Neutrals | walls/white clothing less yellow | whites become blue/green/pink |
| Shadows | detail more visible | lifted gray fog / milky blacks |
| Highlights | not blown out | flattened/clipped brights |
| Saturation | less VHS-punchy | dull/desaturated |
| Blue recovery | less brown/yellow, more neutral/cool | entire image turns cyan |
| Scene consistency | natural over time | LUT feels good only in one lighting condition |

### 11.6 Contact sheets vs live video

Use both.

Contact sheet is best for:

- choosing/tuning LUTs
- comparing 85% vs 100%
- judging many scenes quickly
- skin/neutral/shadow/highlight comparisons

Live video is best for final QC:

- lifted temporal noise
- chroma flicker
- color instability frame-to-frame
- noise that becomes more visible after shadow lift
- motion areas that look strange

Recommended order:

```text
synthetic chart
→ contact sheets from validation clips
→ short live side-by-side review clips
→ longer random segment final check
```

Example 3-way live review:

```bash
ffmpeg -i vhs_original.mkv -i vhs_lut85.mkv -i vhs_lut100.mkv \
-filter_complex "[0:v][1:v][2:v]hstack=inputs=3" \
-c:v libx264 -crf 18 -preset slow review_original_85_100.mp4
```

Example contact sheet from review video:

```bash
ffmpeg -i review_original_85_100.mp4 \
-vf "fps=1/5,scale=iw:-1,tile=1x8" \
-frames:v 1 review_contact_sheet.jpg
```

For a 20-second clip:

```bash
ffmpeg -i review_original_85_100.mp4 \
-vf "fps=1,tile=1x20" \
-frames:v 1 review_contact_sheet.jpg
```

### 11.7 Scopes

Use FFmpeg scopes for quick checks:

```bash
ffplay corrected.mkv -vf waveform
```

```bash
ffplay corrected.mkv -vf vectorscope
```

What to check:

- Waveform: blacks not excessively lifted; highlights not flattened/clipped.
- RGB parade if available: neutral areas not strongly blue/green/red unless scene calls for it.
- Vectorscope: skin tones not pushed too far toward magenta/cyan/green.

---

## 12. Intercept sanity

An intercept can help correct a real VHS black-level/pedestal offset, but it increases risk of lifted blacks and clipping.

Do not judge only by the intercept value.

Inspect:

- neutral ramp mapping
- clipping rates before clamping
- actual video pixel clipping
- real-frame contact sheets

If RGB values are normalized to `[0,1]`, a plausible intercept might be small, e.g.:

```text
[0.02, 0.03, 0.04]
```

Suspicious:

```text
[0.12, 0.10, 0.15]
```

But the full transform matters more than the intercept alone. Higher-order terms can compensate in highlights, so a positive intercept does not automatically blow highlights.

---

## 13. Simple correction vs LUT

A simple FFmpeg curves correction was tested against the 2-pair LUT.

Prior rough result:

| Method | Mean ΔE vs Video8 | RGB MAE | Takeaway |
|---|---:|---:|---|
| VHS uncorrected | ~14.8 | ~31.3 | visibly off |
| Auto-fit simple RGB channel curves | ~7.8 | ~12.2 | big improvement |
| Auto-fit linear RGB matrix | ~6.5 | ~11.3 | almost LUT-quality |
| 2-pair LUT full strength | ~6.3 | ~10.9 | best |
| 2-pair LUT 85% | ~6.7 | ~11.8 | safer batch default |

Conclusion:

```text
A simple correction works surprisingly well,
but the LUT is not overkill.
```

Because the LUT is easy to apply and performs best, it remains the recommended final mechanism.

The fact that the matrix fit gets close to the LUT is good evidence that the problem is a systematic transfer-chain bias, not random scene-specific weirdness.

---

## 14. Suggested Codex tasks

Codex should extend the current v1 fitter into a more complete pipeline. Suggested deliverables:

### 14.1 Pair configuration

Add support for a YAML or JSON config with per-pair metadata:

```yaml
pairs:
  - id: pair01_indoor_faces
    ref: video8_pair01.mp4
    src: vhs_pair01.mp4
    role: train
    weight: 1.0
    crop:
      x0: 20
      y0: 10
      x1: -20
      y1: -20
    mask:
      bottom: 14
      top: 3
    notes: indoor faces, neutral wall

  - id: pair02_outdoor_daylight
    ref: video8_pair02.mp4
    src: vhs_pair02.mp4
    role: validation
    weight: 1.0
```

Useful fields:

- `id`
- `ref`
- `src`
- `role`: `train` / `validation` / `ignore`
- `weight`
- `start/end` or `trim` if not already pre-cut
- `crop`
- `mask_top`
- `mask_bottom`
- optional `frame_offset`
- optional `notes`

### 14.2 Candidate model support

Support fitting variants:

```text
linear_intercept
linear_no_intercept
poly2_intercept
poly2_no_intercept
rootpoly2_intercept
rootpoly2_no_intercept
poly3_intercept
```

Generate both full and strength-blended LUTs:

```text
candidate_full.cube
candidate_strength85.cube
```

### 14.3 Validation report

Generate a report directory per run:

```text
runs/YYYYMMDD_HHMM_lutfit/
  config.yaml
  luts/
    poly2_intercept_full.cube
    poly2_intercept_strength85.cube
    rootpoly2_intercept_full.cube
    ...
  metrics/
    train_metrics.json
    validation_metrics.json
    per_pair_metrics.csv
    neutral_ramp.csv
    clipping_stats.csv
  images/
    sanity_chart_original.png
    sanity_chart_candidates.png
    pair01_contact_sheet.jpg
    pair02_contact_sheet.jpg
    validation_summary_contact_sheet.jpg
  videos/
    pair01_review.mp4
    pair02_review.mp4
  report.md
```

### 14.4 Metrics to include

Per candidate and per pair:

- RGB MAE before/after.
- ΔE-ish mean/median/p90/p95.
- Luma mean/p1/p5/p50/p95/p99.
- Saturation mean/p95.
- RGB means.
- Clipping rate pre-clamp on sampled pixels.
- Neutral ramp table.
- Optional train vs validation gap.

### 14.5 Contact sheets

Generate these automatically:

For matched validation:

```text
Video8 | VHS original | LUT 85% | LUT 100%
```

For VHS-only review:

```text
VHS original | LUT 85% | LUT 100%
```

For candidate comparison:

```text
VHS original | poly2 85% | rootpoly2 85% | matrix 85%
```

### 14.6 Sanity chart generation

Add a command to generate and apply LUTs to a synthetic sanity chart:

```bash
python lut_pipeline.py make-sanity-chart --out sanity_chart.png
python lut_pipeline.py apply-sanity --lut candidate.cube --out sanity_candidate.png
```

Chart should include neutral ramp, RGB ramps, color patches, skin-ish patches.

### 14.7 Neutral ramp and clipping diagnostics

Add a command:

```bash
python lut_pipeline.py inspect-lut --lut candidate.cube
```

or inspect the underlying fitted model before cube export.

Output:

- neutral ramp mapping
- min/max output before clipping
- percent grid values out of range
- monotonicity check for neutral ramp
- whether neutral ramp develops strong color cast

### 14.8 Frame alignment support

Potential later enhancement:

- detect rough temporal offset between Video8 and VHS by sampling low-res luma frames
- estimate offset via frame-to-frame similarity
- allow `frame_offset` per pair
- optionally print warnings if alignment appears poor

This is useful because mismatched pairs will corrupt LUT fitting.

---

## 15. Example user-facing workflow after tooling exists

### Step 1: Prepare clips

Create 5–10 matched pairs:

```text
video8_pair01.mp4 | vhs_pair01.mp4
video8_pair02.mp4 | vhs_pair02.mp4
...
```

The VHS clips can be the existing transcodes if they are from the same pipeline.

### Step 2: Create config

```bash
python lut_pipeline.py init-config --pairs-dir pairs/ --out lut_config.yaml
```

Manually edit roles and notes.

### Step 3: Fit candidates

```bash
python lut_pipeline.py fit \
  --config lut_config.yaml \
  --models poly2_intercept,rootpoly2_intercept,linear_intercept \
  --strengths 1.0,0.85 \
  --out runs/lutfit_001
```

### Step 4: Inspect report

Open:

```text
runs/lutfit_001/report.md
```

Review:

- validation metrics
- synthetic sanity chart
- neutral ramp
- clipping stats
- matched validation contact sheets
- live review clips

### Step 5: Choose LUT

Expected default:

```text
poly2_intercept_strength85.cube
```

But choose based on validation results.

### Step 6: Apply in production raw VHS transcode

Use chosen LUT after standard preprocessing:

```bash
ffmpeg -i raw_vhs.mkv \
-vf "bwdif=mode=send_field:parity=auto:deint=all,
     hqdn3d=1.5:1.125:2.25:1.6875,
     scale='trunc(ih*dar/2)*2:ih':flags=lanczos,
     setsar=1,
     format=gbrp,
     lut3d=LUTs/chosen_lut.cube:interp=tetrahedral,
     format=yuv420p10le,
     drawbox=x=0:y=0:w=iw:h=3:color=black:t=fill,
     drawbox=x=0:y=ih-12:w=iw:h=12:color=black:t=fill,
     setparams=range=limited" \
-c:v libx265 -crf 18 -preset slow \
-pix_fmt yuv420p10le \
-color_range tv -colorspace smpte170m -color_primaries smpte170m -color_trc smpte170m \
-c:a copy output.mkv
```

---

## 16. Things not to do

Do not:

- average `.cube` LUT files directly
- train on mismatched clips
- train on clips with scene cuts or major motion if avoidable
- train on borders/date text/head-switching noise
- trust training metrics only
- choose a higher-degree polynomial just because training error improves
- apply the LUT before deinterlacing/standard cleanup unless the LUT was trained at that exact stage
- apply LUT to already-final HEVC and re-encode if raw masters are available
- assume 100% strength is best for batch use from only a few pairs
- judge intercept sanity only by the intercept value; inspect the full mapping and clipping stats

---

## 17. Current recommendation summary

Current best practical path:

```text
1. Select 5–10 diverse matched Video8/VHS-transcode pairs.
2. Reserve 1–3 matched pairs for validation.
3. Fit pooled global LUTs, not per-scene LUTs.
4. Compare poly2/intercept, rootpoly2/intercept, and linear/matrix candidates.
5. Export full and 85% variants.
6. Validate using synthetic sanity chart, neutral ramp, clipping stats, matched hold-out metrics, contact sheets, and live review clips.
7. Default to ordinary degree-2 polynomial + intercept at 85% strength unless validation indicates otherwise.
8. Apply the chosen LUT during the raw VHS final transcode, after bwdif/hqdn3d/scale and before final yuv420p10le encoding.
```

This preserves a manageable workflow: one general VHS-to-Video8 correction LUT, plus only small manual per-scene tweaks if a scene is genuinely unusual.
