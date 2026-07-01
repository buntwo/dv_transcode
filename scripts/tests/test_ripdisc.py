from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

import ripdisc


class FakeRunner(ripdisc.CommandRunner):
    def __init__(
        self,
        responses: dict[tuple[str, ...], ripdisc.CommandResult] | None = None,
        available: set[str] | None = None,
    ) -> None:
        super().__init__()
        self.responses = responses or {}
        self.available = available or set()
        self.calls: list[tuple[list[str], Path | None, bool]] = []
        self.foreground_calls: list[tuple[list[str], Path | None, bool]] = []

    def which(self, command: str) -> str | None:
        return f"/fake/{command}" if command in self.available else None

    def run(self, command, cwd: Path | None = None, check: bool = True):  # type: ignore[no-untyped-def]
        command = list(command)
        self.calls.append((command, cwd, check))
        result = self.responses.get(tuple(command), self.default_result(command))
        self._log(result, cwd)
        self.apply_side_effect(command, cwd, result)
        if check and result.returncode != 0:
            raise ripdisc.ToolError(f"{ripdisc.format_command(command)} failed")
        return result

    def run_foreground(self, command, cwd: Path | None = None, check: bool = True):  # type: ignore[no-untyped-def]
        command = list(command)
        self.foreground_calls.append((command, cwd, check))
        result = self.responses.get(tuple(command), ripdisc.CommandResult(command, 0, b"", b""))
        self._log(result, cwd)
        if check and result.returncode != 0:
            raise ripdisc.ToolError(f"{ripdisc.format_command(command)} failed")
        return result

    def default_result(self, command: list[str]) -> ripdisc.CommandResult:
        if len(command) >= 3 and command[1].endswith("iso_tool.py") and command[2] == "list":
            return ripdisc.CommandResult(command, 1, b"", b"mount failed")
        return ripdisc.CommandResult(command, 0, b"", b"")

    def apply_side_effect(self, command: list[str], cwd: Path | None, result: ripdisc.CommandResult) -> None:
        if result.returncode != 0 or cwd is None:
            return
        if command[:2] == ["cdrdao", "read-cd"]:
            datafile = cwd / command[command.index("--datafile") + 1]
            tocfile = cwd / command[-1]
            datafile.write_bytes(raw_mode2_form1_bin())
            tocfile.write_text("CD_ROM_XA\n", encoding="utf-8")
        elif command and command[0] == "toc2cue":
            (cwd / command[2]).write_text('FILE "disc.bin" BINARY\n', encoding="utf-8")
        elif command and command[0] == "bchunk":
            (cwd / f"{command[3]}01.iso").write_bytes(b"cooked iso")


class UnmountingFakeRunner(FakeRunner):
    def __init__(
        self,
        mount_to_hide: Path,
        responses: dict[tuple[str, ...], ripdisc.CommandResult] | None = None,
        available: set[str] | None = None,
    ) -> None:
        super().__init__(responses, available)
        self.mount_to_hide = mount_to_hide

    def apply_side_effect(self, command: list[str], cwd: Path | None, result: ripdisc.CommandResult) -> None:
        super().apply_side_effect(command, cwd, result)
        if result.returncode == 0 and command[:2] == ["diskutil", "unmountDisk"] and self.mount_to_hide.exists():
            self.mount_to_hide.rename(self.mount_to_hide.with_name(f"{self.mount_to_hide.name}.unmounted"))


def raw_mode2_form1_bin() -> bytes:
    sectors = []
    for sector_number in range(32):
        sector = bytearray(2352)
        if sector_number == 16:
            sector[24:24 + 6] = b"\x01CD001"
        sectors.append(bytes(sector))
    return b"".join(sectors)


def plist_bytes(value: object) -> bytes:
    return plistlib.dumps(value)


def standard_probe_responses(tmp_path: Path, *, kind: str = "cd", mount: Path | None = None) -> dict[tuple[str, ...], ripdisc.CommandResult]:
    disk = {"Content": "Apple_partition_scheme", "OpticalMediaType": "CD-ROM" if kind == "cd" else "DVD-ROM"}
    partition = {"Content": "CD_ROM_Mode_1", "TotalSize": 123904}
    trackinfo = {"Tracks": [{"Track Type": "Data"}]}
    mount_output = ""
    list_plist = {"AllDisksAndPartitions": [{"Partitions": [{"DeviceIdentifier": "disk4s0"}]}]}

    if mount is not None:
        mount_output = f"/dev/disk4s0 on {mount} (cd9660, local, read-only)\n"

    if kind == "audio":
        disk = {"OpticalMediaType": "CD-ROM"}
        trackinfo = {"Tracks": [{"Track Type": "Audio"}, {"Track Type": "Audio"}]}
        list_plist = {"AllDisksAndPartitions": [{"Partitions": []}]}

    return {
        ("diskutil", "info", "-plist", "/dev/disk4"): ripdisc.CommandResult([], 0, plist_bytes(disk)),
        ("diskutil", "list", "-plist", "/dev/disk4"): ripdisc.CommandResult([], 0, plist_bytes(list_plist)),
        ("diskutil", "info", "-plist", "/dev/disk4s0"): ripdisc.CommandResult([], 0, plist_bytes(partition)),
        ("mount",): ripdisc.CommandResult([], 0, mount_output.encode()),
        ("drutil", "status"): ripdisc.CommandResult([], 0, f"Type: {disk.get('OpticalMediaType', '')}\n".encode()),
        ("drutil", "trackinfo", "-xml"): ripdisc.CommandResult([], 0, plist_bytes(trackinfo)),
    }


def test_output_paths_and_existing_output_refusal(tmp_path: Path) -> None:
    paths = ripdisc.build_paths(tmp_path, "Dinner_at_Aunt")

    assert paths.target_dir == tmp_path / "Dinner_at_Aunt"
    assert paths.iso == tmp_path / "Dinner_at_Aunt" / "Dinner_at_Aunt.iso"

    paths.target_dir.mkdir()
    (paths.target_dir / "old.iso").write_text("old", encoding="utf-8")

    with pytest.raises(ripdisc.ToolError, match="non-empty output directory"):
        ripdisc.ensure_clean_target(paths)


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("", []),
        ("0,2,0 PLEXTOR CD-R PX-W1210A\n", ["0,2,0"]),
        ("ATA:1,0,0 HL-DT-ST DVDRW\n", ["ATA:1,0,0"]),
        ("IOCompactDiscServices:0,0,0 MATSHITA DVD-R UJ-8A8\n", ["IOCompactDiscServices:0,0,0"]),
        (
            "IOService:/AppleARMPE/usb/PIONEER/IODVDServices : PIONEER, DVD-RW  DVR-111, 1.23\n",
            ["IOService:/AppleARMPE/usb/PIONEER/IODVDServices"],
        ),
        (
            "IOCompactDiscServices:0,0,0 MATSHITA DVD-R\n"
            "IOCompactDiscServices:1,0,0 PIONEER BD-RW\n",
            ["IOCompactDiscServices:0,0,0", "IOCompactDiscServices:1,0,0"],
        ),
    ],
)
def test_parse_cdrdao_scanbus(output: str, expected: list[str]) -> None:
    assert ripdisc.parse_cdrdao_scanbus(output) == expected


def test_resolve_cdrdao_device_rejects_zero_or_multiple() -> None:
    zero = FakeRunner({("cdrdao", "scanbus"): ripdisc.CommandResult([], 0, b"")})
    with pytest.raises(ripdisc.ToolError, match="attempted diskutil unmountDisk"):
        ripdisc.resolve_cdrdao_device(zero, "/dev/disk4")

    unparsed = FakeRunner({("cdrdao", "scanbus"): ripdisc.CommandResult([], 0, b"weird scanbus output\n")})
    with pytest.raises(ripdisc.ToolError, match="weird scanbus output"):
        ripdisc.resolve_cdrdao_device(unparsed, "/dev/disk4")

    multiple = FakeRunner(
        {
            ("cdrdao", "scanbus"): ripdisc.CommandResult(
                [],
                0,
                b"IOCompactDiscServices:0,0,0 MATSHITA DVD-R\nIOCompactDiscServices:1,0,0 PIONEER BD-RW\n",
            )
        }
    )
    with pytest.raises(ripdisc.ToolError, match="multiple"):
        ripdisc.resolve_cdrdao_device(multiple, "/dev/disk4")


def test_resolve_cdrdao_device_unmounts_and_retries_when_in_use() -> None:
    ioservice = "IOService:/AppleARMPE/usb/PIONEER/IODVDServices"
    runner = FakeRunner(
        {
            ("cdrdao", "scanbus"): ripdisc.CommandResult(
                [],
                0,
                f"{ioservice} : PIONEER, DVD-RW  DVR-111, 1.23\n".encode(),
            )
        }
    )

    assert ripdisc.resolve_cdrdao_device(runner, "/dev/disk4") == ioservice
    assert ["diskutil", "unmountDisk", "/dev/disk4"] in [call[0] for call in runner.calls]


def test_classification_from_mocked_probe_data(tmp_path: Path) -> None:
    mount = tmp_path / "DISC"
    mount.mkdir()
    runner = FakeRunner(standard_probe_responses(tmp_path, kind="cd", mount=mount))

    probe = ripdisc.probe_device("/dev/disk4", runner)

    assert ripdisc.classify_probe(probe) == "cd-data"


def test_cd_in_dvd_capable_drive_stays_cd(tmp_path: Path) -> None:
    mount = tmp_path / "DISC"
    mount.mkdir()
    responses = standard_probe_responses(tmp_path, kind="cd", mount=mount)
    responses[("drutil", "status")] = ripdisc.CommandResult(
        [],
        0,
        b"Vendor   Product           Rev\nMATSHITA DVD-R UJ-8A8 1.00\nType: CD-ROM\n",
    )
    runner = FakeRunner(responses)

    probe = ripdisc.probe_device("/dev/disk4", runner)

    assert ripdisc.classify_probe(probe) == "cd-data"


def test_vcd_markers_classify_as_vcd(tmp_path: Path) -> None:
    mount = tmp_path / "VCD_DISC"
    (mount / "VCD").mkdir(parents=True)
    (mount / "VCD" / "INFO.VCD").write_text("", encoding="utf-8")
    runner = FakeRunner(standard_probe_responses(tmp_path, kind="cd", mount=mount))

    probe = ripdisc.probe_device("/dev/disk4", runner)

    assert ripdisc.classify_probe(probe) == "cd-vcd"

    (mount / "VCD" / "INFO.VCD").unlink()
    (mount / "VCD").rmdir()
    assert ripdisc.classify_probe(probe) == "cd-vcd"


def test_audio_only_cd_rejection(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner(standard_probe_responses(tmp_path, kind="audio"))

    status = ripdisc.main(["--device", "/dev/disk4", "--name", "Audio", "--out", str(tmp_path)], runner=runner)

    assert status == 2
    assert "Unsupported audio CD" in capsys.readouterr().err


def test_probe_only_reports_workflow_and_exits_before_ripping(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mount = tmp_path / "DISC"
    mount.mkdir()
    runner = FakeRunner(standard_probe_responses(tmp_path, kind="cd", mount=mount))

    status = ripdisc.main(
        ["--device", "/dev/disk4", "--name", "Probe", "--out", str(tmp_path), "--probe-only"],
        runner=runner,
    )

    assert status == 0
    commands = [call[0] for call in runner.calls]
    assert ["cdrdao", "scanbus"] not in commands
    assert not any(command[:2] == ["cdrdao", "read-cd"] for command in commands)
    assert not any(command and command[0] == "bchunk" for command in commands)
    assert not runner.foreground_calls
    assert not (tmp_path / "Probe").exists()

    output = capsys.readouterr().out
    assert "Initial classification: cd-data" in output
    assert "Selected workflow: cd (autodetected cd-data)" in output
    assert "Command log: disabled in probe-only mode" in output
    assert "Probe-only mode: exiting before ripping without writing output files" in output


def test_dvd_path_delegates_to_ripdvd_with_nested_out(tmp_path: Path) -> None:
    runner = FakeRunner(standard_probe_responses(tmp_path, kind="dvd"))

    status = ripdisc.main(
        [
            "--device",
            "/dev/disk4",
            "--name",
            "Dinner",
            "--out",
            str(tmp_path),
            "--yes",
            "--retries",
            "5",
            "--no-eject",
            "--no-direct",
            "--raw-read",
            "--auto-slice",
            "--size-from-diskutil",
        ],
        runner=runner,
    )

    assert status == 0
    commands = [call[0] for call in runner.foreground_calls]
    dvd_command = next(command for command in commands if command[0].endswith("ripdvd.sh"))
    assert dvd_command[dvd_command.index("--out") + 1] == str(tmp_path / "Dinner")
    assert "--yes" in dvd_command
    assert ["--retries", "5"]


def test_cd_path_reads_raw_converts_cooks_lists_and_hashes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mount = tmp_path / "DISC"
    mount.mkdir()
    (mount / "PHOTO.JPG").write_text("photo", encoding="utf-8")
    responses = standard_probe_responses(tmp_path, kind="cd", mount=mount)
    responses[("cdrdao", "scanbus")] = ripdisc.CommandResult([], 0, b"IOCompactDiscServices:0,0,0 MATSHITA DVD-R\n")
    runner = FakeRunner(responses, available={"toc2cue", "bchunk"})

    status = ripdisc.main(["--device", "/dev/disk4", "--name", "Photos", "--out", str(tmp_path), "--kind", "cd"], runner=runner)

    assert status == 0
    commands = [call[0] for call in runner.calls]
    assert [
        "cdrdao",
        "read-cd",
        "--device",
        "IOCompactDiscServices:0,0,0",
        "--read-raw",
        "--datafile",
        "Photos.bin",
        "Photos.toc",
    ] in commands
    assert ["toc2cue", "Photos.toc", "Photos.cue"] in commands
    assert ["bchunk", "Photos.bin", "Photos.cue", "Photos.bchunk"] in commands
    assert not any(command and command[0] == "ddrescue" for command in commands)
    assert ["drutil", "tray", "eject"] in commands
    assert (tmp_path / "Photos" / "Photos.iso").is_file()
    assert (tmp_path / "Photos" / "Photos.files.txt").read_text(encoding="utf-8") == "PHOTO.JPG\n"
    assert (tmp_path / "Photos" / "Photos.probe.json").is_file()
    assert (tmp_path / "Photos" / "Photos.commands.log").is_file()
    assert "Photos.probe.json" in (tmp_path / "Photos" / "Photos.sha256").read_text(encoding="utf-8")

    output = capsys.readouterr().out
    assert "=== Media autodetection ===" in output
    assert "Mounted filesystems for device:" in output
    assert "Initial classification: cd-data" in output
    assert "Selected workflow: cd (forced by --kind cd)" in output
    assert "cdrdao scanbus: found 1 optical drive(s)" in output
    assert "cdrdao device: autodetected IOCompactDiscServices:0,0,0" in output
    assert "toc2cue: found; attempting .cue conversion" in output
    assert "CD classification after raw image capture: cd-data" in output
    assert "Cooked data ISO: converting captured BIN/CUE to ISO with bchunk" in output
    assert "Cooked data ISO: wrote Photos.iso" in output
    assert "VCD/SVCD extraction: not selected" in output
    assert "File listing: cooked ISO mount/list failed; falling back" in output
    assert "File listing: generated from mounted source" in output
    assert "Eject: drutil tray eject for /dev/disk4" in output
    assert "Eject: complete" in output

    command_log = (tmp_path / "Photos" / "Photos.commands.log").read_text(encoding="utf-8")
    assert "=== Media autodetection ===" in command_log
    assert "Selected workflow: cd (forced by --kind cd)" in command_log
    assert "cdrdao device: autodetected IOCompactDiscServices:0,0,0" in command_log


def test_cd_path_no_eject_skips_physical_eject(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    mount = tmp_path / "DISC"
    mount.mkdir()
    responses = standard_probe_responses(tmp_path, kind="cd", mount=mount)
    responses[("cdrdao", "scanbus")] = ripdisc.CommandResult([], 0, b"IOCompactDiscServices:0,0,0 MATSHITA DVD-R\n")
    runner = FakeRunner(responses, available={"toc2cue", "bchunk"})

    status = ripdisc.main(
        ["--device", "/dev/disk4", "--name", "NoEject", "--out", str(tmp_path), "--kind", "cd", "--no-eject"],
        runner=runner,
    )

    assert status == 0
    commands = [call[0] for call in runner.calls]
    assert ["drutil", "tray", "eject"] not in commands
    assert "Eject: skipped because --no-eject was passed" in capsys.readouterr().out


def test_cd_path_does_not_probe_or_extract_vcd_from_captured_image(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mount = tmp_path / "DISC"
    mount.mkdir()
    responses = standard_probe_responses(tmp_path, kind="cd", mount=mount)
    responses[("cdrdao", "scanbus")] = ripdisc.CommandResult([], 0, b"IOCompactDiscServices:0,0,0 MATSHITA DVD-R\n")
    runner = FakeRunner(responses, available={"toc2cue", "vcd-info", "bchunk"})

    status = ripdisc.main(["--device", "/dev/disk4", "--name", "Movie", "--out", str(tmp_path), "--kind", "cd"], runner=runner)

    assert status == 0
    commands = [call[0] for call in runner.calls]
    assert ["bchunk", "Movie.bin", "Movie.cue", "Movie.bchunk"] in commands
    assert not any(command and command[0] == "vcd-info" for command in commands)
    assert not any(command and command[0] == "vcdxrip" for command in commands)
    output = capsys.readouterr().out
    assert "CD classification after raw image capture: cd-data" in output
    assert "VCD/SVCD extraction: not selected" in output


def test_cd_path_keeps_vcd_marker_classification_after_unmount(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mount = tmp_path / "VCD_DISC"
    (mount / "VCD").mkdir(parents=True)
    (mount / "VCD" / "INFO.VCD").write_text("", encoding="utf-8")
    responses = standard_probe_responses(tmp_path, kind="cd", mount=mount)
    responses[("cdrdao", "scanbus")] = ripdisc.CommandResult([], 0, b"IOCompactDiscServices:0,0,0 MATSHITA DVD-R\n")
    runner = UnmountingFakeRunner(mount, responses, available={"toc2cue", "bchunk"})

    status = ripdisc.main(["--device", "/dev/disk4", "--name", "MarkedVcd", "--out", str(tmp_path)], runner=runner)

    assert status == 0
    commands = [call[0] for call in runner.calls]
    assert ["bchunk", "MarkedVcd.bin", "MarkedVcd.cue", "MarkedVcd.bchunk"] not in commands
    assert not any(command and command[0] == "vcd-info" for command in commands)
    assert not any(command and command[0] == "vcdxrip" for command in commands)
    output = capsys.readouterr().out
    assert "Initial classification: cd-vcd" in output
    assert "CD classification after raw image capture: cd-vcd" in output
    assert "Cooked data ISO: skipped because disc is VCD/SVCD" in output
    assert "VCD/SVCD extraction: deferred to extractdisc.py" in output


def test_cd_path_does_not_run_vcd_tools_when_cue_missing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mount = tmp_path / "DISC"
    mount.mkdir()
    responses = standard_probe_responses(tmp_path, kind="cd", mount=mount)
    responses[("cdrdao", "scanbus")] = ripdisc.CommandResult([], 0, b"IOCompactDiscServices:0,0,0 MATSHITA DVD-R\n")
    runner = FakeRunner(responses, available={"vcd-info", "bchunk"})

    status = ripdisc.main(["--device", "/dev/disk4", "--name", "MovieNoCue", "--out", str(tmp_path), "--kind", "cd"], runner=runner)

    assert status == 0
    commands = [call[0] for call in runner.calls]
    assert not any(command and command[0] == "bchunk" for command in commands)
    assert not any(command and command[0] == "vcd-info" for command in commands)
    assert not any(command and command[0] == "vcdxrip" for command in commands)
    output = capsys.readouterr().out
    assert "Cooked data ISO: .cue is not present; skipping BIN/CUE conversion" in output
    assert "VCD/SVCD extraction: not selected" in output
