"""Regression tests for `verify_package.py`.

These build a small but structurally real prepared tree and a ZIP over it, then
prove the checker accepts it and rejects each way it can go wrong: a missing
marker, the wrong version, an archive whose entries are not under the top folder,
an archive whose root does not hold the marker, and an unsafe `..` entry.
"""

import hashlib
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


def build_archive(root: Path, resources: Path, name: str = "pack.zip",
                  prefix: str | None = None) -> Path:
    """A zip of every file in `resources`, under `prefix` (the tree's own name by default)."""
    archive = root / name
    top = resources.name if prefix is None else prefix
    with zipfile.ZipFile(archive, "w") as zf:
        for path in sorted(resources.rglob("*")):
            if path.is_file():
                relative = path.relative_to(resources).as_posix()
                zf.write(path, f"{top}/{relative}" if top else relative)
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
        self.assertTrue(any("no horizon-resources.json" in p for p in result["problems"]))

    def test_root_without_marker_fails(self):
        archive = self.root / "nomarker.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr(f"{self.resources.name}/assets/file.txt", "x")
        result = verify_package.check_archive(archive, self.resources)
        self.assertFalse(result["ok"])
        self.assertTrue(any("no horizon-resources.json" in p for p in result["problems"]))

    def test_a_stray_entry_beside_the_tree_fails(self):
        # A marker under one folder, but a file that escapes it: the game reads the
        # tree from the marker's level, so the stray is not part of the package.
        archive = build_archive(self.root, self.resources, name="stray.zip",
                                prefix="resources")
        with zipfile.ZipFile(archive, "a") as zf:
            zf.writestr("loose.txt", "x")
        result = verify_package.check_archive(archive, self.resources)
        self.assertFalse(result["ok"])
        self.assertTrue(any("outside the tree" in p for p in result["problems"]))

    def test_the_marker_at_the_archive_root_passes(self):
        # Compressing the tree's contents rather than the folder: the game reads it.
        archive = build_archive(self.root, self.resources, name="flat.zip", prefix="")
        result = verify_package.check_archive(archive, self.resources)
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["root"], "<archive root>")

    def test_one_shared_folder_above_the_marker_passes(self):
        # What `horizon-client --pack-resources` writes: everything under `resources/`.
        archive = build_archive(self.root, self.resources, name="one.zip", prefix="resources")
        result = verify_package.check_archive(archive, self.resources)
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["root"], "resources")

    def test_two_shared_folders_above_the_marker_pass(self):
        # Somebody who compressed one folder too many: still read, as the importer does.
        archive = build_archive(self.root, self.resources, name="two.zip",
                                prefix="Horizon/26.3")
        result = verify_package.check_archive(archive, self.resources)
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["root"], "Horizon/26.3")

    def test_three_shared_folders_above_the_marker_fail(self):
        # One folder too many past what the importer descends: it refuses the archive.
        archive = build_archive(self.root, self.resources, name="three.zip",
                                prefix="a/b/c")
        result = verify_package.check_archive(archive, self.resources)
        self.assertFalse(result["ok"])

    def test_a_tree_file_missing_from_the_archive_fails(self):
        # The archive omits a file the prepared tree holds: the game would not have it.
        archive = build_archive(self.root, self.resources)
        trimmed = self.root / "trimmed.zip"
        with zipfile.ZipFile(archive) as src, zipfile.ZipFile(trimmed, "w") as dst:
            entries = [n for n in src.namelist() if not n.endswith("/")]
            dropped = next(n for n in entries if n.endswith("asset-index.json"))
            for name in entries:
                if name != dropped:
                    dst.writestr(name, src.read(name))
        result = verify_package.check_archive(trimmed, self.resources)
        self.assertFalse(result["ok"])
        self.assertTrue(any("missing from the archive" in p for p in result["problems"]))

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


class CoreShaders(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.shaders = self.root / "assets" / "minecraft" / "shaders"
        (self.shaders / "include").mkdir(parents=True)
        (self.shaders / "core").mkdir(parents=True)
        (self.shaders / "include" / "projection.glsl").write_text("// proj\n")
        (self.shaders / "include" / "globals.glsl").write_text("// globals\n")

    def tearDown(self):
        self._tmp.cleanup()

    def test_resolved_includes_pass(self):
        (self.shaders / "core" / "terrain.vsh").write_text(
            "#include <minecraft:projection.glsl>\n#include <minecraft:globals.glsl>\n"
        )
        result = verify_package.check_core_shaders(self.root)
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["checked"], 3)  # the core file and both include files
        self.assertEqual(result["includes"], 2)

    def test_a_missing_include_fails(self):
        (self.shaders / "core" / "terrain.vsh").write_text("#include <minecraft:missing.glsl>\n")
        result = verify_package.check_core_shaders(self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("missing.glsl" in p for p in result["problems"]))

    def test_a_commented_out_include_is_ignored(self):
        (self.shaders / "core" / "terrain.vsh").write_text(
            "// #include <minecraft:missing.glsl>\n"
            "/* #include <minecraft:also_missing.glsl> */\n"
            "#include <minecraft:projection.glsl>\n"
        )
        result = verify_package.check_core_shaders(self.root)
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["includes"], 1)

    def test_a_relative_include_resolves_from_its_folder(self):
        (self.shaders / "include" / "common.glsl").write_text('#include "globals.glsl"\n')
        result = verify_package.check_core_shaders(self.root)
        self.assertTrue(result["ok"], result["problems"])

    def test_moj_import_is_recognised(self):
        (self.shaders / "core" / "terrain.vsh").write_text("#moj_import <minecraft:missing.glsl>\n")
        result = verify_package.check_core_shaders(self.root)
        self.assertFalse(result["ok"])


class JsonMetadata(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_valid_json_passes(self):
        models = self.root / "assets" / "minecraft" / "models"
        models.mkdir(parents=True)
        (models / "block.json").write_text('{"parent": "block/cube"}')
        (self.root / "assets" / "pack.mcmeta").write_text('{"pack": {"pack_format": 1}}')
        result = verify_package.check_json_metadata(self.root)
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["checked"], 2)

    def test_a_malformed_file_fails(self):
        (self.root / "data").mkdir()
        (self.root / "data" / "tags.json").write_text('{"a": }')
        result = verify_package.check_json_metadata(self.root)
        self.assertFalse(result["ok"])
        self.assertTrue(any("tags.json" in p for p in result["problems"]))

    def test_a_non_json_file_is_ignored(self):
        (self.root / "assets").mkdir()
        (self.root / "assets" / "notes.txt").write_text("not json at all")
        result = verify_package.check_json_metadata(self.root)
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["checked"], 0)


class MarkerHashes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name: str, data: bytes) -> str:
        (self.root / name).write_bytes(data)
        return hashlib.sha256(data).hexdigest()

    def test_matching_hashes_pass(self):
        digest = self._write("facts.json", b'{"a":1}')
        result = verify_package.check_marker_hashes(self.root, {"facts_sha256": digest})
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["checked"], 1)

    def test_a_changed_file_fails(self):
        self._write("facts.json", b'{"a":1}')
        result = verify_package.check_marker_hashes(self.root, {"facts_sha256": "0" * 64})
        self.assertFalse(result["ok"])
        self.assertTrue(any("facts.json" in p for p in result["problems"]))

    def test_a_missing_recorded_file_fails(self):
        result = verify_package.check_marker_hashes(
            self.root, {"health_files_sha256": {"damage-types.json": "0" * 64}}
        )
        self.assertFalse(result["ok"])
        self.assertTrue(any("damage-types.json" in p for p in result["problems"]))

    def test_exporter_hashes_are_skipped(self):
        # The exporter maps name Java sources the package does not carry; they must not
        # be reported as missing files.
        result = verify_package.check_marker_hashes(
            self.root, {"facts_exporters_sha256": {"mods/x.java": "0" * 64}}
        )
        self.assertTrue(result["ok"], result["problems"])
        self.assertEqual(result["checked"], 0)


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
