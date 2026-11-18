"""Regression tests for `verify_package.py`.

These build a small but structurally real prepared tree and a ZIP over it, then
prove the checker accepts it and rejects each way it can go wrong: a missing
marker, the wrong version, an archive whose entries are not under the top folder,
an archive whose root does not hold the marker, and an unsafe `..` entry.
"""

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.tests import _path  # noqa: F401 - puts `tools` on sys.path
import verify_package

VERSION = "26.3"
SCHEMA = 7


def build_tree(root: Path, version: str = VERSION, schema: int = SCHEMA) -> Path:
    resources = root / "horizon-resources"
    (resources / "assets" / "minecraft").mkdir(parents=True)
    (resources / "assets" / "minecraft" / "sounds.json").write_text("{}")
    (resources / "assets" / "pack.mcmeta").write_text("{}")
    (resources / "assets" / "minecraft" / "font").mkdir(parents=True)
    (resources / "assets" / "minecraft" / "font" / "unifont.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    (resources / "asset-index.json").write_text("{}")
    (resources / "horizon-resources.json").write_text(
        json.dumps({"schema": schema, "minecraft": version})
    )
    return resources


def build_archive(root: Path, resources: Path, name: str = "pack.zip") -> Path:
    archive = root / name
    top = resources.name
    with zipfile.ZipFile(archive, "w") as zf:
        for path in sorted(resources.rglob("*")):
            if path.is_file():
                zf.write(path, f"{top}/{path.relative_to(resources).as_posix()}")
    return archive


class Tree(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_complete_tree_passes(self):
        result = verify_package.check_tree(build_tree(self.root), VERSION, SCHEMA)
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["minecraft"], VERSION)

    def test_missing_marker_fails(self):
        resources = build_tree(self.root)
        (resources / "horizon-resources.json").unlink()
        result = verify_package.check_tree(resources, VERSION, SCHEMA)
        self.assertFalse(result["ok"])
        self.assertTrue(any("horizon-resources.json" in p for p in result["problems"]))

    def test_wrong_version_fails(self):
        result = verify_package.check_tree(build_tree(self.root, version="26.2"), VERSION, SCHEMA)
        self.assertFalse(result["ok"])

    def test_wrong_schema_fails(self):
        result = verify_package.check_tree(build_tree(self.root, schema=6), VERSION, SCHEMA)
        self.assertFalse(result["ok"])

    def test_missing_sounds_json_fails(self):
        resources = build_tree(self.root)
        (resources / "assets" / "minecraft" / "sounds.json").unlink()
        result = verify_package.check_tree(resources, VERSION, SCHEMA)
        self.assertFalse(result["ok"])

    def test_small_tree_warns(self):
        result = verify_package.check_tree(build_tree(self.root), VERSION, SCHEMA)
        self.assertTrue(any("below" in w for w in result["warnings"]))


class Archive(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.resources = build_tree(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_well_formed_archive_passes(self):
        archive = build_archive(self.root, self.resources)
        result = verify_package.check_archive(archive, self.resources)
        self.assertTrue(result["ok"], result["problems"])
        self.assertGreater(result["entries"], 0)

    def test_missing_archive_fails(self):
        result = verify_package.check_archive(self.root / "nope.zip", self.resources)
        self.assertFalse(result["ok"])

    def test_entry_outside_the_top_folder_fails(self):
        archive = self.root / "bad.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("elsewhere/file.txt", "x")
        result = verify_package.check_archive(archive, self.resources)
        self.assertFalse(result["ok"])
        self.assertTrue(any("top folder" in p for p in result["problems"]))

    def test_root_without_marker_fails(self):
        archive = self.root / "nomarker.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr(f"{self.resources.name}/assets/file.txt", "x")
        result = verify_package.check_archive(archive, self.resources)
        self.assertFalse(result["ok"])
        self.assertTrue(any("does not hold" in p for p in result["problems"]))

    def test_dot_dot_entry_fails(self):
        archive = self.root / "unsafe.zip"
        top = self.resources.name
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr(f"{top}/horizon-resources.json", "{}")
            zf.writestr(f"{top}/../escape.txt", "x")
        result = verify_package.check_archive(archive, self.resources)
        self.assertFalse(result["ok"])


class Shaders(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_absent_shaderpacks_is_not_a_problem(self):
        result = verify_package.check_shaders(self.root)
        self.assertTrue(result["ok"])
        self.assertFalse(result["present"])

    def test_a_valid_pack_passes(self):
        pack = self.root / "shaderpacks" / "good" / "shaders"
        pack.mkdir(parents=True)
        result = verify_package.check_shaders(self.root)
        self.assertTrue(result["ok"])

    def test_a_pack_without_shaders_fails(self):
        (self.root / "shaderpacks" / "broken").mkdir(parents=True)
        result = verify_package.check_shaders(self.root)
        self.assertFalse(result["ok"])

    def test_a_zip_pack_is_opened(self):
        zip_path = self.root / "shaderpacks" / "pack.zip"
        zip_path.parent.mkdir(parents=True)
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("shaders/composite.fsh", "void main(){}")
        result = verify_package.check_shaders(self.root)
        self.assertTrue(result["ok"])


class Main(unittest.TestCase):
    def test_end_to_end_passes_and_writes_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            resources = build_tree(root)
            archive = build_archive(root, resources)
            report = root / "package-report.json"
            argv = ["verify_package.py", "--resources", str(resources), "--archive", str(archive),
                    "--version", VERSION, "--schema", str(SCHEMA), "--report", str(report),
                    "--allow-warnings"]
            saved = sys.argv
            sys.argv = argv
            try:
                status = verify_package.main()
            finally:
                sys.argv = saved
            self.assertEqual(status, 0)
            self.assertTrue(json.loads(report.read_text())["ok"])

    def test_warnings_fail_without_allow_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            resources = build_tree(root)
            archive = build_archive(root, resources)
            report = root / "package-report.json"
            argv = ["verify_package.py", "--resources", str(resources), "--archive", str(archive),
                    "--version", VERSION, "--schema", str(SCHEMA), "--report", str(report)]
            saved = sys.argv
            sys.argv = argv
            try:
                status = verify_package.main()
            finally:
                sys.argv = saved
            # The tree is far below the size floor, so it warns; without --allow-warnings
            # that must fail the run rather than slip through.
            self.assertNotEqual(status, 0)


if __name__ == "__main__":
    unittest.main()
