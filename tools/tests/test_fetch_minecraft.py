"""Regression tests for `fetch_minecraft.py`.

The dry run is the part that must never touch the disk or a game archive. The
network call that reads Mojang's small metadata is the one boundary these tests
stand in for; everything else is the real code.
"""

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from tools.tests import _path  # noqa: F401 - puts `tools` on sys.path
import fetch_minecraft


def lock_file(dir: Path, **overrides) -> Path:
    minecraft = {
        "version": "26.3",
        "manifest_sha1": "a" * 40,
        "client_sha1": "b" * 40,
        "server_sha1": "c" * 40,
        "java_major": 25,
    }
    minecraft.update(overrides)
    path = dir / "research-lock.json"
    path.write_text(json.dumps({"minecraft": minecraft}))
    return path


class LoadLock(unittest.TestCase):
    def test_missing_minecraft_section_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock.json"
            path.write_text(json.dumps({}))
            with self.assertRaises(SystemExit):
                fetch_minecraft.load_lock(path)

    def test_missing_key_exits(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock.json"
            path.write_text(json.dumps({"minecraft": {"version": "26.3"}}))
            with self.assertRaises(SystemExit):
                fetch_minecraft.load_lock(path)

    def test_valid_lock_is_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = lock_file(Path(tmp))
            lock = fetch_minecraft.load_lock(path)
            self.assertEqual(lock["version"], "26.3")


class Sha1(unittest.TestCase):
    def test_sha1_file_matches_hashlib(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f"
            path.write_bytes(b"minecraft")
            self.assertEqual(
                fetch_minecraft.sha1_file(path), hashlib.sha1(b"minecraft").hexdigest()
            )


class DryRun(unittest.TestCase):
    """A dry run must create, modify and download nothing."""

    def test_dry_run_writes_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            minecraft_dir = root / "minecraft"
            server_jar = root / "server.jar"
            index = json.dumps({"objects": {"a": {"hash": "e" * 40}}}).encode()
            # The version JSON pins the asset index by SHA-1, so build it with the
            # real digest of the bytes the fake fetch will return.
            manifest = json.dumps({
                "id": "26.3",
                "downloads": {
                    "client": {"url": "https://example/client", "sha1": "b" * 40},
                    "server": {"url": "https://example/server", "sha1": "c" * 40},
                },
                "libraries": [],
                "assetIndex": {"id": "26", "url": "https://example/index",
                               "sha1": hashlib.sha1(index).hexdigest()},
            }).encode()
            lock = lock_file(root, manifest_sha1=hashlib.sha1(manifest).hexdigest())

            calls: list[str] = []

            def fake_fetch(url: str, attempts: int = 4) -> bytes:
                calls.append(url)
                if url.endswith("26.3.json"):
                    return manifest
                return index

            saved = fetch_minecraft._fetch_bytes
            fetch_minecraft._fetch_bytes = fake_fetch
            try:
                args = fetch_minecraft.argparse.Namespace(
                    lock=lock, minecraft_dir=minecraft_dir, server_jar=server_jar,
                    jobs=1, dry_run=True,
                )
                status = fetch_minecraft.run(args)
            finally:
                fetch_minecraft._fetch_bytes = saved

            self.assertEqual(status, 0)
            self.assertFalse(minecraft_dir.exists(), "dry run created the Minecraft folder")
            self.assertFalse(server_jar.exists(), "dry run wrote the server jar")
            # Only Mojang's small metadata was read, never an archive or asset object.
            self.assertEqual(len(calls), 2)
            self.assertTrue(all("piston-meta" in c or "example" in c for c in calls))

    def test_dry_run_rejects_a_manifest_that_does_not_match_the_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock = lock_file(root)
            manifest = json.dumps({
                "id": "26.3",
                "downloads": {"client": {"sha1": "WRONG"}, "server": {"sha1": "c" * 40}},
                "assetIndex": {"url": "https://example/index", "sha1": "d" * 40},
            }).encode()

            saved = fetch_minecraft._fetch_bytes
            fetch_minecraft._fetch_bytes = lambda url, attempts=4: manifest
            try:
                args = fetch_minecraft.argparse.Namespace(
                    lock=lock, minecraft_dir=root / "minecraft",
                    server_jar=root / "server.jar", jobs=1, dry_run=True,
                )
                with self.assertRaises(SystemExit):
                    fetch_minecraft.run(args)
            finally:
                fetch_minecraft._fetch_bytes = saved


if __name__ == "__main__":
    unittest.main()
