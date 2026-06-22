# VHS-to-Video8 LUT Experiments: Tone Protection Pass

## Summary

This experiment pass investigated why the current best LUT can look washed out even though it scores well on RGB MAE and Delta E. The observed failure mode is specific:

- Shadow detail is usefully restored; this should be preserved.
- Midtones and highlights, especially non-yellow/non-warm material, can be lifted too much.
- Strong yellow indoor scenes are already corrected well by the current final LUT, sometimes slightly too much.
- Other colors probably need less correction than the global polynomial LUT currently applies.

The strongest result from this pass is that the previous final LUT remains the best by ordinary color metrics, but several variants reduce non-shadow luma over-lift while keeping most of the yellow-cast correction. The most promising candidate is `expt6l_light_luma_damp`, followed by `expt6i_highlight_protect_soft`, `expt6d_no_high_luma`, `expt6c_y_damp_high_chroma_full`, and `expt6q_hybrid_ycbcr_loose`.

The practical recommendation is to visually compare the shortlist, especially `expt6l`, against the previous final LUT on Access videos 06, 07, 08, 09, 10, 11, and 16.

## Context

The previous final LUT is:

```text
LUTs/vhs_to_video8_standard_i_native_1M_random_seed2002_strength85.cube
```

It was fit as a standard degree-2 RGB polynomial with intercept, 1M random samples, native resolution sampling, edge masking, and 85% output strength.

It is still the best all-around candidate by RGB MAE and Delta E on the original validation split:

```text
previous final: RGB MAE 18.19, mean dE76 8.88
raw VHS:        RGB MAE 35.19, mean dE76 16.56
```

However, visual review suggested that its success comes partly from broad luma lift. That helps crushed VHS shadows, but can wash out non-shadow scenes.

## Transfer-Chain Hypothesis

The new experiments were based on the idea that the VHS degradation is more likely a luma/chroma transfer problem than a fully arbitrary RGB problem.

Plausible VHS dub mechanisms:

- Luma compression/crushing in the dub or capture chain.
- Chroma bandwidth loss and phase/amplitude errors.
- White balance or decoder matrix bias causing warm/yellow cast.
- Different behavior between shadow detail recovery and highlight/midtone reproduction.

That suggests the correction should not be a fully global RGB warp. It should:

- allow shadow luma lift,
- damp mid/high luma lift,
- preserve strong yellow correction,
- reduce correction on non-warm mid/high material,
- avoid new highlight clipping.

## Prior Model-Spec Experiments

Before this tone-protection pass, three broader model families were tested as expt5:

| Experiment | Model | RGB MAE | dE76 |
|---|---|---:|---:|
| Previous final | RGB degree-2 polynomial + intercept | 18.19 | 8.88 |
| expt5a | RGB polynomial with luma regularization | 18.57 | 9.12 |
| expt5b | Joint YCbCr polynomial | 18.51 | 9.05 |
| expt5c | Hybrid luma curve + YCbCr chroma residual | 20.14 | 9.56 |

Conclusion: direct refits in RGB+luma, YCbCr, and hybrid YCbCr did not beat the previous final model. The hybrid model was too constrained with its first-pass defaults.

## New Evaluation Metrics

`evaluate_luts.py` was extended with tone-sensitive metrics in addition to RGB MAE and dE76:

- `shadow_luma_lift`: average luma lift for raw luma below 0.25.
- `mid_luma_lift`: average luma lift for raw luma 0.25-0.65.
- `high_luma_lift`: average luma lift for raw luma above 0.65.
- `mid_luma_bias`: output luma minus target luma in midtones.
- `high_luma_bias`: output luma minus target luma in highlights.
- `nonshadow_positive_luma_bias`: positive luma error in non-shadows.
- `nonwarm_mid_high_luma_bias`: luma bias on non-warm mid/high pixels.
- `nonshadow_luma_over_p95`: 95th percentile of positive non-shadow luma error.
- `new_clip_pct`: pixels clipped by the LUT that were not clipped in raw.
- `warm_yellow_delta_e76_mean`: color error on warm/yellow pixels.

A tone-protection score was used for ranking:

```text
dE76
+ 0.10 * nonshadow_positive_luma_bias
+ 0.02 * nonshadow_luma_over_p95
+ 0.35 * new_clip_pct
```

This is not a final objective function. It is a triage score designed to surface candidates that trade a small amount of color accuracy for less washed-out non-shadow material.

## Experiment Design

The expt6 runner generated 18 candidates:

```text
run_expt6_tone_protection.py
```

All were evaluated on:

```text
generated_video_pairs/validation_geometry_normalized_pairs.txt
```

The candidate families were:

1. Lower global strength:
   - `expt6a_final_strength75`
   - `expt6b_final_strength70`

2. Post-processed variants of the previous final LUT:
   - Convert the LUT grid input/output to YCbCr.
   - Apply the previous final LUT's luma delta with different strength by input luma.
   - Apply the previous final LUT's chroma delta with different strength by input luma.
   - Add optional extra correction strength for warm/yellow colors.
   - Export back as a normal RGB `.cube`.

3. New refits:
   - stronger RGB luma regularization,
   - YCbCr affine model,
   - looser/stricter hybrid YCbCr tone/chroma models.

The post-processed LUT variants were deliberately pragmatic: keep the known-good final LUT's color direction, but reduce the parts most likely to cause washed-out mid/high tones.

## Results

Full metrics are here:

```text
generated_video_pairs/evaluations/expt6_tone_protection/evaluation_all/validation_metrics.csv
generated_video_pairs/evaluations/expt6_tone_protection/experiment_summary.csv
```

Top candidates by tone-protection score:

| Rank | Candidate | Score | RGB MAE | dE76 | Non-shadow +Y | Non-warm mid/high Y bias | Warm/yellow dE |
|---:|---|---:|---:|---:|---:|---:|---:|
| 1 | `expt6l_light_luma_damp` | 10.930 | 18.37 | 8.94 | 10.54 | 8.39 | 9.86 |
| 2 | `expt6c_y_damp_high_chroma_full` | 10.943 | 18.84 | 9.10 | 9.62 | 4.24 | 9.98 |
| 3 | `expt6d_no_high_luma` | 10.961 | 18.64 | 9.03 | 10.20 | 5.46 | 9.89 |
| 4 | `expt6i_highlight_protect_soft` | 10.972 | 18.49 | 8.98 | 10.62 | 6.94 | 9.84 |
| 5 | `expt6q_hybrid_ycbcr_loose` | 10.974 | 19.00 | 9.17 | 9.47 | 2.14 | 10.04 |
| 14 | previous final | 11.195 | 18.19 | 8.88 | 12.15 | 12.00 | 9.74 |

Interpretation:

- The previous final still wins pure RGB MAE and dE76.
- `expt6l` is closest to the previous final while reducing non-shadow over-lift.
- `expt6c`, `expt6d`, and `expt6q` more aggressively protect non-warm mid/high luma, but cost more color accuracy.
- `expt6i` is a soft compromise: metrics close to final, with some highlight protection.
- The constrained refit models did not clearly win. The best improvements came from post-processing the previous final LUT's correction vector in YCbCr space.

## Shortlist

The shortlist selected for visual review:

| Candidate | Rationale |
|---|---|
| `expt6l_light_luma_damp` | Best tone-protection score; minimal dE penalty; likely safest drop-in. |
| `expt6i_highlight_protect_soft` | Closest to previous final while reducing highlight/midtone lift somewhat. |
| `expt6d_no_high_luma` | More decisive highlight luma protection with moderate color penalty. |
| `expt6c_y_damp_high_chroma_full` | Keeps chroma correction but strongly damps high-luma lift. |
| `expt6q_hybrid_ycbcr_loose` | Best of the hybrid refits by tone score; strongest non-warm mid/high protection among shortlist. |

Validation contact sheet:

```text
generated_video_pairs/evaluations/expt6_tone_protection/evaluation_shortlist/validation_lut_contact_sheet.png
```

Access subset sheets:

```text
generated_video_pairs/lut_review_sheets/expt6_shortlist_access_subset/
```

The Access subset includes videos:

```text
06, 07, 08, 09, 10, 11, 16
```

There are 35 sheets total: 5 shortlisted LUTs x 7 videos.

## Candidate Notes

### `expt6l_light_luma_damp`

This is the leading candidate. It keeps the previous final LUT mostly intact but damps luma lift, especially outside shadows.

Compared with previous final:

```text
dE76:                     8.88 -> 8.94
RGB MAE:                 18.19 -> 18.37
non-shadow +Y bias:      12.15 -> 10.54
non-warm mid/high bias:  12.00 ->  8.39
new clip pct:             0.25 ->  0.00
```

This is the most attractive tradeoff if visual review confirms it is less washed out.

### `expt6i_highlight_protect_soft`

This is also close to the previous final by dE and RGB MAE:

```text
dE76:                     8.88 -> 8.98
RGB MAE:                 18.19 -> 18.49
non-shadow +Y bias:      12.15 -> 10.62
non-warm mid/high bias:  12.00 ->  6.94
```

It may be better than `expt6l` if the visual problem is mainly highlights rather than midtones.

### `expt6c_y_damp_high_chroma_full`

This keeps chroma correction strong but damps luma lift in mid/high ranges:

```text
dE76:                     8.88 -> 9.10
non-shadow +Y bias:      12.15 -> 9.62
non-warm mid/high bias:  12.00 -> 4.24
```

This is a useful diagnostic: if it looks much better, the problem is mostly luma lift rather than chroma correction.

### `expt6d_no_high_luma`

This removes high-luma lift while preserving most chroma correction:

```text
dE76:                     8.88 -> 9.03
non-shadow +Y bias:      12.15 -> 10.20
non-warm mid/high bias:  12.00 -> 5.46
```

This is a good candidate if highlights are the main issue.

### `expt6q_hybrid_ycbcr_loose`

This is the best fully refit hybrid candidate in the tone-protection ranking:

```text
dE76:                     8.88 -> 9.17
non-shadow +Y bias:      12.15 -> 9.47
non-warm mid/high bias:  12.00 -> 2.14
```

It protects non-warm mid/high luma strongly but may under-correct or look flatter because the model is structurally constrained.

## What Did Not Work

- Simply making blue identity was much worse by metrics and visually was not promising.
- Standard degree-2 without intercept was worse overall.
- The first expt5 hybrid YCbCr model was too conservative and did not fit color well enough.
- Strong RGB luma regularization reduced luma lift but hurt warm/yellow correction and overall color accuracy.
- The affine YCbCr refit was interesting but still showed too much high-luma lift.
- Global 70-75% strength helped tone somewhat but was not as targeted as luma/chroma-gated post-processing.

## Conclusions

The current problem is not that the previous final LUT is fundamentally wrong. It is directionally correct, especially for yellow indoor scenes and crushed shadows. The problem is that its correction is too global: it applies too much luma lift to non-shadow, non-warm material.

The best path appears to be:

```text
keep the previous final LUT's learned correction direction
but gate/dampen its luma component by input luma and warmth
```

This supports the transfer-chain hypothesis: the VHS dub likely needs a luma/chroma correction, but not a uniform RGB polynomial correction everywhere in color space.

## Recommended Next Steps

1. Visually review `expt6l`, `expt6i`, `expt6d`, `expt6c`, and `expt6q` on the Access subset sheets.
2. If `expt6l` looks good, promote it to the next candidate LUT and generate full Access contact sheets.
3. If `expt6l` is still washed out, compare `expt6c` and `expt6d`; those are more aggressive luma-protection variants.
4. If yellow correction becomes too weak, blend toward `expt6f`/`expt6g`-style yellow gating or add a stronger warm-color correction gate.
5. If none of the shortlist works visually, the next experimental axis should be scene-adaptive LUT blending: one LUT for strongly yellow indoor scenes and one conservative tone-protected LUT for neutral scenes.

## Artifacts

Experiment root:

```text
generated_video_pairs/evaluations/expt6_tone_protection/
```

All candidate LUTs:

```text
generated_video_pairs/evaluations/expt6_tone_protection/luts/
```

All-candidate metrics:

```text
generated_video_pairs/evaluations/expt6_tone_protection/evaluation_all/validation_metrics.csv
```

Summary ranking:

```text
generated_video_pairs/evaluations/expt6_tone_protection/experiment_summary.csv
```

Shortlist validation contact sheet:

```text
generated_video_pairs/evaluations/expt6_tone_protection/evaluation_shortlist/validation_lut_contact_sheet.png
```

Access subset visual review:

```text
generated_video_pairs/lut_review_sheets/expt6_shortlist_access_subset/
```
