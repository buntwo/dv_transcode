#!/usr/bin/env python3
import argparse
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


SPLIT_ALIASES = {
    "training": "train",
    "train": "train",
    "validation": "validation",
    "valid": "validation",
}


@dataclass(frozen=True)
class Adjustment:
    b_offset_frames: int = 0
    good: bool = False


def parse_finetune(path):
    adjustments = {"train": {}, "validation": {}}
    split = None

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line:
            continue

        lowered = line.lower()
        if lowered in SPLIT_ALIASES:
            split = SPLIT_ALIASES[lowered]
            continue

        if split is None:
            raise ValueError(f"Adjustment row appeared before split header: {line}")

        match = re.match(r"^(\d+)\.\s*(.+)$", line, flags=re.IGNORECASE)
        if not match:
            raise ValueError(f"Could not parse adjustment row: {line}")

        row_index = int(match.group(1))
        note = match.group(2).strip().lower()
        if note == "good":
            adjustments[split][row_index] = Adjustment(good=True)
            continue

        offset_match = re.match(r"^b\s+is\s+(ahead|behind)\s+by\s+(\d+)\s+frames?$", note)
        if not offset_match:
            raise ValueError(f"Could not parse adjustment note: {line}")

        direction = offset_match.group(1)
        frame_count = int(offset_match.group(2))
        offset = frame_count if direction == "ahead" else -frame_count
        adjustments[split][row_index] = Adjustment(b_offset_frames=offset)

    return adjustments


def count_frames(path):
    output = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=nb_read_frames",
            "-of",
            "default=nk=1:nw=1",
            str(path),
        ],
        text=True,
    ).strip()
    return int(output)


def trim_clip(input_path, output_path, start_frame, frame_count):
    end_frame = start_frame + frame_count
    vf = f"trim=start_frame={start_frame}:end_frame={end_frame},setpts=PTS-STARTPTS"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            vf,
            "-c:v",
            "ffv1",
            "-level",
            "3",
            "-g",
            "1",
            "-slicecrc",
            "1",
            str(output_path),
        ],
        check=True,
    )


def copy_pair(src_a, src_b, dst_a, dst_b):
    shutil.copy2(src_a, dst_a)
    shutil.copy2(src_b, dst_b)


def write_adjusted_pair(src_a, src_b, dst_a, dst_b, adjustment):
    if adjustment.good:
        copy_pair(src_a, src_b, dst_a, dst_b)
        return "copied good pair"

    frames_a = count_frames(src_a)
    frames_b = count_frames(src_b)
    offset = adjustment.b_offset_frames

    if offset > 0:
        # B is later than A, so drop the same number of early frames from A.
        start_a = offset
        start_b = 0
    elif offset < 0:
        # B is earlier than A, so drop early frames from B.
        start_a = 0
        start_b = -offset
    else:
        start_a = 0
        start_b = 0

    output_frames = min(frames_a - start_a, frames_b - start_b)
    if output_frames <= 0:
        raise ValueError(f"Offset leaves no overlapping frames for {src_a} / {src_b}")

    trim_clip(src_a, dst_a, start_a, output_frames)
    trim_clip(src_b, dst_b, start_b, output_frames)
    return f"trimmed A start={start_a}, B start={start_b}, frames={output_frames}"


def row_indices(source_dir):
    indices = set()
    for path in source_dir.glob("pair_*_A.mkv"):
        match = re.match(r"pair_(\d+)_A\.mkv$", path.name)
        if match:
            indices.add(int(match.group(1)))
    return sorted(indices)


def process_split(split, source_dir, output_dir, adjustments):
    output_dir.mkdir(parents=True, exist_ok=True)

    for row_index in row_indices(source_dir):
        src_a = source_dir / f"pair_{row_index:03d}_A.mkv"
        src_b = source_dir / f"pair_{row_index:03d}_B.mkv"
        dst_a = output_dir / src_a.name
        dst_b = output_dir / src_b.name

        adjustment = adjustments.get(row_index, Adjustment(good=True))
        result = write_adjusted_pair(src_a, src_b, dst_a, dst_b, adjustment)
        print(f"{split} row {row_index:03d}: {result}")


def main():
    parser = argparse.ArgumentParser(
        description="Apply frame-level A/B alignment notes to generated FFV1 pairs."
    )
    parser.add_argument("--base-dir", type=Path, default=Path("generated_video_pairs"))
    parser.add_argument(
        "--finetune",
        type=Path,
        default=Path("generated_video_pairs/generated_pairs_finetune"),
    )
    parser.add_argument("--train-source", default="train_bad")
    parser.add_argument("--validation-source", default="validation_bad")
    parser.add_argument("--train-output", default="train")
    parser.add_argument("--validation-output", default="validation")
    args = parser.parse_args()

    adjustments = parse_finetune(args.finetune)
    process_split(
        "train",
        args.base_dir / args.train_source,
        args.base_dir / args.train_output,
        adjustments["train"],
    )
    process_split(
        "validation",
        args.base_dir / args.validation_source,
        args.base_dir / args.validation_output,
        adjustments["validation"],
    )


if __name__ == "__main__":
    main()
