# Tomato Agent 首次 GitHub Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Commit the current workspace changes in reviewable groups, push them to `origin/main`, and publish the first Windows x64 `v0.1.0` GitHub Release with repeatable CI and release workflows.

**Architecture:** Keep the existing Ubuntu verification workflow and extend its coverage with layered-memory tests. Add a Windows Rust test job and a Windows release workflow with a manual build-only dry-run plus a `v0.1.0` tag publication path. Both use the same build steps for the UI, Python engine and NSIS installer; only the tag path creates a GitHub Release.

**Tech Stack:** Git, GitHub Actions, Python 3.12/PyInstaller, Node 22/pnpm 10.34.5, Rust stable/Cargo, Tauri 2, PowerShell, GitHub CLI.

**Spec:** `docs/superpowers/specs/2026-11-11-first-release-design.md`

## Global Constraints

- Keep the seven pre-existing local commits unchanged; never force-push `main`.
- Release version and tag are both `0.1.0` and `v0.1.0` respectively.
- Build only the existing Windows x64 NSIS target; no signing secrets or unsigned-binary signing claims.
- CI and release workflows must not call real model or embedding providers, Docker/OpenShell acceptance, or the 180-request blind evaluator.
- Keep the old memory CRUD/extraction implementation frozen; do not broaden or rewrite its two known legacy gate failures as part of CI setup.
- Stage only visible source, tests, evaluation assets and documentation; do not force-add ignored logs, databases, local settings or build artifacts.
- Push only after fresh local verification and a fresh check that `origin/main` still equals the previously observed base commit `a590ba5ebe837e3fce5460f64be1f581cc2768ad`.

---

### Task 1: Add the layered-memory regression to Ubuntu CI

**Files:**
- Modify: `.github/workflows/verification.yml`
- Test: `backend/tests/test_layered_memory.py`

**Interfaces:**
- CI consumes the already-installed `backend/.venv` and runs the new pipeline test without provider credentials.

- [x] **Step 1: Add a dedicated workflow step**

Add a step after backend dependency installation:

```yaml
      - name: Layered memory regressions
        working-directory: backend
        run: .venv/bin/python -m pytest -q tests/test_layered_memory.py
```

- [x] **Step 2: Run the same command locally**

Run from `backend/`:

```bash
.venv/bin/python -m pytest -q tests/test_layered_memory.py
```

Expected: all layered-memory tests pass using temporary SQLite databases and fake clients.

### Task 2: Add Windows Rust verification

**Files:**
- Modify: `.github/workflows/verification.yml`
- Test: `desktop/src-tauri/Cargo.toml`, `desktop/src-tauri/Cargo.lock`

**Interfaces:**
- A separate `windows-latest` job checks the repository's actual single Cargo target; `Cargo.toml` currently defines no `desktop-chat` feature.

- [x] **Step 1: Add an independent Windows job**

Add a job with `actions/checkout@v4`, `dtolnay/rust-toolchain@stable`, `Swatinem/rust-cache@v2`, and this command:

```powershell
cargo test --locked --manifest-path desktop/src-tauri/Cargo.toml
```

Keep `contents: read` at workflow scope and set a finite job timeout.

- [x] **Step 2: Run the equivalent test on the local host if supported**

Run from the repository root:

```bash
cargo test --locked --manifest-path desktop/src-tauri/Cargo.toml
```

Expected: success locally where system Tauri dependencies exist; Windows Actions remains the authoritative Windows-target result.

### Task 3: Add the tag-triggered Windows Release workflow and notes

**Files:**
- Create: `.github/workflows/release.yml`
- Create: `docs/releases/v0.1.0.md`
- Use: `desktop/src-tauri/tauri.conf.json`, `desktop/pnpm-lock.yaml`, `desktop/ui/pnpm-lock.yaml`, `desktop/scripts/windows.ps1`

**Interfaces:**
- Triggers: manual dry-run on a branch and push of exactly `v0.1.0`.
- Build output: `Tomato Agent_0.1.0_x64-setup.exe`, matching `.sha256`, and `manifest.json`.
- Release publisher: `gh release create`, authenticated only by `${{ secrets.GITHUB_TOKEN }}` with workflow `contents: write`.

- [x] **Step 1: Write release notes with verified product scope**

Document first-release scope, first-launch model configuration, the unsigned installer warning, and the known clean-install/uninstall data-retention validation gap. Do not claim provider acceptance or installer signing.

- [x] **Step 2: Create the Windows workflow**

The workflow must:

1. Run only on tag `v0.1.0`; use `windows-latest` and `timeout-minutes: 60`.
2. Check out the tagged source, set up Python 3.12, Node 22, pnpm 10.34.5, and stable Rust.
3. Install the UI and Tauri dependencies in their own directories with their separate frozen pnpm lockfiles.
4. Build `backend/dist/tomato-agent-engine.exe` with PyInstaller 6.17.0 using the collection flags in `desktop/scripts/windows.ps1`.
5. From `desktop/`, run Tauri NSIS build with Cargo `--locked` and assert exactly one x64 setup executable exists.
6. Generate a lowercase SHA-256 sidecar and JSON manifest containing tag/version, `${{ github.sha }}`, `windows-x64`, filename, byte size and digest.
7. On manual runs, upload the three files as an Actions artifact and do not create a GitHub Release.
8. On a `v0.1.0` tag push, validate the downloaded files against the manifest, create or resume a draft release from `docs/releases/v0.1.0.md`, upload all three files, then publish the draft. Refuse to overwrite an already-published release.

- [x] **Step 3: Validate workflow and release metadata locally**

Run `git diff --check`; parse both workflow YAML documents with actionlint; verify tag/version and manifest fields match `tauri.conf.json` and the release notes. Do not simulate publishing from the local machine.

### Task 4: Re-run local verification and audit all changes

**Files:** all current tracked/untracked worktree changes plus the two workflow files and release notes.

- [x] **Step 1: Run local backend and UI gates**

Run:

```bash
python3 -m unittest scripts/test_verify.py
python3 scripts/verify.py module assistant execution rag writing ui --artifact-root verification-artifacts
python3 scripts/verify.py integration --timeout 600 --artifact-root verification-artifacts
```

Run the layered-memory command from Task 1 and `git diff --check` as well. Do not run the old full memory gate or real-model/180-request evaluators.

- [x] **Step 2: Inspect staged candidates and sensitive files**

Review `git status --short`, each changed file, lockfiles, evaluation data and documentation. Search candidate staged files for API keys, bearer tokens, SQLite databases, provider diagnostics, and local absolute paths. Confirm only expected source/docs are staged.

### Task 5: Commit the worktree in functional groups

**Interfaces:**
- Each commit is a cohesive review unit; commit messages use the repository's existing conventional format.
- Never amend or rewrite the seven prior local commits or the already-approved design-document commit.

- [x] **Step 1: Commit desktop chat/project changes**

Stage the related Rust chat store/commands/runtime, desktop engine bridge, UI project navigation, UI styles and desktop tests. Review `git diff --cached` and commit with a `feat(desktop): ...` message.

- [x] **Step 2: Commit backend Assistant and layered memory**

Stage Assistant/runtime/dependency/API changes, layered memory source and focused tests. Review `git diff --cached` and commit with a `feat(memory): ...` message.

- [x] **Step 3: Commit memory evaluation assets and documentation**

Stage memory evaluator modules, fixtures, eval JSON, user/design documents and release design/plan docs. Do not execute the 180-request evaluator. Review `git diff --cached` and commit with a `test(memory): ...` or `docs(memory): ...` message as appropriate.

- [x] **Step 4: Commit CI and release automation**

Stage workflow files, release notes, and any verification entry-point change. Review `git diff --cached` and commit with a `ci: ...` message.

- [x] **Step 5: Verify the final local commit list and clean visible worktree**

Run `git status --short`, `git log --oneline origin/main..HEAD`, and `git diff origin/main..HEAD --check`. Confirm the ignored development log remains untracked/ignored and is not force-added.

### Task 6: Push main and wait for CI

- [ ] **Step 1: Check for remote movement**

Run `git ls-remote origin refs/heads/main`. Continue only if it still returns `a590ba5ebe837e3fce5460f64be1f581cc2768ad`; otherwise stop and rebase/review without force-pushing.

- [ ] **Step 2: Push the fast-forward commits**

Run `git push origin main`; do not use force options.

- [ ] **Step 3: Wait for the push workflow**

Use GitHub CLI to inspect the workflow run for the pushed SHA and wait for completion. Proceed only when the Ubuntu verification job and Windows Rust job pass.

- [ ] **Step 4: Run the release workflow manually against main**

Use `gh workflow run release.yml --ref main`, wait for the build-only run, and verify its Windows installer, checksum and manifest artifact. The workflow must not create a GitHub Release during this dry-run.

### Task 7: Publish and verify `v0.1.0`

- [ ] **Step 1: Confirm the tag is unused and source is the verified main SHA**

Check local and remote tags and assert `HEAD` is the SHA whose CI passed.

- [ ] **Step 2: Create and push the annotated release tag**

Create local annotated tag `v0.1.0` on that verified commit and push only that tag.

- [ ] **Step 3: Wait for release workflow and inspect published assets**

Use GitHub CLI to wait for the tag workflow. Verify the Release is published and contains installer, `.sha256`, and `manifest.json`; compare manifest commit/tag/version with the source and confirm the checksum matches the uploaded installer.
