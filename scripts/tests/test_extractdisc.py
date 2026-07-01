from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import extractdisc


class FakeRunner(extractdisc.CommandRunner):
    def __init__(
        self,
        available: set[str] | None = None,
        vcd_info_output: bytes = b"",
        makemkv_info_output: bytes = b"",
        makemkv_info_returncode: int = 0,
        makemkv_mkv_output: bytes | None = None,
    ) -> None:
        self.available = {"hdiutil"} if available is None else available
        self.vcd_info_output = vcd_info_output
        self.makemkv_info_output = makemkv_info_output
        self.makemkv_info_returncode = makemkv_info_returncode
        self.makemkv_mkv_output = makemkv_mkv_output
        self.calls: list[tuple[list[str], Path | None, bool]] = []

    def which(self, command: str) -> str | None:
        return f"/fake/{command}" if command in self.available else None

    def run(self, command, cwd: Path | None = None, check: bool = True):  # type: ignore[no-untyped-def]
        command = list(command)
        self.calls.append((command, cwd, check))
        if command and command[0] == "vcd-info":
            return extractdisc.CommandResult(command, 0 if self.vcd_info_output else 1, self.vcd_info_output)
        if command and command[0] == "bchunk":
            assert cwd is not None
            (cwd / f"{command[3]}01.iso").write_bytes(b"generated iso")
        if command and command[0] == "vcdxrip":
            assert cwd is not None
            (cwd / command[command.index("--output-file") + 1]).write_bytes(b"xml")
            (cwd / "avseq01.mpg").write_bytes(b"video")
        if command[:3] == ["makemkvcon", "-r", "info"]:
            return extractdisc.CommandResult(command, self.makemkv_info_returncode, self.makemkv_info_output)
        if command[:3] == ["makemkvcon", "-r", "mkv"]:
            if self.makemkv_mkv_output is not None:
                return extractdisc.CommandResult(command, 0, self.makemkv_mkv_output)
            output_dir = Path(command[5])
            output_dir.mkdir(parents=True, exist_ok=True)
            title_id = int(command[4])
            (output_dir / f"title_t{title_id:02d}.mkv").write_bytes(b"mkv")
            output = b'MSG:5004,0,2,"1 titles saved, 0 failed","%1 titles saved, %2 failed","1","0"\n'
            return extractdisc.CommandResult(command, 0, output)
        return extractdisc.CommandResult(command, 0)


@contextmanager
def fake_attached_image_for(mounts: dict[Path, Path], image: Path) -> Iterator[list[SimpleNamespace]]:
    yield [SimpleNamespace(mount_point=mounts[image])]


def patch_iso_mounts(monkeypatch: pytest.MonkeyPatch, mounts: dict[Path, Path]) -> None:
    @contextmanager
    def fake_attached_image(image: Path) -> Iterator[list[SimpleNamespace]]:
        yield [SimpleNamespace(mount_point=mounts[image])]

    monkeypatch.setattr(extractdisc.iso_tool, "attached_image", fake_attached_image)


MAKEMKV_TWO_TITLE_OUTPUT = b"""TCOUNT:2
TINFO:0,9,0,"0:42:00"
TINFO:0,10,0,"3.8 GB"
TINFO:0,11,0,"4080218931"
TINFO:0,27,0,"title_t00.mkv"
SINFO:0,0,1,0,"Video"
SINFO:0,0,5,0,"Mpeg2"
SINFO:0,1,1,0,"Audio"
SINFO:0,1,3,0,"English"
SINFO:0,1,5,0,"AC3"
TINFO:1,9,0,"0:21:30"
TINFO:1,10,0,"1.9 GB"
TINFO:1,11,0,"2040109465"
TINFO:1,27,0,"title_t01.mkv"
SINFO:1,0,1,0,"Video"
SINFO:1,0,5,0,"Mpeg2"
SINFO:1,1,1,0,"Audio"
SINFO:1,1,3,0,"English"
SINFO:1,1,5,0,"AC3"
SINFO:1,2,1,0,"Subtitles"
SINFO:1,2,3,0,"English"
"""


def test_discovery_excludes_extracted_dirs_and_bchunk_artifacts(tmp_path: Path) -> None:
    (tmp_path / "disc.iso").write_bytes(b"iso")
    (tmp_path / "disc.bchunk01.iso").write_bytes(b"generated")
    (tmp_path / "disc.bin").write_bytes(b"bin")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "other.iso").write_bytes(b"iso")
    (tmp_path / "extracted").mkdir()
    (tmp_path / "extracted" / "ignored.iso").write_bytes(b"iso")
    (tmp_path / "disc.extracted").mkdir()
    (tmp_path / "disc.extracted" / "ignored.iso").write_bytes(b"iso")
    (tmp_path / "nested" / "extracted").mkdir()
    (tmp_path / "nested" / "extracted" / "ignored.bin").write_bytes(b"bin")
    (tmp_path / "nested" / "other.extracted").mkdir()
    (tmp_path / "nested" / "other.extracted" / "ignored.iso").write_bytes(b"iso")

    assert [path.relative_to(tmp_path) for path in extractdisc.discover_images(tmp_path)] == [
        Path("disc.bin"),
        Path("disc.iso"),
        Path("nested/other.iso"),
    ]


def test_preflight_table_reports_actions_and_blockers_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    iso = tmp_path / "photos.iso"
    iso.write_bytes(b"iso")
    missing_companion = tmp_path / "orphan.bin"
    missing_companion.write_bytes(b"bin")
    mount = tmp_path / "MOUNT"
    mount.mkdir()
    patch_iso_mounts(monkeypatch, {iso: mount})

    status = extractdisc.main([str(tmp_path), "--plan-only"], runner=FakeRunner())

    assert status == 0
    assert not (tmp_path / "photos.extracted").exists()
    output = capsys.readouterr().out
    assert "Image" in output
    assert "Media" in output
    assert "Content" in output
    assert "Output" in output
    assert "Action" in output
    assert "photos.iso" in output
    assert "CD" in output
    assert "data" in output
    assert "extract ISO" in output
    assert "orphan.bin" in output
    assert "blocked: missing companion .cue or .toc" in output
    assert "Blocked jobs:" in output


def test_preflight_progress_prints_to_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    iso = tmp_path / "disc.iso"
    iso.write_bytes(b"iso")
    mount = tmp_path / "MOUNT"
    mount.mkdir()
    patch_iso_mounts(monkeypatch, {iso: mount})

    status = extractdisc.main([str(tmp_path), "--plan-only"], runner=FakeRunner())

    assert status == 0
    captured = capsys.readouterr()
    assert "[####################] 1/1 preflight disc.iso" in captured.err
    assert "[####################] 1/1 preflight disc.iso" not in captured.out
    assert "Image" in captured.out


def test_preflight_table_aligns_cjk_paths_by_display_width(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ascii_iso = tmp_path / "abcde.iso"
    cjk_iso = tmp_path / "写真.iso"
    ascii_iso.write_bytes(b"iso")
    cjk_iso.write_bytes(b"iso")
    ascii_mount = tmp_path / "ASCII_MOUNT"
    cjk_mount = tmp_path / "CJK_MOUNT"
    ascii_mount.mkdir()
    cjk_mount.mkdir()
    patch_iso_mounts(monkeypatch, {ascii_iso: ascii_mount, cjk_iso: cjk_mount})

    status = extractdisc.main([str(tmp_path), "--plan-only"], runner=FakeRunner())

    assert status == 0
    lines = capsys.readouterr().out.splitlines()
    ascii_line = next(line for line in lines if line.startswith("abcde.iso"))
    cjk_line = next(line for line in lines if line.startswith("写真.iso"))
    assert extractdisc.display_width(ascii_line.split("CD", 1)[0]) == extractdisc.display_width(
        cjk_line.split("CD", 1)[0]
    )


def test_same_stem_data_iso_omits_bin_from_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    iso = tmp_path / "disc.iso"
    bin_file = tmp_path / "disc.bin"
    iso.write_bytes(b"iso")
    bin_file.write_bytes(b"bin")
    mount = tmp_path / "MOUNT"
    mount.mkdir()
    patch_iso_mounts(monkeypatch, {iso: mount})

    status = extractdisc.main([str(tmp_path), "--plan-only"], runner=FakeRunner(available={"hdiutil"}))

    assert status == 0
    output = capsys.readouterr().out
    assert "disc.iso" in output
    assert "CD" in output
    assert "data" in output
    assert "extract ISO" in output
    assert "disc.bin" not in output
    assert "blocked: missing companion .cue or .toc" not in output
    assert "Blocked jobs:" not in output


def test_existing_extracted_directory_skips_with_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    iso = tmp_path / "disc.iso"
    iso.write_bytes(b"iso")
    mount = tmp_path / "MOUNT"
    mount.mkdir()
    extracted = tmp_path / "disc.extracted"
    extracted.mkdir()
    (extracted / "already.txt").write_text("done", encoding="utf-8")
    patch_iso_mounts(monkeypatch, {iso: mount})
    extracted_calls: list[tuple[Path, Path, bool]] = []

    def fake_extract_image_files(image: Path, output_dir: Path, overwrite: bool = False) -> int:
        extracted_calls.append((image, output_dir, overwrite))
        return 3

    monkeypatch.setattr(
        extractdisc.iso_tool,
        "extract_image_files",
        fake_extract_image_files,
    )

    status = extractdisc.main([str(tmp_path), "--yes"], runner=FakeRunner())

    assert status == 0
    assert extracted_calls == []
    output = capsys.readouterr().out
    assert "CD" in output
    assert "data" in output
    assert "skip: disc.extracted/ already exists; assuming already extracted" in output
    assert "No runnable extraction jobs." in output


def test_empty_extracted_directory_remains_runnable_for_dvd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    iso = tmp_path / "movie.iso"
    iso.write_bytes(b"iso")
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    mount = tmp_path / "DVD_MOUNT"
    (mount / "VIDEO_TS").mkdir(parents=True)
    (mount / "VIDEO_TS" / "VIDEO_TS.IFO").write_text("", encoding="utf-8")
    patch_iso_mounts(monkeypatch, {iso: mount})
    runner = FakeRunner(available={"hdiutil", "makemkvcon"}, makemkv_info_output=MAKEMKV_TWO_TITLE_OUTPUT)

    status = extractdisc.main([str(tmp_path), "--yes"], runner=runner)

    assert status == 0
    commands = [call[0] for call in runner.calls]
    assert ["makemkvcon", "-r", "info", f"iso:{iso.resolve()}"] in commands
    output_dir = tmp_path / "movie.extracted"
    assert ["makemkvcon", "-r", "mkv", f"iso:{iso.resolve()}", "0", str(output_dir.resolve())] in commands
    output = capsys.readouterr().out
    assert "movie.extracted/ missing" in output
    assert f"output -> {output_dir.resolve()}" in output
    assert "skip: legacy extracted/ already exists; assuming already extracted" not in output


def test_confirmation_plan_only_and_yes_control_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    iso = tmp_path / "disc.iso"
    iso.write_bytes(b"iso")
    mount = tmp_path / "MOUNT"
    mount.mkdir()
    patch_iso_mounts(monkeypatch, {iso: mount})
    extracted_calls: list[tuple[Path, Path, bool]] = []

    def fake_extract_image_files(image: Path, output_dir: Path, overwrite: bool = False) -> int:
        extracted_calls.append((image, output_dir, overwrite))
        return 1

    monkeypatch.setattr(
        extractdisc.iso_tool,
        "extract_image_files",
        fake_extract_image_files,
    )

    assert extractdisc.main([str(tmp_path)], runner=FakeRunner(), input_func=lambda prompt: "no") == 1
    assert extracted_calls == []
    assert extractdisc.main([str(tmp_path), "--plan-only"], runner=FakeRunner(), input_func=lambda prompt: "yes") == 0
    assert extracted_calls == []
    assert extractdisc.main([str(tmp_path), "--yes"], runner=FakeRunner()) == 0
    assert extracted_calls == [(iso, tmp_path / "disc.extracted", False)]


def test_blocked_jobs_are_reported_but_do_not_stop_runnable_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    iso = tmp_path / "disc.iso"
    orphan = tmp_path / "orphan.bin"
    iso.write_bytes(b"iso")
    orphan.write_bytes(b"bin")
    mount = tmp_path / "MOUNT"
    mount.mkdir()
    patch_iso_mounts(monkeypatch, {iso: mount})
    extracted_calls: list[tuple[Path, Path, bool]] = []

    def fake_extract_image_files(image: Path, output_dir: Path, overwrite: bool = False) -> int:
        extracted_calls.append((image, output_dir, overwrite))
        return 1

    monkeypatch.setattr(
        extractdisc.iso_tool,
        "extract_image_files",
        fake_extract_image_files,
    )

    status = extractdisc.main([str(tmp_path), "--yes"], runner=FakeRunner())

    assert status == 0
    assert extracted_calls == [(iso, tmp_path / "disc.extracted", False)]
    output = capsys.readouterr().out
    assert "blocked: missing companion .cue or .toc" in output
    assert "Blocked jobs:" in output
    assert f"Extracting {iso} ... 1 file" in output


def test_extraction_error_logs_cleans_output_and_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fail_dir = tmp_path / "fail"
    ok_dir = tmp_path / "ok"
    fail_dir.mkdir()
    ok_dir.mkdir()
    fail_iso = fail_dir / "fail.iso"
    ok_iso = ok_dir / "ok.iso"
    fail_iso.write_bytes(b"iso")
    ok_iso.write_bytes(b"iso")
    fail_mount = tmp_path / "FAIL_MOUNT"
    ok_mount = tmp_path / "OK_MOUNT"
    fail_mount.mkdir()
    ok_mount.mkdir()
    patch_iso_mounts(monkeypatch, {fail_iso: fail_mount, ok_iso: ok_mount})
    extracted_calls: list[Path] = []

    def fake_extract_image_files(image: Path, output_dir: Path, overwrite: bool = False) -> int:
        extracted_calls.append(image)
        if image == fail_iso:
            (output_dir / "partial.txt").write_text("partial", encoding="utf-8")
            raise extractdisc.iso_tool.ToolError("copy failed")
        (output_dir / "done.txt").write_text("done", encoding="utf-8")
        return 1

    monkeypatch.setattr(
        extractdisc.iso_tool,
        "extract_image_files",
        fake_extract_image_files,
    )

    status = extractdisc.main([str(tmp_path), "--yes"], runner=FakeRunner())

    assert status == 0
    assert extracted_calls == [fail_iso, ok_iso]
    assert not (fail_dir / "fail.extracted").exists()
    assert (ok_dir / "ok.extracted" / "done.txt").exists()
    captured = capsys.readouterr()
    assert f"Extracting {fail_iso} ... failed" in captured.out
    assert f"Extracting {ok_iso} ... 1 file" in captured.out
    assert f"ERROR: extraction failed for {fail_iso}: copy failed" in captured.err
    assert f"Removed partial output directory: {fail_dir / 'fail.extracted'}" in captured.err


def test_iso_data_delegates_and_dvd_video_uses_makemkv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data_dir = tmp_path / "data"
    dvd_dir = tmp_path / "dvd"
    data_dir.mkdir()
    dvd_dir.mkdir()
    data_iso = data_dir / "data.iso"
    dvd_iso = dvd_dir / "movie.iso"
    data_iso.write_bytes(b"iso")
    dvd_iso.write_bytes(b"iso")
    data_mount = tmp_path / "DATA_MOUNT"
    dvd_mount = tmp_path / "DVD_MOUNT"
    data_mount.mkdir()
    (dvd_mount / "VIDEO_TS").mkdir(parents=True)
    (dvd_mount / "VIDEO_TS" / "VIDEO_TS.IFO").write_text("", encoding="utf-8")
    patch_iso_mounts(monkeypatch, {data_iso: data_mount, dvd_iso: dvd_mount})
    extracted_calls: list[tuple[Path, Path, bool]] = []

    def fake_extract_image_files(image: Path, output_dir: Path, overwrite: bool = False) -> int:
        extracted_calls.append((image, output_dir, overwrite))
        return 4

    monkeypatch.setattr(
        extractdisc.iso_tool,
        "extract_image_files",
        fake_extract_image_files,
    )

    runner = FakeRunner(available={"hdiutil", "makemkvcon"}, makemkv_info_output=MAKEMKV_TWO_TITLE_OUTPUT)

    status = extractdisc.main([str(tmp_path), "--yes"], runner=runner)

    assert status == 0
    assert extracted_calls == [(data_iso, data_dir / "data.extracted", False)]
    commands = [call[0] for call in runner.calls]
    assert [
        "makemkvcon",
        "-r",
        "info",
        f"iso:{dvd_iso.resolve()}",
    ] in commands
    assert [
        "makemkvcon",
        "-r",
        "mkv",
        f"iso:{dvd_iso.resolve()}",
        "0",
        str((dvd_dir / "movie.extracted").resolve()),
    ] in commands
    assert [
        "makemkvcon",
        "-r",
        "mkv",
        f"iso:{dvd_iso.resolve()}",
        "1",
        str((dvd_dir / "movie.extracted").resolve()),
    ] in commands
    output = capsys.readouterr().out
    assert "DVD" in output
    assert "video" in output
    assert "extract MKV titles" in output
    assert "title 0 -> title_t00.mkv" in output
    assert "title 1 -> title_t01.mkv" in output
    assert f"Extracting {data_iso} ... 4 files" in output
    assert f"Extracting {dvd_iso} ..." in output


def test_dvd_video_plan_only_does_not_scan_titles_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    iso = tmp_path / "movie.iso"
    iso.write_bytes(b"iso")
    mount = tmp_path / "DVD_MOUNT"
    (mount / "VIDEO_TS").mkdir(parents=True)
    (mount / "VIDEO_TS" / "VIDEO_TS.IFO").write_text("", encoding="utf-8")
    patch_iso_mounts(monkeypatch, {iso: mount})
    runner = FakeRunner(available={"hdiutil", "makemkvcon"}, makemkv_info_output=MAKEMKV_TWO_TITLE_OUTPUT)

    status = extractdisc.main([str(tmp_path), "--plan-only"], runner=runner)

    assert status == 0
    commands = [call[0] for call in runner.calls]
    assert not any(command[:3] == ["makemkvcon", "-r", "info"] for command in commands)
    assert not any(command[:3] == ["makemkvcon", "-r", "mkv"] for command in commands)
    output = capsys.readouterr().out
    assert "DVD" in output
    assert "video" in output
    assert "extract MKV titles" in output
    assert "DVD titles:" not in output


def test_dvd_video_scan_flag_plans_titles_without_extracting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    iso = tmp_path / "movie.iso"
    iso.write_bytes(b"iso")
    mount = tmp_path / "DVD_MOUNT"
    (mount / "VIDEO_TS").mkdir(parents=True)
    (mount / "VIDEO_TS" / "VIDEO_TS.IFO").write_text("", encoding="utf-8")
    patch_iso_mounts(monkeypatch, {iso: mount})
    runner = FakeRunner(available={"hdiutil", "makemkvcon"}, makemkv_info_output=MAKEMKV_TWO_TITLE_OUTPUT)

    status = extractdisc.main([str(tmp_path), "--plan-only", "--scan-dvd-titles"], runner=runner)

    assert status == 0
    commands = [call[0] for call in runner.calls]
    assert [
        "makemkvcon",
        "-r",
        "info",
        f"iso:{iso.resolve()}",
    ] in commands
    assert not any(command[:3] == ["makemkvcon", "-r", "mkv"] for command in commands)
    output = capsys.readouterr().out
    assert "DVD" in output
    assert "video" in output
    assert "extract 2 MKV title(s)" in output
    assert "DVD titles:" in output
    assert "title 0, title_t00.mkv, duration 0:42:00, size 3.8 GB, Video: Mpeg2; Audio: English AC3" in output
    assert (
        "title 1, title_t01.mkv, duration 0:21:30, size 1.9 GB, "
        "Video: Mpeg2; Audio: English AC3; Subtitles: English"
    ) in output


def test_dvd_video_yes_runs_one_makemkv_command_per_title(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    iso = tmp_path / "movie.iso"
    iso.write_bytes(b"iso")
    mount = tmp_path / "DVD_MOUNT"
    (mount / "VIDEO_TS").mkdir(parents=True)
    (mount / "VIDEO_TS" / "VIDEO_TS.IFO").write_text("", encoding="utf-8")
    patch_iso_mounts(monkeypatch, {iso: mount})
    runner = FakeRunner(available={"hdiutil", "makemkvcon"}, makemkv_info_output=MAKEMKV_TWO_TITLE_OUTPUT)

    status = extractdisc.main([str(tmp_path), "--yes"], runner=runner)

    assert status == 0
    mkv_commands = [call[0] for call in runner.calls if call[0][:3] == ["makemkvcon", "-r", "mkv"]]
    assert mkv_commands == [
        ["makemkvcon", "-r", "mkv", f"iso:{iso.resolve()}", "0", str((tmp_path / "movie.extracted").resolve())],
        ["makemkvcon", "-r", "mkv", f"iso:{iso.resolve()}", "1", str((tmp_path / "movie.extracted").resolve())],
    ]
    output = capsys.readouterr().out
    assert "scanning DVD titles ... 2 files" in output
    assert "title 0 -> title_t00.mkv ... done" in output
    assert "title 1 -> title_t01.mkv ... done" in output
    assert "2 files" in output


def test_dvd_video_makemkv_zero_exit_failure_logs_reason_and_cleans_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    iso = tmp_path / "movie.iso"
    iso.write_bytes(b"iso")
    mount = tmp_path / "DVD_MOUNT"
    (mount / "VIDEO_TS").mkdir(parents=True)
    (mount / "VIDEO_TS" / "VIDEO_TS.IFO").write_text("", encoding="utf-8")
    patch_iso_mounts(monkeypatch, {iso: mount})
    runner = FakeRunner(
        available={"hdiutil", "makemkvcon"},
        makemkv_info_output=MAKEMKV_TWO_TITLE_OUTPUT,
        makemkv_mkv_output=(
            b'MSG:1002,32,1,"LIBMKV_TRACE: Exception: Error while reading input","LIBMKV_TRACE: %1",'
            b'"Exception: Error while reading input"\n'
            b'MSG:5003,0,2,"Failed to save title 0 to file /tmp/title_t00.mkv","Failed to save title %1 to file %2",'
            b'"0","/tmp/title_t00.mkv"\n'
            b'MSG:5004,128,2,"0 titles saved, 1 failed","%1 titles saved, %2 failed","0","1"\n'
        ),
    )

    status = extractdisc.main([str(tmp_path), "--yes"], runner=runner)

    assert status == 0
    assert not (tmp_path / "movie.extracted").exists()
    captured = capsys.readouterr()
    assert "title 0 -> title_t00.mkv ... failed" in captured.out
    assert "LIBMKV_TRACE: Exception: Error while reading input" in captured.err
    assert "Failed to save title 0 to file /tmp/title_t00.mkv" in captured.err
    assert "0 titles saved, 1 failed" in captured.err
    assert f"Removed partial output directory: {tmp_path / 'movie.extracted'}" in captured.err


def test_makemkv_title_filename_falls_back_to_title_id() -> None:
    titles = extractdisc.parse_makemkv_titles('TINFO:7,9,0,"0:01:00"\n')

    assert len(titles) == 1
    assert titles[0].filename == "title_t07.mkv"


@pytest.mark.parametrize(
    ("runner", "blocked_text"),
    [
        (FakeRunner(available={"hdiutil"}), "missing tool: makemkvcon"),
    ],
)
def test_dvd_video_makemkv_missing_blocks_before_prompting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    runner: FakeRunner,
    blocked_text: str,
) -> None:
    iso = tmp_path / "movie.iso"
    iso.write_bytes(b"iso")
    mount = tmp_path / "DVD_MOUNT"
    (mount / "VIDEO_TS").mkdir(parents=True)
    (mount / "VIDEO_TS" / "VIDEO_TS.IFO").write_text("", encoding="utf-8")
    patch_iso_mounts(monkeypatch, {iso: mount})

    def fail_if_prompted(prompt: str) -> str:
        raise AssertionError(f"unexpected prompt: {prompt}")

    status = extractdisc.main([str(tmp_path)], runner=runner, input_func=fail_if_prompted)

    assert status == 0
    output = capsys.readouterr().out
    assert blocked_text in output
    assert "Blocked jobs:" in output
    assert "No runnable extraction jobs." in output
    assert not (tmp_path / "movie.extracted").exists()


@pytest.mark.parametrize(
    ("runner", "blocked_text"),
    [
        (
            FakeRunner(available={"hdiutil", "makemkvcon"}, makemkv_info_returncode=1),
            "MakeMKV scan failed",
        ),
        (
            FakeRunner(available={"hdiutil", "makemkvcon"}, makemkv_info_output=b"TCOUNT:0\n"),
            "MakeMKV found no titles",
        ),
    ],
)
def test_dvd_video_scan_flag_blocks_before_prompting_on_scan_problems(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    runner: FakeRunner,
    blocked_text: str,
) -> None:
    iso = tmp_path / "movie.iso"
    iso.write_bytes(b"iso")
    mount = tmp_path / "DVD_MOUNT"
    (mount / "VIDEO_TS").mkdir(parents=True)
    (mount / "VIDEO_TS" / "VIDEO_TS.IFO").write_text("", encoding="utf-8")
    patch_iso_mounts(monkeypatch, {iso: mount})

    def fail_if_prompted(prompt: str) -> str:
        raise AssertionError(f"unexpected prompt: {prompt}")

    status = extractdisc.main(
        [str(tmp_path), "--scan-dvd-titles"],
        runner=runner,
        input_func=fail_if_prompted,
    )

    assert status == 0
    output = capsys.readouterr().out
    assert blocked_text in output
    assert "Blocked jobs:" in output
    assert "No runnable extraction jobs." in output
    assert not (tmp_path / "movie.extracted").exists()


def test_legacy_extracted_directory_skips_dvd_after_media_detection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    iso = tmp_path / "movie.iso"
    iso.write_bytes(b"iso")
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    (extracted / "already.mkv").write_bytes(b"mkv")
    mount = tmp_path / "DVD_MOUNT"
    (mount / "VIDEO_TS").mkdir(parents=True)
    (mount / "VIDEO_TS" / "VIDEO_TS.IFO").write_text("", encoding="utf-8")
    patch_iso_mounts(monkeypatch, {iso: mount})
    runner = FakeRunner(available={"hdiutil", "makemkvcon"})

    status = extractdisc.main([str(tmp_path), "--yes"], runner=runner)

    assert status == 0
    assert runner.calls == []
    output = capsys.readouterr().out
    assert "DVD" in output
    assert "video" in output
    assert "skip: legacy extracted/ already exists; assuming already extracted" in output
    assert "No runnable extraction jobs." in output


def test_cd_audio_bin_is_skipped(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bin_file = tmp_path / "audio.bin"
    cue = tmp_path / "audio.cue"
    bin_file.write_bytes(b"bin")
    cue.write_text('FILE "audio.bin" BINARY\n  TRACK 01 AUDIO\n', encoding="utf-8")

    status = extractdisc.main(
        [str(tmp_path), "--yes"],
        runner=FakeRunner(available={"vcd-info"}, vcd_info_output=b""),
    )

    assert status == 0
    output = capsys.readouterr().out
    audio_line = next(line for line in output.splitlines() if line.startswith("audio.bin"))
    assert "audio.bin" in audio_line
    assert "CD" in audio_line
    assert "AUDIO" in audio_line
    assert audio_line.index("CD") < audio_line.index("AUDIO")
    assert "skip: CD audio extraction is out of scope" in output


def test_force_cd_data_iso_skips_mount_probe_and_extracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    iso = tmp_path / "disc.iso"
    iso.write_bytes(b"iso")
    extracted_calls: list[tuple[Path, Path, bool]] = []

    def fail_if_mounted(image: Path):  # type: ignore[no-untyped-def]
        raise AssertionError(f"unexpected mount: {image}")

    def fake_extract_image_files(image: Path, output_dir: Path, overwrite: bool = False) -> int:
        extracted_calls.append((image, output_dir, overwrite))
        return 1

    monkeypatch.setattr(extractdisc.iso_tool, "attached_image", fail_if_mounted)
    monkeypatch.setattr(extractdisc.iso_tool, "extract_image_files", fake_extract_image_files)

    status = extractdisc.main(
        [str(tmp_path), "--yes", "--force-media-type", "cd-data"],
        runner=FakeRunner(available={"hdiutil"}),
    )

    assert status == 0
    assert extracted_calls == [(iso, tmp_path / "disc.extracted", False)]


def test_force_cd_vcd_bin_skips_vcd_info_and_uses_vcdxrip(tmp_path: Path) -> None:
    bin_file = tmp_path / "movie.bin"
    cue = tmp_path / "movie.cue"
    bin_file.write_bytes(b"bin")
    cue.write_text('FILE "movie.bin" BINARY\n  TRACK 01 MODE2/2352\n', encoding="utf-8")
    runner = FakeRunner(available={"vcdxrip"})

    status = extractdisc.main(
        [str(tmp_path), "--yes", "--force-media-type", "cd-vcd"],
        runner=runner,
    )

    assert status == 0
    commands = [call[0] for call in runner.calls]
    assert not any(command[0] == "vcd-info" for command in commands)
    assert [
        "vcdxrip",
        "--cue-file",
        str(cue.resolve()),
        "--output-file",
        "movie.vcd.xml",
    ] in commands


def test_force_cd_audio_bin_uses_cd_audio_media_type(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bin_file = tmp_path / "audio.bin"
    bin_file.write_bytes(b"bin")

    status = extractdisc.main(
        [str(tmp_path), "--yes", "--force-media-type", "cd-audio"],
        runner=FakeRunner(available=set()),
    )

    assert status == 0
    output = capsys.readouterr().out
    audio_line = next(line for line in output.splitlines() if line.startswith("audio.bin"))
    assert "CD" in audio_line
    assert "AUDIO" in audio_line
    assert "skip: CD audio extraction is out of scope" in audio_line
    assert "No runnable extraction jobs." in output


def test_vcd_bin_cue_uses_vcd_info_and_vcdxrip_from_extracted(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bin_file = tmp_path / "movie.bin"
    cue = tmp_path / "movie.cue"
    bin_file.write_bytes(b"bin")
    cue.write_text('FILE "movie.bin" BINARY\n  TRACK 01 MODE2/2352\n', encoding="utf-8")
    runner = FakeRunner(available={"vcd-info", "vcdxrip"}, vcd_info_output=b"Video CD 2.0\n")

    status = extractdisc.main([str(tmp_path), "--yes"], runner=runner)

    assert status == 0
    commands = [call[0] for call in runner.calls]
    assert [
        "vcd-info",
        "--cue-file",
        str(cue.resolve()),
        "--show-format",
        "--no-banner",
        "--no-header",
        "--no-delimiter",
    ] in commands
    assert [
        "vcdxrip",
        "--cue-file",
        str(cue.resolve()),
        "--output-file",
        "movie.vcd.xml",
    ] in commands
    assert runner.calls[-1][1] == tmp_path / "movie.extracted"
    assert f"Extracting {bin_file} ... 2 files" in capsys.readouterr().out


def test_existing_extracted_bin_still_reports_media_type(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bin_file = tmp_path / "movie.bin"
    cue = tmp_path / "movie.cue"
    bin_file.write_bytes(b"bin")
    cue.write_text('FILE "movie.bin" BINARY\n  TRACK 01 MODE2/2352\n', encoding="utf-8")
    extracted = tmp_path / "movie.extracted"
    extracted.mkdir()
    (extracted / "already.mpg").write_bytes(b"video")
    runner = FakeRunner(available={"vcd-info", "vcdxrip"}, vcd_info_output=b"Video CD 2.0\n")

    status = extractdisc.main([str(tmp_path), "--yes"], runner=runner)

    assert status == 0
    commands = [call[0] for call in runner.calls]
    assert any(command[0] == "vcd-info" for command in commands)
    assert not any(command[0] == "vcdxrip" for command in commands)
    output = capsys.readouterr().out
    movie_line = next(line for line in output.splitlines() if line.startswith("movie.bin"))
    assert "CD" in movie_line
    assert "VCD/SVCD" in movie_line
    assert "skip: movie.extracted/ already exists; assuming already extracted" in movie_line
    assert "No runnable extraction jobs." in output


def test_cd_data_bin_cue_uses_bchunk_then_extracts_generated_iso(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bin_file = tmp_path / "data.bin"
    cue = tmp_path / "data.cue"
    bin_file.write_bytes(b"bin")
    cue.write_text('FILE "data.bin" BINARY\n  TRACK 01 MODE1/2352\n', encoding="utf-8")
    extracted_calls: list[tuple[str, Path, bool]] = []

    def fake_extract_image_files(image: Path, output_dir: Path, overwrite: bool = False) -> int:
        extracted_calls.append((image.name, output_dir, overwrite))
        return 5

    monkeypatch.setattr(
        extractdisc.iso_tool,
        "extract_image_files",
        fake_extract_image_files,
    )
    runner = FakeRunner(available={"hdiutil", "vcd-info", "bchunk"}, vcd_info_output=b"")

    status = extractdisc.main([str(tmp_path), "--yes"], runner=runner)

    assert status == 0
    commands = [call[0] for call in runner.calls]
    assert ["bchunk", str(bin_file.resolve()), str(cue.resolve()), "data"] in commands
    assert extracted_calls == [("data01.iso", tmp_path / "data.extracted", False)]
    assert not (tmp_path / ".data.extractdisc-bchunk").exists()
    assert f"Extracting {bin_file} ... 5 files" in capsys.readouterr().out
