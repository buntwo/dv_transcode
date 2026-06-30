from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from iso_tool import (
    ToolError,
    copy_contents,
    mounted_path_is_video_dvd,
    parse_attached_dev_entries,
    parse_attached_volumes,
    parse_diskutil_mount_points,
    parse_diskutil_partition_devices,
)


def test_parse_attached_volumes_uses_mountable_entities() -> None:
    plist_bytes = plistlib.dumps(
        {
            "system-entities": [
                {"dev-entry": "/dev/disk5"},
                {
                    "dev-entry": "/dev/disk5s1",
                    "mount-point": "/Volumes/09 26 2017",
                    "volume-kind": "udf",
                },
            ]
        }
    )

    volumes = parse_attached_volumes(plist_bytes)

    assert len(volumes) == 1
    assert volumes[0].dev_entry == "/dev/disk5s1"
    assert volumes[0].mount_point == Path("/Volumes/09 26 2017")
    assert volumes[0].volume_kind == "udf"


def test_parse_attached_dev_entries_keeps_unmounted_devices() -> None:
    plist_bytes = plistlib.dumps(
        {
            "system-entities": [
                {"dev-entry": "/dev/disk5"},
                {"dev-entry": "/dev/disk5s1", "mount-point": "/Volumes/Disc"},
                {"dev-entry": "/dev/disk5"},
            ]
        }
    )

    assert parse_attached_dev_entries(plist_bytes) == ["/dev/disk5", "/dev/disk5s1"]


def test_parse_diskutil_mount_points_reads_device_and_partition_mounts() -> None:
    plist_bytes = plistlib.dumps(
        {
            "MountPoint": "/Volumes/WHOLE",
            "AllDisksAndPartitions": [
                {
                    "Partitions": [
                        {"DeviceIdentifier": "disk4s0", "MountPoint": "/Volumes/DATA"},
                        {"DeviceIdentifier": "disk4s1"},
                    ]
                }
            ],
        }
    )

    assert parse_diskutil_mount_points(plist_bytes) == [Path("/Volumes/WHOLE"), Path("/Volumes/DATA")]


def test_parse_diskutil_partition_devices_reads_all_partitions() -> None:
    plist_bytes = plistlib.dumps(
        {
            "AllDisksAndPartitions": [
                {
                    "Partitions": [
                        {"DeviceIdentifier": "disk4s0"},
                        {"DeviceIdentifier": "disk4s1"},
                        {},
                    ]
                }
            ]
        }
    )

    assert parse_diskutil_partition_devices(plist_bytes) == ["/dev/disk4s0", "/dev/disk4s1"]


def test_mounted_path_is_video_dvd_requires_video_ts_ifo(tmp_path: Path) -> None:
    video_ts = tmp_path / "video_ts"
    video_ts.mkdir()
    (video_ts / "video_ts.ifo").write_bytes(b"")

    assert mounted_path_is_video_dvd(tmp_path)


def test_mounted_path_is_video_dvd_rejects_photo_disc(tmp_path: Path) -> None:
    (tmp_path / "photos").mkdir()

    assert not mounted_path_is_video_dvd(tmp_path)


def test_copy_contents_refuses_overwrite_by_default(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "photo.jpg").write_text("new", encoding="utf-8")
    (target / "photo.jpg").write_text("old", encoding="utf-8")

    with pytest.raises(ToolError, match="Refusing to overwrite"):
        copy_contents(source, target, overwrite=False)

    assert (target / "photo.jpg").read_text(encoding="utf-8") == "old"


def test_copy_contents_allows_overwrite_when_requested(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "photo.jpg").write_text("new", encoding="utf-8")
    (target / "photo.jpg").write_text("old", encoding="utf-8")

    copy_contents(source, target, overwrite=True)

    assert (target / "photo.jpg").read_text(encoding="utf-8") == "new"
