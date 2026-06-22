# Building a Color-Correction Lab for Old VHS Tapes

This project started as a color problem and turned into a small engineering system.

The original question was simple enough: can we learn a transform that makes VHS captures look more like cleaner Video8 references? The archive had paired source material, the target was a 3D LUT, and the workflow sounded like a normal fitting problem.

It did not stay that way.

The hard parts were not only polynomial models, color spaces, or ffmpeg filter syntax. The hard parts were engineering problems: generating aligned training data, keeping experiments reproducible, making visual review fast, avoiding huge disk mistakes, preserving 10-bit paths, timing real encode overhead, and finally inserting a selected correction into a production transcode pipeline without turning the pipeline into a one-off pile of flags.

By the end, the `LUT_fitting` directory had grown into a real little lab:

```text
44 Python scripts
1 shell review player
5 markdown reports/plans/posts
about 15k lines total
```

The largest color-correction workflow commit added about 10k lines across 42 repo files. That sounds like a lot for "find a LUT", but most of the code is not the final correction. Most of the code is scaffolding that made the final correction trustworthy.

## The First Rule: Generate Data, Do Not Hand-Wave It

The project began with clip-selection manifests:

```text
video_pairs_raw_training
video_pairs_raw_validation
```

Those files described where matching clips should come from, but they were not training data yet. The first engineering step was to generate actual paired clips:

```text
pair_001_A.mkv
pair_001_B.mkv
pair_002_A.mkv
pair_002_B.mkv
...
```

The clips were rendered as FFV1 intraframe video so later adjustments would not repeatedly damage the source. We used accurate seek and kept the generated clips in explicit train/validation directories.

That choice mattered. Once we had stable files, every later stage could consume a manifest instead of reinterpreting source timestamps. The data boundary became:

```text
manifest rows -> generated clip pairs -> normalized clip pairs -> fitter manifest
```

That may sound mundane, but it prevented a common failure mode in media projects: every script secretly knowing how to find and trim source media in a slightly different way.

## Fixing Alignment Before Fitting Color

The first LUTs looked terrible. Skin tones went blue, highlights washed out, and the numbers were not enough to explain what was happening.

The investigation turned into an alignment problem. We built tools to inspect blends, checkerboards, boosted differences, and downsampled fitter views. The important discovery was that some pairs were shifted, and later that scale/shift differences could vary per pair.

That led to a geometry-normalization stage:

```text
normalize_pair_geometry.py
```

Rather than ask the fitter to be robust to misregistered pixels, we optimized each pair independently, transformed the B video, copied A, and wrote a CSV of per-pair geometry metrics. That created a cleaner fitting dataset:

```text
generated_video_pairs/train_geometry_normalized/
generated_video_pairs/validation_geometry_normalized/
generated_video_pairs/train_geometry_normalized_pairs.txt
generated_video_pairs/validation_geometry_normalized_pairs.txt
```

This was one of the most important engineering calls in the project. Color fitting assumes that pixel `i` in image A corresponds to pixel `i` in image B. If that assumption is false, more model complexity just learns garbage. The right fix was not a better polynomial. It was preprocessing.

## Manifests as Contracts

The project ended up with several manifest-like files:

- raw clip selection manifests
- generated train/validation pair manifests
- geometry-normalized pair manifests
- timing CSVs
- optimization result CSVs
- review clip manifests

This was deliberate. Each stage wrote enough structured output that the next stage could be rerun without remembering the previous command by hand.

The evaluator, fitter, alignment tools, and review-sheet generators all operated on explicit paths. That made the workflow boring in a good way. If an experiment failed, the artifact that failed was usually identifiable:

```text
bad source pair
bad geometry normalization
bad sampling strategy
bad color model
bad review visualization
bad production placement
```

Without those boundaries, all of those would have collapsed into one vague complaint: "the LUT looks bad."

## The Fitter Grew Into an Experiment Harness

The original fitter was extended to support a matrix of model choices:

```text
intercept vs no intercept
standard polynomial vs root polynomial
100% vs 85% output strength
random sampling vs pair-balanced sampling
luma-bin sampling experiments
different sample counts
different input resolutions
```

Most of that logic lives in:

```text
fit_vhs_lut.py
fit_lut_grid.py
evaluate_luts.py
```

The fitting code did not become a giant framework. It stayed script-oriented, but with enough shared command-line structure and output conventions to compare runs honestly. That was the right level of abstraction for this project. We needed fast iteration more than a polished library.

The early experiments answered engineering questions as much as color-science questions:

- Is 500k samples enough? No.
- Does 2M stabilize the fit? Mostly.
- Does downscaling to 320x240 or 160x120 matter? It changes the tradeoff, but full-resolution sampling remained preferable once geometry was normalized.
- Does a bigger sample count fix washout? No, not by itself.
- Does a metric win guarantee a visual win? Definitely not.

Each of those answers came from a script that produced comparable CSVs and review artifacts.

## Visual Review Became a First-Class Output

The project wrote a lot of code that did not fit models at all. It generated things to look at.

Examples:

```text
generate_alignment_overlays.py
generate_lut_comparison_grid.py
generate_pair_lut_comparison_grid.py
generate_lut_review_sheets.py
generate_luma_threshold_grid.py
generate_transformed_video_review_sheets.py
play_review_videos.sh
```

This was necessary because the metrics were useful but incomplete. A model could improve RGB MAE and dE76 while making the tape feel worse. Some corrections lifted shadows, but also opened midtones and highlights until the image looked washed out. Some curves were mathematically smooth and still produced patchy-looking shadow regions.

The review tools changed the development loop. Instead of asking "what is the best number?", we could ask:

```text
What does this do on indoor yellow scenes?
What does this do outdoors?
What happens to faces?
What happens to dark clothing?
What happens to already-decent clips?
What happens on original 10-bit masters, not only fitted pairs?
```

Contact sheets and multi-video playback were not polish. They were measurement instruments.

## The LUT Was Not Sacred

The project name says LUT, but the final result is not a LUT.

That is another engineering lesson: the artifact you start with is not always the artifact you should ship.

We tried multiple LUT families. We also tried luma gates, tone-protection variants, monotonic luma ramps, YCbCr-inspired models, root polynomials, and curve-based shadow lifts. Several of them were defensible. Some were clever. Some even improved metrics.

But the final production candidate came from ffmpeg's built-in filters:

```text
eq=gamma=...
colorcorrect=...
50/50 blend with original
```

The selected color graph is:

```text
split=2[orig][work];[work]eq=gamma=1.43214046,colorcorrect=rl=-0.004439:bl=0.012896:rh=-0.004175:bh=0.012128:saturation=0.880000[filt];[orig][filt]blend=all_expr='0.500000*A+0.500000*B'
```

It is saved in:

```text
LUTs/vhs_to_video8_previous50_visual_winner.ffmpeg-filtergraph
```

We also tested baking this filtergraph back into a `.cube` LUT. It was not faster. The native ffmpeg filtergraph won on speed and explainability. That simplified the production path: no LUT approximation, no extra color-space conversions, no large cube file, and fewer hidden assumptions.

## Color Spaces: Debugging the Filtergraph, Not the Theory

One of the subtler bugs came from misunderstanding where `colorcorrect` operates. We had a chain that forced a GBR conversion and assumed that meant the correction was being applied in RGB. Later checks against ffmpeg behavior showed that `colorcorrect` is a YUV-domain filter; ffmpeg converted back to YUV before applying it.

That changed how we interpreted earlier tests. Some observed differences were not because `colorcorrect` changed semantics in RGB. They were because we had inserted a YUV -> RGB -> YUV round trip.

The fix was not philosophical. It was practical:

```text
stay source-native
avoid unnecessary format conversions
test filtergraph equivalence on frames
measure speed on real video
```

The production graph is intentionally a pure filtergraph without explicit RGB conversion.

## Timing Was Treated as Data

A color correction that looks good but doubles encode time is not a small decision when the source is a pile of huge 10-bit masters on an external disk.

So we wrote generation scripts that timed control and corrected encodes separately:

```text
generate_final_winner_master_clips.py
generate_final_winner_pair_clips.py
fill_final_winner_master_clips_parallel.py
```

The timing strategy mattered. For the overhead estimate, control and corrected clips were encoded sequentially so they were not competing with each other for CPU or disk. After enough clean timing data existed, the rest of the review clips were parallel-filled for throughput.

The final overhead estimate was reasonable:

```text
master clips: median overhead 18.7%, mean 22.2%
train/validation pairs: median overhead 28.0%, mean 30.5%
```

That is not free, but it is cheap enough for the final access transcodes.

## Disk Management Became Part of the Design

Generated video artifacts got large quickly.

At one point the final review set alone was tens of gigabytes:

```text
final_visual_winner: about 66G
denoise workflow review: about 13G
```

Earlier dead-end review videos consumed about 93G and were later deleted. The scripts also gained free-space guards so generation would stop before filling the disk.

This is an engineering detail that matters in media workflows: if the tools make it easy to generate review clips, they must also make it hard to accidentally consume the whole volume.

The repo `.gitignore` also keeps generated clips out of git:

```text
generated_video_pairs/
```

The committed repo contains code, manifests, reports, and reproducible experiment scripts. The giant review outputs stay local.

## Production Integration Was a Separate Phase

Once the winner was selected, the final step was not "done." It had to be inserted into the actual access-transcode path.

The production CLI is:

```text
scripts/transcode_access.py
```

but the real shared logic is in:

```text
scripts/transcode_core.py
```

We added:

```text
--vhs-color-correct
```

with validation that it only works with:

```text
--format vhs
```

The production placement is now:

```text
bwdif
crop masked rows
hqdn3d
VHS color correction, if enabled
pad masked rows back as black
scale=trunc(ih*dar/2)*2:ih:flags=lanczos+accurate_rnd+full_chroma_int
setsar=1
setparams=range=limited
format=yuv420p10le, for libx265
```

The crop/pad detail was a late but important improvement. Earlier versions used `drawbox` to black out VHS junk rows. That blacked the final pixels, but it also meant denoise and color filters saw those hard black rows. Cropping before denoise/color removes the bad rows from the active processing region, then padding restores the expected geometry before scale.

For default VHS masks:

```text
top = 3
bottom = 12
crop=w=iw:h=ih-15:x=0:y=3
pad=w=iw:h=ih+15:x=0:y=3:color=black
```

The common `scale` filter also now uses explicit flags in all generated modes:

```text
flags=lanczos+accurate_rnd+full_chroma_int
```

Those flags remove ambiguity from ffmpeg defaults and matched the visual preference from the denoise/scale review set.

## The Shell Wrapper Stayed Human-Editable

The final VHS batch wrapper was intentionally simple:

```text
/Users/btu/scratch/Videos/transcode_vhs_color_split.sh
```

It splits files into two arrays:

```bash
color_correct_input_files=(...)
no_color_correct_input_files=(...)
```

All files started in the color-correct group. The point was not to build a database or config format. The point was to make it easy to manually move tapes between the two groups after visual review.

That is a good example of the project's general engineering style. Automate the expensive and error-prone parts. Leave the judgement calls in a format a human can edit without ceremony.

## Tests and Smoke Checks

The production integration has tests in:

```text
scripts/tests/test_transcode3.py
```

The tests check:

- argument parsing
- VHS-only validation for `--vhs-color-correct`
- generated filter ordering
- crop/pad masking
- scale flags
- libx265 format behavior
- custom `--vf` override behavior

We also ran real-source ffmpeg smokes against a VHS master to make sure the composed filtergraph parsed and executed.

The full suite currently passes:

```text
244 passed
```

That is not a formal proof that every tape will look good. It is a guardrail against breaking the pipeline while making small improvements.

## The Most Important Engineering Pattern

The project worked because it separated questions:

```text
Can we generate clean pairs?
Are they aligned?
What data does the fitter see?
Which model improves metrics?
Which model survives visual review?
What does it cost to encode?
Where does it belong in the transcode chain?
Can we rerun it?
Can we delete the heavy artifacts safely?
```

Each question got code. Not always elegant code, but scoped code with files, manifests, CSVs, and review outputs.

That is why the final answer is boring in the best way:

```text
a small ffmpeg filtergraph
a CLI flag
a tested production filter order
a human-editable batch script
```

The visible correction is modest. The engineering behind it is the larger result.

Old home-video restoration is full of ambiguous judgement calls: VHS vs Video8, NTSC vs possible PAL, shadow lift vs washout, denoise vs texture, metric wins vs visual wins. The code cannot remove those judgement calls. What it can do is make each one observable, reversible, and repeatable.

That is what we built.
