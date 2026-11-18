"""Regression tests for `verify_ogg.py`.

The audio checks must catch a real problem, not just a missing file. These build
real Ogg Vorbis files with ffmpeg (skipped when ffmpeg is absent), then prove the
verifier accepts a good set and rejects a truncated stream, an empty file and a
`sounds.json` that names a file which is not there.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.tests import _path  # noqa: F401 - puts `tools` on sys.path
import verify_ogg

FFMPEG = shutil.which("ffmpeg")


def make_vorbis(path: Path, seconds: float = 1.0) -> bool:
    if FFMPEG is None:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [FFMPEG, "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:a", "libvorbis", str(path)],
        capture_output=True,
    )
    return result.returncode == 0 and path.is_file()


def truncate(path: Path, by: int = 200) -> None:
    data = path.read_bytes()
    path.write_bytes(data[: len(data) - by])


class Fixture:
    """A minimal resources folder the verifier can read."""

    def __init__(self, root: Path):
        self.root = root
        self.assets = root / "assets" / "minecraft"
        self.sounds = self.assets / "sounds"

    def write_sounds_json(self, mapping: dict) -> None:
        self.assets.mkdir(parents=True, exist_ok=True)
        (self.assets / "sounds.json").write_text(json.dumps(mapping))


class Container(unittest.TestCase):
    def test_parse_ogg_accepts_a_real_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.ogg"
            if not make_vorbis(path):
                self.skipTest("ffmpeg unavailable")
            parsed = verify_ogg.parse_ogg(path.read_bytes())
            self.assertIn("first_payload", parsed)

    def test_parse_ogg_rejects_garbage(self):
        with self.assertRaises(verify_ogg.OggError):
            verify_ogg.parse_ogg(b"not an ogg file at all")

    def test_parse_ogg_rejects_empty(self):
        with self.assertRaises(verify_ogg.OggError):
            verify_ogg.parse_ogg(b"")


class Decode(unittest.TestCase):
    def test_decode_accepts_a_good_stream(self):
        if FFMPEG is None:
            self.skipTest("ffmpeg unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ok.ogg"
            self.assertTrue(make_vorbis(path))
            ok, error = verify_ogg.decode_vorbis(FFMPEG, path)
            self.assertTrue(ok, error)

    def test_decode_rejects_a_truncated_stream(self):
        if FFMPEG is None:
            self.skipTest("ffmpeg unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trunc.ogg"
            self.assertTrue(make_vorbis(path, seconds=2.0))
            # Cut deep enough to remove a whole audio page, not just the end-of-stream
            # page: losing only the EOS page still decodes, which is why the container
            # check (not this one) is what reports a single-page trim.
            truncate(path, by=2500)
            ok, error = verify_ogg.decode_vorbis(FFMPEG, path)
            self.assertFalse(ok)
            self.assertTrue(error)

    def test_is_vorbis_detects_the_codec(self):
        if FFMPEG is None:
            self.skipTest("ffmpeg unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ok.ogg"
            self.assertTrue(make_vorbis(path))
            self.assertTrue(verify_ogg.is_vorbis(path))
            path.write_bytes(b"\x00" * 64)
            self.assertFalse(verify_ogg.is_vorbis(path))


class EndToEnd(unittest.TestCase):
    def _run(self, fixture: Fixture, extra=()):
        report = fixture.root / "report.json"
        argv = ["verify_ogg.py", "--resources", str(fixture.root),
                "--report", str(report), *extra]
        import sys
        saved = sys.argv
        sys.argv = argv
        try:
            status = verify_ogg.main()
        finally:
            sys.argv = saved
        return status, json.loads(report.read_text())

    def test_a_complete_package_passes(self):
        if FFMPEG is None:
            self.skipTest("ffmpeg unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            make_vorbis(fixture.sounds / "a.ogg")
            make_vorbis(fixture.sounds / "b.ogg")
            fixture.write_sounds_json({"a": {"sounds": ["a"]}, "b": {"sounds": ["b"]}})
            status, report = self._run(fixture)
            self.assertEqual(status, 0, report)
            self.assertTrue(report["ok"])
            self.assertEqual(report["ogg"]["count"], 2)
            self.assertEqual(report["decode"]["decoded"], 2)
            self.assertEqual(report["decode"]["failed"], 0)

    def test_a_truncated_stream_fails(self):
        if FFMPEG is None:
            self.skipTest("ffmpeg unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            make_vorbis(fixture.sounds / "a.ogg")
            make_vorbis(fixture.sounds / "bad.ogg", seconds=2.0)
            truncate(fixture.sounds / "bad.ogg")
            fixture.write_sounds_json({"a": {"sounds": ["a"]}})
            status, report = self._run(fixture)
            self.assertNotEqual(status, 0)
            self.assertFalse(report["ok"])
            self.assertGreaterEqual(report["ogg"]["failed"] + report["decode"]["failed"], 1)

    def test_a_missing_referenced_sound_fails(self):
        if FFMPEG is None:
            self.skipTest("ffmpeg unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            make_vorbis(fixture.sounds / "a.ogg")
            fixture.write_sounds_json({"a": {"sounds": ["a"]}, "ghost": {"sounds": ["ghost"]}})
            status, report = self._run(fixture)
            self.assertNotEqual(status, 0)
            self.assertGreaterEqual(report["sounds_json"].get("missing_files_count", 0), 1)

    def test_no_decode_skips_the_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            # No ffmpeg needed: a zero-byte file is enough to make the point.
            fixture.sounds.mkdir(parents=True)
            (fixture.sounds / "a.ogg").write_bytes(b"")
            fixture.write_sounds_json({})
            status, report = self._run(fixture, extra=["--no-decode"])
            self.assertFalse(report["decode"]["ran"])
            # The empty file still fails the container check, so the run is a failure.
            self.assertNotEqual(status, 0)

    def test_decode_required_when_ffmpeg_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Fixture(Path(tmp))
            fixture.sounds.mkdir(parents=True)
            fixture.write_sounds_json({})
            status, report = self._run(fixture, extra=["--ffmpeg", "/nonexistent/ffmpeg"])
            # ffmpeg was named but cannot run: the decode stage does not run and the
            # run must not pass on a missing decode.
            self.assertFalse(report["decode"]["ran"])
            self.assertNotEqual(status, 0)


if __name__ == "__main__":
    unittest.main()
