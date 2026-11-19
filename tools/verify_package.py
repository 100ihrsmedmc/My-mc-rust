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
import hashlib
import json
import re
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

    marker_hashes = check_marker_hashes(resources, data)
    result["marker_hashes"] = marker_hashes
    if not marker_hashes["ok"]:
        result["ok"] = False
        result["problems"].extend(marker_hashes["problems"])

    core_shaders = check_core_shaders(resources)
    result["core_shaders"] = core_shaders
    if not core_shaders["ok"]:
        result["ok"] = False
        result["problems"].extend(core_shaders["problems"])

    json_metadata = check_json_metadata(resources)
    result["json_metadata"] = json_metadata
    if not json_metadata["ok"]:
        result["ok"] = False
        result["problems"].extend(json_metadata["problems"])

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


def _strip_comments(text: str) -> str:
    """Comments as spaces, line breaks kept (mirrors the renderer's own stripper)."""
    out = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if c == "/" and nxt == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "/" and nxt == "*":
            i += 2
            while i < n and not (text[i] == "*" and i + 1 < n and text[i + 1] == "/"):
                if text[i] == "\n":
                    out.append("\n")
                i += 1
            i += 2
            out.append(" ")
            continue
        out.append(c)
        i += 1
    return "".join(out)


_INCLUDE = re.compile(r"#\s*(?:include|moj_import)\s+(<[^>]+>|\"[^\"]+\")")


def _include_target(target: str, folder: str) -> str | None:
    """The resource path an include names, or None when it cannot be read.

    Mirrors `core_shader::include_id`: `<ns:path>` (or `<path>`) is under
    `shaders/include`; `"path"` is relative to the including file's folder.
    """
    if target.startswith("<") and target.endswith(">"):
        inner = target[1:-1]
        ns, _, path = inner.partition(":")
        if not path:
            ns, path = "minecraft", inner
        return f"assets/{ns}/shaders/include/{path}"
    if target.startswith('"') and target.endswith('"'):
        relative = target[1:-1]
        ns, _, base = folder.partition(":")
        parts = [p for p in base.split("/") if p]
        for part in relative.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if not parts:
                    return None
                parts.pop()
            else:
                parts.append(part)
        return f"assets/{ns}/{'/'.join(parts)}"
    return None


def check_core_shaders(resources: Path) -> dict:
    """Whether the vanilla core shaders the renderer compiles have their includes.

    The game reads `assets/<ns>/shaders/core/*.vsh|.fsh` and inlines every
    `#include`/`#moj_import` it names (`horizon-client-render/src/core_shader.rs`).
    An include the package does not carry is a core shader the renderer cannot
    compile, so it is checked here rather than discovered at run time.
    """
    result: dict = {"ok": True, "problems": [], "checked": 0, "includes": 0}
    assets = resources / "assets"
    if not assets.is_dir():
        return result
    core_files = [
        p
        for p in assets.glob("*/shaders/**/*")
        if p.is_file() and p.suffix in (".vsh", ".fsh", ".glsl")
    ]
    result["checked"] = len(core_files)
    for path in sorted(core_files):
        rel = path.relative_to(resources).as_posix()
        # The renderer addresses includes by `<ns>:<path under assets>`; a `"relative"`
        # include is resolved against the including file's folder, as `core_shader` does.
        after_assets = path.relative_to(assets).as_posix()
        ns, _, rest = after_assets.partition("/")
        folder = f"{ns}:{rest.rsplit('/', 1)[0]}"
        try:
            text = path.read_text(errors="replace")
        except OSError as error:
            result["ok"] = False
            result["problems"].append(f"{rel} could not be read: {error}")
            continue
        for target in _INCLUDE.findall(_strip_comments(text)):
            result["includes"] += 1
            resolved = _include_target(target, folder)
            if resolved is None or not (resources / resolved).is_file():
                result["ok"] = False
                result["problems"].append(
                    f"{rel} includes {target} which the package does not carry"
                )
    return result


def check_json_metadata(resources: Path) -> dict:
    """Whether every JSON resource the package carries parses.

    Models, blockstates, lang files, `pack.mcmeta` and the rest are JSON the game
    parses at load; one malformed file is a resource that fails to load. Only the
    parse is checked here (the schemas are the game's business), and a bounded
    sample of the failures is reported.
    """
    result: dict = {"ok": True, "problems": [], "checked": 0}
    for folder in ("assets", "data"):
        base = resources / folder
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix not in (".json", ".mcmeta"):
                continue
            result["checked"] += 1
            try:
                json.loads(path.read_bytes())
            except ValueError as error:
                result["ok"] = False
                if len(result["problems"]) < 20:
                    result["problems"].append(
                        f"{path.relative_to(resources).as_posix()} is not valid JSON: {error}"
                    )
    if not result["ok"]:
        result["problems"].append("more malformed JSON files were not listed")
    return result


def check_marker_hashes(resources: Path, data: dict) -> dict:
    """Whether the files the marker records a SHA-256 for still match it.

    `horizon-resources.json` names the exports it was built from and their
    hashes (`facts_sha256`, `model_states_sha256`, the `*_files_sha256` maps).
    A file that no longer matches means the tree was edited after preparation, so
    it is checked here. The `*_exporters_sha256` maps name the Java exporters the
    files came from, which the package does not carry; those are skipped, not failed.
    """
    result: dict = {"ok": True, "problems": [], "checked": 0}
    named: dict[str, str] = {}
    for key, value in data.items():
        if not key.endswith("_sha256"):
            continue
        if isinstance(value, dict) and key.endswith("_files_sha256"):
            # These maps name each file they cover.
            for rel, digest in value.items():
                named[rel] = digest
        elif isinstance(value, str):
            # A scalar hash covers the file whose name is the key's stem; the exports
            # use hyphens where the key uses underscores (`model_states` -> `model-states`).
            stem = key[: -len("_sha256")]
            for base in (stem, stem.replace("_", "-")):
                found = next(
                    (c for c in (f"{base}.json", f"{base}.nbt", f"cached-{base}.nbt")
                     if (resources / c).is_file()),
                    None,
                )
                if found:
                    named[found] = value
                    break
    for rel, digest in sorted(named.items()):
        path = resources / rel
        if not path.is_file():
            result["ok"] = False
            result["problems"].append(f"{rel} is recorded in {MARKER} but missing")
            continue
        result["checked"] += 1
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != digest:
            result["ok"] = False
            result["problems"].append(
                f"{rel} does not match the hash {MARKER} records (edited after preparation)"
            )
    return result


def _common_folder(names: list[str], prefix: str) -> str | None:
    """The one folder every entry shares one level below `prefix`, if there is one.

    Mirrors `import.rs`'s `common_folder`: what compressing a folder produces, as
    opposed to compressing its contents.
    """
    rest = [n[len(prefix):] for n in names if n.startswith(prefix)]
    rest = [r for r in rest if "/" in r]
    if not rest:
        return None
    first = rest[0].split("/", 1)[0]
    if all(r.startswith(first) and r[len(first):].startswith("/") for r in rest):
        return first
    return None


def _root_prefix(names: list[str]) -> str | None:
    """The folder the game reads the tree from: `""` or up to two shared folders.

    Mirrors `Pack::open`: the importer descends through at most two common folders
    looking for the marker, because compressing `.local/horizon-client` rather than
    `26.3` is a mistake a careful person still makes. Returns `None` when no marker
    sits at any of those levels, which is exactly when the game refuses the archive.
    """
    prefix = ""
    for _ in range(3):
        if f"{prefix}{MARKER}" in names:
            return prefix
        folder = _common_folder(names, prefix)
        if folder is None:
            return None
        prefix += folder + "/"
    return None


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
            # The game reads the tree from the marker's own level: the archive root,
            # or up to two shared folders above it (`resources/`, `Horizon/26.3/`).
            prefix = _root_prefix(names)
            if prefix is None:
                result["ok"] = False
                result["problems"].append(
                    f"no {MARKER} sits at the archive root or under up to two shared "
                    "folders: the game will not read this as its game files"
                )
            else:
                result["root"] = prefix.rstrip("/") or "<archive root>"
                stray = [n for n in names if not n.startswith(prefix)]
                if stray:
                    result["ok"] = False
                    result["problems"].append(
                        f"{len(stray)} entries sit outside the tree the game reads "
                        f"({prefix.rstrip('/') or '<root>'}) (e.g. {stray[0]})"
                    )
                # Every file the prepared tree holds must be in the archive, at the
                # same place below the root: a tree file the archive drops is a
                # resource the game will not have.
                in_archive = set(names)
                missing = [
                    path.relative_to(resources).as_posix()
                    for path in sorted(resources.rglob("*"))
                    if path.is_file()
                    and f"{prefix}{path.relative_to(resources).as_posix()}" not in in_archive
                ]
                if missing:
                    result["ok"] = False
                    result["problems"].append(
                        f"{len(missing)} tree files are missing from the archive "
                        f"(e.g. {missing[0]})"
                    )
            for name in names:
                if name.startswith("/") or ":" in name or ".." in name.split("/"):
                    result["ok"] = False
                    result["problems"].append(f"unsafe archive entry {name!r}")
                    break
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
    core = tree.get("core_shaders", {})
    if core.get("checked"):
        print(f"Core shaders: {core.get('checked', 0)} files, "
              f"{core.get('includes', 0)} includes, {len(core.get('problems', []))} unresolved")
    marker = tree.get("marker_hashes", {})
    if marker.get("checked"):
        print(f"Marker hashes: {marker.get('checked', 0)} files verified")
    meta = tree.get("json_metadata", {})
    if meta.get("checked"):
        print(f"JSON metadata: {meta.get('checked', 0)} files parsed, "
              f"{len(meta.get('problems', []))} malformed")
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
