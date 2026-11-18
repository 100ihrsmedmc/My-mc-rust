# Horizon iOS resources — Minecraft: Java Edition 26.3

This repository builds and verifies the resource package [Horizon iOS](https://github.com/datlun2k11-pixel/a-bit-better-horizon-ios)
needs for Minecraft: Java Edition **26.3**, and uploads it as a GitHub Actions
artifact named **`horizon-resources`**.

The package is ~400 MB and is what the phone imports on first run.

## How to run it

1. Open the **Actions** tab.
2. Pick **Prepare Horizon resources** on the left.
3. Click **Run workflow**. The defaults are fine; the inputs are:
   - `source_repo` — the Horizon iOS source to build the preparer from
     (default `datlun2k11-pixel/a-bit-better-horizon-ios`),
   - `source_ref` — its branch, tag or commit (default `main`),
   - `verify_shaders` — build and run the headless shader probe (default on),
   - `verify_metal` — also run the probe on a macOS runner and require the real
     Metal backend (default off; needs a GitHub-hosted macOS runner).
4. Wait for the job to finish (roughly 20–40 minutes: the Rust build dominates,
   then the resource preparation, packing and checks). There is a second,
   optional job `metal` that re-runs the shader probe on a macOS runner and
   requires the real Metal backend; it is **off by default** because not every
   account can provision a GitHub-hosted macOS runner (see "Shaders" below).

Command line, if you prefer:

```sh
gh workflow run prepare-game-files.yml \
  -f source_repo=datlun2k11-pixel/a-bit-better-horizon-ios \
  -f source_ref=main
gh run watch
```

## Where to download the ZIP

When the run finishes, open it and download the **`horizon-resources`** artifact
from the run's *Artifacts* box (bottom of the run summary). It is a ZIP that
contains:

| File | What it is |
|---|---|
| `26.3.zip` | the resource package the phone imports (`horizon-client --pack-resources` writes exactly this) |
| `SHA256SUMS.txt` | the SHA-256 of `26.3.zip` |
| `VALIDATION-REPORT.md` | the full validation report (audio, package, shaders) |
| `reports/audio-report.json` | the OGG verification report |
| `reports/package-report.json` | the archive/tree verification report |
| `reports/shader-report.json` | the raw shader-probe report |
| `reports/shader-findings.json` | the shader report with the findings table |
| `reports/meta.json` | the version and the source commit that produced the package |

Unzip it and give `26.3.zip` to Horizon iOS.

## What the package contains, and where the files come from

`horizon-prepare --complete` imports the pinned Minecraft 26.3 files and writes
the resources under `assets/` plus a `horizon-resources.json` marker.

The game files are **Mojang's public downloads**, fetched from
`piston-meta.mojang.com`, `libraries.minecraft.net` and
`resources.download.minecraft.net`. No Microsoft account and no game ownership
are needed to fetch them, and each file is checked against the SHA-1 that
`research-lock.json` pins. **Nothing is fabricated and no third-party mirror is
used.** The package is Mojang's assets: keep it private and do not redistribute
it.

## What the workflow verifies

### Audio (OGG) — `tools/verify_ogg.py`

- every required `.ogg` is present (the Minecraft asset index is the source of truth);
- each file is a structurally valid Ogg stream (page CRC, version, page-sequence
  continuity, beginning-of-stream and end-of-stream pages), so empty, truncated
  and corrupted files are caught — not just missing ones;
- the Vorbis/Opus codec header parses with a sane sample rate and channel count;
- the files sit at the Minecraft 26.3 layout `assets/<namespace>/sounds/...`;
- every sound event in `sounds.json` names files that all exist;
- the prepared bytes are identical to the originals, by SHA-1 against the asset
  index, so the original audio is preserved unchanged.

It prints the OGG count, the failed files and the missing required audio
resources, and fails the run if any is non-zero.

### Shaders (Iris / OptiFine packs) — `tools/shader-probe` + `tools/verify_shader.py`

The probe builds against the game's real loader (`horizon-shaders`) and, with no
GPU, discovers packs in a `shaderpacks/` folder, opens them, preprocesses every
stage file, loads each dimension's programs and lowers every module through
`naga` to **Metal (MSL)**, SPIR-V, HLSL and GLSL. Built with `--features gpu`
(as the workflow does), it then hands every lowered module to the renderer's own
call, wgpu's `create_shader_module(ShaderSource::Naga(..))`, against a headless
software adapter (lavapipe), so module acceptance is checked rather than assumed.
The workflow then runs the same probe again on a **macOS runner** with
`--require-backend metal`: the probe fails rather than fall back if Metal is
unavailable, and `verify_shader.py --require-backend metal` fails unless the
report shows the modules were accepted on Metal. That makes "a real Metal driver
accepts the lowered modules" a checked fact on the backend iOS actually uses.
This macOS job is gated on the `verify_metal` input (default off) because
GitHub-hosted macOS runners are not available to every account; turn it on when
the account can provision `macos-15`.
It runs against the packs the repository ships for this (`post-test`,
`terrain-test`, `config-test`) and the run fails if any pack fails to open, any
module fails to lower, or wgpu rejects a module.

**Be precise about what that proves.** It proves the loader can discover, parse,
translate, lower and hand packs to the renderer, and that a real Metal driver
accepts the lowered modules — the path a pack must survive to be applied. It does
**not** prove a pack renders as its author intended on a phone: module acceptance
is necessary but not sufficient, because it does not exercise the draw calls,
framebuffer formats or per-frame state a phone uses.
The repository's own iOS notes say third-party pack rendering is not established
and needs a physical iPad; the probe's report says so too, and the validation
report keeps that distinction. The shader-support question is answered honestly
in `VALIDATION-REPORT.md`'s findings table, which marks on-device rendering
**NOT VERIFIED** and lists the OpenGL/Metal limitations.

### Package and archive — `tools/verify_package.py`

- the prepared tree's layout and schema (7) and the Minecraft version it claims;
- the archive's integrity, entry count and that every tree file is in it;
- that every `#include`/`#moj_import` the vanilla core shaders name resolves to a
  file the package carries (`assets/<ns>/shaders/...`), so no core shader is left
  uncompilable — the same resolution `horizon-client-render`'s `core_shader` does;
- that every file `horizon-resources.json` records a SHA-256 for still matches it,
  so a tree edited after preparation is caught;
- that every JSON resource (`assets/**/*.json`, `data/**/*.json`, `*.mcmeta`) parses.

## Failure handling

- If a pinned Minecraft file is missing or does not match its SHA-1, the fetch
  step stops and says which file.
- If any check fails, the run fails; no check is skipped silently.
- The workflow never reports shader functionality as working when it is not
  verified, and never fabricates a game asset.
- Nothing in this repository is deleted or overwritten by a run.

## Repository layout

```
.github/workflows/prepare-game-files.yml   the workflow
tools/fetch_minecraft.py                    fetch the pinned files from Mojang
tools/fetch_jdk.py                          fetch the pinned Temurin JDK
tools/verify_ogg.py                         audio (OGG) verification
tools/verify_package.py                     package/archive verification
tools/verify_shader.py                      shader-support verdict
tools/write_report.py                       assemble VALIDATION-REPORT.md
tools/shader-probe/                         the headless shader probe (Rust)
tools/tests/                                regression tests for the tools
```

Run the tool tests locally with:

```sh
python3 -m unittest discover -s tools/tests -t .
```

CI runs the same command before the downloads, so a broken tool fails fast.
