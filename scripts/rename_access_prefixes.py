#!/usr/bin/env python3

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from transcode_naming import (
    build_access_output_name_from_rel_dir,
    build_legacy_access_output_name_from_rel_dir,
)


DATE_PREFIX_RE = re.compile(r"^\d{8}_")


@dataclass(frozen=True)
class RenamePlan:
    source: Path
    target: Path


@dataclass
class Totals:
    files: int = 0
    planned: int = 0
    renamed: int = 0
    conflicts: int = 0
    malformed: int = 0


@dataclass(frozen=True)
class ReportRow:
    status: str
    source: str
    detail: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rename Access MP4s to the shortened prefix format from transcode command logs."
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Project root containing Logs/, Access/, and Originals/ (default: .)",
    )
    parser.add_argument("--apply", action="store_true", help="Perform renames in place; default is dry run")
    parser.add_argument("--verbose", action="store_true", help="Print grouped per-log reporting")
    return parser.parse_args()


def display_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def split_date_prefix(filename: str) -> tuple[str, str]:
    if DATE_PREFIX_RE.match(filename):
        return filename[:9], filename[9:]
    return "", filename


def derive_rel_dir(access_file: Path, access_root: Path) -> Path:
    try:
        return access_file.parent.relative_to(access_root)
    except ValueError as exc:
        raise ValueError(f"file is not under access root {access_root}: {access_file}") from exc


def parse_name_against_prefixes(filename: str, rel_dir: Path) -> tuple[str, str, str]:
    if not filename.endswith(".mp4"):
        raise ValueError(f"not an mp4: {filename}")

    date_prefix, base_name = split_date_prefix(filename)
    base_stem = base_name[:-4]
    rel_dir_str = str(rel_dir)
    new_prefix = build_access_output_name_from_rel_dir(rel_dir_str, stem="__STEM__")[:-len("__STEM__.mp4") - 1]
    legacy_prefix = build_legacy_access_output_name_from_rel_dir(rel_dir_str, stem="__STEM__")[:-len("__STEM__.mp4") - 1]

    for prefix, mode in ((legacy_prefix, "legacy"), (new_prefix, "current")):
        marker = f"{prefix}_"
        if base_stem.startswith(marker):
            return date_prefix, base_stem[len(marker) :], mode

    raise ValueError(f"filename does not match expected Access prefix for {rel_dir}: {filename}")


def build_rename_plan(access_file: Path, access_root: Path) -> RenamePlan:
    rel_dir = derive_rel_dir(access_file, access_root)
    date_prefix, stem_and_suffix, _mode = parse_name_against_prefixes(access_file.name, rel_dir)
    target_name = date_prefix + build_access_output_name_from_rel_dir(rel_dir, stem=stem_and_suffix)
    return RenamePlan(source=access_file, target=access_file.with_name(target_name))


def format_group(root: Path, group_name: str, rows: list[ReportRow]) -> None:
    if not rows:
        return
    source_width = max(len(row.source) for row in rows)
    print()
    print(f"DIR   {group_name}")
    for row in rows:
        print(f"{row.status:<6} {row.source:<{source_width}}  {row.detail}")


def process_access_dir(root: Path, access_root: Path, rel_dir: Path, apply: bool, verbose: bool, totals: Totals) -> None:
    rows: list[ReportRow] = []
    dir_path = access_root / rel_dir
    for access_file in sorted(dir_path.glob("*.mp4")):
        totals.files += 1
        try:
            plan = build_rename_plan(access_file, access_root)
        except ValueError as exc:
            totals.malformed += 1
            rows.append(ReportRow("ERROR", access_file.name, str(exc)))
            continue

        if plan.source == plan.target:
            rows.append(ReportRow("SKIP", plan.source.name, "already matches"))
        elif plan.target.exists():
            totals.conflicts += 1
            rows.append(ReportRow("SKIP", plan.source.name, f"conflict -> {plan.target.name}"))
        else:
            totals.planned += 1
            action = "RENAME" if apply else "PLAN"
            if apply:
                plan.source.rename(plan.target)
                totals.renamed += 1
            rows.append(ReportRow(action, plan.source.name, f"-> {plan.target.name}"))

    if verbose or rows:
        format_group(root, f"Access/{rel_dir}", rows)


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    access_root = root / "Access"
    if not access_root.is_dir():
        print(f"ERROR: Access directory not found under root: {access_root}", file=sys.stderr)
        return 1

    totals = Totals()
    rel_dirs = sorted({path.parent.relative_to(access_root) for path in access_root.glob("**/*.mp4")})
    for rel_dir in rel_dirs:
        process_access_dir(root, access_root, rel_dir, args.apply, args.verbose, totals)

    mode = "apply" if args.apply else "dry-run"
    print(
        f"SUMMARY: mode={mode} files={totals.files} planned={totals.planned} "
        f"renamed={totals.renamed} conflicts={totals.conflicts} malformed={totals.malformed}"
    )
    return 1 if totals.malformed else 0


if __name__ == "__main__":
    raise SystemExit(main())
