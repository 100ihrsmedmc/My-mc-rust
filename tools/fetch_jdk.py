#!/usr/bin/env python3
"""Fetch a Temurin (Eclipse Adoptium) JDK for the pinned Java major version.

`horizon-prepare` runs Java against the imported Minecraft jars (the exporters and
the data generator). The pinned version needs Java 25 (`research-lock.json`'s
`minecraft.java_major`). This downloads the official Temurin build for the
runner's architecture and prints its `JAVA_HOME`.

Nothing here is game data and nothing is third-party: Temurin is the OpenJDK
build Adoptium publishes at api.adoptium.net.

Usage:
  python3 tools/fetch_jdk.py --major 25 --dest /opt/jdk
  # prints the JAVA_HOME path on the last line
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import tarfile
import urllib.request
from pathlib import Path

API = "https://api.adoptium.net/v3/binary/latest/{major}/ga/linux/{arch}/jdk/hotspot/normal/eclipse"
USER_AGENT = "horizon-ios-resource-preparation/1.0 (+github-actions)"


def arch() -> str:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x64"
    if machine in ("aarch64", "arm64"):
        return "aarch64"
    raise SystemExit(f"unsupported architecture {machine!r} for the Temurin JDK")


def find_java_home(dest: Path) -> Path | None:
    for candidate in sorted(dest.glob("jdk*")):
        if (candidate / "bin" / "java").is_file():
            return candidate
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--major", type=int, required=True, help="the Java feature version")
    parser.add_argument("--dest", type=Path, required=True, help="where to unpack the JDK")
    args = parser.parse_args()

    existing = find_java_home(args.dest)
    if existing is not None:
        print(f"Already present: {existing}")
        print(existing)
        return 0

    url = API.format(major=args.major, arch=arch())
    args.dest.mkdir(parents=True, exist_ok=True)
    tarball = args.dest / "temurin.tar.gz"
    print(f"Downloading {url}", file=sys.stderr)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=300) as response, tarball.open("wb") as handle:
        while chunk := response.read(1 << 20):
            handle.write(chunk)

    with tarfile.open(tarball) as tar:
        tar.extractall(args.dest)
    tarball.unlink()

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
