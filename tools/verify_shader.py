#!/usr/bin/env python3
"""Turn a `horizon-shader-probe` report into an honest shader-support verdict.

The probe (tools/shader-probe) runs the game's real shader-pack loader
(`horizon-shaders`) with no GPU: it discovers packs in a `shaderpacks` folder,
opens them, preprocesses every stage file, loads each dimension's programs and
lowers every module through naga to Metal (MSL), SPIR-V, HLSL and GLSL. This
script reads that report and says, precisely:

  * what the loader can do (discovery, opening, translation, lowering), which is
    testable without a device, and
  * what that does NOT prove (that a pack renders as its author intended on a
    phone), which needs a physical iPad.

It never reports unsupported functionality as working. If the probe found no
packs, or any module failed to lower, this exits non-zero and says so.

Usage:
  python3 tools/verify_shader.py --report shader-report.json [--expect-pack NAME]...
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# What this workflow can and cannot establish about shaders on Horizon iOS.
# Each entry is (claim, status) with status one of: verified, unverified, limited.
FINDINGS: list[tuple[str, str, str]] = [
    (
        "Pack discovery",
        "verified",
        "A `shaderpacks/` folder is scanned one level deep and its `shaders/`-format "
        "packs and zips are found (horizon-shaders::discover).",
    ),
    (
        "Pack opening and metadata",
        "verified",
        "Each pack opens as a folder or zip; `shaders.properties`, the `block/item/"
        "entity/dimension` id files and `en_us.lang` are parsed.",
    ),
    (
        "Preprocessing",
        "verified",
        "Every `.vsh/.fsh/.gsh/.csh` stage file in the base and world0/world-1/world1 "
        "folders goes through the GLSL preprocessor with the game's macros.",
    ),
    (
        "Translation and lowering",
        "verified",
        "Each dimension's programs are translated to naga modules and lowered to "
        "Metal (MSL), SPIR-V, HLSL and GLSL. This is the step that decides whether "
        "the loader can hand a module to the renderer.",
    ),
    (
        "Renderer module acceptance (wgpu)",
        "verified",
        "Every lowered module is handed to the renderer's own call, wgpu's "
        "`create_shader_module(ShaderSource::Naga(..))`, on a headless wgpu adapter "
        "(software is enough). This is the boundary the game crosses after lowering; "
        "it is checked here, not assumed.",
    ),
    (
        "Shader configuration and pack metadata preserved",
        "verified",
        "The pack's options, screens, profiles and `shaders.properties` keys are read "
        "into the pack's settings (horizon-shaders::options/screens).",
    ),
    (
        "On-device rendering",
        "unverified",
        "Lowering to MSL is not proof a pack renders as intended. The repository's "
        "own iOS notes say device rendering of third-party packs is not established; "
        "that needs a physical iPad.",
    ),
    (
        "OpenGL",
        "limited",
        "The iOS build does not use OpenGL: it renders through wgpu on Metal. Packs "
        "are not run as raw OpenGL; their GLSL is translated, so an OpenGL-only "
        "feature that does not translate cannot run.",
    ),
    (
        "Metal (iOS renderer)",
        "limited",
        "Metal is the iOS backend and the probe lowers to MSL, so the translation "
        "target exists. But the repository records an outstanding translation lead "
        "for one program's `vaPosition` attribute in an external pack, unisolated; "
        "a pack using it may fail to compile on device.",
    ),
    (
        "Shader support is genuinely functional?",
        "limited",
        "The loader path is implemented and testable: discovery, parsing, "
        "translation, MSL lowering and wgpu's own create_shader_module all pass here. "
        "That is not the same as packs rendering correctly on a phone, which this "
        "workflow cannot confirm.",
    ),
]


def verdict_label(status: str) -> str:
    return {"verified": "PASS (loader level)", "unverified": "NOT VERIFIED", "limited": "LIMITED"}[status]


def summarise(report: dict, expected: list[str], expect_config: bool = False,
              require_backend: str | None = None) -> int:
    packs = report.get("packs", [])
    failures = report.get("failures", [])
    problems: list[str] = []

    # The `gpu` stage is what makes module acceptance a checked fact rather than an
    # assumption. A report without it is a weaker run; say so instead of passing quietly.
    accepted = report.get("modules_accepted_by_wgpu")
    adapter = report.get("wgpu_adapter")
    if accepted is None:
        problems.append(
            "the probe report has no `modules_accepted_by_wgpu`: it was built without the "
            "`gpu` feature, so wgpu's create_shader_module was never exercised"
        )
    elif not accepted:
        problems.append(
            "no headless wgpu adapter was available, so module acceptance by "
            "create_shader_module was not checked"
        )
    else:
        print(f"wgpu adapter: {adapter}")

    # When a specific backend was required (a real GPU), the report must show the
    # modules were accepted on that backend -- not merely on some adapter.
    if require_backend is not None:
        backend = (report.get("wgpu_backend") or "").lower()
        if not accepted:
            problems.append(
                f"a {require_backend} backend was required but the modules were not accepted "
                "by any adapter"
            )
        elif backend != require_backend.lower():
            problems.append(
                f"a {require_backend} backend was required but the probe ran on {backend or '?'}"
            )
        else:
            print(f"Backend required and used: {report.get('wgpu_backend')}")

    print(f"Packs discovered: {len(packs)}")
    total_modules = 0
    total_lowers = 0
    diag_errors = 0
    diag_warnings = 0
    for pack in packs:
        name = pack.get("name") or pack.get("path")
        if not pack.get("opened", True):
            problems.append(f"{name}: could not be opened: {pack.get('error')}")
            print(f"  {name}: FAILED to open")
            continue
        modules = pack.get("modules_lowered", 0)
        backends = pack.get("backend_failures", [])
        gpu = pack.get("gpu_failures", [])
        total_modules += modules
        total_lowers += modules * 4
        # Every note the loader recorded, classified. A skipped program is fatal; the
        # rest are kept and counted, never dropped.
        notes = pack.get("notes", [])
        skipped = [n for n in notes if n.get("severity") == "error"]
        diag_errors += len(skipped) + pack.get("preprocess_diagnostics", 0)
        diag_warnings += len(notes) - len(skipped) + pack.get("preprocess_warnings", 0)
        state = "ok" if pack.get("applies") and not backends and not gpu and not skipped else "FAILED"
        print(
            f"  {name}: {pack.get('stage_files', 0)} stage files, {modules} modules, "
            f"{len(backends)} backend failures, {len(gpu)} wgpu rejections, "
            f"{len(skipped)} skipped programs, "
            f"{pack.get('preprocess_diagnostics', 0)} preprocess diagnostics -> {state}"
        )
        for line in backends[:5]:
            problems.append(f"{name}: {line}")
        for line in gpu[:5]:
            problems.append(f"{name}: wgpu rejected: {line}")
        for note in skipped[:5]:
            problems.append(f"{name}: program skipped: {note.get('note')}")
        # The pack's options, screens and profiles are the loader's own readers; report
        # their counts as the evidence behind the "configuration preserved" finding.
        opts = pack.get("options", {})
        screens = pack.get("screens", {})
        profiles = pack.get("profiles", {})
        print(
            f"      config: {opts.get('count', 0)} options, {screens.get('count', 0)} screens, "
            f"{profiles.get('count', 0)} profiles, {pack.get('shaders_properties_keys', 0)} properties keys"
        )
        if not pack.get("config_read", True):
            problems.append(f"{name}: options/screens/profiles were not read")
        if pack.get("preprocess_diagnostics_detail"):
            for d in pack["preprocess_diagnostics_detail"][:5]:
                print(f"      diagnostic: {d.get('file')}:{d.get('line')}: {d.get('message')}")
        for note in notes[:5]:
            if note.get("severity") != "error":
                print(f"      note: {note.get('note')}")

    print(f"Diagnostics: {diag_errors} error-level, {diag_warnings} warning/info-level")

    for line in failures:
        problems.append(line)

    if not packs:
        problems.append("no shader packs were discovered: the probe read nothing")

    for want in expected:
        if not any((p.get("name") or "") == want or want in (p.get("path") or "") for p in packs):
            problems.append(f"expected pack {want!r} was not discovered")

    # `--expect-config` turns the "configuration and pack metadata preserved" finding
    # into a checked fact: at least one pack must have produced a non-zero number of
    # options, screens and profiles through the loader's own readers. A report where
    # those are all zero cannot support that claim.
    if expect_config:
        richest = max(
            packs,
            key=lambda p: (
                p.get("options", {}).get("count", 0)
                + p.get("screens", {}).get("count", 0)
                + p.get("profiles", {}).get("count", 0)
            ),
            default={},
        )
        counts = (
            richest.get("options", {}).get("count", 0),
            richest.get("screens", {}).get("count", 0),
            richest.get("profiles", {}).get("count", 0),
        )
        if not all(count > 0 for count in counts):
            problems.append(
                "--expect-config was given but no pack produced non-zero options, screens and "
                f"profiles (best pack: {richest.get('name')!r}: "
                f"{counts[0]} options, {counts[1]} screens, {counts[2]} profiles)"
            )
        else:
            print(
                f"Configuration read: {richest.get('name')} -> "
                f"{counts[0]} options, {counts[1]} screens, {counts[2]} profiles"
            )

    print(f"Modules lowered: {total_modules} across 4 backends ({total_lowers} lowers)")

    print("\nShader support findings:")
    for claim, status, detail in FINDINGS:
        print(f"  [{verdict_label(status)}] {claim}")
        print(f"      {detail}")

    report["findings"] = [
        {"claim": claim, "status": status, "detail": detail} for claim, status, detail in FINDINGS
    ]
    report["problems"] = problems
    report["ok"] = not problems

    if problems:
        print("\nSHADER VERIFICATION FAILED:")
        for problem in problems:
            print(f"  {problem}")
        return 1

    print(
        "\nShader loader verification: PASS.\n"
        "The loader discovers, parses, translates and lowers every pack, and wgpu's "
        "create_shader_module accepts every lowered module on a headless adapter.\n"
        "On-device (Metal) rendering: NOT VERIFIED by this workflow -- module acceptance "
        "is necessary but not sufficient; the real Metal backend on a physical iPad is required."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True, help="the probe's JSON report")
    parser.add_argument("--write", type=Path, help="where to write the annotated report")
    parser.add_argument(
        "--expect-pack",
        action="append",
        default=[],
        help="a pack name that must have been discovered (repeatable)",
    )
    parser.add_argument(
        "--expect-config",
        action="store_true",
        help="require that at least one pack produced non-zero options, screens and profiles",
    )
    parser.add_argument(
        "--require-backend",
        default=None,
        help="require that the report shows module acceptance on this wgpu backend (e.g. metal)",
    )
    args = parser.parse_args()

    if not args.report.is_file():
        print(f"error: {args.report} is missing: run the probe first", file=sys.stderr)
        return 1
    report = json.loads(args.report.read_text())
    status = summarise(report, args.expect_pack, args.expect_config, args.require_backend)

    if args.write:
        args.write.parent.mkdir(parents=True, exist_ok=True)
        args.write.write_text(json.dumps(report, indent=2) + "\n")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
