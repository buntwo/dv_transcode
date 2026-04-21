#!/usr/bin/env python3
# coded by ChatGPT

from __future__ import annotations

import argparse
import errno
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from utils import auto_sibling_dir_for_path

PART_RE = re.compile(r"^(?P<prefix>.+?)_part(?P<num>\d+)(?:-[0-9]+)?\.dv$", re.IGNORECASE)


@dataclass(frozen=True)
class Group:
    start: int
    end: int

    @property
    def singleton(self) -> bool:
        """Return whether this group contains exactly one part."""
        return self.start == self.end


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for split and unsplit modes."""
    parser = argparse.ArgumentParser(
        description="Split a DV file with dvpackager or unsplit selected consecutive parts."
    )
    parser.add_argument(
        "--dvpackager-bin",
        default="dvpackager",
        help="Path to dvpackager binary",
    )
    parser.add_argument(
        "--originals-dirname",
        default="Originals",
        help="Directory name used as the root of original captures",
    )
    parser.add_argument(
        "--logs-dirname",
        default="Logs",
        help="Directory name used for command logs",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    split_p = subparsers.add_parser(
        "split",
        help="Split a DV file into parts using dvpackager",
    )
    split_p.add_argument("input_dv", help="Input DV file to split")
    split_p.add_argument(
        "-s",
        action="store_true",
        help="Split at recording start markers",
    )
    split_p.add_argument(
        "-d",
        action="store_true",
        help="Split at non-consecutive recording timestamps",
    )
    split_p.add_argument(
        "--output-dir",
        help="Output directory for split files (default: sibling 'split' directory)",
    )
    split_p.add_argument(
        "--overwrite",
        action="store_true",
        help="Remove an existing split directory before splitting",
    )
    split_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions without writing files",
    )

    unsplit_p = subparsers.add_parser(
        "unsplit",
        help="Merge selected consecutive split parts back into larger DV files",
    )
    unsplit_p.add_argument(
        "input_dir",
        help="Original DV directory containing a split/ subdirectory",
    )
    unsplit_p.add_argument("spec", help='Grouping spec like "1-3,4,5-9,10"')
    unsplit_p.add_argument(
        "-o",
        "--output-dir",
        help="Output directory (default: original dv directory)",
    )
    unsplit_p.add_argument(
        "--pattern",
        default="*_part*.dv",
        help='Glob for input files inside split/ (default: "*_part*.dv")',
    )
    unsplit_p.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite output files if they already exist",
    )
    unsplit_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print actions without writing files",
    )

    return parser.parse_args()


def setup_logging(level: str) -> None:
    """Configure basic logging output."""
    logging.basicConfig(level=getattr(logging, level), format="%(levelname)s: %(message)s")


def write_command_log(log_dir: Path, filename: str, argv: list[str], dry_run: bool) -> Path:
    """Write the invoked command to a .cmd log file in the auto log directory."""
    log_dir.mkdir(parents=True, exist_ok=True)
    cmd_path = log_dir / filename
    body = " ".join(map(shlex_quote, argv)) + "\n"
    if dry_run:
        logging.info("Would write command log: %s", cmd_path)
    else:
        cmd_path.write_text(body, encoding="utf-8")
    return cmd_path


def shlex_quote(s: str) -> str:
    """Quote a string for shell-safe display in command logs."""
    import shlex

    return shlex.quote(s)


def parse_spec(spec: str) -> list[Group]:
    """Parse the grouping spec into ordered part ranges."""
    groups: list[Group] = []
    for token in spec.split(","):
        token = token.strip()
        if not token:
            raise ValueError("empty token in spec")
        if "-" in token:
            a, b = token.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(token)
        if start <= 0 or end <= 0:
            raise ValueError(f"part numbers must be positive: {token}")
        if end < start:
            raise ValueError(f"invalid descending range: {token}")
        groups.append(Group(start, end))
    return groups


def discover_parts(input_dir: Path, pattern: str) -> tuple[str, dict[int, Path]]:
    """Find input part files and validate the discovered sequence."""
    prefix: str | None = None
    parts: dict[int, Path] = {}

    for path in sorted(input_dir.glob(pattern)):
        m = PART_RE.match(path.name)
        if not m:
            logging.debug("Skipping unmatched file: %s", path.name)
            continue

        this_prefix = m.group("prefix")
        num = int(m.group("num"))

        if prefix is None:
            prefix = this_prefix
        elif this_prefix != prefix:
            raise ValueError(
                f"found mixed prefixes: {prefix!r} and {this_prefix!r}; keep one tape/job per input directory"
            )

        if num in parts:
            raise ValueError(f"duplicate part number {num}: {parts[num]} and {path}")

        parts[num] = path

    if prefix is None or not parts:
        raise ValueError(f'no valid *_partN.dv files found in {input_dir}')

    nums = sorted(parts)
    expected = list(range(nums[0], nums[-1] + 1))
    if nums != expected:
        missing = sorted(set(expected) - set(nums))
        raise ValueError(f"input parts are not contiguous; missing part(s): {missing}")

    return prefix, parts


def validate_spec(groups: list[Group], existing_parts: list[int]) -> None:
    """Ensure the requested grouping covers all parts exactly once."""
    spec_parts = [n for g in groups for n in range(g.start, g.end + 1)]

    if spec_parts != sorted(spec_parts):
        raise ValueError("spec is not in ascending order")
    if len(spec_parts) != len(set(spec_parts)):
        raise ValueError("spec has overlapping or duplicate parts")
    if spec_parts != existing_parts:
        raise ValueError(
            f"spec must cover all parts exactly once.\n"
            f"  existing: {existing_parts}\n"
            f"  spec:     {spec_parts}"
        )

    for g1, g2 in zip(groups, groups[1:]):
        if g2.start != g1.end + 1:
            raise ValueError(f"groups must be adjacent with no gaps: {g1.start}-{g1.end}, {g2.start}-{g2.end}")


def ensure_output_path(output_file: Path, overwrite: bool) -> None:
    """Check output overwrite behavior and remove an existing file if allowed."""
    if output_file.exists():
        if not overwrite:
            raise FileExistsError(f"output already exists: {output_file}")
        output_file.unlink()


def link_or_copy(src: Path, dst: Path, overwrite: bool) -> None:
    """Create a hard link for a singleton part, falling back to copy across filesystems."""
    ensure_output_path(dst, overwrite)
    try:
        os.link(src, dst)
    except OSError as e:
        if e.errno == errno.EXDEV:
            shutil.copy2(src, dst)
        else:
            raise


def run_dvpackager_unpackage(
    dvpackager_bin: str,
    input_files: list[Path],
    output_file: Path,
    overwrite: bool,
) -> None:
    """Merge a group of parts into one DV file using dvpackager unpackage mode."""
    ensure_output_path(output_file, overwrite)

    with tempfile.TemporaryDirectory(prefix="dv_unsplit_") as tmp:
        tmpdir = Path(tmp)
        cmd = [dvpackager_bin, "-u", *map(str, input_files)]
        logging.debug("Running: %s", " ".join(map(str, cmd)))
        subprocess.run(cmd, cwd=tmpdir, check=True)

        generated = list(tmpdir.glob("unpackaged_*.dv"))
        if len(generated) != 1:
            raise RuntimeError(
                f"expected exactly one unpackaged_*.dv output, found {len(generated)} in {tmpdir}"
            )

        shutil.move(str(generated[0]), str(output_file))


def run_split(args: argparse.Namespace) -> int:
    """Split an input DV file into a sibling split directory and log the command."""
    input_dv = Path(args.input_dv).resolve()
    if not input_dv.is_file():
        logging.error("input_dv is not a file: %s", input_dv)
        return 1

    if not args.s and not args.d:
        logging.error("split requires at least one segmentation rule: -s and/or -d")
        return 1

    output_dir = Path(args.output_dir).resolve() if args.output_dir else input_dv.parent / "split"

    if output_dir.exists() and any(output_dir.iterdir()):
        if not args.overwrite:
            logging.error("output_dir already exists and is not empty: %s", output_dir)
            return 1
        if not args.dry_run:
            shutil.rmtree(output_dir)

    if not args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [args.dvpackager_bin]
    if args.s:
        cmd.append("-s")
    if args.d:
        cmd.append("-d")
    cmd += ["-e", "dv", "-o", str(output_dir), str(input_dv)]

    log_dir = auto_sibling_dir_for_path(
        input_dv,
        originals_dirname=args.originals_dirname,
        sibling_dirname=args.logs_dirname,
    )
    log_path = write_command_log(log_dir, "split.cmd", cmd, args.dry_run)

    if args.dry_run:
        logging.info("Would split: %s", input_dv)
        logging.info("Would write to: %s", output_dir)
        logging.info("Would log command to: %s", log_path)
        logging.info("Command: %s", " ".join(map(shlex_quote, cmd)))
        return 0

    subprocess.run(cmd, check=True)

    created_files = sorted(output_dir.glob("*.dv"))
    logging.info("Split complete.")
    logging.info("Input DV: %s", input_dv)
    logging.info("Split dir: %s", output_dir)
    logging.info("Command log: %s", log_path)
    logging.info("Created %d file(s).", len(created_files))
    return 0


def run_unsplit(args: argparse.Namespace) -> int:
    """Unsplit selected consecutive parts into merged or linked outputs and log the command."""
    base_path = Path(args.input_dir).resolve()

    if base_path.is_file():
        base_dir = base_path.parent
    else:
        base_dir = base_path

    if not base_dir.is_dir():
        logging.error("input_dir is not a directory: %s", base_dir)
        return 1

    split_dir = base_dir / "split"
    if not split_dir.is_dir():
        logging.error("split directory not found: %s", split_dir)
        return 1

    output_dir = Path(args.output_dir).resolve() if args.output_dir else base_dir

    try:
        groups = parse_spec(args.spec)
        prefix, part_map = discover_parts(split_dir, args.pattern)
        existing_parts = sorted(part_map)
        validate_spec(groups, existing_parts)
    except Exception as e:
        logging.error("%s", e)
        return 1

    cmd = [Path(sys.argv[0]).name, "unsplit", str(base_path), args.spec]
    if args.output_dir:
        cmd += ["-o", str(output_dir)]
    if args.pattern != "*_part*.dv":
        cmd += ["--pattern", args.pattern]
    if args.overwrite:
        cmd += ["--overwrite"]
    if args.dry_run:
        cmd += ["--dry-run"]

    log_dir = auto_sibling_dir_for_path(
        base_dir,
        originals_dirname=args.originals_dirname,
        sibling_dirname=args.logs_dirname,
    )
    log_path = write_command_log(log_dir, "unsplit.cmd", cmd, args.dry_run)

    created_outputs: list[tuple[Path, int, int]] = []

    for g in groups:
        out_path = output_dir / f"{prefix}_part{g.start}.dv"
        input_files = [part_map[n] for n in range(g.start, g.end + 1)]
        created_outputs.append((out_path, g.start, g.end))

        if args.dry_run:
            continue

        if g.singleton:
            link_or_copy(input_files[0], out_path, args.overwrite)
        else:
            run_dvpackager_unpackage(args.dvpackager_bin, input_files, out_path, args.overwrite)

    if args.dry_run:
        logging.info("Would unsplit from: %s", split_dir)
        logging.info("Would write to: %s", output_dir)
        logging.info("Would log command to: %s", log_path)
        logging.info("Would create:")
        for path, start, end in created_outputs:
            label = f"{start}" if start == end else f"{start}-{end}"
            logging.info("  %s  <- parts %s", path.name, label)
        return 0

    logging.info("Unsplit complete.")
    logging.info("Input dir: %s", base_dir)
    logging.info("Split dir: %s", split_dir)
    logging.info("Output dir: %s", output_dir)
    logging.info("Command log: %s", log_path)
    logging.info("Created file(s):")
    for path, start, end in created_outputs:
        label = f"{start}" if start == end else f"{start}-{end}"
        logging.info("  %s  <- parts %s", path.name, label)
    return 0


def main() -> int:
    """Dispatch to split or unsplit mode."""
    args = parse_args()
    setup_logging(args.log_level)

    if args.command == "split":
        return run_split(args)
    if args.command == "unsplit":
        return run_unsplit(args)

    logging.error("Unknown command: %s", args.command)
    return 1


if __name__ == "__main__":
    sys.exit(main())
