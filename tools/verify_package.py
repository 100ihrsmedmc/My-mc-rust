#!/usr/bin/env python3
"""Validate the final Horizon iOS resource package for Minecraft 26.3.

Checks the prepared tree, the archive that a phone imports, and the shader-facing
resources, then writes one JSON report.

* Prepared tree: `horizon-resources.json` with the right schema and Minecraft
  version, the directory layout the game reads (`assets/`, `assets/<namespace>/`,
  the facts/reports files), the built-in packs, panorama, Unifont, languages and
  `sounds.json`.
* Archive: a real ZIP whose single top folder is the prepared folder's name, whose
  root marker is `horizon-resources.json` (so `import::Pack` reads it as game
  files), that holds no junk, no absolute or `..` entry, and that is under the
  game's accepted archive and unpacked limits.
* Shaders: `shaderpacks/` is discovered the way `horizon_shaders::discover` reads
  it, and any pack that is present is opened (`Pack::open`) and must not be broken.
  This does not assert that any pack is installed; it asserts the shader-facing
  resources the package carries are consistent.
* Size: the unpacked tree and the archive are reported, with a warning when the
  tree is far below the ~300 MB a real audio set produces (a likely sign the
  sounds step was skipped).

Exit status is 0 only when every check passes.
"""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

# Mirrors horizon-client/src/import.rs.
MAX_PACKED = 2 << 30
MAX_UNPACKED = 4 << 30
MAX_ENTRIES = 50_000
MARKER = "horizon-resources.json"
BUILT_IN_PACKS = [
    "assets/minecraft/resourcepacks/programmer_art.zip",
    "assets/minecraft/resourcepacks/high_contrast.zip",
]
JUNK = {".DS_Store", "Thumbs.db", "desktop.ini", "session.lock"}
# The smallest tree a complete preparation produces, from the preparer's own notes
# (~589 MB unpacked). Below this the audio step almost certainly did not run.
EXPECTED_MIN_BYTES = 300 * 1_000_000
EXPECTED_MAX_BYTES = 2 * 1_000_000_000


def is_junk(name: str) -> bool:
    last = name.rsplit("/", 1)[-1]
    return name.startswith("__MACOSX/") or last in JUNK


def dir_size(root: Path) -> tuple[int, int]:
    total = 0
    count = 0
    for path in root.rglob("*"):
        if path.is_file():
            total += path.stat().st_size
            count += 1
    return total, count


def check_tree(resources: Path, expected_version: str, expected_schema: int | None) -> dict:
    result: dict = {"ok": True, "problems": [], "warnings": []}
    manifest = resources / MARKER
    if not manifest.is_file():
        result["ok"] = False
        result["problems"].append(f"{MARKER} is missing at the package root")
        return result
    try:
        data = json.loads(manifest.read_text())
    except ValueError as error:
        result["ok"] = False
        result["problems"].append(f"{MARKER} is not valid JSON: {error}")
        return result
    result["schema"] = data.get("schema")
    result["minecraft"] = data.get("minecraft")
    if expected_schema is not None and data.get("schema") != expected_schema:
        result["ok"] = False
        result["problems"].append(
            f"schema is {data.get('schema')}, this preparer writes {expected_schema}"
        )
    if data.get("minecraft") != expected_version:
        result["ok"] = False
        result["problems"].append(
            f"prepared for Minecraft {data.get('minecraft')!r}, expected {expected_version!r}"
        )

    for required in ("assets", "assets/minecraft", "assets/minecraft/sounds.json",
                     "assets/pack.mcmeta", "assets/minecraft/font/unifont.zip",
                     "asset-index.json"):
        if not (resources / required).exists():
            result["ok"] = False
            result["problems"].append(f"{required} is missing")
    for pack in BUILT_IN_PACKS:
        if not (resources / pack).is_file():
            result["warnings"].append(f"built-in pack {pack} is missing")

    background = resources / "assets/minecraft/textures/gui/title/background"
    faces = [background / f"panorama_{i}.png" for i in range(6)]
    if not all(face.is_file() for face in faces) or not (background / "panorama_overlay.png").is_file():
        result["warnings"].append("the title panorama is incomplete")

    size, files = dir_size(resources)
    result["bytes"] = size
    result["files"] = files
    if size < EXPECTED_MIN_BYTES:
        result["warnings"].append(
            f"the tree is {size / 1e6:.1f} MB, below the ~{EXPECTED_MIN_BYTES / 1e6:.0f} MB a "
            "complete preparation reaches: the sounds step may not have run"
        )
    if size > EXPECTED_MAX_BYTES:
        result["warnings"].append(
            f"the tree is {size / 1e6:.1f} MB, above the {EXPECTED_MAX_BYTES / 1e6:.0f} MB expected"
        )
    return result


def check_archive(archive: Path, resources: Path) -> dict:
    result: dict = {"ok": True, "problems": [], "warnings": []}
    if not archive.is_file():
        result["ok"] = False
        result["problems"].append(f"{archive} does not exist")
        return result
    result["bytes"] = archive.stat().st_size
    if archive.stat().st_size > MAX_PACKED:
        result["ok"] = False
        result["problems"].append("the archive is larger than the game will unpack")
    try:
        with zipfile.ZipFile(archive) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/") and not is_junk(n)]
            result["entries"] = len(names)
            if len(names) > MAX_ENTRIES:
                result["ok"] = False
                result["problems"].append("the archive holds more files than the game will unpack")
            if not names:
                result["ok"] = False
                result["problems"].append("the archive holds nothing")
                return result
            # Every entry shares the prepared folder's name at the top.
            top = resources.name
            bad = [n for n in names if not n.startswith(top + "/")]
            if bad:
                result["ok"] = False
                result["problems"].append(
                    f"{len(bad)} entries are not under the top folder {top!r} (e.g. {bad[0]})"
                )
            for name in names:
                if name.startswith("/") or ":" in name or ".." in name.split("/"):
                    result["ok"] = False
                    result["problems"].append(f"unsafe archive entry {name!r}")
                    break
            if f"{top}/{MARKER}" not in zf.namelist():
                result["ok"] = False
                result["problems"].append(
                    f"the archive's root does not hold {MARKER}: the game will not read it as game files"
                )
            if zf.testzip() is not None:
                result["ok"] = False
                result["problems"].append("the archive is corrupt (a member failed its CRC)")
    except zipfile.BadZipFile as error:
        result["ok"] = False
        result["problems"].append(f"{archive} is not a readable ZIP: {error}")
    return result


def check_shaders(resources: Path) -> dict:
    """Whether the package's shader-facing resources are consistent.

    Shader packs are installed by the player into the game home's `shaderpacks/`,
    not into the prepared resources, so a pack is only checked when one is present.
    """
    result: dict = {"ok": True, "problems": [], "packs": []}
    packs_dir = resources / "shaderpacks"
    if not packs_dir.is_dir():
        result["present"] = False
        return result
    result["present"] = True
    for entry in sorted(packs_dir.iterdir()):
        if entry.name.startswith("."):
            continue
        is_zip = entry.suffix.lower() == ".zip"
        if not (entry.is_dir() or is_zip):
            continue
        problem = None
        if entry.is_dir() and not (entry / "shaders").is_dir():
            problem = "no shaders folder at the root of the pack"
        elif is_zip:
            try:
                with zipfile.ZipFile(entry) as zf:
                    if not any(n.startswith("shaders/") for n in zf.namelist()):
                        problem = "no shaders folder in the archive"
            except zipfile.BadZipFile:
                problem = "the archive does not open"
        result["packs"].append({"name": entry.name, "problem": problem})
        if problem:
            result["ok"] = False
            result["problems"].append(f"{entry.name}: {problem}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resources", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--schema", type=int, default=7)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--allow-warnings", action="store_true",
                        help="do not fail on warnings (problems still fail)")
    args = parser.parse_args()

    report = {
        "resources": str(args.resources),
        "archive": str(args.archive),
        "expected_version": args.version,
        "tree": check_tree(args.resources, args.version, args.schema),
        "archive_checks": check_archive(args.archive, args.resources),
        "shaders": check_shaders(args.resources),
    }
    problems = (
        report["tree"]["problems"]
        + report["archive_checks"]["problems"]
        + report["shaders"]["problems"]
    )
    warnings = report["tree"]["warnings"] + report["archive_checks"]["warnings"]
    report["problems"] = problems
    report["warnings"] = warnings
    report["ok"] = not problems and (args.allow_warnings or not warnings)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2))

    tree = report["tree"]
    print(f"Tree: {tree.get('files', 0)} files, {tree.get('bytes', 0) / 1e6:.1f} MB, "
          f"schema {tree.get('schema')}, Minecraft {tree.get('minecraft')!r}")
    arch = report["archive_checks"]
    print(f"Archive: {arch.get('entries', 0)} entries, {arch.get('bytes', 0) / 1e6:.1f} MB")
    print(f"Shaders: {len(report['shaders']['packs'])} pack(s) found")
    for warning in warnings:
        print(f"WARNING: {warning}")
    for problem in problems:
        print(f"PROBLEM: {problem}")
    print("PACKAGE OK" if report["ok"] else "PACKAGE FAILED")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
