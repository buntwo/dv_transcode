#!/usr/bin/env python3
"""Extract files from preserved optical disc image files."""

from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import iso_tool
from utils import display_width
from utils import format_progress
from utils import pad_display


CD_SIZE_LIMIT = 900 * 1024 * 1024
BCHUNK_ARTIFACT_RE = re.compile(r".+\.bchunk\d+\.iso$", re.IGNORECASE)


class ToolError(Exception):
    """An expected operational error that should be shown without a traceback."""


@dataclass(frozen=True)
class CommandResult:
    command: list[str]
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


class CommandRunner:
    def which(self, command: str) -> str | None:
        return shutil.which(command)

    def run(self, command: Sequence[str], cwd: Path | None = None, check: bool = True) -> CommandResult:
        result = subprocess.run(
            list(command),
            cwd=str(cwd) if cwd is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        command_result = CommandResult(
            command=list(command),
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
        if check and command_result.returncode != 0:
            output = command_output_text(command_result).strip()
            raise ToolError(f"{format_command(command)} failed: {output or f'exit status {command_result.returncode}'}")
        return command_result


@dataclass
class DVDTitle:
    title_id: int
    duration: str = ""
    size: str = ""
    byte_size: int | None = None
    output_filename: str = ""
    track_summary: str = ""

    @property
    def filename(self) -> str:
        return self.output_filename or f"title_t{self.title_id:02d}.mkv"


@dataclass
class ExtractJob:
    image: Path
    root: Path
    image_kind: str
    media_type: str = "unknown"
    output_dir: Path | None = None
    output_status: str = "not checked"
    action: str = "pending"
    blockers: list[str] = field(default_factory=list)
    skip_reason: str | None = None
    cue: Path | None = None
    toc: Path | None = None
    dvd_titles: list[DVDTitle] = field(default_factory=list)

    @property
    def runnable(self) -> bool:
        return not self.blockers and self.skip_reason is None and self.action.startswith("extract ")

    @property
    def blocked(self) -> bool:
        return bool(self.blockers)


def command_output_text(result: CommandResult) -> str:
    return (result.stdout + result.stderr).decode("utf-8", errors="replace")


def format_command(command: Sequence[str]) -> str:
    return " ".join(sh_quote(str(part)) for part in command)


def sh_quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:=,+@%-]+", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract files from preserved .iso and .bin disc images.")
    parser.add_argument("root", type=Path, help="Root directory to scan recursively")
    parser.add_argument("--yes", action="store_true", help="Run the printed extraction plan without prompting")
    parser.add_argument("--plan-only", action="store_true", help="Print the preflight plan and exit without writing")
    parser.add_argument(
        "--scan-dvd-titles",
        action="store_true",
        help="Run the slower MakeMKV title scan during preflight and show DVD title details",
    )
    parser.add_argument(
        "--force-media-type",
        choices=("cd-data", "dvd-data", "dvd-video", "cd-vcd", "cd-audio"),
        help="Bypass media probing and force the extraction strategy for every discovered image",
    )
    parser.add_argument("--verbose", action="store_true", help="Print command output details when extraction fails")
    return parser.parse_args(argv)


def discover_images(root: Path) -> list[Path]:
    images: list[Path] = []
    for current_root, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(dirname for dirname in dirnames if not is_output_dirname(dirname))
        current_path = Path(current_root)
        for filename in sorted(filenames):
            path = current_path / filename
            suffix = path.suffix.casefold()
            if suffix not in {".iso", ".bin"}:
                continue
            if suffix == ".iso" and is_bchunk_artifact(path):
                continue
            images.append(path)
    return images


def is_output_dirname(dirname: str) -> bool:
    lowered = dirname.casefold()
    return lowered == "extracted" or lowered.endswith(".extracted")


def is_bchunk_artifact(path: Path) -> bool:
    return BCHUNK_ARTIFACT_RE.fullmatch(path.name) is not None


def preferred_output_dir(image: Path) -> Path:
    return image.parent / f"{image.stem}.extracted"


def legacy_output_dir(image: Path) -> Path:
    return image.parent / "extracted"


def build_plan(
    root: Path,
    runner: CommandRunner,
    *,
    show_progress: bool = False,
    progress_stream=None,
    scan_dvd_titles: bool = False,
    force_media_type: str | None = None,
) -> list[ExtractJob]:
    root = root.resolve()
    jobs = [
        ExtractJob(
            image=image,
            root=root,
            image_kind=image.suffix.casefold().lstrip("."),
            output_dir=preferred_output_dir(image),
        )
        for image in discover_images(root)
    ]

    progress = PreflightProgress(root, len(jobs), stream=progress_stream) if show_progress and jobs else None
    try:
        for job in jobs:
            if force_media_type is not None:
                classify_forced_job(job, runner, force_media_type, scan_dvd_titles=scan_dvd_titles)
                report_preflight_progress(progress, job)
            elif job.image_kind == "iso":
                classify_iso_job(job, runner, scan_dvd_titles=scan_dvd_titles)
                report_preflight_progress(progress, job)

        dropped_bins = same_stem_bins_with_data_iso(jobs)
        for job in dropped_bins:
            report_preflight_progress(progress, job)
        jobs = [job for job in jobs if job not in dropped_bins]

        for job in jobs:
            if job.skip_reason is not None or job.blockers:
                continue
            if force_media_type is None and job.image_kind == "bin":
                classify_bin_job(job, runner)
                report_preflight_progress(progress, job)

        apply_output_policy(jobs)
        return jobs
    finally:
        if progress is not None:
            progress.finish()


def classify_job(job: ExtractJob, runner: CommandRunner) -> None:
    if job.image_kind == "iso":
        classify_iso_job(job, runner, scan_dvd_titles=False)
    elif job.image_kind == "bin":
        classify_bin_job(job, runner)
    else:
        job.blockers.append(f"unsupported image extension: {job.image.suffix}")
        job.action = "blocked: unsupported image extension"


def classify_forced_job(
    job: ExtractJob,
    runner: CommandRunner,
    media_type: str,
    *,
    scan_dvd_titles: bool = False,
) -> None:
    if job.image_kind == "iso":
        classify_forced_iso_job(job, runner, media_type, scan_dvd_titles=scan_dvd_titles)
    elif job.image_kind == "bin":
        classify_forced_bin_job(job, runner, media_type)
    else:
        job.blockers.append(f"unsupported image extension: {job.image.suffix}")
        job.action = "blocked: unsupported image extension"


def classify_forced_iso_job(
    job: ExtractJob,
    runner: CommandRunner,
    media_type: str,
    *,
    scan_dvd_titles: bool = False,
) -> None:
    if media_type not in {"cd-data", "dvd-data", "dvd-video"}:
        job.blockers.append(f"forced media type {media_type} is not valid for ISO images")
        job.action = f"blocked: forced media type {media_type} is not valid for ISO images"
        return

    job.media_type = media_type
    if media_type == "dvd-video":
        classify_dvd_video_job(job, runner, scan_dvd_titles=scan_dvd_titles)
        return

    if runner.which("hdiutil") is None:
        job.blockers.append("missing tool: hdiutil")
        job.action = "blocked: missing tool hdiutil"
    else:
        job.action = "extract ISO"


def classify_forced_bin_job(job: ExtractJob, runner: CommandRunner, media_type: str) -> None:
    if media_type not in {"cd-data", "cd-vcd", "cd-audio"}:
        job.blockers.append(f"forced media type {media_type} is not valid for BIN images")
        job.action = f"blocked: forced media type {media_type} is not valid for BIN images"
        return

    job.cue = same_stem_path(job.image, ".cue")
    job.toc = same_stem_path(job.image, ".toc")
    job.media_type = media_type
    if media_type == "cd-audio":
        job.skip_reason = "CD audio extraction is out of scope"
        job.action = "skip: CD audio extraction is out of scope"
    elif media_type == "cd-vcd":
        if runner.which("vcdxrip") is None:
            job.blockers.append("missing tool: vcdxrip")
            job.action = "blocked: missing tool vcdxrip"
        else:
            job.action = "extract with vcdxrip"
    else:
        if job.cue is None:
            job.blockers.append("missing .cue for BIN/CUE data extraction")
            job.action = "blocked: missing .cue for BIN/CUE data extraction"
        elif runner.which("bchunk") is None:
            job.blockers.append("missing tool: bchunk")
            job.action = "blocked: missing tool bchunk"
        elif runner.which("hdiutil") is None:
            job.blockers.append("missing tool: hdiutil")
            job.action = "blocked: missing tool hdiutil"
        else:
            job.action = "extract BIN/CUE via bchunk"


def classify_iso_job(job: ExtractJob, runner: CommandRunner, *, scan_dvd_titles: bool = False) -> None:
    if runner.which("hdiutil") is None:
        job.blockers.append("missing tool: hdiutil")
        job.action = "blocked: missing tool hdiutil"
        return

    try:
        with iso_tool.attached_image(job.image) as volumes:
            if any(iso_tool.mounted_path_is_video_dvd(volume.mount_point) for volume in volumes):
                job.media_type = "dvd-video"
            else:
                job.media_type = "cd-data" if job.image.stat().st_size <= CD_SIZE_LIMIT else "dvd-data"
    except (OSError, iso_tool.ToolError) as exc:
        job.blockers.append(f"image probe failed: {exc}")
        job.action = "blocked: image probe failed"
        return

    if job.media_type == "dvd-video":
        classify_dvd_video_job(job, runner, scan_dvd_titles=scan_dvd_titles)
    else:
        job.action = "extract ISO"


def classify_dvd_video_job(job: ExtractJob, runner: CommandRunner, *, scan_dvd_titles: bool = False) -> None:
    if runner.which("makemkvcon") is None:
        job.blockers.append("missing tool: makemkvcon")
        job.action = "blocked: missing tool makemkvcon"
        return

    if not scan_dvd_titles:
        job.action = "extract MKV titles"
        return

    try:
        job.dvd_titles = scan_makemkv_titles(job, runner)
    except ToolError as exc:
        job.blockers.append(str(exc))
        job.action = f"blocked: {exc}"
        return

    job.action = f"extract {len(job.dvd_titles)} MKV title(s)"


def scan_makemkv_titles(job: ExtractJob, runner: CommandRunner) -> list[DVDTitle]:
    result = runner.run(["makemkvcon", "-r", "info", f"iso:{job.image.resolve()}"], check=False)
    if result.returncode != 0:
        raise ToolError("MakeMKV scan failed")

    try:
        titles = parse_makemkv_titles(command_output_text(result))
    except ValueError as exc:
        raise ToolError(f"MakeMKV scan parse failed: {exc}") from exc

    if not titles:
        raise ToolError("MakeMKV found no titles")
    return titles


def parse_makemkv_titles(output: str) -> list[DVDTitle]:
    title_values: dict[int, dict[str, str]] = {}
    title_streams: dict[int, dict[int, dict[int, str]]] = {}

    def values_for(title_id: int) -> dict[str, str]:
        return title_values.setdefault(title_id, {})

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if ":" not in line:
            continue
        record, payload = line.split(":", 1)
        try:
            fields = next(csv.reader([payload]))
        except csv.Error as exc:
            raise ValueError(f"invalid CSV in {record}") from exc

        if record == "TINFO" and len(fields) >= 4:
            title_id = parse_int(fields[0])
            code = parse_int(fields[1])
            if title_id is None or code is None:
                continue
            value = fields[-1]
            title = values_for(title_id)
            if code == 9:
                title["duration"] = value
            elif code == 10:
                title["size"] = value
            elif code == 11:
                title["byte_size"] = value
            elif code == 27:
                title["output_filename"] = value
        elif record == "SINFO" and len(fields) >= 5:
            title_id = parse_int(fields[0])
            stream_id = parse_int(fields[1])
            code = parse_int(fields[2])
            if title_id is None or stream_id is None or code is None:
                continue
            title_streams.setdefault(title_id, {}).setdefault(stream_id, {})[code] = fields[-1]

    titles: list[DVDTitle] = []
    for title_id in sorted(title_values):
        values = title_values[title_id]
        titles.append(
            DVDTitle(
                title_id=title_id,
                duration=values.get("duration", ""),
                size=values.get("size", ""),
                byte_size=parse_int(values.get("byte_size", "")),
                output_filename=values.get("output_filename", ""),
                track_summary=format_makemkv_track_summary(title_streams.get(title_id, {})),
            )
        )
    return titles


def parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def format_makemkv_track_summary(streams: dict[int, dict[int, str]]) -> str:
    grouped: dict[str, list[str]] = {"Video": [], "Audio": [], "Subtitles": []}
    for stream in streams.values():
        kind = stream.get(1, "")
        label = {
            "video": "Video",
            "audio": "Audio",
            "subtitles": "Subtitles",
        }.get(kind.casefold())
        if label is None:
            continue
        detail = stream_detail(stream)
        if detail and detail not in grouped[label]:
            grouped[label].append(detail)

    parts = []
    for label in ("Video", "Audio", "Subtitles"):
        details = grouped[label]
        if details:
            parts.append(f"{label}: {', '.join(details[:3])}")
    return "; ".join(parts)


def stream_detail(stream: dict[int, str]) -> str:
    details: list[str] = []
    for code in (3, 5, 6, 7):
        value = stream.get(code, "").strip()
        if not value or value in details:
            continue
        details.append(value)
    return " ".join(details)


def classify_bin_job(job: ExtractJob, runner: CommandRunner) -> None:
    job.cue = same_stem_path(job.image, ".cue")
    job.toc = same_stem_path(job.image, ".toc")

    if job.cue is None and job.toc is None:
        job.blockers.append("missing companion .cue or .toc")
        job.action = "blocked: missing companion .cue or .toc"
        return
    if runner.which("vcd-info") is None:
        job.blockers.append("missing tool: vcd-info")
        job.action = "blocked: missing tool vcd-info"
        return

    source_args = vcd_info_source_args(job)
    result = runner.run(
        ["vcd-info", *source_args, "--show-format", "--no-banner", "--no-header", "--no-delimiter"],
        check=False,
    )
    output = command_output_text(result)
    media_type = classify_bin_from_metadata(job, output)
    if media_type is None:
        if result.returncode == 0:
            media_type = "cd-data"
        else:
            job.blockers.append("could not classify BIN image")
            job.action = "blocked: could not classify BIN image"
            return

    job.media_type = media_type
    if media_type == "cd-audio":
        job.skip_reason = "CD audio extraction is out of scope"
        job.action = "skip: CD audio extraction is out of scope"
    elif media_type == "cd-vcd":
        if runner.which("vcdxrip") is None:
            job.blockers.append("missing tool: vcdxrip")
            job.action = "blocked: missing tool vcdxrip"
        else:
            job.action = "extract with vcdxrip"
    else:
        if job.cue is None:
            job.blockers.append("missing .cue for BIN/CUE data extraction")
            job.action = "blocked: missing .cue for BIN/CUE data extraction"
        elif runner.which("bchunk") is None:
            job.blockers.append("missing tool: bchunk")
            job.action = "blocked: missing tool bchunk"
        elif runner.which("hdiutil") is None:
            job.blockers.append("missing tool: hdiutil")
            job.action = "blocked: missing tool hdiutil"
        else:
            job.action = "extract BIN/CUE via bchunk"


def same_stem_path(path: Path, suffix: str) -> Path | None:
    candidate = path.with_suffix(suffix)
    return candidate if candidate.exists() else None


def vcd_info_source_args(job: ExtractJob) -> list[str]:
    if job.cue is not None:
        return ["--cue-file", str(job.cue.resolve())]
    return ["--bin-file", str(job.image.resolve())]


def classify_bin_from_metadata(job: ExtractJob, vcd_info_output: str) -> str | None:
    text = vcd_info_output.casefold()
    if any(token in text for token in ("svcd", "super video cd", "video cd", "vcd")):
        return "cd-vcd"

    tracks = track_types_from_companion(job)
    if tracks:
        has_audio = any(track == "audio" for track in tracks)
        has_data = any(track == "data" for track in tracks)
        if has_audio and not has_data:
            return "cd-audio"
        if has_data:
            return "cd-data"

    output_tracks = track_types_from_text(vcd_info_output)
    if output_tracks:
        has_audio = any(track == "audio" for track in output_tracks)
        has_data = any(track == "data" for track in output_tracks)
        if has_audio and not has_data:
            return "cd-audio"
        if has_data:
            return "cd-data"

    return None


def track_types_from_companion(job: ExtractJob) -> list[str]:
    if job.cue is not None:
        return track_types_from_text(read_text_lossy(job.cue))
    if job.toc is not None:
        return track_types_from_text(read_text_lossy(job.toc))
    return []


def track_types_from_text(text: str) -> list[str]:
    tracks: list[str] = []
    for line in text.splitlines():
        lowered = line.casefold()
        if "audio" in lowered or "cd-da" in lowered or "cdda" in lowered:
            tracks.append("audio")
        elif any(token in lowered for token in ("mode1", "mode2", "cd_rom", "cd-rom", "data", "xa")):
            tracks.append("data")
    return tracks


def read_text_lossy(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def same_stem_bins_with_data_iso(jobs: list[ExtractJob]) -> list[ExtractJob]:
    data_isos = {
        (job.image.parent, job.image.stem.casefold())
        for job in jobs
        if job.image_kind == "iso" and not job.blockers and job.media_type in {"cd-data", "dvd-data"}
    }
    return [
        job
        for job in jobs
        if job.image_kind == "bin" and (job.image.parent, job.image.stem.casefold()) in data_isos
    ]


def apply_output_policy(jobs: list[ExtractJob]) -> None:
    for job in jobs:
        if job.output_dir is None:
            continue
        if job.blockers or job.skip_reason is not None:
            if job.output_status == "not checked":
                job.output_status = describe_output_dir(job.output_dir)
            continue

        if not apply_existing_output_policy(job):
            job.output_status = describe_output_dir(job.output_dir)

    runnable_by_output: dict[Path, list[ExtractJob]] = {}
    for job in jobs:
        if job.runnable and job.output_dir is not None:
            runnable_by_output.setdefault(job.output_dir, []).append(job)

    for output_dir, output_jobs in runnable_by_output.items():
        if len(output_jobs) <= 1:
            continue
        for job in output_jobs:
            job.blockers.append("multiple runnable images target this output directory")
            job.action = "blocked: multiple runnable images target this output directory"
            job.output_status = describe_output_dir(output_dir)

    for job in jobs:
        if job.runnable and job.image_kind == "bin" and job.media_type == "cd-data":
            temp_dir = bchunk_temp_dir(job)
            if temp_dir.exists():
                job.blockers.append(f"temp path exists: {temp_dir.name}")
                job.action = f"blocked: temp path exists: {temp_dir.name}"


def apply_existing_output_policy(job: ExtractJob) -> bool:
    if job.output_dir is None:
        return False

    if apply_output_path_policy(job, job.output_dir, legacy=False):
        return True

    legacy_dir = legacy_output_dir(job.image)
    if legacy_dir != job.output_dir and apply_output_path_policy(job, legacy_dir, legacy=True):
        return True

    return False


def apply_output_path_policy(job: ExtractJob, output_dir: Path, *, legacy: bool) -> bool:
    if not output_dir.exists():
        return False

    job.output_status = describe_output_dir(output_dir)
    label = output_label(output_dir)
    prefix = "legacy " if legacy else ""
    if output_dir.is_dir() and output_dir_non_empty(output_dir):
        job.skip_reason = f"{prefix}{label} already exists; assuming already extracted"
        job.action = f"skip: {prefix}{label} already exists; assuming already extracted"
        return True
    if not output_dir.is_dir():
        job.blockers.append(f"{prefix}{label.rstrip('/')} exists but is not a directory")
        job.action = f"blocked: {prefix}{label.rstrip('/')} exists but is not a directory"
        return True
    return False


def describe_output_dir(output_dir: Path) -> str:
    label = output_label(output_dir)
    if not output_dir.exists():
        return f"{label} missing"
    if not output_dir.is_dir():
        return f"{label} exists but is not a directory"
    return f"{label} non-empty" if output_dir_non_empty(output_dir) else f"{label} empty"


def output_label(output_dir: Path) -> str:
    return f"{output_dir.name}/"


def output_dir_non_empty(output_dir: Path) -> bool:
    if not output_dir.is_dir():
        return output_dir.exists()
    try:
        next(output_dir.iterdir())
    except StopIteration:
        return False
    except OSError:
        return True
    return True


def bchunk_temp_dir(job: ExtractJob) -> Path:
    return job.image.parent / f".{job.image.stem}.extractdisc-bchunk"


class PreflightProgress:
    def __init__(self, root: Path, total: int, stream=None) -> None:
        self.root = root
        self.total = total
        self.current = 0
        self.stream = stream or sys.stderr
        self.interactive = bool(getattr(self.stream, "isatty", lambda: False)())
        self.last_width = 0

    def update(self, image: Path) -> None:
        self.current += 1
        label = f"preflight {display_path(image, self.root)}"
        line = format_progress(self.current, self.total, label)
        if self.interactive:
            padding = " " * max(0, self.last_width - display_width(line))
            self.stream.write(f"\r{line}{padding}")
            self.stream.flush()
            self.last_width = display_width(line)
        else:
            print(line, file=self.stream)

    def finish(self) -> None:
        if self.interactive and self.current:
            self.stream.write("\n")
            self.stream.flush()


def report_preflight_progress(progress: PreflightProgress | None, job: ExtractJob) -> None:
    if progress is not None:
        progress.update(job.image)


def print_plan(jobs: Sequence[ExtractJob], root: Path) -> None:
    headers = ("Image", "Media", "Content", "Output", "Action")
    rows = [headers]
    for job in jobs:
        media, content = display_media_columns(job.media_type)
        rows.append(
            (
                display_path(job.image, root),
                media,
                content,
                job.output_status,
                job.action,
            )
        )

    widths = [max(display_width(row[index]) for row in rows) for index in range(len(headers))]
    for index, row in enumerate(rows):
        print("  ".join(pad_display(value, widths[column]) for column, value in enumerate(row)))
        if index == 0:
            print("  ".join("-" * width for width in widths))

    print_dvd_title_details(jobs, root)

    blocked = [job for job in jobs if job.blocked]
    if blocked:
        print()
        print("Blocked jobs:")
        for job in blocked:
            reasons = "; ".join(job.blockers)
            print(f"- {display_path(job.image, root)}: {reasons}")


def print_dvd_title_details(jobs: Sequence[ExtractJob], root: Path) -> None:
    dvd_jobs = [job for job in jobs if job.dvd_titles]
    if not dvd_jobs:
        return

    print()
    print("DVD titles:")
    for job in dvd_jobs:
        print(f"- {display_path(job.image, root)}:")
        for title in job.dvd_titles:
            details = [f"title {title.title_id}", title.filename]
            if title.duration:
                details.append(f"duration {title.duration}")
            if title.size:
                details.append(f"size {title.size}")
            if title.track_summary:
                details.append(title.track_summary)
            print(f"  - {', '.join(details)}")


def display_media_columns(media_type: str) -> tuple[str, str]:
    if media_type == "cd-audio":
        return "CD", "AUDIO"
    if "-" not in media_type:
        return media_type, ""

    media, content = media_type.split("-", 1)
    media_label = media.upper()
    content_label = {
        "data": "data",
        "video": "video",
        "audio": "audio",
        "vcd": "VCD/SVCD",
    }.get(content, content)
    return media_label, content_label


def display_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def extract_jobs(jobs: Sequence[ExtractJob], runner: CommandRunner, verbose: bool = False) -> None:
    for job in jobs:
        if not job.runnable:
            continue
        assert job.output_dir is not None
        try:
            if job.media_type == "dvd-video":
                print(f"Extracting {job.image} ...")
                file_count = extract_dvd_titles(job, runner)
            elif job.image_kind == "iso":
                job.output_dir.mkdir(parents=True, exist_ok=True)
                print(f"Extracting {job.image} ... ", end="", flush=True)
                file_count = iso_tool.extract_image_files(job.image, job.output_dir, overwrite=False)
            elif job.media_type == "cd-data":
                job.output_dir.mkdir(parents=True, exist_ok=True)
                print(f"Extracting {job.image} ... ", end="", flush=True)
                file_count = extract_bin_data(job, runner, verbose=verbose)
            elif job.media_type == "cd-vcd":
                job.output_dir.mkdir(parents=True, exist_ok=True)
                print(f"Extracting {job.image} ... ", end="", flush=True)
                file_count = extract_vcd(job, runner)
            else:
                job.output_dir.mkdir(parents=True, exist_ok=True)
                print(f"Extracting {job.image} ... ", end="", flush=True)
                file_count = 0
            print(format_file_count(file_count))
        except (OSError, ToolError, iso_tool.ToolError) as exc:
            print("failed")
            print(f"ERROR: extraction failed for {job.image}: {exc}", file=sys.stderr)
            cleanup_partial_output(job.output_dir)


def cleanup_partial_output(output_dir: Path) -> None:
    if not output_dir.exists():
        return
    if not output_dir.is_dir() or output_dir.is_symlink():
        print(f"WARNING: leaving partial output path that is not a directory: {output_dir}", file=sys.stderr)
        return
    try:
        shutil.rmtree(output_dir)
    except OSError as exc:
        print(f"WARNING: failed to remove partial output directory {output_dir}: {exc}", file=sys.stderr)
    else:
        print(f"Removed partial output directory: {output_dir}", file=sys.stderr)


def format_file_count(file_count: int) -> str:
    noun = "file" if file_count == 1 else "files"
    return f"{file_count} {noun}"


def extract_bin_data(job: ExtractJob, runner: CommandRunner, verbose: bool) -> int:
    assert job.cue is not None
    assert job.output_dir is not None
    temp_dir = bchunk_temp_dir(job)
    if temp_dir.exists():
        raise ToolError(f"Temp path exists: {temp_dir}")
    temp_dir.mkdir()
    try:
        prefix = job.image.stem
        result = runner.run(
            ["bchunk", str(job.image.resolve()), str(job.cue.resolve()), prefix],
            cwd=temp_dir,
            check=True,
        )
        if verbose and command_output_text(result).strip():
            print(command_output_text(result).strip())
        generated = sorted(temp_dir.glob(f"{prefix}*.iso"))
        if len(generated) != 1:
            names = ", ".join(path.name for path in generated) or "none"
            raise ToolError(f"bchunk generated {len(generated)} ISO files ({names}); expected exactly one")
        return iso_tool.extract_image_files(generated[0], job.output_dir, overwrite=False)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def extract_vcd(job: ExtractJob, runner: CommandRunner) -> int:
    assert job.output_dir is not None
    job.output_dir.mkdir(parents=True, exist_ok=True)
    before = regular_files_under(job.output_dir)
    if job.cue is not None:
        source_args = ["--cue-file", str(job.cue.resolve())]
    else:
        source_args = ["--bin-file", str(job.image.resolve())]
    runner.run(
        ["vcdxrip", *source_args, "--output-file", f"{job.image.stem}.vcd.xml"],
        cwd=job.output_dir,
        check=True,
    )
    return len(regular_files_under(job.output_dir) - before)


def extract_dvd_titles(job: ExtractJob, runner: CommandRunner) -> int:
    assert job.output_dir is not None
    if not job.dvd_titles:
        print("  scanning DVD titles ... ", end="", flush=True)
        job.dvd_titles = scan_makemkv_titles(job, runner)
        print(format_file_count(len(job.dvd_titles)))

    job.output_dir.mkdir(parents=True, exist_ok=True)
    image_arg = f"iso:{job.image.resolve()}"
    output_arg = str(job.output_dir.resolve())
    print(f"  output -> {output_arg}")
    for title in job.dvd_titles:
        print(f"  title {title.title_id} -> {title.filename} ... ", end="", flush=True)
        result = runner.run(
            ["makemkvcon", "-r", "mkv", image_arg, str(title.title_id), output_arg],
            check=True,
        )
        verify_makemkv_title_saved(result, job.output_dir / title.filename)
        print("done")
    return len(job.dvd_titles)


def verify_makemkv_title_saved(result: CommandResult, output_file: Path) -> None:
    failed_messages: list[str] = []
    for raw_line in command_output_text(result).splitlines():
        line = raw_line.strip()
        if not line.startswith("MSG:"):
            continue
        try:
            fields = next(csv.reader([line.split(":", 1)[1]]))
        except csv.Error:
            continue
        if len(fields) < 4:
            continue
        code = fields[0]
        message = fields[3]
        if code in {"1002", "5003"}:
            failed_messages.append(message)
        elif code in {"5004", "5037"} and not re.search(r"\b0 failed\b", message):
            failed_messages.append(message)

    if failed_messages:
        raise ToolError("; ".join(failed_messages))
    if not output_file.exists():
        raise ToolError(f"MakeMKV reported success but did not create {output_file.name}")
    if output_file.stat().st_size == 0:
        raise ToolError(f"MakeMKV created empty output file {output_file.name}")


def regular_files_under(root: Path) -> set[Path]:
    files: set[Path] = set()
    for current_root, _, filenames in os.walk(root):
        current_path = Path(current_root)
        for filename in filenames:
            path = current_path / filename
            if path.is_file():
                files.add(path.relative_to(root))
    return files


def confirm(input_func: Callable[[str], str]) -> bool:
    answer = input_func("Proceed with extraction? [y/N] ")
    return answer.strip().casefold() in {"y", "yes"}


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: CommandRunner | None = None,
    input_func: Callable[[str], str] = input,
) -> int:
    args = parse_args(argv)
    runner = runner or CommandRunner()

    try:
        root = args.root.resolve()
        if not root.is_dir():
            raise ToolError(f"Root is not a directory: {root}")

        jobs = build_plan(
            root,
            runner,
            show_progress=True,
            scan_dvd_titles=args.scan_dvd_titles,
            force_media_type=args.force_media_type,
        )
        print_plan(jobs, root)

        if args.plan_only:
            return 0
        if not any(job.runnable for job in jobs):
            print()
            print("No runnable extraction jobs.")
            return 0
        if not args.yes and not confirm(input_func):
            print("Extraction cancelled.")
            return 1

        extract_jobs(jobs, runner, verbose=args.verbose)
        return 0
    except (ToolError, iso_tool.ToolError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
