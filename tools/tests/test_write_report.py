"""Regression tests for `write_report.py`, the validation-report gate.

The gate exists so a failed check cannot still produce a green artifact. These
tests pin that: a missing report, a non-JSON report, a report with the wrong shape
and a report that says `ok: false` must each make the writer exit non-zero, and a
complete, passing set must write the Markdown report.
"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tools.tests import _path  # noqa: F401 - puts `tools` on sys.path
import write_report


def audio_report(ok=True):
    return {
        "ogg": {"count": 10, "bytes": 1000, "failed": 0, "failures": [], "codecs": {"vorbis": 10}},
        "decode": {"ran": True, "tool": "/usr/bin/ffmpeg", "decoded": 10, "failed": 0, "failures": []},
        "structure": {"ok": True, "problems": []},
        "sounds_json": {"present": True, "events": 3, "referenced_files": 3,
                        "missing_files": [], "missing_files_count": 0, "malformed": False},
        "asset_index": {"checked": True, "expected_ogg": 10, "missing": [], "mismatched": [],
                        "extra_ogg": 0},
        "ok": ok,
    }


def package_report(ok=True):
    return {
        "tree": {"files": 100, "bytes": 400_000_000, "schema": 7, "minecraft": "26.3"},
        "archive_checks": {"entries": 100, "bytes": 300_000_000, "ok": True},
        "shaders": {"present": False, "ok": True, "packs": []},
        "ok": ok,
    }


def shader_report(ok=True):
    return {
        "packs": [{"name": "p", "stage_files": 2, "modules_lowered": 4,
                   "backend_failures": [], "gpu_failures": [], "applies": True}],
        "diagnostics": {"total": 0, "errors": 0, "warnings": 0},
        "modules_accepted_by_wgpu": True,
        "wgpu_adapter": "llvmpipe",
        "ok": ok,
    }


def meta():
    return {"version": "26.3", "source_repo": "x", "source_commit": "abc",
            "mojang_hosts": "piston-meta.mojang.com"}


class Gate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, data):
        path = self.dir / name
        path.write_text(json.dumps(data) if not isinstance(data, str) else data)
        return path

    def run_writer(self, flags=(), **paths):
        argv = ["write_report.py"]
        for flag, path in paths.items():
            argv += [f"--{flag.replace('_', '-')}", str(path)]
        argv += [f"--{flag.replace('_', '-')}" for flag in flags]
        argv += ["--out", str(self.dir / "out.md")]
        import sys
        saved = sys.argv
        sys.argv = argv
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                status = write_report.main()
        finally:
            sys.argv = saved
        return status, out.getvalue() + err.getvalue()

    def full(self, **overrides):
        paths = {
            "audio": self.write("audio.json", overrides.get("audio", audio_report())),
            "package": self.write("package.json", overrides.get("package", package_report())),
            "shader": self.write("shader.json", overrides.get("shader", shader_report())),
            "shader_findings": self.write("findings.json", {"findings": []}),
            "meta": self.write("meta.json", meta()),
        }
        return paths

    def test_a_complete_passing_set_writes_the_report(self):
        status, _ = self.run_writer(**self.full())
        self.assertEqual(status, 0)
        self.assertTrue((self.dir / "out.md").is_file())

    def test_missing_audio_report_fails(self):
        paths = self.full()
        paths["audio"] = self.dir / "absent.json"
        status, message = self.run_writer(**paths)
        self.assertNotEqual(status, 0)
        self.assertIn("missing", message)

    def test_invalid_json_fails(self):
        paths = self.full()
        paths["audio"] = self.write("audio.json", "{not json")
        status, message = self.run_writer(**paths)
        self.assertNotEqual(status, 0)
        self.assertIn("not valid JSON", message)

    def test_wrong_shape_fails(self):
        paths = self.full()
        paths["package"] = self.write("package.json", {"tree": {}})
        status, message = self.run_writer(**paths)
        self.assertNotEqual(status, 0)
        self.assertIn("missing required field", message)

    def test_audio_ok_false_fails(self):
        status, message = self.run_writer(**self.full(audio=audio_report(ok=False)))
        self.assertNotEqual(status, 0)
        self.assertIn("audio report reports failure", message)

    def test_package_ok_false_fails(self):
        status, _ = self.run_writer(**self.full(package=package_report(ok=False)))
        self.assertNotEqual(status, 0)

    def test_shader_ok_false_fails(self):
        status, _ = self.run_writer(**self.full(shader=shader_report(ok=False)))
        self.assertNotEqual(status, 0)

    def test_missing_decode_field_fails(self):
        broken = audio_report()
        del broken["decode"]
        status, message = self.run_writer(**self.full(audio=broken))
        self.assertNotEqual(status, 0)
        self.assertIn("decode", message)

    def test_shader_skipped_needs_no_shader_report(self):
        paths = {
            "audio": self.write("audio.json", audio_report()),
            "package": self.write("package.json", package_report()),
            "meta": self.write("meta.json", meta()),
        }
        status, _ = self.run_writer(flags=["shader-skipped"], **paths)
        self.assertEqual(status, 0)
        self.assertIn("SKIPPED", (self.dir / "out.md").read_text())

    def test_shader_report_required_without_skip_flag(self):
        paths = {
            "audio": self.write("audio.json", audio_report()),
            "package": self.write("package.json", package_report()),
            "meta": self.write("meta.json", meta()),
        }
        status, message = self.run_writer(**paths)
        self.assertNotEqual(status, 0)
        self.assertIn("shader report is required", message)


if __name__ == "__main__":
    unittest.main()
