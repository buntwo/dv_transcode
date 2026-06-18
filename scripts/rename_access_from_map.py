#!/usr/bin/env python3
"""Rename Access files from a CSV stem mapping."""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


REQUIRED_COLUMNS = ("original_stem", "renamed_stem")


@dataclass(frozen=True)
class MappingRow:
    row_number: int
    sequence_number: int
    original_stem: str
    renamed_stem: str


@dataclass(frozen=True)
class RenamePlan:
    source: Path
    target: Path


@dataclass(frozen=True)
class PreflightResult:
    plans: list[RenamePlan]
    errors: list[str]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rename files in an Access directory using original_stem and renamed_stem CSV columns."
    )
    parser.add_argument("--file-dir", type=Path, required=True, help="Directory containing Access files")
    parser.add_argument("--map-file", type=Path, required=True, help="CSV containing original_stem and renamed_stem")
    parser.add_argument("--apply", action="store_true", help="Perform renames in place; default is dry run")
    parser.add_argument(
        "--reverse",
        action="store_true",
        help="Rename mapped files back to original_stem names using the same CSV",
    )
    parser.add_argument(
        "--preserve-underscores",
        action="store_true",
        help="Keep underscores in renamed_stem; default converts underscores to spaces",
    )
    return parser.parse_args(argv)


def read_mapping_rows(map_file: Path) -> tuple[list[MappingRow], list[str]]:
    errors: list[str] = []
    rows: list[MappingRow] = []

    try:
        with map_file.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = reader.fieldnames or []
            missing_columns = [column for column in REQUIRED_COLUMNS if column not in fieldnames]
            if missing_columns:
                return [], [f"Missing required CSV column(s): {', '.join(missing_columns)}"]

            seen_original_stems: set[str] = set()
            for sequence_number, row in enumerate(reader, start=1):
                row_number = sequence_number + 1
                original_stem = (row.get("original_stem") or "").strip()
                renamed_stem = (row.get("renamed_stem") or "").strip()

                if not original_stem:
                    errors.append(f"Row {row_number}: original_stem is blank")
                    continue
                if not renamed_stem:
                    errors.append(f"Row {row_number}: renamed_stem is blank")
                    continue
                if original_stem in seen_original_stems:
                    errors.append(f"Row {row_number}: duplicate original_stem: {original_stem}")
                    continue

                seen_original_stems.add(original_stem)
                rows.append(MappingRow(row_number, sequence_number, original_stem, renamed_stem))
    except OSError as exc:
        return [], [f"Could not read map file {map_file}: {exc}"]

    return rows, errors


def display_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def padded_sequence_number(sequence_number: int) -> str:
    return str(sequence_number).zfill(2)


def normalize_renamed_stem(renamed_stem: str, preserve_underscores: bool) -> str:
    if preserve_underscores:
        return renamed_stem
    return renamed_stem.replace("_", " ")


def mapped_stem(row: MappingRow, preserve_underscores: bool) -> str:
    normalized_stem = normalize_renamed_stem(row.renamed_stem, preserve_underscores)
    return f"{padded_sequence_number(row.sequence_number)} {normalized_stem}"


def find_matching_files(file_dir: Path, stem: str) -> list[Path]:
    return sorted(path for path in file_dir.iterdir() if path.is_file() and path.stem == stem)


def build_preflight(
    file_dir: Path,
    map_file: Path,
    preserve_underscores: bool = False,
    reverse: bool = False,
) -> PreflightResult:
    errors: list[str] = []
    plans: list[RenamePlan] = []

    if not file_dir.is_dir():
        errors.append(f"File directory not found: {file_dir}")
        return PreflightResult([], errors)

    rows, row_errors = read_mapping_rows(map_file)
    errors.extend(row_errors)

    for row in rows:
        source_stem = mapped_stem(row, preserve_underscores) if reverse else row.original_stem
        target_stem = row.original_stem if reverse else mapped_stem(row, preserve_underscores)
        matches = find_matching_files(file_dir, source_stem)
        if not matches:
            errors.append(f"Row {row.row_number}: no source file found for stem {source_stem}")
            continue
        if len(matches) > 1:
            names = ", ".join(path.name for path in matches)
            errors.append(f"Row {row.row_number}: multiple source files found for stem {source_stem}: {names}")
            continue

        source = matches[0]
        target_name = f"{target_stem}{source.suffix}"
        plans.append(RenamePlan(source=source, target=source.with_name(target_name)))

    target_to_source: dict[Path, Path] = {}
    for plan in plans:
        existing_source = target_to_source.get(plan.target)
        if existing_source is not None:
            errors.append(
                "Duplicate planned target "
                f"{display_path(plan.target, file_dir)} from {existing_source.name} and {plan.source.name}"
            )
            continue
        target_to_source[plan.target] = plan.source

        if plan.target.exists():
            try:
                same_file = plan.target.samefile(plan.source)
            except OSError:
                same_file = False
            if not same_file:
                errors.append(
                    f"Target already exists for {plan.source.name}: {display_path(plan.target, file_dir)}"
                )

    return PreflightResult(plans, errors)


def print_report(result: PreflightResult, file_dir: Path, apply: bool) -> None:
    mode = "apply" if apply else "dry-run"
    for plan in result.plans:
        action = "RENAME" if apply and not result.errors else "PLAN"
        print(f"{action} {display_path(plan.source, file_dir)} -> {display_path(plan.target, file_dir)}")

    for error in result.errors:
        print(f"ERROR {error}", file=sys.stderr)

    if result.errors:
        print(f"SUMMARY: mode={mode} planned={len(result.plans)} errors={len(result.errors)} renamed=0")
    else:
        renamed = len(result.plans) if apply else 0
        print(f"SUMMARY: mode={mode} planned={len(result.plans)} errors=0 renamed={renamed}")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    file_dir = args.file_dir.resolve()
    map_file = args.map_file.resolve()

    result = build_preflight(
        file_dir,
        map_file,
        preserve_underscores=args.preserve_underscores,
        reverse=args.reverse,
    )
    if not result.errors and args.apply:
        for plan in result.plans:
            plan.source.rename(plan.target)

    print_report(result, file_dir, args.apply)
    return 1 if result.errors else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
