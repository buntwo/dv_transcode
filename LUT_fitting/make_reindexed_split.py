#!/usr/bin/env python3
import argparse
import csv
import os
import shutil
from pathlib import Path


TRAIN_KEEP = [2, 4, 6, 8, 11]
VALIDATION_TO_TRAIN = [3, 7]


def pair_paths(input_dir, index):
    stem = f"pair_{index:03d}"
    a_path = input_dir / f"{stem}_A.mkv"
    b_path = input_dir / f"{stem}_B.mkv"
    if not a_path.exists() or not b_path.exists():
        raise FileNotFoundError(f"Missing pair {stem} in {input_dir}")
    return a_path, b_path


def discover_indices(input_dir):
    indices = []
    for path in sorted(input_dir.glob("pair_*_A.mkv")):
        try:
            indices.append(int(path.stem.split("_")[1]))
        except (IndexError, ValueError) as exc:
            raise RuntimeError(f"Unexpected pair filename: {path.name}") from exc
    if not indices:
        raise RuntimeError(f"No pair_*_A.mkv files found in {input_dir}")
    return indices


def link_or_copy(src, dst):
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def write_pair(src_a, src_b, output_dir, new_index):
    stem = f"pair_{new_index:03d}"
    dst_a = output_dir / f"{stem}_A.mkv"
    dst_b = output_dir / f"{stem}_B.mkv"
    link_or_copy(src_a, dst_a)
    link_or_copy(src_b, dst_b)
    return dst_a, dst_b


def build_manifest(pairs, manifest_path, relative_to):
    base = relative_to.resolve()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w") as out:
        for a_path, b_path in pairs:
            a_rel = a_path.resolve().relative_to(base)
            b_rel = b_path.resolve().relative_to(base)
            out.write(f"{a_rel}|{b_rel}\n")


def add_selection(rows, source_name, source_dir, indices):
    for index in indices:
        src_a, src_b = pair_paths(source_dir, index)
        rows.append(
            {
                "source_split": source_name,
                "source_index": index,
                "source_a": src_a,
                "source_b": src_b,
            }
        )


def write_split(rows, output_dir):
    output_dir.mkdir(parents=True)
    written = []
    map_rows = []
    for new_index, row in enumerate(rows, start=1):
        dst_a, dst_b = write_pair(row["source_a"], row["source_b"], output_dir, new_index)
        written.append((dst_a, dst_b))
        map_rows.append(
            {
                "new_index": new_index,
                "source_split": row["source_split"],
                "source_index": row["source_index"],
                "new_a": dst_a.name,
                "new_b": dst_b.name,
            }
        )
    return written, map_rows


def main():
    parser = argparse.ArgumentParser(
        description="Build the blue-rebalanced train/validation split from normalized clips."
    )
    parser.add_argument(
        "--train-dir",
        type=Path,
        default=Path("generated_video_pairs/train_geometry_normalized"),
    )
    parser.add_argument(
        "--validation-dir",
        type=Path,
        default=Path("generated_video_pairs/validation_geometry_normalized"),
    )
    parser.add_argument(
        "--out-root",
        type=Path,
        default=Path("generated_video_pairs/splits/blue_rebalance"),
    )
    parser.add_argument(
        "--relative-to",
        type=Path,
        default=Path.cwd(),
        help="Write manifest paths relative to this directory.",
    )
    args = parser.parse_args()

    if args.out_root.exists():
        raise SystemExit(f"Refusing to clobber existing output: {args.out_root}")

    train_indices = discover_indices(args.train_dir)
    validation_indices = discover_indices(args.validation_dir)

    train_rows = []
    validation_rows = []

    add_selection(train_rows, "train", args.train_dir, TRAIN_KEEP)
    add_selection(train_rows, "validation", args.validation_dir, VALIDATION_TO_TRAIN)

    add_selection(
        validation_rows,
        "train",
        args.train_dir,
        [index for index in train_indices if index not in TRAIN_KEEP],
    )
    add_selection(
        validation_rows,
        "validation",
        args.validation_dir,
        [index for index in validation_indices if index not in VALIDATION_TO_TRAIN],
    )

    train_dir = args.out_root / "train"
    validation_dir = args.out_root / "validation"
    train_pairs, train_map = write_split(train_rows, train_dir)
    validation_pairs, validation_map = write_split(validation_rows, validation_dir)

    train_manifest = args.out_root / "train_pairs.txt"
    validation_manifest = args.out_root / "validation_pairs.txt"
    build_manifest(train_pairs, train_manifest, args.relative_to)
    build_manifest(validation_pairs, validation_manifest, args.relative_to)

    with (args.out_root / "split_map.csv").open("w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["target_split", "new_index", "source_split", "source_index", "new_a", "new_b"],
        )
        writer.writeheader()
        for row in train_map:
            writer.writerow({"target_split": "train", **row})
        for row in validation_map:
            writer.writerow({"target_split": "validation", **row})

    print(f"Wrote {len(train_pairs)} train pairs to {train_dir}")
    print(f"Wrote {len(validation_pairs)} validation pairs to {validation_dir}")
    print(f"Wrote {train_manifest}")
    print(f"Wrote {validation_manifest}")
    print(f"Wrote {args.out_root / 'split_map.csv'}")


if __name__ == "__main__":
    main()
