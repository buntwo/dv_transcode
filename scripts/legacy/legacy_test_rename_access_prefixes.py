import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import rename_access_prefixes  # noqa: E402


class TestRenameAccessPrefixes(unittest.TestCase):
    def test_compute_target_from_access_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            access_root = root / "Access"
            output_path = access_root / "Set 1" / "1 Disney" / "20011125_Set_1_1_Disney_out.mp4"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"video")

            plan = rename_access_prefixes.build_rename_plan(output_path, access_root)

            self.assertEqual(plan.source, output_path)
            self.assertEqual(plan.target.name, "20011125_Set_1_1_out.mp4")

    def test_conflict_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            access_root = root / "Access"
            conflict_output = root / "Access" / "Set 1" / "1 Disney" / "Set_1_1_Disney_out_part1.mp4"
            conflict_target = root / "Access" / "Set 1" / "1 Disney" / "Set_1_1_out_part1.mp4"
            conflict_output.parent.mkdir(parents=True, exist_ok=True)
            conflict_output.write_bytes(b"old")
            conflict_target.write_bytes(b"new")

            totals = rename_access_prefixes.Totals()
            rename_access_prefixes.process_access_dir(
                root, access_root, Path("Set 1") / "1 Disney", apply=False, verbose=False, totals=totals
            )

            self.assertEqual(totals.conflicts, 1)
            self.assertEqual(totals.planned, 0)

    def test_apply_renames_mp4_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            access_root = root / "Access"
            access_dir = root / "Access" / "Set 1" / "1 Disney"
            source = access_dir / "Set_1_1_Disney_out.mp4"
            other = access_dir / "Set_1_1_Disney_out.txt"
            access_dir.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"video")
            other.write_text("sidecar", encoding="utf-8")

            totals = rename_access_prefixes.Totals()
            rename_access_prefixes.process_access_dir(
                root, access_root, Path("Set 1") / "1 Disney", apply=True, verbose=False, totals=totals
            )

            self.assertFalse(source.exists())
            self.assertTrue((access_dir / "Set_1_1_out.mp4").exists())
            self.assertTrue(other.exists())
            self.assertEqual(totals.renamed, 1)

    def test_already_renamed_part_letters_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            access_root = root / "Access"
            access_dir = access_root / "Set 1" / "2 Disney + Brian birthday + piano"
            source = access_dir / "20011125_Set_1_2_Disney_+_Brian_birthday_+_piano_out_partA.mp4"
            access_dir.mkdir(parents=True, exist_ok=True)
            source.write_bytes(b"video")

            plan = rename_access_prefixes.build_rename_plan(source, access_root)

            self.assertEqual(plan.target.name, "20011125_Set_1_2_out_partA.mp4")


if __name__ == "__main__":
    unittest.main()
