# Standard CI and Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing GitHub Actions pipeline support repeatable SemVer-tagged Windows releases that stop at a verifiable Draft Release for manual publication.

**Architecture:** Keep the current Ubuntu verification and Windows Rust jobs. Add a small Python standard-library release-version contract checker used by CI and the Windows release workflow. Generalize the existing release workflow to derive artifact metadata from the verified tag, create/update a Draft only, and keep manual dispatch build-only.

**Tech Stack:** GitHub Actions, Python 3.12 (`json`, `tomllib`, `unittest`), PowerShell, Tauri 2, GitHub CLI.

**Spec:** `docs/superpowers/specs/2026-11-11-standard-ci-release-design.md`

## Global Constraints

- Release tags use stable `vMAJOR.MINOR.PATCH` SemVer and match `tauri.conf.json` and the Rust package version.
- Formal tag builds require `docs/releases/<tag>.md`; manual dispatch remains artifact-only.
- Only the release publishing job receives `contents: write` and it leaves the Release in Draft state.
- Do not add signing secrets, real Provider calls, external acceptance tests, or automatic public publishing.
- Do not push tags or publish a Release as part of local implementation or verification.

---

### Task 1: Add and test the release version contract

**Files:**
- Create: `scripts/release_version.py`
- Create: `scripts/test_release_version.py`
- Modify: `.github/workflows/verification.yml`
- Modify: `backend/pyproject.toml`
- Modify: `backend/uv.lock`

**Interfaces:**
- `python scripts/release_version.py --tag vX.Y.Z` validates a stable SemVer tag against the Tauri and Cargo package versions and verifies `docs/releases/vX.Y.Z.md` exists.
- `python scripts/release_version.py --check-current` validates the checked-in Tauri and Cargo versions without requiring a tag or release notes.
- Python runtime, test, and packaging dependencies are installed from `backend/uv.lock`; PyInstaller is in the `build` dependency group.
- The functions are imported by standard-library unit tests and accept explicit file paths so invalid/mismatch cases can use temporary files.

- [x] Write tests for accepted stable tags and rejection of leading zeroes, prerelease tags, malformed tags, Tauri mismatch, Cargo mismatch, and missing notes.
- [x] Run `python3 -m unittest scripts.test_release_version -v` and confirm the new tests fail before implementation.
- [x] Implement the parser/checker with `json` and `tomllib`; print one actionable error and exit nonzero on failure.
- [x] Run the focused tests and verify current repository metadata with `python3 scripts/release_version.py --tag v0.1.0`.
- [x] Add the pinned PyInstaller build dependency group and refresh `backend/uv.lock`.
- [x] Switch Ubuntu CI to `uv sync --locked --extra test` and add the release contract tests to `.github/workflows/verification.yml`.

### Task 2: Generalize Windows release build metadata

**Files:**
- Modify: `.github/workflows/release.yml`
- Modify: `desktop/scripts/windows.ps1`

**Interfaces:**
- Tag trigger matches `v*`; the version checker rejects anything outside stable SemVer before package work begins.
- `workflow_dispatch` builds current source and uploads an artifact without using a tag or touching a GitHub Release.
- Build outputs are `<product>-<version>-x64-setup.exe`, the matching `.sha256`, and a manifest containing version, nullable tag, source commit, target, filename, byte size, and digest.
- GitHub Release and local Windows build-release package the Python engine from the same locked environment; local archive metadata derives its version and installer path from Tauri config.

- [x] Change tag trigger to `v*`, retain `workflow_dispatch`, and add per-ref concurrency with cancellation disabled.
- [x] Invoke the release-version checker for tag builds and `--check-current` for manual builds.
- [x] Install the `build` dependency group with `uv sync --locked` and use the resulting Python 3.12 environment to package the engine.
- [x] Update `desktop/scripts/windows.ps1` to use the same locked Python environment and read app version and NSIS filename from Tauri config.
- [x] Derive installer name, manifest version, tag, checksum validation path, and notes path from validated workflow values; remove every `0.1.0` literal from the workflow.
- [x] Keep artifact upload for both triggers and ensure only tag events enter the Draft creation job.
- [x] Validate the YAML syntax and search the workflow to confirm no version-specific constants remain.

### Task 3: Stop tag runs at an idempotent Draft Release

**Files:**
- Modify: `.github/workflows/release.yml`
- Create: `docs/releases/README.md`

**Interfaces:**
- Tag release job validates checksum, manifest tag/version/commit/target, and the tag-specific notes file before creating/updating a Draft.
- A rerun may replace assets only while the GitHub Release is still a Draft; a published Release causes a clear failure.
- Human publication happens in GitHub after the checklist is complete; no workflow step changes `isDraft` to false.

- [x] Rename the publisher job to reflect Draft creation, preserve job-level `contents: write`, and keep global permissions read-only.
- [x] Create a Draft if absent; update notes and clobber assets if the existing Release is a Draft; reject an already-published Release.
- [x] Remove the command that publishes the Release.
- [x] Add a concise release checklist covering notes, asset names, manifest/commit, SHA-256, Windows installation and first launch, and the unsigned-installer notice.
- [x] Review manual-dispatch and tag paths to prove only the tag path writes a Draft and neither path automatically publishes.

### Task 4: Verify and record the completed stages

**Files:**
- Modify local ignored log: `docs/development-log/2026-11-11.md`

- [x] Run focused release-version tests and the existing CI-equivalent verification commands available locally.
- [x] Parse both workflow YAML files with `actionlint`; add a Windows PowerShell parser step to CI because the local Linux host has no PowerShell runtime.
- [x] Run `git diff --check` and inspect all tracked changes and remaining hard-coded release versions.
- [x] Append concise phase logs with completed work, issues, resolution, and verification results.
