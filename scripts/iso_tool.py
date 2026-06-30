#!/usr/bin/env python3
"""Work with optical disc image files on macOS."""

from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AttachedVolume:
    dev_entry: str
    mount_point: Path
    volume_kind: str | None = None


class ToolError(Exception):
    """An expected operational error that should be shown without a traceback."""


def run_bytes(command: Sequence[str]) -> bytes:
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise ToolError(f"{' '.join(command)} failed: {stderr or f'exit status {result.returncode}'}")
    return result.stdout


def parse_attached_volumes(plist_bytes: bytes) -> list[AttachedVolume]:
    plist = parse_plist(plist_bytes, "hdiutil")

    volumes: list[AttachedVolume] = []
    for entity in plist.get("system-entities", []):
        dev_entry = entity.get("dev-entry")
        mount_point = entity.get("mount-point")
        if not dev_entry or not mount_point:
            continue
        volumes.append(
            AttachedVolume(
                dev_entry=dev_entry,
                mount_point=Path(mount_point),
                volume_kind=entity.get("volume-kind"),
            )
        )

    return volumes


def parse_plist(plist_bytes: bytes, tool_name: str) -> dict:
    try:
        plist = plistlib.loads(plist_bytes)
    except plistlib.InvalidFileException as exc:
        raise ToolError(f"Could not parse {tool_name} plist output: {exc}") from exc

    if not isinstance(plist, dict):
        raise ToolError(f"Could not parse {tool_name} plist output: top-level value is not a dictionary")

    return plist


def parse_attached_dev_entries(plist_bytes: bytes) -> list[str]:
    plist = parse_plist(plist_bytes, "hdiutil")
    dev_entries: list[str] = []
    for entity in plist.get("system-entities", []):
        dev_entry = entity.get("dev-entry")
        if dev_entry:
            dev_entries.append(dev_entry)

    return unique_strings(dev_entries)


@contextmanager
def attached_image(image: Path) -> Iterator[list[AttachedVolume]]:
    plist_bytes = run_bytes(["hdiutil", "attach", "-readonly", "-noautoopen", "-plist", str(image)])
    dev_entries = parse_attached_dev_entries(plist_bytes)
    volumes = parse_attached_volumes(plist_bytes)

    try:
        if not volumes:
            raise ToolError(f"No mountable volumes found in {image}")
        yield volumes
    finally:
        for dev_entry in dev_entries:
            subprocess.run(["hdiutil", "detach", dev_entry], stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def iter_paths(root: Path, include_dirs: bool = False) -> Iterator[Path]:
    for current_root, dirnames, filenames in os.walk(root):
        dirnames.sort()
        filenames.sort()
        current_path = Path(current_root)

        if include_dirs:
            for dirname in dirnames:
                yield current_path / dirname

        for filename in filenames:
            yield current_path / filename


def relative_display_path(path: Path, root: Path) -> str:
    return str(path.relative_to(root))


def list_image_files(image: Path, include_dirs: bool = False, absolute: bool = False, null: bool = False) -> None:
    separator = "\0" if null else "\n"
    with attached_image(image) as volumes:
        for volume in volumes:
            for path in iter_paths(volume.mount_point, include_dirs=include_dirs):
                display = str(path) if absolute else relative_display_path(path, volume.mount_point)
                print(display, end=separator)


def ensure_extract_target_available(source: Path, target: Path, overwrite: bool) -> None:
    if target.exists() and not overwrite:
        raise ToolError(f"Refusing to overwrite existing path: {target}")
    if source.is_dir() and target.exists() and not target.is_dir():
        raise ToolError(f"Cannot copy directory over non-directory: {target}")


def copy_contents(source_dir: Path, output_dir: Path, overwrite: bool) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    for current_root, dirnames, filenames in os.walk(source_dir):
        dirnames.sort()
        filenames.sort()
        current_path = Path(current_root)
        relative_root = current_path.relative_to(source_dir)
        target_root = output_dir / relative_root
        target_root.mkdir(parents=True, exist_ok=True)

        for dirname in dirnames:
            source = current_path / dirname
            target = target_root / dirname
            ensure_extract_target_available(source, target, overwrite)
            target.mkdir(exist_ok=True)

        for filename in filenames:
            source = current_path / filename
            target = target_root / filename
            ensure_extract_target_available(source, target, overwrite)
            shutil.copy2(source, target, follow_symlinks=False)


def volume_output_dir(base_output_dir: Path, volumes: list[AttachedVolume], volume: AttachedVolume) -> Path:
    if len(volumes) == 1:
        return base_output_dir

    volume_name = volume.mount_point.name or Path(volume.dev_entry).name
    return base_output_dir / volume_name


def extract_image_files(image: Path, output_dir: Path, overwrite: bool = False) -> None:
    with attached_image(image) as volumes:
        for volume in volumes:
            copy_contents(volume.mount_point, volume_output_dir(output_dir, volumes, volume), overwrite=overwrite)


def child_by_name_casefold(directory: Path, name: str) -> Path | None:
    wanted = name.casefold()
    try:
        for child in directory.iterdir():
            if child.name.casefold() == wanted:
                return child
    except OSError:
        return None
    return None


def mounted_path_is_video_dvd(mount_point: Path) -> bool:
    video_ts = child_by_name_casefold(mount_point, "VIDEO_TS")
    if video_ts is None or not video_ts.is_dir():
        return False

    video_ts_ifo = child_by_name_casefold(video_ts, "VIDEO_TS.IFO")
    return video_ts_ifo is not None and video_ts_ifo.is_file()


def image_is_video_dvd(image: Path) -> bool:
    with attached_image(image) as volumes:
        return any(mounted_path_is_video_dvd(volume.mount_point) for volume in volumes)


def parse_diskutil_mount_points(plist_bytes: bytes) -> list[Path]:
    plist = parse_plist(plist_bytes, "diskutil")

    mount_points: list[Path] = []

    mount_point = plist.get("MountPoint")
    if mount_point:
        mount_points.append(Path(mount_point))

    for partition in plist.get("AllDisksAndPartitions", [{}])[0].get("Partitions", []):
        partition_mount_point = partition.get("MountPoint")
        if partition_mount_point:
            mount_points.append(Path(partition_mount_point))

    return unique_paths(mount_points)


def parse_diskutil_partition_devices(plist_bytes: bytes) -> list[str]:
    plist = parse_plist(plist_bytes, "diskutil")
    devices: list[str] = []

    for disk in plist.get("AllDisksAndPartitions", []):
        for partition in disk.get("Partitions", []):
            identifier = partition.get("DeviceIdentifier")
            if identifier:
                devices.append(f"/dev/{identifier}")

    return unique_strings(devices)


def unique_paths(paths: Sequence[Path]) -> list[Path]:
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        unique.append(path)
    return unique


def unique_strings(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def device_mount_points(device: str) -> list[Path]:
    mount_points: list[Path] = []
    partition_devices: list[str] = []

    try:
        mount_points.extend(parse_diskutil_mount_points(run_bytes(["diskutil", "info", "-plist", device])))
    except ToolError:
        pass

    try:
        list_plist = run_bytes(["diskutil", "list", "-plist", device])
        mount_points.extend(parse_diskutil_mount_points(list_plist))
        partition_devices.extend(parse_diskutil_partition_devices(list_plist))
    except ToolError:
        pass

    for partition_device in partition_devices:
        try:
            mount_points.extend(parse_diskutil_mount_points(run_bytes(["diskutil", "info", "-plist", partition_device])))
        except ToolError:
            pass

    if not mount_points:
        raise ToolError(f"No mounted filesystems found for {device}")

    return unique_paths(mount_points)


def device_is_video_dvd(device: str) -> bool:
    return any(mounted_path_is_video_dvd(mount_point) for mount_point in device_mount_points(device))


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="List, extract, and inspect optical disc images.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List files in an image")
    list_parser.add_argument("image", type=Path, help="ISO or raw optical disc image")
    list_parser.add_argument("--include-dirs", action="store_true", help="Include directories in the listing")
    list_parser.add_argument("--absolute", action="store_true", help="Print mounted absolute paths")
    list_parser.add_argument("--null", action="store_true", help="Separate output paths with NUL bytes")

    extract_parser = subparsers.add_parser("extract", help="Extract files from an image")
    extract_parser.add_argument("image", type=Path, help="ISO or raw optical disc image")
    extract_parser.add_argument("--output-dir", type=Path, required=True, help="Directory to copy files into")
    extract_parser.add_argument("--overwrite", action="store_true", help="Overwrite existing output files")

    dvd_parser = subparsers.add_parser("is-video-dvd", help="Check whether an image, device, or mount point is a Video DVD")
    dvd_source = dvd_parser.add_mutually_exclusive_group(required=True)
    dvd_source.add_argument("--image", type=Path, help="ISO or raw optical disc image")
    dvd_source.add_argument("--device", help="Disk device such as /dev/disk4 or /dev/disk4s0")
    dvd_source.add_argument("--mount-point", type=Path, help="Mounted volume path")

    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        if args.command == "list":
            list_image_files(args.image, include_dirs=args.include_dirs, absolute=args.absolute, null=args.null)
            return 0

        if args.command == "extract":
            extract_image_files(args.image, args.output_dir, overwrite=args.overwrite)
            return 0

        if args.command == "is-video-dvd":
            if args.image is not None:
                is_video_dvd = image_is_video_dvd(args.image)
            elif args.device is not None:
                is_video_dvd = device_is_video_dvd(args.device)
            else:
                is_video_dvd = mounted_path_is_video_dvd(args.mount_point)

            print("yes" if is_video_dvd else "no")
            return 0 if is_video_dvd else 1

    except ToolError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"ERROR: unknown command: {args.command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
