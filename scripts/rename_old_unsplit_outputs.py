#!/usr/bin/env python3

from __future__ import annotations

import argparse
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

from dv_unpackager import index_to_letters, parse_spec


@dataclass(frozen=True)
class Job:
    log_path: Path
    job_dir: Path
    stem_map: list[tuple[str, str]]


@dataclass
class Totals:
    jobs: int = 0
    rename_plans: int = 0
    renamed: int = 0
    missing: int = 0
    conflicts: int = 0
    malformed: int = 0


@dataclass(frozen=True)
class ReportRow:
    status: str
    scope: str
    source: str
    detail: str


def display_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Rename old numeric unsplit outputs (out_part1, out_part10, ...) "
            "to the newer letter-based naming (out_partA, out_partB, ...)."
        )
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Project root containing Logs/, Access/, and Originals/ (default: .)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform renames in place; default is dry run",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print additional per-job progress",
    )
    return parser.parse_args()


def derive_job_dir(input_arg: str) -> Path:
    input_path = Path(input_arg)
    parts = input_path.parts

    try:
        originals_idx = max(i for i, part in enumerate(parts) if part == "Originals")
    except ValueError as exc:
        raise ValueError(f"input_dir does not contain 'Originals': {input_arg}") from exc

    rel_parts = list(parts[originals_idx + 1 :])
    if not rel_parts:
        raise ValueError(f"input_dir does not point to a job under Originals: {input_arg}")

    if rel_parts[-1] == "split":
        rel_parts.pop()
    elif "." in Path(rel_parts[-1]).name:
        rel_parts.pop()

    if not rel_parts:
        raise ValueError(f"could not derive job-relative directory from input_dir: {input_arg}")

    return Path(*rel_parts)


def parse_unsplit_job(log_path: Path) -> Job:
    try:
        line = log_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"failed to read {log_path}: {exc}") from exc

    if not line:
        raise ValueError(f"empty command log: {log_path}")

    try:
        argv = shlex.split(line)
    except ValueError as exc:
        raise ValueError(f"invalid shell quoting in {log_path}: {exc}") from exc

    if len(argv) < 4 or argv[1] != "unsplit":
        raise ValueError(f"unexpected unsplit.cmd format: {log_path}")

    input_dir = argv[2]
    spec = argv[3]

    try:
        groups = parse_spec(spec)
    except Exception as exc:
        raise ValueError(f"invalid spec in {log_path}: {exc}") from exc

    job_dir = derive_job_dir(input_dir)
    stem_map = [
        (f"out_part{group.start}", f"out_part{index_to_letters(idx)}")
        for idx, group in enumerate(groups, start=1)
    ]

    return Job(log_path=log_path, job_dir=job_dir, stem_map=stem_map)


def find_access_matches(job_access_dir: Path, old_stem: str) -> list[Path]:
    return sorted(path for path in job_access_dir.glob("*.mp4") if path.name.endswith(f"_{old_stem}.mp4"))


def find_log_matches(job_logs_dir: Path, old_stem: str) -> list[Path]:
    stem_re = re.compile(rf"^{re.escape(old_stem)}(?:$|[^0-9])")
    return sorted(
        path
        for path in job_logs_dir.iterdir()
        if path.is_file() and stem_re.match(path.name) and path.name not in {"unsplit.cmd", "split.cmd"}
    )


def rename_target(path: Path, old_stem: str, new_stem: str) -> Path:
    if path.name.startswith(old_stem):
        return path.with_name(new_stem + path.name[len(old_stem) :])
    suffix = f"_{old_stem}.mp4"
    if path.name.endswith(suffix):
        return path.with_name(path.name[: -len(suffix)] + f"_{new_stem}.mp4")
    raise ValueError(f"file does not match stem {old_stem}: {path}")


def format_job_report(job: Job, root: Path, rows: list[ReportRow]) -> None:
    if not rows:
        return

    source_width = max(len(row.source) for row in rows)
    print()
    print(f"JOB   {job.job_dir}")
    print(f"LOG   {display_path(job.log_path, root)}")
    print(f"DIR   Access/{job.job_dir}")
    print(f"DIR   Logs/{job.job_dir}")
    for row in rows:
        print(f"{row.status:<6} {row.scope:<6} {row.source:<{source_width}}  {row.detail}")


def handle_path(source: Path, target: Path, scope: str, apply: bool, totals: Totals) -> ReportRow | None:
    if source == target:
        return None
    if target.exists():
        totals.conflicts += 1
        return ReportRow("SKIP", scope, source.name, f"conflict -> {target.name}")

    action = "RENAME" if apply else "PLAN"
    totals.rename_plans += 1
    if apply:
        source.rename(target)
        totals.renamed += 1
    return ReportRow(action, scope, source.name, f"-> {target.name}")


def process_job(root: Path, job: Job, apply: bool, verbose: bool, totals: Totals) -> None:
    totals.jobs += 1
    job_access_dir = root / "Access" / job.job_dir
    job_logs_dir = root / "Logs" / job.job_dir
    rows: list[ReportRow] = []

    for old_stem, new_stem in job.stem_map:
        access_matches = find_access_matches(job_access_dir, old_stem) if job_access_dir.is_dir() else []
        log_matches = find_log_matches(job_logs_dir, old_stem) if job_logs_dir.is_dir() else []

        if not access_matches:
            totals.missing += 1
            rows.append(ReportRow("SKIP", "Access", f"{old_stem}.mp4", "missing"))
        for source in access_matches:
            row = handle_path(source, rename_target(source, old_stem, new_stem), "Access", apply, totals)
            if row is not None:
                rows.append(row)

        if not log_matches:
            totals.missing += 1
            rows.append(ReportRow("SKIP", "Logs", old_stem, "missing"))
        for source in log_matches:
            row = handle_path(source, rename_target(source, old_stem, new_stem), "Logs", apply, totals)
            if row is not None:
                rows.append(row)

    if verbose or rows:
        format_job_report(job, root, rows)


def main() -> int:
    args = parse_args()
    root = Path(args.root).resolve()
    logs_root = root / "Logs"

    if not logs_root.is_dir():
        print(f"ERROR: Logs directory not found under root: {logs_root}", file=sys.stderr)
        return 1

    totals = Totals()
    unsplit_logs = sorted(logs_root.glob("**/unsplit.cmd"))

    for log_path in unsplit_logs:
        try:
            job = parse_unsplit_job(log_path)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            totals.malformed += 1
            continue
        process_job(root, job, args.apply, args.verbose, totals)

    mode = "apply" if args.apply else "dry-run"
    print(
        f"SUMMARY: mode={mode} jobs={totals.jobs} planned={totals.rename_plans} "
        f"renamed={totals.renamed} missing={totals.missing} conflicts={totals.conflicts} "
        f"malformed={totals.malformed}"
    )
    return 1 if totals.malformed else 0


if __name__ == "__main__":
    raise SystemExit(main())
