#!/usr/bin/env python3
"""Front door for optical disc archival on macOS."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ToolError(Exception):
    """An expected operational error that should be shown without a traceback."""


@dataclass(frozen=True)
class CommandResult:
    command: list[str]
    returncode: int
    stdout: bytes = b""
    stderr: bytes = b""


@dataclass(frozen=True)
class MountInfo:
    device: str
    mount_point: Path
    fstype: str


@dataclass(frozen=True)
class DiskInfo:
    device: str
    plist: dict[str, Any]

    @property
    def content(self) -> str:
        return str(self.plist.get("Content") or self.plist.get("FilesystemType") or "")

    @property
    def optical_type(self) -> str:
        return str(self.plist.get("OpticalMediaType") or self.plist.get("DeviceProtocol") or "")

    @property
    def size_bytes(self) -> int | None:
        for key in ("TotalSize", "MediaSize", "Size"):
            value = self.plist.get(key)
            if isinstance(value, int) and value > 0:
                return value
        return None


@dataclass(frozen=True)
class Probe:
    device: str
    disk_info: DiskInfo | None
    partition_infos: list[DiskInfo]
    mounts: list[MountInfo]
    vcd_markers: bool
    drutil_status: str
    trackinfo: Any | None

    @property
    def data_mounts(self) -> list[MountInfo]:
        return [mount for mount in self.mounts if mount.fstype.lower() in {"cd9660", "udf"}]

    @property
    def preferred_data_mount(self) -> MountInfo | None:
        return self.data_mounts[0] if self.data_mounts else None


@dataclass(frozen=True)
class Paths:
    base_out: Path
    name: str
    target_dir: Path
    iso: Path
    mapfile: Path
    bin: Path
    toc: Path
    cue: Path
    vcd_xml: Path
    probe_json: Path
    commands_log: Path
    files_txt: Path
    sha256: Path
    vcd_dir: Path


class CommandRunner:
    def __init__(self, log_path: Path | None = None) -> None:
        self.log_path = log_path

    def set_log_path(self, log_path: Path) -> None:
        self.log_path = log_path

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
        self._log(command_result, cwd)
        if check and command_result.returncode != 0:
            stderr = command_result.stderr.decode("utf-8", errors="replace").strip()
            raise ToolError(f"{format_command(command)} failed: {stderr or f'exit status {command_result.returncode}'}")
        return command_result

    def run_foreground(self, command: Sequence[str], cwd: Path | None = None, check: bool = True) -> CommandResult:
        command = list(command)
        log_file = None
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = self.log_path.open("ab")

        try:
            if log_file is not None:
                if cwd is not None:
                    log_file.write(f"cwd: {cwd}\n".encode())
                log_file.write(f">>> {format_command(command)}\n".encode())
                log_file.write(b"--- output ---\n")
                log_file.flush()

            process = subprocess.Popen(
                command,
                cwd=str(cwd) if cwd is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            assert process.stdout is not None

            while True:
                chunk = os.read(process.stdout.fileno(), 4096)
                if not chunk:
                    break
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
                if log_file is not None:
                    log_file.write(chunk)
                    log_file.flush()

            returncode = process.wait()
            if log_file is not None:
                if log_file.tell() > 0:
                    log_file.write(b"\n")
                log_file.write(f">>> exit status: {returncode}\n".encode())

            result = CommandResult(command=command, returncode=returncode)
            if check and returncode != 0:
                raise ToolError(f"{format_command(command)} failed: exit status {returncode}")
            return result
        finally:
            if log_file is not None:
                log_file.close()

    def _log(self, result: CommandResult, cwd: Path | None) -> None:
        if self.log_path is None:
            return

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as log:
            if cwd is not None:
                log.write(f"cwd: {cwd}\n")
            log.write(f">>> {format_command(result.command)}\n")
            log.write(f">>> exit status: {result.returncode}\n")
            if result.stdout:
                log.write("--- stdout ---\n")
                log.write(result.stdout.decode("utf-8", errors="replace"))
                if not result.stdout.endswith(b"\n"):
                    log.write("\n")
            if result.stderr:
                log.write("--- stderr ---\n")
                log.write(result.stderr.decode("utf-8", errors="replace"))
                if not result.stderr.endswith(b"\n"):
                    log.write("\n")


class StatusLogger:
    def __init__(self, log_path: Path | None = None) -> None:
        self.log_path = log_path

    def write(self, message: str = "") -> None:
        print(message)
        if self.log_path is None:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as log:
            log.write(f"{message}\n")


def format_command(command: Sequence[str]) -> str:
    return " ".join(sh_quote(part) for part in command)


def sh_quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:=,+@%-]+", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def parse_mounts(mount_output: str) -> list[MountInfo]:
    mounts: list[MountInfo] = []
    pattern = re.compile(r"^(\S+) on (.+?) \(([^,\)]+)")
    for line in mount_output.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        mounts.append(MountInfo(match.group(1), Path(match.group(2)), match.group(3)))
    return mounts


def parse_plist_bytes(raw: bytes, tool_name: str) -> Any:
    try:
        return plistlib.loads(raw)
    except plistlib.InvalidFileException as exc:
        raise ToolError(f"Could not parse {tool_name} plist output: {exc}") from exc


def disk_info_from_plist(device: str, raw: bytes) -> DiskInfo:
    parsed = parse_plist_bytes(raw, "diskutil")
    if not isinstance(parsed, dict):
        raise ToolError("Could not parse diskutil plist output: top-level value is not a dictionary")
    return DiskInfo(device=device, plist=parsed)


def partition_devices_from_diskutil_list(raw: bytes) -> list[str]:
    parsed = parse_plist_bytes(raw, "diskutil")
    if not isinstance(parsed, dict):
        return []

    devices: list[str] = []
    for disk in parsed.get("AllDisksAndPartitions", []):
        if not isinstance(disk, dict):
            continue
        for partition in disk.get("Partitions", []):
            if not isinstance(partition, dict):
                continue
            identifier = partition.get("DeviceIdentifier")
            if identifier:
                devices.append(f"/dev/{identifier}")
    return unique_strings(devices)


def probe_device(device: str, runner: CommandRunner) -> Probe:
    disk_info: DiskInfo | None = None
    partition_infos: list[DiskInfo] = []
    trackinfo: Any | None = None

    info_result = runner.run(["diskutil", "info", "-plist", device], check=False)
    if info_result.returncode == 0:
        disk_info = disk_info_from_plist(device, info_result.stdout)

    list_result = runner.run(["diskutil", "list", "-plist", whole_disk_device(device)], check=False)
    if list_result.returncode == 0:
        for partition_device in partition_devices_from_diskutil_list(list_result.stdout):
            partition_result = runner.run(["diskutil", "info", "-plist", partition_device], check=False)
            if partition_result.returncode == 0:
                partition_infos.append(disk_info_from_plist(partition_device, partition_result.stdout))

    mount_result = runner.run(["mount"], check=False)
    mounts = parse_mounts(mount_result.stdout.decode("utf-8", errors="replace")) if mount_result.returncode == 0 else []

    status_result = runner.run(["drutil", "status"], check=False)
    drutil_status = status_result.stdout.decode("utf-8", errors="replace") if status_result.returncode == 0 else ""

    trackinfo_result = runner.run(["drutil", "trackinfo", "-xml"], check=False)
    if trackinfo_result.returncode == 0 and trackinfo_result.stdout.strip():
        trackinfo = parse_plist_bytes(trackinfo_result.stdout, "drutil trackinfo")

    disk_prefix = whole_disk_name(device)
    filtered_mounts = [
        mount for mount in mounts if Path(mount.device).name == disk_prefix or Path(mount.device).name.startswith(f"{disk_prefix}s")
    ]

    return Probe(
        device=device,
        disk_info=disk_info,
        partition_infos=partition_infos,
        mounts=filtered_mounts,
        vcd_markers=any(mount_has_vcd_markers(mount.mount_point) for mount in filtered_mounts),
        drutil_status=drutil_status,
        trackinfo=trackinfo,
    )


def classify_probe(probe: Probe, vcd_detected: bool = False) -> str:
    if vcd_detected or probe_has_vcd_markers(probe):
        return "cd-vcd"

    diskutil_type_text = " ".join(
        value
        for value in [
            probe.disk_info.optical_type if probe.disk_info is not None else "",
            probe.disk_info.content if probe.disk_info is not None else "",
            " ".join(info.content for info in probe.partition_infos),
        ]
        if value
    ).lower()

    if "dvd" in diskutil_type_text:
        return "dvd"

    track_types = track_type_strings(probe.trackinfo)
    has_audio = any(track_is_audio(track_type) for track_type in track_types)
    has_track_data = any(track_is_data(track_type) for track_type in track_types)
    has_mount_data = bool(probe.data_mounts)

    if track_types and has_audio and not has_track_data and not has_mount_data:
        return "audio-only"
    if has_audio and (has_track_data or has_mount_data):
        return "cd-mixed"
    if has_track_data or has_mount_data or "cd" in diskutil_type_text:
        return "cd-data"

    return "unknown"


def describe_probe_detection(probe: Probe, classification: str, status: StatusLogger) -> None:
    status.write("=== Media autodetection ===")
    status.write(f"Device: {probe.device}")

    if probe.disk_info is None:
        status.write("diskutil info: unavailable")
    else:
        status.write(
            "diskutil info: "
            f"OpticalMediaType={probe.disk_info.optical_type or '(none)'} "
            f"Content={probe.disk_info.content or '(none)'}"
        )

    if probe.partition_infos:
        status.write("Partitions:")
        for info in probe.partition_infos:
            size = str(info.size_bytes) if info.size_bytes is not None else "(unknown size)"
            status.write(f"  {info.device}: Content={info.content or '(none)'} Size={size}")
    else:
        status.write("Partitions: none reported")

    if probe.mounts:
        status.write("Mounted filesystems for device:")
        for mount in probe.mounts:
            status.write(f"  {mount.device}: {mount.mount_point} ({mount.fstype})")
    else:
        status.write("Mounted filesystems for device: none")

    track_types = track_type_strings(probe.trackinfo)
    status.write(f"Track types: {', '.join(track_types) if track_types else '(not reported)'}")
    status.write(f"Mountable data slice: {describe_mount(probe.preferred_data_mount)}")
    status.write(f"VCD/SVCD filesystem markers: {'yes' if probe_has_vcd_markers(probe) else 'no'}")
    status.write(f"Initial classification: {classification}")


def describe_mount(mount: MountInfo | None) -> str:
    if mount is None:
        return "none"
    return f"{mount.device} mounted at {mount.mount_point} ({mount.fstype})"


def track_type_strings(value: Any) -> list[str]:
    strings: list[str] = []

    def visit(node: Any, key_hint: str = "") -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                visit(child, str(key))
        elif isinstance(node, list):
            for child in node:
                visit(child, key_hint)
        elif isinstance(node, str):
            hint = key_hint.casefold()
            text = node.strip()
            if not text:
                return
            if any(token in hint for token in ("type", "track", "session", "data", "audio")):
                strings.append(text)

    visit(value)
    return strings


def track_is_audio(track_type: str) -> bool:
    lowered = track_type.casefold()
    return any(token in lowered for token in ("audio", "cd-da", "cdda", "red book"))


def track_is_data(track_type: str) -> bool:
    lowered = track_type.casefold()
    return any(token in lowered for token in ("data", "mode", "iso", "cd-rom", "xa"))


def probe_has_vcd_markers(probe: Probe) -> bool:
    return probe.vcd_markers


def mount_has_vcd_markers(mount_point: Path) -> bool:
    if child_by_name_casefold(mount_point, "VCD") is not None:
        vcd = child_by_name_casefold(mount_point, "VCD")
        if vcd is not None and child_by_name_casefold(vcd, "INFO.VCD") is not None:
            return True
    for name in ("SVCD", "MPEGAV", "SEGMENT", "EXT"):
        if child_by_name_casefold(mount_point, name) is not None:
            return True
    return False


def child_by_name_casefold(directory: Path, name: str) -> Path | None:
    wanted = name.casefold()
    try:
        for child in directory.iterdir():
            if child.name.casefold() == wanted:
                return child
    except OSError:
        return None
    return None


def parse_cdrdao_scanbus(output: str) -> list[str]:
    devices: list[str] = []
    address_pattern = re.compile(r"^\s*((?:[A-Za-z][A-Za-z0-9_.+-]*:)?\d+,\d+,\d+)\b\s*(.*)$")
    ioservice_pattern = re.compile(r"^\s*(IOService:.+?)\s+:\s+(.+)$")
    for line in output.splitlines():
        match = address_pattern.match(line)
        if match:
            description = match.group(2).casefold()
            if not description or any(token in description for token in ("cd", "dvd", "bd-", "blu-ray", "optical")):
                devices.append(match.group(1))
            continue

        match = ioservice_pattern.match(line)
        if not match:
            continue
        description = match.group(2).casefold()
        if any(token in description for token in ("cd", "dvd", "bd-", "blu-ray", "optical")):
            devices.append(match.group(1))
    return unique_strings(devices)


def command_output_text(result: CommandResult) -> str:
    return (result.stdout + result.stderr).decode("utf-8", errors="replace")


def cdrdao_device_in_use(result: CommandResult) -> bool:
    return "device already in use" in command_output_text(result).casefold()


def unmount_for_cdrdao(device: str, runner: CommandRunner, status: StatusLogger | None = None) -> None:
    whole_device = whole_disk_device(device)
    if status is not None:
        status.write(f"cdrdao access: unmounting {whole_device} with diskutil unmountDisk")
    result = runner.run(["diskutil", "unmountDisk", whole_device], check=False)
    output = command_output_text(result).strip()
    if result.returncode != 0 and status is not None:
        status.write(f"cdrdao access: diskutil unmountDisk exited {result.returncode}: {output or '(no output)'}")


def resolve_cdrdao_device(
    runner: CommandRunner,
    source_device: str,
    explicit_device: str | None = None,
    status: StatusLogger | None = None,
) -> str:
    if explicit_device:
        if status is not None:
            status.write(f"cdrdao device: using explicit --cdrdao-device {explicit_device}")
        return explicit_device

    unmount_for_cdrdao(source_device, runner, status)
    result = runner.run(["cdrdao", "scanbus"], check=True)
    if cdrdao_device_in_use(result):
        if status is not None:
            status.write("cdrdao scanbus: device still reported in use; retrying after unmountDisk")
        unmount_for_cdrdao(source_device, runner, status)
        result = runner.run(["cdrdao", "scanbus"], check=True)

    scanbus_output = command_output_text(result)
    devices = parse_cdrdao_scanbus(scanbus_output)
    if status is not None:
        status.write(f"cdrdao scanbus: found {len(devices)} optical drive(s)")
    if not devices:
        raw_scanbus = scanbus_output.strip()
        detail = f" Raw scanbus output was:\n{raw_scanbus}" if raw_scanbus else ""
        raise ToolError(
            "cdrdao scanbus did not find an optical drive. "
            "ripdisc attempted diskutil unmountDisk first; pass --cdrdao-device with the exact cdrdao scanbus device if cdrdao needs a specific device."
            f"{detail}"
        )
    if len(devices) > 1:
        joined = ", ".join(devices)
        raise ToolError(f"cdrdao scanbus found multiple optical drives ({joined}); pass --cdrdao-device")
    if status is not None:
        status.write(f"cdrdao device: autodetected {devices[0]}")
    return devices[0]


def whole_disk_name(device: str) -> str:
    base = Path(device).name
    if base.startswith("r"):
        base = base[1:]
    match = re.match(r"^(disk\d+)", base)
    if not match:
        raise ToolError(f"Device must look like /dev/diskN, /dev/rdiskN, or a slice: {device}")
    return match.group(1)


def whole_disk_device(device: str) -> str:
    return f"/dev/{whole_disk_name(device)}"


def raw_device_for(device: str) -> str:
    path = Path(device)
    name = path.name
    if name.startswith("r"):
        return f"/dev/{name}"
    return f"/dev/r{name}"


def build_paths(out: Path, name: str) -> Paths:
    target_dir = out / name
    return Paths(
        base_out=out,
        name=name,
        target_dir=target_dir,
        iso=target_dir / f"{name}.iso",
        mapfile=target_dir / f"{name}.map",
        bin=target_dir / f"{name}.bin",
        toc=target_dir / f"{name}.toc",
        cue=target_dir / f"{name}.cue",
        vcd_xml=target_dir / f"{name}.vcd.xml",
        probe_json=target_dir / f"{name}.probe.json",
        commands_log=target_dir / f"{name}.commands.log",
        files_txt=target_dir / f"{name}.files.txt",
        sha256=target_dir / f"{name}.sha256",
        vcd_dir=target_dir / "vcd",
    )


def ensure_clean_target(paths: Paths) -> None:
    if paths.target_dir.exists() and any(paths.target_dir.iterdir()):
        raise ToolError(f"Refusing to use non-empty output directory: {paths.target_dir}")
    paths.target_dir.mkdir(parents=True, exist_ok=True)


def run_dvd_workflow(args: argparse.Namespace, paths: Paths, runner: CommandRunner, status: StatusLogger) -> None:
    script_path = Path(__file__).resolve().with_name("ripdvd.sh")
    command = [str(script_path), "--device", args.device, "--name", args.name, "--out", str(paths.target_dir)]

    if args.retries is not None:
        command.extend(["--retries", str(args.retries)])
    for enabled, flag in (
        (args.yes, "--yes"),
        (args.no_eject, "--no-eject"),
        (args.no_direct, "--no-direct"),
        (args.raw_read, "--raw-read"),
        (args.auto_slice, "--auto-slice"),
        (args.size_from_diskutil, "--size-from-diskutil"),
    ):
        if enabled:
            command.append(flag)

    status.write("DVD workflow: delegating to ripdvd.sh")
    status.write(f"DVD output directory: {paths.target_dir}")
    status.write("DVD command output: streaming to terminal and commands.log")
    runner.run_foreground(command, check=True)


def run_cd_workflow(
    args: argparse.Namespace,
    paths: Paths,
    probe: Probe,
    runner: CommandRunner,
    status: StatusLogger,
) -> str:
    status.write("=== CD workflow autodetection ===")
    cdrdao_device = resolve_cdrdao_device(runner, args.device, args.cdrdao_device, status=status)
    unmount_for_cdrdao(args.device, runner, status)
    status.write("CD master read: creating .bin/.toc with cdrdao read-cd --read-raw")
    runner.run(
        [
            "cdrdao",
            "read-cd",
            "--device",
            cdrdao_device,
            "--read-raw",
            "--datafile",
            paths.bin.name,
            paths.toc.name,
        ],
        cwd=paths.target_dir,
        check=True,
    )

    if runner.which("toc2cue") is not None:
        status.write("toc2cue: found; attempting .cue conversion")
        runner.run(["toc2cue", paths.toc.name, paths.cue.name], cwd=paths.target_dir, check=False)
    else:
        status.write("toc2cue: not found; skipping .cue conversion")

    classification = classify_probe(probe)
    status.write(f"CD classification after raw image capture: {classification}")

    if classification in {"cd-data", "cd-mixed"}:
        convert_bin_to_iso(paths, runner, status)
    elif classification == "cd-vcd":
        status.write("Cooked data ISO: skipped because disc is VCD/SVCD")
    else:
        status.write("Cooked data ISO: not selected for this CD classification")

    if classification == "cd-vcd":
        status.write("VCD/SVCD extraction: deferred to extractdisc.py")
    else:
        status.write("VCD/SVCD extraction: not selected")

    write_file_listing(paths, probe, runner, status)
    return classification


def convert_bin_to_iso(paths: Paths, runner: CommandRunner, status: StatusLogger) -> None:
    if not paths.bin.exists():
        status.write("Cooked data ISO: .bin master is not present; skipping conversion")
        return
    if not paths.cue.exists():
        status.write("Cooked data ISO: .cue is not present; skipping BIN/CUE conversion")
        return
    if runner.which("bchunk") is None:
        status.write("Cooked data ISO: bchunk not found; skipping BIN/CUE conversion")
        return

    prefix = f"{paths.name}.bchunk"
    status.write("Cooked data ISO: converting captured BIN/CUE to ISO with bchunk")
    result = runner.run(["bchunk", paths.bin.name, paths.cue.name, prefix], cwd=paths.target_dir, check=False)
    if result.returncode != 0:
        status.write("Cooked data ISO: bchunk failed; keeping .bin/.toc/.cue only")
        return

    generated = sorted(paths.target_dir.glob(f"{prefix}*.iso"))
    if len(generated) == 1:
        generated[0].replace(paths.iso)
        status.write(f"Cooked data ISO: wrote {paths.iso.name}")
    elif not generated:
        status.write("Cooked data ISO: bchunk did not produce an ISO; keeping .bin/.toc/.cue only")
    else:
        names = ", ".join(path.name for path in generated)
        status.write(f"Cooked data ISO: bchunk produced multiple ISO files ({names}); leaving them unrenamed")


def write_file_listing(paths: Paths, probe: Probe, runner: CommandRunner, status: StatusLogger) -> None:
    if paths.iso.exists():
        script_path = Path(__file__).resolve().with_name("iso_tool.py")
        result = runner.run([sys.executable, str(script_path), "list", str(paths.iso)], check=False)
        if result.returncode == 0:
            paths.files_txt.write_bytes(result.stdout)
            status.write("File listing: generated from cooked ISO")
            return
        status.write("File listing: cooked ISO mount/list failed; falling back to mounted source if available")

    if probe.preferred_data_mount is not None:
        lines = [relative_display_path(path, probe.preferred_data_mount.mount_point) for path in iter_files(probe.preferred_data_mount.mount_point)]
        paths.files_txt.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
        status.write(f"File listing: generated from mounted source {probe.preferred_data_mount.mount_point}")
    else:
        status.write("File listing: no cooked ISO or mounted source available; skipping")


def iter_files(root: Path) -> Iterable[Path]:
    for current_root, dirnames, filenames in os.walk(root):
        dirnames.sort()
        filenames.sort()
        current_path = Path(current_root)
        for filename in filenames:
            yield current_path / filename


def relative_display_path(path: Path, root: Path) -> str:
    return str(path.relative_to(root))


def write_probe_json(paths: Paths, probe: Probe, classification: str) -> None:
    payload = {
        "device": probe.device,
        "classification": classification,
        "disk_info": probe.disk_info.plist if probe.disk_info is not None else None,
        "partition_infos": [{"device": info.device, "plist": info.plist} for info in probe.partition_infos],
        "mounts": [
            {"device": mount.device, "mount_point": str(mount.mount_point), "fstype": mount.fstype}
            for mount in probe.mounts
        ],
        "track_types": track_type_strings(probe.trackinfo),
    }
    paths.probe_json.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_sha256(paths: Paths) -> None:
    outputs = [
        paths.iso,
        paths.mapfile,
        paths.bin,
        paths.toc,
        paths.cue,
        paths.vcd_xml,
        paths.probe_json,
        paths.files_txt,
    ]
    existing = [path for path in outputs if path.is_file()]
    if not existing:
        return
    with paths.sha256.open("w", encoding="utf-8") as sha_file:
        for path in existing:
            sha_file.write(f"{sha256_file(path)}  {path.name}\n")


def eject_disc(device: str, runner: CommandRunner, status: StatusLogger) -> None:
    status.write(f"Eject: drutil tray eject for {whole_disk_device(device)}")
    result = runner.run(["drutil", "tray", "eject"], check=False)
    if result.returncode == 0:
        status.write("Eject: complete")
    else:
        output = command_output_text(result).strip()
        status.write(f"Eject: drutil tray eject failed with exit status {result.returncode}: {output or '(no output)'}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def unique_strings(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Archive optical discs to preservation-oriented image files.")
    parser.add_argument("--device", required=True, help="Disk device such as /dev/disk4 or /dev/rdisk4")
    parser.add_argument("--name", required=True, help="Output basename; must not contain /")
    parser.add_argument("--out", type=Path, default=Path.cwd(), help="Base output directory")
    parser.add_argument("--kind", choices=("auto", "dvd", "cd"), default="auto", help="Force DVD or CD workflow")
    parser.add_argument("--probe-only", action="store_true", help="Detect and report disc type, then exit before ripping")
    parser.add_argument("--cdrdao-device", help="cdrdao device string, used when scanbus is ambiguous")
    parser.add_argument("--yes", action="store_true", help="Forward confirmation bypass to ripdvd.sh")
    parser.add_argument("--retries", type=int, help="Forward DVD retry count to ripdvd.sh")
    parser.add_argument("--no-eject", action="store_true", help="Forward --no-eject to ripdvd.sh")
    parser.add_argument("--no-direct", action="store_true", help="Forward --no-direct to ripdvd.sh")
    parser.add_argument("--raw-read", action="store_true", help="Forward --raw-read to ripdvd.sh")
    parser.add_argument("--auto-slice", action="store_true", help="Forward --auto-slice to ripdvd.sh")
    parser.add_argument("--size-from-diskutil", action="store_true", help="Forward --size-from-diskutil to ripdvd.sh")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, runner: CommandRunner | None = None) -> int:
    args = parse_args(argv)
    runner = runner or CommandRunner()

    try:
        if "/" in args.name:
            raise ToolError("--name must not contain /")

        paths = build_paths(args.out, args.name)
        if args.probe_only:
            status = StatusLogger()
        else:
            ensure_clean_target(paths)
            runner.set_log_path(paths.commands_log)
            status = StatusLogger(paths.commands_log)
        status.write("=== ripdisc plan ===")
        status.write(f"Output directory: {paths.target_dir}")
        if args.probe_only:
            status.write("Command log: disabled in probe-only mode")
        else:
            status.write(f"Command log: {paths.commands_log}")

        probe = probe_device(args.device, runner)
        classification = classify_probe(probe)
        describe_probe_detection(probe, classification, status)

        if args.kind == "dvd":
            workflow = "dvd"
            workflow_reason = "forced by --kind dvd"
        elif args.kind == "cd":
            workflow = "cd"
            workflow_reason = "forced by --kind cd"
        elif classification == "dvd":
            workflow = "dvd"
            workflow_reason = "autodetected DVD media"
        elif classification in {"cd-data", "cd-vcd", "cd-mixed"}:
            workflow = "cd"
            workflow_reason = f"autodetected {classification}"
        elif classification == "audio-only":
            if not args.probe_only:
                write_probe_json(paths, probe, classification)
            raise ToolError("Unsupported audio CD: Red Book audio extraction is out of scope")
        else:
            if not args.probe_only:
                write_probe_json(paths, probe, classification)
            raise ToolError(f"Could not classify optical disc in {args.device}; pass --kind dvd or --kind cd")

        status.write(f"Selected workflow: {workflow} ({workflow_reason})")

        if args.probe_only:
            status.write("Probe-only mode: exiting before ripping without writing output files")
            return 0

        write_probe_json(paths, probe, classification)

        if workflow == "dvd":
            run_dvd_workflow(args, paths, runner, status)
        else:
            final_classification = run_cd_workflow(args, paths, probe, runner, status)
            if final_classification != classification:
                write_probe_json(paths, probe, final_classification)
                status.write(f"Probe metadata updated with final classification: {final_classification}")

        write_sha256(paths)
        status.write(f"Checksums: {paths.sha256 if paths.sha256.exists() else 'no generated files to checksum'}")
        if workflow == "cd":
            if args.no_eject:
                status.write("Eject: skipped because --no-eject was passed")
            else:
                eject_disc(args.device, runner, status)
        return 0
    except ToolError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
