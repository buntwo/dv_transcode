# Teaching VHS to Remember Video8

This project started with a simple observation: the VHS tapes did not look like the Video8 tapes they came from.

The working theory was that the VHS copies were analog dubs from original Video8 camcorder tapes. The same scenes existed in both forms, but the VHS versions carried a very different look. They were darker. The shadows were crushed. Highlights felt compressed. Colors were warmer, yellower, and more saturated. Blue seemed suppressed. The VHS copies also had the usual analog baggage: chroma smear, noise, ringing, and ragged edges.

The goal was not magic restoration. VHS throws away real information. We were not trying to recover detail that no longer exists. The goal was narrower and more practical:

```text
make the VHS dub look more like the matching Video8 direct capture
```

In other words, learn the color and tone bias of the transfer chain and build a LUT that corrects it.

## The First Plan

We had a set of matched moments: Video8 on one side, VHS on the other. The natural idea was to sample corresponding RGB pixels and fit a transform:

```text
VHS RGB -> Video8 RGB
```

The first fitter used a polynomial model. For a standard degree-2 model with an intercept, the features look like this:

```text
1, R, G, B, R², G², B², RG, RB, GB
```

That gives the model enough flexibility to do more than a simple color matrix. It can learn some brightness-dependent behavior, which matters because the VHS problem is partly a tone-curve problem: crushed shadows, compressed highlights, and biased color balance.

We generated 10-second FFV1 clips from the row-wise training and validation manifests, naming them:

```text
pair_001_A.mkv
pair_001_B.mkv
```

where `A` was the Video8 reference and `B` was the VHS source.

Then we fit LUTs.

They looked terrible.

Skin tones went blue. The metrics were strange. The new LUTs were worse than raw VHS on validation, while an older two-pair LUT still looked good. That was the first big clue: the idea was probably not wrong, but something in the implementation was.

## The Red-Turns-Blue Bug

To isolate the problem, we went back to the two clips that had produced the older known-good LUT. If the current fitter could reproduce a good LUT from those same two pairs, the new data was the problem. If it could not, the fitter was suspect.

The in-memory fit looked good. The math reduced training error. But once exported as a `.cube` and applied through FFmpeg, the result was bad.

So we built the simplest possible test: export an identity LUT and run red, green, and blue through FFmpeg.

The result was decisive. Red came out blue.

The issue was `.cube` line ordering. The script had been writing the LUT grid like this:

```python
for r in grid:
    for g in grid:
        for b in grid:
```

But FFmpeg expects red to vary fastest:

```python
for b in grid:
    for g in grid:
        for r in grid:
```

That small ordering bug scrambled the LUT volume. Once fixed, the current fitter reproduced the older known-good behavior almost exactly:

```text
raw:                RGB MAE 34.24, dE76 16.14
fixed refit 85%:   RGB MAE 15.55, dE76 8.19
old known-good 85% RGB MAE 15.57, dE76 8.35
```

The polynomial fitting was not the culprit. The exported LUT was.

## The Geometry Problem

With the LUT export fixed, we turned back to the new training set. The next issue was subtler: the two videos in each pair did not have the same raster dimensions.

```text
Video8 A: 640x480
VHS B:   648x486
```

They were both nominally 4:3, but not identical. The VHS side also had black bars added to hide vertical blanking and head-switching noise, plus some edge noise on the left and right.

That matters because a color fitter assumes this:

```text
pixel at location (x, y) in VHS corresponds to pixel at location (x, y) in Video8
```

If a face edge, wall, or shirt lands a few pixels apart, the fitter starts learning from mismatched colors. It might compare skin in one image to background in the other. Enough of that, and the model learns nonsense.

We generated overlay sheets:

- A frame
- B frame
- 50/50 blend
- checkerboard
- boosted difference

The blend suggested a vertical shift. A numerical luma-edge shift diagnostic confirmed it: B generally wanted to move upward by a few pixels.

But a shift alone was not the whole story. There was no reason the two analog capture paths should have exactly the same active-image scale. So we built a geometry normalizer.

For each pair, it:

1. samples a few frames
2. resizes B to A's canvas
3. scores luma-edge alignment
4. searches small center-scale and translation changes
5. applies the best transform to the full B clip
6. copies A unchanged

The result was a new preprocessed training directory:

```text
generated_video_pairs/train_geometry_normalized
```

and a corresponding validation directory:

```text
generated_video_pairs/validation_geometry_normalized
```

The optimizer found a very consistent correction:

```text
sx: mostly 1.000
sy: mostly 1.011-1.017
dx: mostly 0-2 px
dy: mostly -1 to -2 px
```

In plain English: the VHS image needed a slight vertical stretch and a small upward/rightward adjustment.

This was an important pivot. Geometry normalization became part of data preprocessing, not part of the LUT fitter. The fitter should learn color, not compensate for mismatched image placement.

## Choosing the Model

Once the cube export and geometry were fixed, the fits started making sense.

We compared:

- standard degree-2 polynomial with intercept
- standard degree-2 polynomial without intercept
- root-polynomial degree-2 with intercept
- root-polynomial degree-2 without intercept

Each was exported at 100% and 85% strength.

The aggregate validation results favored:

```text
standard degree-2 polynomial + intercept, 85% strength
```

The top numbers were:

```text
raw:                         RGB MAE 35.44, dE76 16.99
standard + intercept 85%:    RGB MAE 18.74, dE76 9.13
standard + intercept 100%:   RGB MAE 19.13, dE76 9.19
root-poly + intercept 85%:   RGB MAE 19.23, dE76 9.43
```

Metrics are not the whole story. Bright outdoor scenes still needed visual review because a transform can reduce average color error while making highlights feel washed out. But as a model choice, standard degree-2 with an intercept was the strongest candidate.

## Sampling Was Its Own Experiment

At first, more samples sounded obviously better.

A full-resolution 640x480 frame has a lot of pixels. Over many frames and pairs, the candidate pool reaches tens of millions of pixels. Why cap at 500k or 1M?

We tested it.

The surprise was that more random samples made the fit worse after a point. At 20M samples, the model degraded badly. The likely explanation is that the extra samples were not clean information. They included more hard pixels: residual misalignment, noise, edges, smear, motion differences, and other outliers. Robust weighting helped, but did not make the model immune.

So we ran a sampling sensitivity experiment with three seeds at each sample count:

```text
500k, 1M, 2M, 5M
```

The result:

```text
500k: RGB 18.498 +/- 0.017, dE 9.091 +/- 0.011
1M:   RGB 18.426 +/- 0.016, dE 9.026 +/- 0.011
2M:   RGB 18.760 +/- 0.014, dE 9.148 +/- 0.013
5M:   RGB 20.470 +/- 0.219, dE 10.054 +/- 0.158
```

The seed-to-seed variation was tiny. Random sampling was stable. But 1M was better than 2M, and 5M was clearly worse.

The lesson was not "use as many pixels as possible." The lesson was:

```text
use enough clean, representative pixels
```

For this dataset, 1M random samples was the sweet spot.

## Downscaling: Helpful or Harmful?

The original fitter sampled at 160x120. That made sense early on: VHS is noisy, and downsampling hides small alignment errors.

But once geometry was normalized, we tested whether native-size fitting was better.

To keep the comparison fair, we scaled sample count by resolution:

```text
640x480 native: 1M samples
320x240:        250k samples
160x120:        64k samples
```

Results:

```text
raw:          RGB MAE 35.44, dE76 16.99
native 1M:    RGB MAE 18.41, dE76 9.02
320x240 250k: RGB MAE 18.67, dE76 9.17
160x120 64k:  RGB MAE 18.99, dE76 9.28
```

Native fitting won. The downscaled versions were not bad, especially 320x240, but after geometry correction there was no longer a strong reason to throw away spatial precision before sampling.

## Where We Landed

The final default fitter settings are:

```text
model: standard degree-2 polynomial
intercept: yes
strength: 0.85
sample extraction: native size, no scale
mask: left 56, right 56, top 40, bottom 96
max samples: 1,000,000
sampling: random
seed: 2002
LUT size: 33
fps: 2
```

The command is now simple:

```bash
uv run python fit_vhs_lut.py \
  --pairs generated_video_pairs/train_geometry_normalized_pairs.txt \
  --out LUTs/final_vhs_to_video8_standard_i_85.cube
```

That command encodes the hard-won lessons:

- export `.cube` in FFmpeg's expected order
- normalize geometry before fitting
- mask the edges
- fit at native size
- use a flexible but not excessive polynomial model
- sample enough, but not too much
- use 85% strength as the safer batch correction

## The Human Part

This was not a straight line.

At one point, the model looked mathematically plausible but visually absurd. At another, more data made the fit worse. Metrics chose a candidate, but visual review raised concerns about washed-out bright scenes. The geometry looked "close enough" until overlays showed it was not. And the biggest bug was a humble ordering convention inside a LUT file.

That is typical of work with old analog media. The problem is never just color. It is color plus timing, geometry, noise, capture behavior, masking, interpolation, and evaluation. Each piece can quietly invalidate the next one.

The useful pattern was to keep narrowing the question:

```text
Is the fitter math wrong?
Is the exported LUT wrong?
Are the clips aligned?
Are the borders contaminating samples?
Does more sampling help?
Does downscaling help?
Do the metrics agree with the eye?
```

By the end, the pipeline was not just better tuned. It was better understood.

And that matters because the final LUT is not a magic artifact. It is the product of a reproducible workflow: matched clips, geometry normalization, masked native sampling, robust polynomial fitting, validation metrics, sanity charts, and visual review.

That is the difference between a lucky grade and a correction pipeline.

