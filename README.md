# 8mm Video Transcoding

This repository is an operator-maintained workflow for DV captures from analog and digital 8mm-family tapes, plus VHS access-copy work. It is built around three media types:

- `video8`
- `digital8`
- `vhs`

The repository assumes a sibling-directory output model:

- `Originals/`: source DV captures
- `Access/`: transcoded access copies
- `Logs/`: ffmpeg logs, command logs, and Digital8 sidecars
- `scripts/`: Python tooling

The scripts are written for maintainers who already have DV captures and the required command-line tools available. This README describes current behavior only.

**Repository Layout**
Top-level directories:

- `Originals/`: source captures, typically one tape or logical job per child directory
- `Access/`: MP4 access derivatives, named from the relative path under `Originals/`
- `Logs/`: transcode logs, `dv_unpackager` command logs, and Digital8 CSV/SRT sidecars
- `scripts/`: transcode, split/unsplit, and helper scripts

Path model:

- The naming helpers expect files to live under `Originals/<set>/<child>/.../<file>`.
- `transcode_naming.py` derives the access filename from the relative directory path under `Originals/`.
- The output name is not a mirror of the full path. It shortens the second path segment by taking the first token before the first space when possible.
- Example: `Originals/Set 1/1 Disney/retakes/out.dv` becomes `Access/Set 1/1 Disney/retakes/Set_1_1_retakes_out.mp4`.

Sibling-directory behavior:

- `transcode3.py` and `dv_unpackager.py` both use `Originals/...` as the anchor when creating sibling directories under `Access/` and `Logs/`.
- The helper that computes those sibling paths creates directories eagerly.
- That matters for validation and dry operations on read-only paths or NAS mounts: even "just checking" can fail if the script cannot create the sibling directory tree.
- `dv_unpackager.py split` writes raw DV parts to a per-tape `split/` directory next to the input DV by default, unless `--output-dir` is supplied.

**Core Workflow**
Normal operator flow:

1. Inspect the raw DV capture under `Originals/...` and decide whether it should stay as one file or be split into logical sections.
2. For captures that need splitting, use `scripts/dv_unpackager.py split` to generate raw DV parts with `dvpackager`.
3. If the initial split is too granular, use `scripts/dv_unpackager.py unsplit` or `split-unsplit` to regroup consecutive split parts into logical outputs such as `_partA.dv`, `_partB.dv`, and so on.
4. Transcode the DV files you want to keep with `scripts/transcode3.py`.
5. After transcode, review the automatic duration audit, or run `validate-duration` directly if you want an audit-only pass.

What is optional vs enforced:

- Splitting is optional.
- Unsplit regrouping is optional.
- Transcode duration validation is enabled by default in `--mode transcode` and can be skipped with `--no-validate-duration`.
- `--mode preview` never writes final access outputs and never runs duration validation.

**VHS Color Split Workflow**
For the VHS color-split access workflow, run these commands from `/Users/btu/scratch/Videos`.
The VHS shell entrypoints and rename map live in this repository under `scripts/vhs_workflow/`; the shared Python tools remain under `scripts/scripts/`. Media outputs still live under `/Users/btu/scratch/Videos` by default. Master video files are expected under `/Volumes/TU/tu.brian.2026.05.09/data/masters/tape` by default. Generated task files such as `transcode_vhs_color_split.tasks` are not checked in.

Path overrides:

- `VHS_DATA_ROOT`: media/output root, default `/Users/btu/scratch/Videos`
- `VHS_MASTER_ROOT`: master video root, default `/Volumes/TU/tu.brian.2026.05.09/data/masters/tape`

Example:

```bash
VHS_DATA_ROOT=/path/to/Videos \
VHS_MASTER_ROOT=/path/to/masters/tape \
scripts/vhs_workflow/transcode_vhs_color_split.sh
```

1. Transcode the Access MP4s from the VHS file list.

```bash
scripts/vhs_workflow/transcode_vhs_color_split.sh
```

The default VHS color-split transcode now performs fixed-gain audio in the same ffmpeg pass as the video transcode. Generated tasks pass `--audio-gain 12 --audio-peak-ceiling -1.5`, so each input is analyzed with `volumedetect` before the video transcode starts. Analysis runs against the source media audio after the same pre-gain cleanup/channel filter chain that the final transcode will use, then the final command appends `volume=12dB`.

Audio-analysis JSON is written in the transcode log directory as `<stem>.audio_analysis.json`, for example `Logs_crf22/08.audio_analysis.json`. The JSON stores measured facts and cache identity, not the requested gain, so changing gain values can reuse valid analysis. Cache validity requires matching input path, file size, audio stream, start/end range, and pre-gain audio filter chain.

If the estimated post-gain peak would exceed the ceiling, the job fails before running the video transcode. `08.mkv` still uses the right-channel pan before analysis and before final gain.

To run through the parallel runner instead:

```bash
scripts/vhs_workflow/transcode_vhs_color_split.sh --parallel
```

Useful audio-analysis controls:

- `--mode analyze-audio`: write/reuse audio-analysis JSON and exit without creating video outputs
- `--refresh-audio-analysis`: ignore valid cache and rerun `volumedetect`
- `--audio-analysis PATH_OR_DIR`: read/write analysis JSON at an explicit file or directory
- `--no-audio-analysis`: apply `--audio-gain` without verification; command logs mark this as unverified

2. Rename the Access files from the name map.

The rename tool is `scripts/scripts/rename_access_from_map.py`. It is dry-run by default; add `--apply` only after the plan looks right.

```bash
uv --project scripts run scripts/scripts/rename_access_from_map.py \
  --file-dir Access_crf22 \
  --map-file scripts/vhs_workflow/access_name_map.csv

uv --project scripts run scripts/scripts/rename_access_from_map.py \
  --file-dir Access_crf22 \
  --map-file scripts/vhs_workflow/access_name_map.csv \
  --apply
```

3. Create contact sheets and spectrograms for review.

```bash
uv --project scripts run scripts/scripts/contact_sheet.py \
  --output-dir Access_crf22/contact_sheets \
  Access_crf22/*.mp4

uv --project scripts run scripts/scripts/spectrogram.py \
  --output-dir Access_crf22/spectrograms \
  Access_crf22/*.mp4
```

Legacy/manual audio replacement path:

The FLAC extraction and normalizer wrappers are still available when you explicitly need the old two-step audio replacement flow. They are no longer required for the default VHS color-split access transcode.

```bash
scripts/vhs_workflow/transcode_vhs_color_split_extract_audio.sh \
  --output-dir Access_crf22_audio
```

This wrapper uses the same task list as `transcode_vhs_color_split.sh`, including the per-file audio-channel setting for `08.mkv`. It prints the full extraction plan, file counts, channel counts, and output paths before asking for confirmation.

```bash
scripts/vhs_workflow/transcode_vhs_color_split_normalize_audio.sh \
  --access-copy-dir Access_crf22 \
  --audio-dir Access_crf22_audio \
  --output-dir Access_crf22_normalized
```

This wrapper prints the full normalization plan before asking for confirmation. The underlying normalizer requires exact stem matching: `Access_crf22/Foo.mp4` pairs with `Access_crf22_audio/Foo.flac`. It writes normalized MP4s plus `metadata.csv` in the output directory.

**Video8 vs Digital8**
`video8` flow:

- Input DV is transcoded directly.
- No DVRescue CSV extraction or subtitle generation happens.
- Default bottom crop is `7` rows.
- Default bottom pad is the same as the crop value, so the script restores the cropped height with black padding.
- Default denoise preset is `light`.

`digital8` flow:

- Input DV is first processed through `dvrescue --csv <input> -m -`.
- `scripts/add_play_time_columns.py` adds `play_time_seconds` and `play_time_hhmmss` columns from `FramePos`.
- `scripts/create_srt.py` turns the CSV into one subtitle cue per whole playback second using the `rdt` timestamp.
- `transcode3.py` burns that SRT into the output with ffmpeg subtitles styling near the lower-right corner.
- If the CSV contains a usable first-frame `rdt`, the MP4 output is renamed to start with `YYYYMMDD_`.
- If no usable first-frame `rdt` is found, the output keeps the base access filename with no date prefix.

Digital8 validation behavior:

- Duration validation first looks for the exact expected MP4 path.
- If that file is missing and the format is `digital8`, validation then searches for exactly one dated file matching `*_<expected_name>.mp4`.
- One match is accepted.
- Zero matches is reported as a missing output.
- More than one dated match is reported as ambiguous and fails validation.

**Script Reference**
`scripts/transcode3.py`

- Main transcoder and duration-audit tool.
- Modes:
  - `transcode`: generate MP4 outputs, logs, and optionally run duration validation after the batch
  - `preview`: pipe ffmpeg output to `ffplay` instead of writing the final MP4
  - `validate-duration`: skip transcoding and only audit durations
  - `analyze-audio`: write/reuse audio-analysis JSON and skip video output
- Important defaults:
  - `--codec hevc`
  - `--denoise light`
  - `--deint-mode send_field`
  - `--q 70`
  - `video8` default `--crop-bottom 7`
  - `digital8` default `--crop-bottom 0`
  - `--pad-bottom` defaults to the crop value
  - duration validation enabled by default after `--mode transcode`
  - duration tolerance default `0.17` seconds, which is 5 NTSC DV frames at 29.97 fps
- Audio mapping:
  - default is `-map 0:a:0?`
  - `--map-both-audio` adds `0:a:1?`
- Fixed-gain audio:
  - `--audio-gain DB` appends `volume=<DB>dB` to the audio filter chain
  - by default, fixed gain is verified with source-media `volumedetect` before video transcode
  - `--audio-peak-ceiling DB` sets the maximum allowed estimated post-gain peak, default `-1.5`
  - `--audio-analysis PATH_OR_DIR` reuses/writes analysis JSON outside the default log-dir path
  - `--refresh-audio-analysis` reruns analysis even when a valid cache exists
  - `--no-audio-analysis` applies gain without verification and records that in command logs
- Output and logging:
  - final MP4 goes under the matching `Access/...` sibling path
  - ffmpeg stderr is logged to `Logs/.../<stem>_access_<timestamp>.log`
  - the ffmpeg command and key settings are written to `Logs/.../<stem>_transcode_cmd_<timestamp>.log`
  - audio-analysis JSON defaults to `Logs/.../<stem>.audio_analysis.json`
  - Digital8 sidecars live in `Logs/...` as `<stem>.frameinfo.csv`, `<stem>.frameinfo.with_play_time.csv`, and `<stem>.record_time_overlay.srt`
- Preview mode:
  - uses a temporary runtime directory for ffmpeg logs and any Digital8 CSV/SRT sidecars
  - still builds paths from the normal `Originals`/`Access`/`Logs` model first
  - does not write the final MP4
  - opens `ffplay` with a window title based on the DV stem
- Validation logic:
  - groups `out.dv` and any `out_partX.dv` inputs into one logical source based on the shared base name
  - compares original logical DV duration, summed input DV duration, and summed output MP4 duration
  - errors if original-vs-input or original-vs-output differ by more than the tolerance
  - reports `delta input vs mp4` for audit, but current failure logic is keyed to original-vs-input and original-vs-output

`scripts/dv_unpackager.py`

- Wrapper around `dvpackager` split and unpackage operations.
- Commands:
  - `split`: split one DV file into numbered raw DV parts
  - `unsplit`: regroup consecutive numbered parts into lettered outputs such as `_partA.dv`
  - `split-unsplit`: do both in sequence
- `split` defaults to passing all segmentation flags when none are specified: `-3 -s -d -t`.
- `split` writes a command log to `Logs/.../split.cmd`.
- Default split output directory is `<tape_dir>/split/` unless `--output-dir` is supplied.
- `unsplit` expects a contiguous numbered split series such as `capture_part1.dv`, `capture_part2.dv`, and so on.
- `unsplit` requires the spec to cover all existing parts exactly once, in ascending adjacent groups.
- Group labels follow group order, not source numbering: first group is `_partA`, second is `_partB`, etc.
- Single-part groups are hard-linked when possible and copied across filesystems if needed.
- Multi-part groups are merged through `dvpackager -u`.
- `unsplit` writes a command log to `Logs/.../unsplit.cmd`.

`scripts/add_play_time_columns.py`

- Reads a DVRescue CSV.
- Requires a frame-number column, default `FramePos`.
- Adds:
  - `play_time_seconds`
  - `play_time_hhmmss`
- Uses NTSC DV rate `30000/1001`.

`scripts/create_srt.py`

- Reads a CSV that contains playback time and `rdt`.
- Defaults to `play_time_seconds` for timing and `rdt` for subtitle text.
- Keeps the first observed `rdt` per whole playback second and writes one-second SRT cues.
- Truncates fractional `rdt` values to whole seconds before writing subtitles.

`scripts/contact_sheet.py`

- Generates timestamped JPG contact sheets for one or more videos.
- Defaults to a 5x4 grid, a 2340px sheet width, and a header with filename, size, duration, dimensions, frame rate, video codec, and audio details.
- Derives thumbnail tile dimensions from the sheet width, grid columns, spacing, and each video's display aspect ratio.
- Samples frames at evenly spaced interior timestamps using fast ffmpeg input seeks.
- Requires `ffmpeg` with the `drawtext` filter and ImageMagick `magick`.
- Writes `<input>.contact_sheet.jpg` at `--jpeg-qscale 4` unless `--output` is supplied.
- Explicit `--output some/path.png` still writes PNG.
- Use `-o`/`--output-dir` to write that auto-generated filename into a separate directory.
- `--output` is only valid with one input.

Example:

```bash
uv run python scripts/contact_sheet.py Access/example.mp4
uv run python scripts/contact_sheet.py --output-dir visualizations Access/example.mp4
uv run python scripts/contact_sheet.py --output-dir visualizations Access/*.mp4
```

`scripts/spectrogram.py`

- Generates audio spectrogram PNGs for one or more media files.
- Uses ffmpeg `showspectrumpic` with separate channels, log amplitude scale, linear frequency scale, and legend enabled.
- Defaults to the full input duration and a 1920x1080 PNG.
- Adds the source filename at the upper-left using the same mixed-language font fallback as contact sheets.
- Covers the lower-left libavfilter credit after rendering.
- Writes `<input>.spectrogram.png` unless `--output` is supplied.
- Use `-o`/`--output-dir` to write auto-generated filenames into a separate directory.
- `--output` is only valid with one input.

Example:

```bash
uv run python scripts/spectrogram.py -o visualizations Access/example.mp4
uv run python scripts/spectrogram.py -o visualizations Access/*.mp4
```

Internal helpers:

- `scripts/transcode_naming.py`: access filename construction from `Originals/...` relative paths
- `scripts/utils.py`: sibling directory resolution and creation

**Validation and Audit**
Logical grouping:

- Validation maps `out_partA.dv`, `out_partB.dv`, or `out_part1.dv` back to logical source `out.dv`.
- Inputs that do not match the `_part...` pattern are validated as standalone files.
- When multiple split outputs are passed together, validation sums all of them before comparing against the logical original.

What validates cleanly today:

- An unsplit logical group validates correctly when the matching original `out.dv` exists and the passed input files share the same base name.
- Split outputs validate correctly when the corresponding `_part...dv` inputs exist and the output MP4 names match the per-part access naming.
- Digital8 dated outputs validate correctly when exactly one dated MP4 matches the expected base access filename.

Current caveats:

- Validation output path discovery still depends on `build_paths()`, so it inherits the same `Originals` path assumptions and sibling-directory creation behavior as transcode mode.
- Validation on read-only or NAS paths can fail because path resolution creates sibling `Access/` and `Logs/` directories even when the goal is only to inspect durations.
- Split-output validation assumes matching `_part...dv` source files are what you pass in. The grouping logic is based on those filenames.
- Digital8 validation only has special handling for dated outputs. Other unexpected renames are not discovered automatically.

**Examples**
Plain Video8 transcode:

```bash
python3 scripts/transcode3.py --mode transcode --format video8 \
  Originals/Set\ 2/1\ 2001/out.dv
```

Digital8 transcode:

```bash
python3 scripts/transcode3.py --mode transcode --format digital8 \
  Originals/Set\ 5/12\ Vacation/out.dv
```

Preview a tape without writing the final MP4:

```bash
python3 scripts/transcode3.py --mode preview --format video8 \
  Originals/Set\ 2/1\ 2001/out.dv
```

Validate durations for one unsplit DV:

```bash
python3 scripts/transcode3.py --mode validate-duration --format video8 \
  Originals/Set\ 2/1\ 2001/out.dv
```

Validate durations for split logical parts:

```bash
python3 scripts/transcode3.py --mode validate-duration --format video8 \
  Originals/Set\ 5/12\ Vacation/out_partA.dv \
  Originals/Set\ 5/12\ Vacation/out_partB.dv
```

Split a DV file with default segmentation flags (`-3 -s -d -t`):

```bash
python3 scripts/dv_unpackager.py split \
  Originals/Set\ 5/12\ Vacation/out.dv
```

Unsplit numbered split parts into grouped outputs:

```bash
python3 scripts/dv_unpackager.py unsplit \
  Originals/Set\ 5/12\ Vacation \
  1-3,4,5-9,10
```

Split and immediately unsplit in one pass:

```bash
python3 scripts/dv_unpackager.py split-unsplit \
  Originals/Set\ 5/12\ Vacation/out.dv \
  1-3,4,5-9,10
```

Generate play-time columns manually:

```bash
python3 scripts/add_play_time_columns.py \
  Logs/Set\ 5/12\ Vacation/out.frameinfo.csv
```

Generate an SRT manually:

```bash
python3 scripts/create_srt.py \
  Logs/Set\ 5/12\ Vacation/out.frameinfo.with_play_time.csv
```

**Operational Notes / Pitfalls**
- Digital8 outputs may be renamed with a leading `YYYYMMDD_` date derived from the first `rdt` timestamp. That is expected behavior, and validation knows how to resolve exactly one such dated file.
- Split and unsplit naming are different layers:
  - `dvpackager split` creates numbered raw DV parts such as `_part1.dv`
  - `dv_unpackager unsplit` creates logical grouped outputs such as `_partA.dv`, `_partB.dv`
  - `transcode3.py` names MP4s from the relative `Originals/...` path plus the DV stem it receives
- Preview mode is for inspection only. It still creates temporary runtime artifacts and still depends on the normal path model being valid.
- Validation on read-only media, SMB/NAS paths, or otherwise restricted mounts can fail before probing durations because sibling directory helpers create directories.
- External dependencies are required and assumed to be on `PATH`:
  - `python3`
  - `ffmpeg`
  - `ffprobe`
  - `ffplay` for preview mode
  - `dvrescue` for `digital8`
  - `dvpackager` for split/unsplit operations
