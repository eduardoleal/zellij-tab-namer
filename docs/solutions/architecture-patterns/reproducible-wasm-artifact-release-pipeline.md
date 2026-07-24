---
title: Reproducible WASM artifact and release pipeline
date: 2026-07-22
category: architecture-patterns
module: zellij-tab-namer
problem_type: architecture_pattern
component: development_workflow
severity: medium
applies_when:
  - "A native Zellij plugin needs one reproducible path from source checks to a downloadable WASM artifact."
  - "Version tags must publish immutable GitHub Release assets that match the Cargo package version."
tags:
  - github-actions
  - wasm
  - rust
  - artifact-packaging
  - release-automation
  - supply-chain
related_components:
  - testing_framework
  - tooling
---

# Reproducible WASM artifact and release pipeline

## Context

The native Zellij plugin needs more than a successful developer-machine build: consumers need a reproducible `.wasm` file, a checksum for installation, and a release path that cannot silently publish a tag for the wrong package version. The repository therefore treats checking, compiling, packaging, and publishing as one artifact pipeline.

[PR #2](https://github.com/eduardoleal/zellij-tab-namer/pull/2) introduced this pipeline alongside the native plugin and is merged. Its `Build WASM artifact` check passed; `Publish GitHub release` was skipped because that run was not a version-tag push. The same head was verified locally with formatting, locked Clippy, library tests, and a locked `wasm32-wasip1` release build—the same checks encoded in `.github/workflows/wasm.yml:39-49`.

## Guidance

Keep artifact production in one build job and make every later stage consume that exact output. The workflow triggers for pull requests, pushes to `main`, matching version tags, and manual runs (`.github/workflows/wasm.yml:3-10`). It pins the Rust version and WASM target, then runs dependency-sensitive Cargo commands with `--locked` (`.github/workflows/wasm.yml:19-23`, `.github/workflows/wasm.yml:33-49`). A changed or incomplete lockfile therefore fails instead of silently resolving different dependencies.

```yaml
- run: cargo fmt --all -- --check
- run: cargo clippy --locked --all-targets -- -D warnings
- run: cargo test --locked --lib
- run: cargo build --locked --release \
    --target "$WASM_TARGET" --bin "$WASM_BINARY"
```

Package the compiled binary under a stable consumer-facing name and generate its checksum in the same directory. The workflow copies the target output into `dist/` (a runtime-generated directory, not tracked source), creates `zellij-tab-namer.wasm.sha256`, and uploads both files as the `zellij-tab-namer-wasm` workflow artifact; missing files fail the step (`.github/workflows/wasm.yml:51-68`). The binary name and source path are explicit in `Cargo.toml:9-12`.

```yaml
- name: Package artifact
  run: |
    mkdir -p dist
    install -m 0644 \
      "target/$WASM_TARGET/release/$WASM_BINARY.wasm" \
      "dist/$WASM_BINARY.wasm"
    cd dist
    sha256sum "$WASM_BINARY.wasm" > "$WASM_BINARY.wasm.sha256"
```

Gate publication at the job level. The release job runs only for a pushed `v*` tag and only after the build succeeds (`.github/workflows/wasm.yml:70-76`). Before publishing, it derives the package version with locked Cargo metadata and requires the tag to equal `v<package-version>` (`.github/workflows/wasm.yml:81-90`). It downloads the build job's artifact and verifies its checksum instead of rebuilding independently (`.github/workflows/wasm.yml:92-99`).

Treat published assets as immutable. The workflow permits release creation only when GitHub returns `404` for that tag; it refuses to replace an existing release before invoking `gh release create` with the verified WASM and checksum (`.github/workflows/wasm.yml:101-130`). The operator instructions use the same contract: update the Cargo version, push the matching tag, and use a new version when bytes change (`README.md:178-191`).

Review API compatibility against the complete locked dependency graph. The direct dependency is exactly `zellij-tile = "=0.44.0"` (`Cargo.toml:6-8`), while `Cargo.lock:2534-2553` resolves its `zellij-utils` dependency to `0.44.3`. The plugin subscribes to and handles `CommandChanged` (`rust/src/main.rs:43-49`, `rust/src/main.rs:52-63`), and the locked Clippy and WASM release builds passed in PR #2. A direct dependency version alone is not enough evidence to reject an API used by the fully locked graph.

## Why This Matters

This creates one chain of custody for distributable bytes: quality checks precede compilation, packaging writes a checksum beside the binary, workflow storage carries those exact files between jobs, and the release job verifies them before publication. The released artifact is the artifact CI checked—not a separate build that merely resembles it.

The trigger and permission boundaries also prevent accidental publication. Ordinary runs exercise the build with repository contents read-only; only the tag-gated release job receives `contents: write` (`.github/workflows/wasm.yml:12-13`, `.github/workflows/wasm.yml:70-76`). A skipped release job outside a version-tag push is expected behavior, not a partial failure.

Exact toolchain, action, and dependency inputs improve reproducibility. The workflow fixes the Rust version, pins third-party actions to commit hashes, and makes Cargo honor the committed lockfile (`.github/workflows/wasm.yml:19-22`, `.github/workflows/wasm.yml:30-31`, `.github/workflows/wasm.yml:60-61`, `.github/workflows/wasm.yml:92-93`).

## When to Apply

- When a repository distributes a compiled plugin or standalone binary through both CI artifacts and tagged releases.
- When users install by URL plus an expected digest; this installer requires HTTPS and SHA-256 for remote WASM sources (`README.md:96-102`).
- When pull requests should prove buildability without gaining permission to publish releases.
- When release bytes must be immutable and traceable to one reviewed build job.

If a future project needs mutable prereleases or multiple platform artifacts, change the policy deliberately rather than weakening the existing-tag refusal or rebuilding independently in the release job.

## Examples

Reproduce the build job locally before pushing:

```sh
cargo fmt --all -- --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked --lib
cargo build --locked --release \
  --target wasm32-wasip1 \
  --bin zellij-tab-namer
```

The shared output path is `target/wasm32-wasip1/release/zellij-tab-namer.wasm` (`README.md:81-93`, `.github/workflows/wasm.yml:51-56`). A normal pull request should finish with a successful build job and a skipped release job.

For a release, make the package version and tag agree:

```sh
# Cargo.toml: version = "0.2.0"
git tag v0.2.0
git push origin v0.2.0
```

The tag push rebuilds and packages the WASM and checksum, checks that the tag matches the Cargo version, downloads and verifies the exact workflow artifact, and publishes both files. Reusing the tag after a release exists fails intentionally; publish a new version and tag instead.

## Related

- [PR #2: add native WASM tab naming and releases](https://github.com/eduardoleal/zellij-tab-namer/pull/2)
- `.github/workflows/wasm.yml` — executable build and release contract
- `README.md:178-191` — operator-facing release procedure
- `Cargo.toml:1-22` and `Cargo.lock:2534-2553` — version and dependency inputs
