# Notes for agents working in this repository

This repository holds only the tooling that prepares and verifies the Horizon
resource package for Minecraft: Java Edition 26.3. The game's source is checked
out at run time by the workflow (`datlun2k11-pixel/a-bit-better-horizon-ios`),
never vendored here.

## Layout

- `.github/workflows/prepare-game-files.yml` — the whole pipeline: fetch pinned
  Mojang files, build the preparer, prepare, pack, verify, report, upload.
- `tools/` — the verifiers and the report writer (Python, stdlib only). The
  workflow copies this directory to `tooling/` in the runner, so workflow paths
  read `tooling/tools/...`.
- `tools/shader-probe/` — a small Rust crate built against the game's real
  `horizon-shaders` crate to test the pack path with no GPU.

## Invariants that matter

- Every check must mirror the game's own code, not an assumption. The archive
  root check mirrors `import.rs`'s `Pack::open`/`common_folder`: the marker may
  sit at the root or under at most two shared folders, and nothing may sit
  outside that root.
- A check that cannot run must fail or be visibly reported, never silently pass.
- Shader support is *not* "works because files exist": the workflow proves
  discovery, parsing, translation and MSL/SPV/HLSL/GLSL lowering with wgpu, and
  states plainly that on-device rendering is NOT VERIFIED.
- Tests need `ffmpeg` on `PATH` for the OGG cases; without it they skip. Install
  it (`sudo apt-get update && sudo apt-get install -y ffmpeg`) before trusting a
  green run.

## Running the checks locally

```
python3 -m unittest discover -s tools/tests -t .        # all verifiers
python3 tools/verify_package.py --resources <tree>/resources \
    --archive <tree>/26.3.zip --version 26.3 --report /tmp/pkg.json
python3 tools/verify_ogg.py --resources <tree>/resources \
    --asset-index <tree>/resources/asset-index.json --report /tmp/audio.json
```

## Gotchas

- `git push` needs `GH_TOKEN` exported in a fresh shell; the environment resets
  between sessions and drops the toolchain and `ffmpeg`.
- The upstream source can be broken on a feature branch while `main` is fine.
  The workflow builds `main`; check `gh api repos/.../commits/<sha>` and
  `compare/main...<sha>` before blaming the workflow for a red build.
