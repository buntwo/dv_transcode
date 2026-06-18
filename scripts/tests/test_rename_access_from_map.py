from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from contextlib import redirect_stdout
from pathlib import Path

import rename_access_from_map


def write_map(path: Path, rows: list[str]) -> None:
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


class TestRenameAccessFromMap(unittest.TestCase):
    def run_cli(
        self,
        file_dir: Path,
        map_file: Path,
        apply: bool = False,
        reverse: bool = False,
        extra_args: list[str] | None = None,
    ) -> tuple[int, str, str]:
        argv = ["--file-dir", str(file_dir), "--map-file", str(map_file)]
        if apply:
            argv.append("--apply")
        if reverse:
            argv.append("--reverse")
        if extra_args:
            argv.extend(extra_args)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = rename_access_from_map.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_basic_dry_run_leaves_file_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "08.mp4"
            map_file = root / "map.csv"
            source.write_bytes(b"video")
            write_map(map_file, ["original_stem,renamed_stem", "08,Birthday Party"])

            code, stdout, stderr = self.run_cli(root, map_file)

            self.assertEqual(code, 0)
            self.assertIn("PLAN 08.mp4 -> 01 Birthday Party.mp4", stdout)
            self.assertEqual(stderr, "")
            self.assertTrue(source.exists())
            self.assertFalse((root / "01 Birthday Party.mp4").exists())

    def test_apply_preserves_extension_case_and_ignores_extra_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "08.MP4"
            map_file = root / "map.csv"
            source.write_bytes(b"video")
            write_map(map_file, ["original_stem,ignored,renamed_stem", "08,whatever,Birthday Party"])

            code, stdout, stderr = self.run_cli(root, map_file, apply=True)

            self.assertEqual(code, 0)
            self.assertIn("RENAME 08.MP4 -> 01 Birthday Party.MP4", stdout)
            self.assertEqual(stderr, "")
            self.assertFalse(source.exists())
            self.assertTrue((root / "01 Birthday Party.MP4").exists())

    def test_apply_preserves_mov_extension(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "08.mov"
            map_file = root / "map.csv"
            source.write_bytes(b"video")
            write_map(map_file, ["original_stem,renamed_stem", "08,Birthday Party"])

            code, stdout, _stderr = self.run_cli(root, map_file, apply=True)

            self.assertEqual(code, 0)
            self.assertIn("RENAME 08.mov -> 01 Birthday Party.mov", stdout)
            self.assertTrue((root / "01 Birthday Party.mov").exists())

    def test_numeric_prefix_comes_from_csv_position_not_original_stem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            map_file = root / "map.csv"
            for name in ("8.mp4", "08.mov", "001.MP4"):
                (root / name).write_bytes(b"video")
            write_map(
                map_file,
                [
                    "original_stem,renamed_stem",
                    "8,Eight",
                    "08,Zero Eight",
                    "001,One",
                ],
            )

            code, stdout, stderr = self.run_cli(root, map_file)

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("PLAN 8.mp4 -> 01 Eight.mp4", stdout)
            self.assertIn("PLAN 08.mov -> 02 Zero Eight.mov", stdout)
            self.assertIn("PLAN 001.MP4 -> 03 One.MP4", stdout)

    def test_missing_required_columns_return_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            map_file = root / "map.csv"
            write_map(map_file, ["original_stem,title", "08,Birthday Party"])

            code, _stdout, stderr = self.run_cli(root, map_file)

            self.assertEqual(code, 1)
            self.assertIn("Missing required CSV column(s): renamed_stem", stderr)

    def test_bad_rows_and_duplicate_original_stems_return_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            map_file = root / "map.csv"
            (root / "08.mp4").write_bytes(b"video")
            write_map(
                map_file,
                [
                    "original_stem,renamed_stem",
                    " ,Blank",
                    "09, ",
                    "08,First",
                    "08,Second",
                ],
            )

            code, stdout, stderr = self.run_cli(root, map_file, apply=True)

            self.assertEqual(code, 1)
            self.assertIn("PLAN 08.mp4 -> 03 First.mp4", stdout)
            self.assertIn("original_stem is blank", stderr)
            self.assertIn("renamed_stem is blank", stderr)
            self.assertIn("duplicate original_stem: 08", stderr)
            self.assertTrue((root / "08.mp4").exists())
            self.assertFalse((root / "03 First.mp4").exists())

    def test_original_stem_can_be_full_non_numeric_file_stem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_stem = "10_Dinner_at_Tus_home_in_Shanghai_Nov._1991_Home_at_Duluth_1991"
            source = root / f"{source_stem}.mp4"
            map_file = root / "map.csv"
            source.write_bytes(b"video")
            write_map(map_file, ["original_stem,renamed_stem", f"{source_stem},Dinner at Tu's"])

            code, stdout, stderr = self.run_cli(root, map_file)

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn(f"PLAN {source.name} -> 01 Dinner at Tu's.mp4", stdout)

    def test_renamed_stem_underscores_convert_to_spaces_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "08.mp4"
            map_file = root / "map.csv"
            source.write_bytes(b"video")
            write_map(map_file, ["original_stem,renamed_stem", "08,Birthday_Party"])

            code, stdout, stderr = self.run_cli(root, map_file)

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("PLAN 08.mp4 -> 01 Birthday Party.mp4", stdout)

    def test_preserve_underscores_keeps_renamed_stem_underscores(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "08.mp4"
            map_file = root / "map.csv"
            source.write_bytes(b"video")
            write_map(map_file, ["original_stem,renamed_stem", "08,Birthday_Party"])

            code, stdout, stderr = self.run_cli(root, map_file, extra_args=["--preserve-underscores"])

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("PLAN 08.mp4 -> 01 Birthday_Party.mp4", stdout)

    def test_reverse_dry_run_maps_positioned_name_back_to_original_stem(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "01 Birthday Party.mp4"
            map_file = root / "map.csv"
            source.write_bytes(b"video")
            write_map(map_file, ["original_stem,renamed_stem", "08,Birthday_Party"])

            code, stdout, stderr = self.run_cli(root, map_file, reverse=True)

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("PLAN 01 Birthday Party.mp4 -> 08.mp4", stdout)
            self.assertTrue(source.exists())
            self.assertFalse((root / "08.mp4").exists())

    def test_reverse_apply_preserves_current_extension_case(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "01 Birthday Party.MP4"
            map_file = root / "map.csv"
            source.write_bytes(b"video")
            write_map(map_file, ["original_stem,renamed_stem", "08,Birthday Party"])

            code, stdout, stderr = self.run_cli(root, map_file, apply=True, reverse=True)

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("RENAME 01 Birthday Party.MP4 -> 08.MP4", stdout)
            self.assertFalse(source.exists())
            self.assertTrue((root / "08.MP4").exists())

    def test_reverse_preserve_underscores_matches_underscored_mapped_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "01 Birthday_Party.mov"
            map_file = root / "map.csv"
            source.write_bytes(b"video")
            write_map(map_file, ["original_stem,renamed_stem", "08,Birthday_Party"])

            code, stdout, stderr = self.run_cli(
                root,
                map_file,
                reverse=True,
                extra_args=["--preserve-underscores"],
            )

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("PLAN 01 Birthday_Party.mov -> 08.mov", stdout)

    def test_forward_apply_then_reverse_apply_restores_original_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original_a = root / "08.mp4"
            original_b = root / "10_Dinner_at_Tus_home_in_Shanghai_Nov._1991_Home_at_Duluth_1991.MP4"
            map_file = root / "map.csv"
            original_a.write_bytes(b"a")
            original_b.write_bytes(b"b")
            write_map(
                map_file,
                [
                    "original_stem,renamed_stem",
                    "08,Birthday_Party",
                    "10_Dinner_at_Tus_home_in_Shanghai_Nov._1991_Home_at_Duluth_1991,Dinner_at_Tu's",
                ],
            )

            forward_code, _forward_stdout, forward_stderr = self.run_cli(root, map_file, apply=True)
            after_forward = sorted(path.name for path in root.iterdir() if path.is_file() and path != map_file)
            reverse_code, _reverse_stdout, reverse_stderr = self.run_cli(root, map_file, apply=True, reverse=True)
            after_reverse = sorted(path.name for path in root.iterdir() if path.is_file() and path != map_file)

            self.assertEqual(forward_code, 0)
            self.assertEqual(forward_stderr, "")
            self.assertEqual(after_forward, ["01 Birthday Party.mp4", "02 Dinner at Tu's.MP4"])
            self.assertEqual(reverse_code, 0)
            self.assertEqual(reverse_stderr, "")
            self.assertEqual(
                after_reverse,
                [
                    "08.mp4",
                    "10_Dinner_at_Tus_home_in_Shanghai_Nov._1991_Home_at_Duluth_1991.MP4",
                ],
            )

    def test_missing_source_and_multiple_extension_matches_return_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            map_file = root / "map.csv"
            (root / "08.mp4").write_bytes(b"video")
            (root / "08.mov").write_bytes(b"video")
            write_map(map_file, ["original_stem,renamed_stem", "08,Birthday", "09,Missing"])

            code, _stdout, stderr = self.run_cli(root, map_file)

            self.assertEqual(code, 1)
            self.assertIn("multiple source files found for stem 08", stderr)
            self.assertIn("no source file found for stem 09", stderr)

    def test_existing_target_and_duplicate_planned_targets_return_errors_and_prevent_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            map_file = root / "map.csv"
            source_a = root / "08.mp4"
            source_b = root / "8.mov"
            existing_target = root / "01 Birthday.mp4"
            source_a.write_bytes(b"a")
            source_b.write_bytes(b"b")
            existing_target.write_bytes(b"existing")
            write_map(map_file, ["original_stem,renamed_stem", "08,Birthday", "8,Birthday"])

            code, stdout, stderr = self.run_cli(root, map_file, apply=True)

            self.assertEqual(code, 1)
            self.assertIn("PLAN 08.mp4 -> 01 Birthday.mp4", stdout)
            self.assertIn("PLAN 8.mov -> 02 Birthday.mov", stdout)
            self.assertIn("Target already exists for 08.mp4", stderr)
            self.assertTrue(source_a.exists())
            self.assertTrue(source_b.exists())

    def test_same_title_rows_get_distinct_position_prefixes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            map_file = root / "map.csv"
            (root / "8.mp4").write_bytes(b"a")
            (root / "08.mp4").write_bytes(b"b")
            write_map(map_file, ["original_stem,renamed_stem", "8,Birthday", "08,Birthday"])

            code, stdout, stderr = self.run_cli(root, map_file, apply=True)

            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            self.assertIn("RENAME 8.mp4 -> 01 Birthday.mp4", stdout)
            self.assertIn("RENAME 08.mp4 -> 02 Birthday.mp4", stdout)
            self.assertTrue((root / "01 Birthday.mp4").exists())
            self.assertTrue((root / "02 Birthday.mp4").exists())

    def test_only_direct_children_are_considered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "nested"
            nested.mkdir()
            map_file = root / "map.csv"
            (nested / "08.mp4").write_bytes(b"video")
            write_map(map_file, ["original_stem,renamed_stem", "08,Birthday"])

            code, _stdout, stderr = self.run_cli(root, map_file)

            self.assertEqual(code, 1)
            self.assertIn("no source file found for stem 08", stderr)


if __name__ == "__main__":
    unittest.main()
