# Release Checklist

When preparing a release:

1. Set the same stable `MAJOR.MINOR.PATCH` version in `desktop/src-tauri/tauri.conf.json` and `desktop/src-tauri/Cargo.toml`.
2. Add curated notes at `docs/releases/v<version>.md` and pass the PR verification workflow.
3. Push the matching `v<version>` tag. The workflow builds the Windows x64 installer and creates or updates a Draft Release.
4. Before publishing, confirm the Draft notes and asset names, verify the `.sha256` file and manifest commit/tag/version, and install and launch the installer on Windows.
5. Keep the unsigned-installer notice in the release notes. Publish the Draft manually only after these checks pass.

Re-running the tag workflow replaces assets only while the matching GitHub Release remains a Draft. It refuses to modify an already-published Release. A manual workflow run only uploads a temporary Actions artifact and does not create a Release.
