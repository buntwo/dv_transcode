import argparse
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dv_unpackager  # noqa: E402


class TestUnsplitOutputNaming(unittest.TestCase):
    def _make_parts(self, split_dir: Path, count: int, prefix: str = "capture") -> None:
        for n in range(1, count + 1):
            (split_dir / f"{prefix}_part{n}.dv").write_bytes(b"")

    def test_unsplit_uses_group_order_letters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base_dir = root / "Originals" / "Tape001"
            split_dir = base_dir / "split"
            split_dir.mkdir(parents=True)
            self._make_parts(split_dir, 10)

            args = argparse.Namespace(
                input_dir=str(base_dir),
                spec="1-3,4,5-9,10",
                output_dir=None,
                pattern="*_part*.dv",
                overwrite=False,
                dry_run=False,
                dvpackager_bin="dvpackager",
                originals_dirname="Originals",
                logs_dirname="Logs",
            )

            singleton_outputs: list[Path] = []
            merged_outputs: list[Path] = []

            def fake_link_or_copy(src: Path, dst: Path, overwrite: bool) -> None:
                singleton_outputs.append(dst)
                dst.write_bytes(b"")

            def fake_merge(bin_path: str, inputs: list[Path], output: Path, overwrite: bool) -> None:
                merged_outputs.append(output)
                output.write_bytes(b"")

            with (
                patch.object(dv_unpackager, "link_or_copy", side_effect=fake_link_or_copy),
                patch.object(dv_unpackager, "run_dvpackager_unpackage", side_effect=fake_merge),
            ):
                rc = dv_unpackager.run_unsplit(args)

            self.assertEqual(rc, 0)
            names = sorted([p.name for p in singleton_outputs + merged_outputs])
            self.assertEqual(
                names,
                ["capture_partA.dv", "capture_partB.dv", "capture_partC.dv", "capture_partD.dv"],
            )

    def test_unsplit_rolls_over_to_double_letters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base_dir = root / "Originals" / "Tape002"
            split_dir = base_dir / "split"
            split_dir.mkdir(parents=True)
            self._make_parts(split_dir, 27)

            spec = ",".join(str(n) for n in range(1, 28))
            args = argparse.Namespace(
                input_dir=str(base_dir),
                spec=spec,
                output_dir=None,
                pattern="*_part*.dv",
                overwrite=False,
                dry_run=False,
                dvpackager_bin="dvpackager",
                originals_dirname="Originals",
                logs_dirname="Logs",
            )

            outputs: list[Path] = []

            def fake_link_or_copy(src: Path, dst: Path, overwrite: bool) -> None:
                outputs.append(dst)
                dst.write_bytes(b"")

            with patch.object(dv_unpackager, "link_or_copy", side_effect=fake_link_or_copy):
                rc = dv_unpackager.run_unsplit(args)

            self.assertEqual(rc, 0)
            names = sorted(p.name for p in outputs)
            self.assertEqual(names[0], "capture_partA.dv")
            self.assertIn("capture_partZ.dv", names)
            self.assertIn("capture_partAA.dv", names)


class TestIndexToLetters(unittest.TestCase):
    def test_index_to_letters_values(self) -> None:
        self.assertEqual(dv_unpackager.index_to_letters(1), "A")
        self.assertEqual(dv_unpackager.index_to_letters(26), "Z")
        self.assertEqual(dv_unpackager.index_to_letters(27), "AA")
        self.assertEqual(dv_unpackager.index_to_letters(52), "AZ")

    def test_index_to_letters_rejects_non_positive(self) -> None:
        with self.assertRaises(ValueError):
            dv_unpackager.index_to_letters(0)


class TestSplitFlags(unittest.TestCase):
    def test_split_accepts_t_as_a_segmentation_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dv = root / "Originals" / "Tape003" / "capture.dv"
            input_dv.parent.mkdir(parents=True)
            input_dv.write_bytes(b"")

            args = argparse.Namespace(
                input_dv=str(input_dv),
                s=False,
                d=False,
                t=True,
                output_dir=None,
                overwrite=False,
                dry_run=False,
                dvpackager_bin="dvpackager",
                originals_dirname="Originals",
                logs_dirname="Logs",
            )

            seen_cmds: list[list[str]] = []

            def fake_run(cmd: list[str], check: bool) -> None:
                seen_cmds.append(cmd)

            with patch.object(dv_unpackager.subprocess, "run", side_effect=fake_run):
                rc = dv_unpackager.run_split(args)

            self.assertEqual(rc, 0)
            self.assertEqual(len(seen_cmds), 1)
            self.assertEqual(seen_cmds[0][:2], ["dvpackager", "-t"])

    def test_split_defaults_to_all_segmentation_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dv = root / "Originals" / "Tape004" / "capture.dv"
            input_dv.parent.mkdir(parents=True)
            input_dv.write_bytes(b"")

            args = argparse.Namespace(
                input_dv=str(input_dv),
                s=False,
                d=False,
                t=False,
                output_dir=None,
                overwrite=False,
                dry_run=False,
                dvpackager_bin="dvpackager",
                originals_dirname="Originals",
                logs_dirname="Logs",
            )

            seen_cmds: list[list[str]] = []

            def fake_run(cmd: list[str], check: bool) -> None:
                seen_cmds.append(cmd)

            with patch.object(dv_unpackager.subprocess, "run", side_effect=fake_run):
                rc = dv_unpackager.run_split(args)

            self.assertEqual(rc, 0)
            self.assertEqual(len(seen_cmds), 1)
            self.assertEqual(seen_cmds[0][:4], ["dvpackager", "-s", "-d", "-t"])


class TestSplitUnsplitCli(unittest.TestCase):
    def test_parse_split_unsplit_accepts_shared_options(self) -> None:
        args = dv_unpackager.parse_args(
            [
                "split-unsplit",
                "input.dv",
                "1-3,4",
                "-s",
                "-t",
                "--output-dir",
                "custom-split",
                "--pattern",
                "capture_part*.dv",
                "--overwrite",
                "--dry-run",
            ]
        )

        self.assertEqual(args.command, "split-unsplit")
        self.assertEqual(args.input_dv, "input.dv")
        self.assertEqual(args.spec, "1-3,4")
        self.assertTrue(args.s)
        self.assertFalse(args.d)
        self.assertTrue(args.t)
        self.assertEqual(args.output_dir, "custom-split")
        self.assertEqual(args.pattern, "capture_part*.dv")
        self.assertTrue(args.overwrite)
        self.assertTrue(args.dry_run)


class TestSplitUnsplitOrchestration(unittest.TestCase):
    def test_split_runs_before_unsplit_and_forwards_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dv = root / "Originals" / "Tape005" / "capture.dv"
            input_dv.parent.mkdir(parents=True)
            input_dv.write_bytes(b"")

            args = argparse.Namespace(
                input_dv=str(input_dv),
                spec="1-3,4",
                s=False,
                d=False,
                t=True,
                output_dir=None,
                pattern="capture_part*.dv",
                overwrite=True,
                dry_run=False,
                dvpackager_bin="dvpackager",
                originals_dirname="Originals",
                logs_dirname="Logs",
            )

            seen: list[tuple[str, argparse.Namespace]] = []

            def fake_run_split(split_args: argparse.Namespace) -> int:
                seen.append(("split", split_args))
                return 0

            def fake_run_unsplit(unsplit_args: argparse.Namespace) -> int:
                seen.append(("unsplit", unsplit_args))
                return 0

            with (
                patch.object(dv_unpackager, "run_split", side_effect=fake_run_split),
                patch.object(dv_unpackager, "run_unsplit", side_effect=fake_run_unsplit),
            ):
                rc = dv_unpackager.run_split_unsplit(args)

            self.assertEqual(rc, 0)
            self.assertEqual([name for name, _ in seen], ["split", "unsplit"])
            self.assertEqual(seen[0][1].input_dv, str(input_dv))
            self.assertTrue(seen[0][1].t)
            self.assertTrue(seen[0][1].overwrite)
            self.assertEqual(seen[1][1].input_dir, str(input_dv.parent.resolve()))
            self.assertEqual(seen[1][1].spec, "1-3,4")
            self.assertEqual(seen[1][1].pattern, "capture_part*.dv")
            self.assertIsNone(seen[1][1].output_dir)
            self.assertTrue(seen[1][1].overwrite)

    def test_split_unsplit_dry_run_reaches_both_phases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dv = root / "Originals" / "Tape006" / "capture.dv"
            input_dv.parent.mkdir(parents=True)
            input_dv.write_bytes(b"")

            args = argparse.Namespace(
                input_dv=str(input_dv),
                spec="1,2",
                s=False,
                d=False,
                t=False,
                output_dir=None,
                pattern="*_part*.dv",
                overwrite=False,
                dry_run=True,
                dvpackager_bin="dvpackager",
                originals_dirname="Originals",
                logs_dirname="Logs",
            )

            seen: list[str] = []

            def fake_run_split(split_args: argparse.Namespace) -> int:
                seen.append("split")
                self.assertTrue(split_args.dry_run)
                return 0

            def fake_run_unsplit(unsplit_args: argparse.Namespace) -> int:
                seen.append("unsplit")
                self.assertTrue(unsplit_args.dry_run)
                return 0

            with (
                patch.object(dv_unpackager, "run_split", side_effect=fake_run_split),
                patch.object(dv_unpackager, "run_unsplit", side_effect=fake_run_unsplit),
            ):
                rc = dv_unpackager.run_split_unsplit(args)

            self.assertEqual(rc, 0)
            self.assertEqual(seen, ["split", "unsplit"])


if __name__ == "__main__":
    unittest.main()
