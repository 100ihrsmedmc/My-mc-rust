"""Regression tests for `verify_shader.py`.

These pin the honesty rules: a report that never exercised wgpu, found no packs,
could not open a pack, had a backend failure, a wgpu rejection or a skipped program
must fail; and `--expect-config` must fail when no pack produced non-zero options,
screens and profiles. A clean report must pass and carry the findings list.
"""

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
import tempfile

from tools.tests import _path  # noqa: F401 - puts `tools` on sys.path
import verify_shader


def clean_pack(name="post-test"):
    return {
        "name": name,
        "opened": True,
        "applies": True,
        "stage_files": 2,
        "modules_lowered": 4,
        "backend_failures": [],
        "gpu_failures": [],
        "notes": [],
        "preprocess_diagnostics": 0,
        "preprocess_warnings": 0,
        "options": {"count": 3, "names": ["A", "B", "C"]},
        "screens": {"count": 1, "names": ["(main)"]},
        "profiles": {"count": 1, "names": ["LOW"]},
        "shaders_properties_keys": 2,
        "config_read": True,
    }


def clean_report(**overrides):
    report = {
        "packs": [clean_pack()],
        "failures": [],
        "modules_accepted_by_wgpu": True,
        "wgpu_adapter": "llvmpipe",
    }
    report.update(overrides)
    return report


class Summarise(unittest.TestCase):
    def run_it(self, report, expected=(), expect_config=False):
        out = io.StringIO()
        with redirect_stdout(out):
            status = verify_shader.summarise(report, list(expected), expect_config)
        return status, out.getvalue(), report

    def test_a_clean_report_passes(self):
        status, _, report = self.run_it(clean_report())
        self.assertEqual(status, 0)
        self.assertTrue(report["ok"])
        self.assertTrue(report["findings"])

    def test_no_wgpu_flag_fails(self):
        report = clean_report()
        del report["modules_accepted_by_wgpu"]
        status, _, _ = self.run_it(report)
        self.assertNotEqual(status, 0)

    def test_no_adapter_fails(self):
        status, _, _ = self.run_it(clean_report(modules_accepted_by_wgpu=False))
        self.assertNotEqual(status, 0)

    def test_no_packs_fails(self):
        status, _, _ = self.run_it(clean_report(packs=[]))
        self.assertNotEqual(status, 0)

    def test_unopened_pack_fails(self):
        pack = clean_pack()
        pack["opened"] = False
        pack["error"] = "broken"
        status, _, _ = self.run_it(clean_report(packs=[pack]))
        self.assertNotEqual(status, 0)

    def test_backend_failure_fails(self):
        pack = clean_pack()
        pack["backend_failures"] = ["msl: nope"]
        status, _, _ = self.run_it(clean_report(packs=[pack]))
        self.assertNotEqual(status, 0)

    def test_wgpu_rejection_fails(self):
        pack = clean_pack()
        pack["gpu_failures"] = ["create_shader_module rejected"]
        status, _, _ = self.run_it(clean_report(packs=[pack]))
        self.assertNotEqual(status, 0)

    def test_skipped_program_fails(self):
        pack = clean_pack()
        pack["notes"] = [{"severity": "error", "note": "no program for terrain"}]
        status, _, _ = self.run_it(clean_report(packs=[pack]))
        self.assertNotEqual(status, 0)

    def test_config_not_read_fails(self):
        pack = clean_pack()
        pack["config_read"] = False
        status, _, _ = self.run_it(clean_report(packs=[pack]))
        self.assertNotEqual(status, 0)

    def test_expect_pack_missing_fails(self):
        status, _, _ = self.run_it(clean_report(), expected=["terrain-test"])
        self.assertNotEqual(status, 0)

    def test_expect_pack_present_passes(self):
        status, _, _ = self.run_it(clean_report(), expected=["post-test"])
        self.assertEqual(status, 0)

    def test_expect_config_all_zero_fails(self):
        pack = clean_pack()
        pack["options"] = {"count": 0, "names": []}
        pack["screens"] = {"count": 0, "names": []}
        pack["profiles"] = {"count": 0, "names": []}
        status, _, _ = self.run_it(clean_report(packs=[pack]), expect_config=True)
        self.assertNotEqual(status, 0)

    def test_expect_config_non_zero_passes(self):
        status, out, _ = self.run_it(clean_report(), expect_config=True)
        self.assertEqual(status, 0)
        self.assertIn("Configuration read", out)

    def test_global_failure_line_fails(self):
        status, _, _ = self.run_it(clean_report(failures=["something broke"]))
        self.assertNotEqual(status, 0)


class Findings(unittest.TestCase):
    def test_findings_are_honest_about_on_device_rendering(self):
        claims = {claim: status for claim, status, _ in verify_shader.FINDINGS}
        self.assertEqual(claims["On-device rendering"], "unverified")
        self.assertEqual(claims["Shader support is genuinely functional?"], "limited")
        # Nothing that needs a device is ever marked verified.
        for claim, status, _ in verify_shader.FINDINGS:
            if "On-device" in claim or "genuinely functional" in claim:
                self.assertNotEqual(status, "verified")


class Main(unittest.TestCase):
    def test_main_writes_the_annotated_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report_path = root / "shader.json"
            report_path.write_text(json.dumps(clean_report()))
            out_path = root / "annotated.json"
            argv = ["verify_shader.py", "--report", str(report_path), "--write", str(out_path),
                    "--expect-pack", "post-test", "--expect-config"]
            saved = sys.argv
            sys.argv = argv
            out = io.StringIO()
            try:
                with redirect_stdout(out):
                    status = verify_shader.main()
            finally:
                sys.argv = saved
            self.assertEqual(status, 0)
            written = json.loads(out_path.read_text())
            self.assertTrue(written["findings"])

    def test_main_missing_report_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv = ["verify_shader.py", "--report", str(Path(tmp) / "absent.json")]
            saved = sys.argv
            sys.argv = argv
            out = io.StringIO()
            try:
                with redirect_stdout(out):
                    status = verify_shader.main()
            finally:
                sys.argv = saved
            self.assertNotEqual(status, 0)


if __name__ == "__main__":
    unittest.main()
