"""Regression tests for `fetch_jdk.py`.

The JDK fetch is a supply-chain boundary: it downloads a third-party archive and
unpacks it. These tests pin the properties that keep it safe -- a pinned release,
a checked SHA-256, and an extraction that refuses to escape its destination --
without any network access.
"""

import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from tools.tests import _path  # noqa: F401 - puts `tools` on sys.path
import fetch_jdk


def tar_with(entries):
    """A gzipped tar holding `entries`: (name, kind, payload).

    kind is "file", "sym", "hard", "abs" or "dev". `payload` is the bytes for a
    file, the link target for a link.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, kind, payload in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                data = payload if isinstance(payload, bytes) else payload.encode()
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            elif kind == "sym":
                info.type = tarfile.SYMTYPE
                info.linkname = payload
                tar.addfile(info)
            elif kind == "hard":
                info.type = tarfile.LNKTYPE
                info.linkname = payload
                tar.addfile(info)
            elif kind == "abs":
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                tar.addfile(info)
            elif kind == "dev":
                info.type = tarfile.CHRTYPE
                info.devmajor, info.devminor = 1, 3
                tar.addfile(info)
    buffer.seek(0)
    return buffer


class SafeMembers(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def members(self, entries):
        with tarfile.open(fileobj=tar_with(entries)) as tar:
            return fetch_jdk.safe_members(tar, self.dest)

    def test_plain_files_are_accepted(self):
        members = self.members([("jdk-25/bin/java", "file", "x")])
        self.assertEqual([m.name for m in members], ["jdk-25/bin/java"])

    def test_dot_dot_is_rejected(self):
        with self.assertRaises(fetch_jdk.JdkError):
            self.members([("../escape", "file", "x")])

    def test_nested_dot_dot_is_rejected(self):
        with self.assertRaises(fetch_jdk.JdkError):
            self.members([("jdk-25/../../escape", "file", "x")])

    def test_absolute_path_is_rejected(self):
        with self.assertRaises(fetch_jdk.JdkError):
            self.members([("/etc/passwd", "file", "x")])

    def test_symlink_outside_is_rejected(self):
        with self.assertRaises(fetch_jdk.JdkError):
            self.members([("jdk-25/link", "sym", "../../outside")])

    def test_symlink_inside_is_accepted(self):
        members = self.members([("jdk-25/link", "sym", "bin/java")])
        self.assertEqual(len(members), 1)

    def test_absolute_symlink_is_rejected(self):
        with self.assertRaises(fetch_jdk.JdkError):
            self.members([("jdk-25/link", "abs", None)])

    def test_hardlink_outside_is_rejected(self):
        with self.assertRaises(fetch_jdk.JdkError):
            self.members([("jdk-25/link", "hard", "../../outside")])

    def test_device_node_is_rejected(self):
        with self.assertRaises(fetch_jdk.JdkError):
            self.members([("jdk-25/null", "dev", None)])


class Lock(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, data):
        path = self.dir / "lock.json"
        path.write_text(json.dumps(data))
        return path

    def test_major_mismatch_exits(self):
        path = self.write({"major": 21, "platforms": {"x64": {"url": "u", "sha256": "s"}}})
        with self.assertRaises(SystemExit):
            fetch_jdk.load_lock(path, 25)

    def test_missing_platforms_exits(self):
        path = self.write({"major": 25, "platforms": {}})
        with self.assertRaises(SystemExit):
            fetch_jdk.load_lock(path, 25)

    def test_matching_major_returns_lock(self):
        path = self.write({"major": 25, "platforms": {"x64": {"url": "u", "sha256": "s"}}})
        lock = fetch_jdk.load_lock(path, 25)
        self.assertEqual(lock["major"], 25)

    def test_repository_lock_is_pinned_and_has_no_latest(self):
        lock = json.loads(fetch_jdk.LOCK.read_text())
        self.assertEqual(lock["major"], 25)
        self.assertIn("platforms", lock)
        for key, platform in lock["platforms"].items():
            self.assertIn(key, ("x64", "aarch64"))
            self.assertRegex(platform["url"], r"^https://github\.com/adoptium/temurin")
            self.assertRegex(platform["sha256"], r"^[0-9a-f]{64}$")
            self.assertNotIn("latest", platform["url"].lower())
        self.assertRegex(lock["release_name"], r"^jdk-\d")


class Sha(unittest.TestCase):
    def test_sha256_file_matches_hashlib(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f"
            path.write_bytes(b"horizon")
            self.assertEqual(
                fetch_jdk.sha256_file(path), hashlib.sha256(b"horizon").hexdigest()
            )


class FindJavaHome(unittest.TestCase):
    def test_finds_the_jdk_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            java = dest / "jdk-25.0.5+8" / "bin" / "java"
            java.parent.mkdir(parents=True)
            java.write_text("")
            self.assertEqual(fetch_jdk.find_java_home(dest).name, "jdk-25.0.5+8")

    def test_none_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(fetch_jdk.find_java_home(Path(tmp)))


if __name__ == "__main__":
    unittest.main()
