#!/usr/bin/env python3
"""Fetch the pinned Temurin (Eclipse Adoptium) JDK for the pinned Java version.

`horizon-prepare` runs Java against the imported Minecraft jars (the exporters and
the data generator). The pinned version needs Java 25 (`research-lock.json`'s
`minecraft.java_major`). This downloads the **exact** Temurin build
`tools/temurin-lock.json` pins -- a fixed release and build, not "latest" -- for the
runner's architecture, checks its SHA-256 against the pinned checksum, and only then
unpacks it. The archive is never executed; `java` is only run by the caller, after
this returns the JAVA_HOME of an archive that matched its checksum.

Nothing here is game data and nothing is third-party: Temurin is the OpenJDK build
Adoptium publishes. The lock is the only place a URL lives, so updating the JDK is a
one-file change (see the lock's `note`).

Usage:
  python3 tools/fetch_jdk.py --major 25 --dest /opt/jdk
  # prints the JAVA_HOME path on the last line
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import tarfile
import urllib.request
from pathlib import Path

USER_AGENT = "horizon-ios-resource-preparation/1.0 (+github-actions)"
LOCK = Path(__file__).with_name("temurin-lock.json")


class JdkError(Exception):
    """A checksum mismatch, an unsafe archive entry, or unusable metadata."""


def arch() -> str:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x64"
    if machine in ("aarch64", "arm64"):
        return "aarch64"
    raise SystemExit(f"unsupported architecture {machine!r} for the Temurin JDK")


def load_lock(lock_path: Path, major: int) -> dict:
    lock = json.loads(lock_path.read_text())
    if lock.get("major") != major:
        raise SystemExit(
            f"{lock_path}: pinned major is {lock.get('major')!r}, this run asked for {major!r}"
        )
    if not isinstance(lock.get("platforms"), dict) or not lock["platforms"]:
        raise SystemExit(f"{lock_path}: no `platforms` to fetch")
    return lock


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_members(tar: tarfile.TarFile, dest: Path) -> list[tarfile.TarInfo]:
    """The archive's members, refusing anything that would escape `dest`.

    Rejects absolute paths, `..` components, links pointing outside `dest`, and
    device/fifo nodes. Extraction happens only over the list this returns.
    """
    dest = dest.resolve()
    members: list[tarfile.TarInfo] = []
    for member in tar.getmembers():
        name = member.name
        if name.startswith("/") or Path(name).is_absolute():
            raise JdkError(f"archive entry is an absolute path: {name!r}")
        target = (dest / name).resolve()
        if target != dest and dest not in target.parents:
            raise JdkError(f"archive entry escapes the destination: {name!r}")
        if member.issym() or member.islnk():
            link_target = (target.parent / member.linkname).resolve()
            if link_target != dest and dest not in link_target.parents:
                raise JdkError(f"archive link points outside the destination: {name!r} -> {member.linkname!r}")
        if member.isdev() or member.isfifo():
            raise JdkError(f"archive entry is not a regular file or directory: {name!r}")
        members.append(member)
    return members


def find_java_home(dest: Path) -> Path | None:
    for candidate in sorted(dest.glob("jdk*")):
        if (candidate / "bin" / "java").is_file():
            return candidate
    return None


def download(url: str, dest: Path, expected_sha256: str) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=600) as response, dest.open("wb") as handle:
        while chunk := response.read(1 << 20):
            handle.write(chunk)
    actual = sha256_file(dest)
    if actual != expected_sha256:
        dest.unlink(missing_ok=True)
        raise JdkError(f"{url}: SHA-256 {actual} != pinned {expected_sha256}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--major", type=int, required=True, help="the Java feature version")
    parser.add_argument("--dest", type=Path, required=True, help="where to unpack the JDK")
    parser.add_argument("--lock", type=Path, default=LOCK, help="the pinned Temurin lock")
    args = parser.parse_args()

    existing = find_java_home(args.dest)
    if existing is not None:
        print(f"Already present: {existing}")
        print(existing)
        return 0

    lock = load_lock(args.lock, args.major)
    key = arch()
    platform = lock["platforms"].get(key)
    if not platform:
        raise SystemExit(f"{args.lock}: no pinned build for {key!r}")
    url, expected_sha256 = platform["url"], platform["sha256"]

    args.dest.mkdir(parents=True, exist_ok=True)
    tarball = args.dest / "temurin.tar.gz"
    print(f"Downloading {lock['release_name']} ({url})", file=sys.stderr)
    try:
        download(url, tarball, expected_sha256)
    except JdkError as error:
        raise SystemExit(f"error: {error}") from error

    print("SHA-256 verified against the pinned checksum", file=sys.stderr)
    try:
        with tarfile.open(tarball) as tar:
            members = safe_members(tar, args.dest)
            tar.extractall(args.dest, members=members)
    except (tarfile.TarError, JdkError) as error:
        raise SystemExit(f"error: {error}") from error
    finally:
        tarball.unlink(missing_ok=True)

    java_home = find_java_home(args.dest)
    if java_home is None:
        raise SystemExit(f"no JDK found under {args.dest} after unpacking")
    version = (java_home / "release").read_text(errors="replace") if (java_home / "release").is_file() else ""
    line = next((l for l in version.splitlines() if l.startswith("JAVA_VERSION=")), "")
    print(f"JDK: {java_home} {line}".strip(), file=sys.stderr)
    print(java_home)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
