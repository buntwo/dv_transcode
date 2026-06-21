#!/usr/bin/env python3
import argparse
import re
from pathlib import Path


def discover_pairs(input_dir):
    pairs = []
    for ref_path in sorted(input_dir.glob("pair_*_A.mkv")):
        match = re.match(r"pair_(\d+)_A\.mkv$", ref_path.name)
        if not match:
            continue

        src_path = input_dir / f"pair_{match.group(1)}_B.mkv"
        if not src_path.exists():
            raise FileNotFoundError(f"Missing B clip for {ref_path.name}: {src_path}")

        pairs.append((ref_path, src_path))

    if not pairs:
        raise RuntimeError(f"No pair_*_A.mkv / pair_*_B.mkv clips found in {input_dir}")

    return pairs


def main():
    parser = argparse.ArgumentParser(
        description="Generate a video8|vhs manifest from pair_NNN_A/B clips."
    )
    parser.add_argument("input_dir", type=Path)
    parser.add_argument("output_manifest", type=Path)
    parser.add_argument(
        "--relative-to",
        type=Path,
        default=Path.cwd(),
        help="Write paths relative to this directory. Defaults to the current directory.",
    )
    args = parser.parse_args()

    pairs = discover_pairs(args.input_dir)
    base = args.relative_to.resolve()

    args.output_manifest.parent.mkdir(parents=True, exist_ok=True)
    with args.output_manifest.open("w") as out:
        for ref_path, src_path in pairs:
            ref = ref_path.resolve().relative_to(base)
            src = src_path.resolve().relative_to(base)
            out.write(f"{ref}|{src}\n")

    print(f"Wrote {len(pairs)} pairs to {args.output_manifest}")


if __name__ == "__main__":
    main()
