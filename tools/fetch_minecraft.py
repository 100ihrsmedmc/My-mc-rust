#!/usr/bin/env python3
"""Fetch the pinned Minecraft: Java Edition files from Mojang's official servers.

Only the exact files `research-lock.json` pins are downloaded -- the version JSON,
the client jar, the libraries, the asset index and every asset object -- straight
from Mojang's public download hosts. Every file is checked against its SHA-1 before
it is used. Nothing comes from a third-party mirror and no Microsoft account is
needed: these are the same public files the official launcher downloads for a
signed-in owner of Minecraft: Java Edition.

Layout written, which is what `horizon-prepare --minecraft-dir` reads:

    <minecraft-dir>/versions/<version>/<version>.json
    <minecraft-dir>/versions/<version>/<version>.jar
    <minecraft-dir>/libraries/<maven path>
    <minecraft-dir>/assets/indexes/<asset index id>.json
    <minecraft-dir>/assets/objects/<first two hex>/<sha1>

and the server jar separately at --server-jar.

A file already present with the right SHA-1 is left alone, so a re-run resumes
instead of downloading it again. Exit status is 0 only when every pinned file is
present and verified.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

PISTON_META = "https://piston-meta.mojang.com"
LIBRARIES = "https://libraries.minecraft.net"
RESOURCES = "https://resources.download.minecraft.net"
USER_AGENT = "horizon-ios-resource-preparation/1.0 (+github-actions)"

_print_lock = threading.Lock()


def log(message: str) -> None:
    with _print_lock:
        print(message, flush=True)


def sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _fetch_bytes(url: str, attempts: int = 4) -> bytes:
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as error:
            last = error
            if attempt < attempts:
                time.sleep(min(2 ** attempt, 15))
    raise RuntimeError(f"could not fetch {url}: {last}")


def download(url: str, dest: Path, expected_sha1: str | None, attempts: int = 4) -> str:
    """Download `url` to `dest`, skipping it when the SHA-1 already matches.

    Returns "kept" when the existing file was used, "fetched" when it was
    downloaded. Raises when the bytes never match the expected SHA-1.
    """
    if dest.is_file() and expected_sha1 and sha1_file(dest) == expected_sha1:
        return "kept"
    dest.parent.mkdir(parents=True, exist_ok=True)
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=120) as response:
                tmp = dest.with_name(dest.name + ".part")
                with tmp.open("wb") as handle:
                    while chunk := response.read(1 << 20):
                        handle.write(chunk)
            if expected_sha1:
                actual = sha1_file(tmp)
                if actual != expected_sha1:
                    tmp.unlink(missing_ok=True)
                    raise RuntimeError(f"{url}: SHA-1 {actual} != pinned {expected_sha1}")
            tmp.replace(dest)
            return "fetched"
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, RuntimeError) as error:
            last = error
            if attempt < attempts:
                time.sleep(min(2 ** attempt, 15))
    raise RuntimeError(f"could not download {url}: {last}")


def load_lock(lock_path: Path) -> dict:
    lock = json.loads(lock_path.read_text())
    minecraft = lock.get("minecraft")
    if not isinstance(minecraft, dict):
        raise SystemExit(f"{lock_path}: no `minecraft` section")
    for key in ("version", "manifest_sha1", "client_sha1", "server_sha1"):
        if not isinstance(minecraft.get(key), str):
            raise SystemExit(f"{lock_path}: `minecraft.{key}` is missing")
    return minecraft


def enumerate_plan(lock: dict) -> dict:
    """What a real run would fetch, reading only Mojang's small metadata.

    Fetches the pinned version JSON and the asset index -- both metadata on
    `piston-meta`, neither a game archive nor an asset object -- and nothing else.
    Writes nothing. Raises when the metadata does not match the lock.
    """
    version = lock["version"]
    manifest_url = f"{PISTON_META}/v1/packages/{lock['manifest_sha1']}/{version}.json"
    manifest_bytes = _fetch_bytes(manifest_url)
    actual = hashlib.sha1(manifest_bytes).hexdigest()
    if actual != lock["manifest_sha1"]:
        raise SystemExit(
            f"the pinned version JSON hashes to {actual}, not {lock['manifest_sha1']}"
        )
    manifest = json.loads(manifest_bytes)
    if manifest.get("id") != version:
        raise SystemExit(f"the pinned version JSON is for {manifest.get('id')!r}, not {version!r}")
    if manifest.get("downloads", {}).get("client", {}).get("sha1") != lock["client_sha1"]:
        raise SystemExit("the pinned version JSON's client jar does not match the lock")
    if manifest.get("downloads", {}).get("server", {}).get("sha1") != lock["server_sha1"]:
        raise SystemExit("the pinned version JSON's server jar does not match the lock")

    libraries = [
        library["downloads"]["artifact"]
        for library in manifest.get("libraries", [])
        if (library.get("downloads") or {}).get("artifact")
        and all((library["downloads"]["artifact"].get(k) for k in ("path", "url", "sha1")))
    ]

    index_info = manifest["assetIndex"]
    index_bytes = _fetch_bytes(index_info["url"])
    if hashlib.sha1(index_bytes).hexdigest() != index_info["sha1"]:
        raise SystemExit("the asset index does not match its published SHA-1")
    objects = json.loads(index_bytes)["objects"]

    return {
        "version": version,
        "manifest_url": manifest_url,
        "client": manifest["downloads"]["client"],
        "server": manifest["downloads"]["server"],
        "index": index_info,
        "libraries": libraries,
        "asset_objects": len(objects),
    }


def run(args: argparse.Namespace) -> int:
    lock = load_lock(args.lock)
    version = lock["version"]
    minecraft_dir = args.minecraft_dir

    if args.dry_run:
        # A dry run must not create, modify or download anything. Only Mojang's
        # small metadata is read, and only to enumerate the plan.
        plan = enumerate_plan(lock)
        log(f"Dry run for Minecraft {version} (nothing is written)")
        log(f"  version JSON: {plan['manifest_url']} -> versions/{version}/{version}.json")
        log(f"  client jar:   {plan['client']['url']}")
        log(f"  server jar:   {plan['server']['url']}")
        log(f"  libraries:    {len(plan['libraries'])} files")
        log(f"  asset index:  {plan['index']['url']} -> assets/indexes/{plan['index']['id']}.json")
        log(f"  assets:       {plan['asset_objects']} objects under assets/objects/<xx>/<sha1>")
        log(f"  total: {len(plan['libraries']) + plan['asset_objects'] + 3} files under {minecraft_dir}")
        log("Dry run: no file was created or modified and no archive or asset object was downloaded.")
        return 0

    minecraft_dir.mkdir(parents=True, exist_ok=True)
    manifest_url = f"{PISTON_META}/v1/packages/{lock['manifest_sha1']}/{version}.json"
    log(f"Pinned manifest: {manifest_url}")
    manifest_bytes = _fetch_bytes(manifest_url)
    actual = hashlib.sha1(manifest_bytes).hexdigest()
    if actual != lock["manifest_sha1"]:
        raise SystemExit(
            f"the pinned version JSON hashes to {actual}, not {lock['manifest_sha1']}"
        )
    manifest = json.loads(manifest_bytes)

    if manifest.get("id") != version:
        raise SystemExit(f"the pinned version JSON is for {manifest.get('id')!r}, not {version!r}")
    if manifest.get("downloads", {}).get("client", {}).get("sha1") != lock["client_sha1"]:
        raise SystemExit("the pinned version JSON's client jar does not match the lock")
    if manifest.get("downloads", {}).get("server", {}).get("sha1") != lock["server_sha1"]:
        raise SystemExit("the pinned version JSON's server jar does not match the lock")

    # The version JSON and the client jar, exactly where horizon-prepare looks.
    version_dir = minecraft_dir / "versions" / version
    version_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = version_dir / f"{version}.json"
    manifest_path.write_bytes(manifest_bytes)
    client = manifest["downloads"]["client"]
    client_path = version_dir / f"{version}.jar"
    log(f"Client jar: {client['url']}")
    download(client["url"], client_path, lock["client_sha1"])

    # The server jar, which horizon-prepare needs but never downloads itself.
    server = manifest["downloads"]["server"]
    log(f"Server jar: {server['url']}")
    download(server["url"], args.server_jar, lock["server_sha1"])

    # The libraries the client runs with, at the paths the version JSON names.
    libraries: list[tuple[str, Path, str]] = []
    for library in manifest.get("libraries", []):
        artifact = (library.get("downloads") or {}).get("artifact")
        if not artifact:
            continue
        relative = artifact.get("path")
        url = artifact.get("url")
        sha1 = artifact.get("sha1")
        if not (relative and url and sha1):
            continue
        libraries.append((url, minecraft_dir / "libraries" / relative, sha1))
    log(f"Libraries: {len(libraries)}")

    # The asset index and every asset object it names.
    index_info = manifest["assetIndex"]
    index_path = minecraft_dir / "assets" / "indexes" / f"{index_info['id']}.json"
    log(f"Asset index: {index_info['url']}")
    download(index_info["url"], index_path, index_info["sha1"])
    objects = json.loads(index_path.read_text())["objects"]
    assets: list[tuple[str, Path, str]] = []
    for info in objects.values():
        sha1 = info["hash"]
        assets.append(
            (f"{RESOURCES}/{sha1[:2]}/{sha1}",
             minecraft_dir / "assets" / "objects" / sha1[:2] / sha1,
             sha1)
        )
    log(f"Assets: {len(assets)} objects")

    jobs = libraries + assets
    counts = {"fetched": 0, "kept": 0}
    failures: list[str] = []
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(download, url, dest, sha1): url for url, dest, sha1 in jobs}
        for future in concurrent.futures.as_completed(futures):
            done += 1
            try:
                counts[future.result()] += 1
            except Exception as error:  # noqa: BLE001 - reported, not swallowed
                failures.append(str(error))
            if done % 500 == 0:
                log(f"  {done}/{len(jobs)} files checked")

    log(f"Libraries and assets: {counts['fetched']} fetched, {counts['kept']} already present")
    if failures:
        log(f"{len(failures)} FAILED:")
        for failure in failures[:40]:
            log(f"  {failure}")
        return 1

    # Final proof: every pinned file is here and hashes correctly.
    missing = []
    for label, path, sha1 in (
        [("version JSON", manifest_path, lock["manifest_sha1"])]
        + [("client jar", client_path, lock["client_sha1"])]
        + [("server jar", args.server_jar, lock["server_sha1"])]
        + [(str(p), p, s) for _, p, s in libraries]
        + [(str(p), p, s) for _, p, s in assets]
    ):
        if not path.is_file() or sha1_file(path) != sha1:
            missing.append(label)
    if missing:
        log(f"{len(missing)} files are missing or do not verify:")
        for name in missing[:40]:
            log(f"  {name}")
        return 1

    log(
        f"OK: {version} fully fetched and verified from Mojang's official servers "
        f"({len(libraries)} libraries, {len(assets)} assets)."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, required=True, help="research-lock.json")
    parser.add_argument("--minecraft-dir", type=Path, required=True,
                        help="the Minecraft folder to fill")
    parser.add_argument("--server-jar", type=Path, required=True,
                        help="where to put the pinned server jar")
    parser.add_argument("--jobs", type=int, default=16, help="parallel downloads")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be fetched, download nothing")
    args = parser.parse_args()
    try:
        return run(args)
    except SystemExit:
        raise
    except Exception as error:  # noqa: BLE001 - the reason must reach the log
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
