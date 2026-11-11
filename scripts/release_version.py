import argparse
import json
import re
import sys
import tomllib
from pathlib import Path


STABLE_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
STABLE_TAG = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
ROOT = Path(__file__).resolve().parents[1]


def validate_current_versions(tauri_config_path, cargo_manifest_path):
    tauri_config_path = Path(tauri_config_path)
    cargo_manifest_path = Path(cargo_manifest_path)
    try:
        tauri_version = json.loads(tauri_config_path.read_text(encoding="utf-8"))["version"]
        with cargo_manifest_path.open("rb") as manifest_file:
            cargo_version = tomllib.load(manifest_file)["package"]["version"]
    except (OSError, KeyError, json.JSONDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"could not read release versions: {error}") from error

    if not isinstance(tauri_version, str) or not STABLE_VERSION.fullmatch(tauri_version):
        raise ValueError(f"Tauri version must be stable SemVer, got {tauri_version!r}")
    if cargo_version != tauri_version:
        raise ValueError(
            f"Cargo version {cargo_version!r} does not match Tauri version {tauri_version!r}"
        )
    return tauri_version


def validate_release_version(tag, tauri_config_path, cargo_manifest_path, release_notes_dir):
    match = STABLE_TAG.fullmatch(tag)
    if not match:
        raise ValueError(f"release tag {tag!r} must use stable SemVer in vMAJOR.MINOR.PATCH form")

    version = ".".join(match.groups())
    current_version = validate_current_versions(tauri_config_path, cargo_manifest_path)
    if version != current_version:
        raise ValueError(
            f"release tag version {version!r} does not match Tauri version {current_version!r}"
        )

    notes_path = Path(release_notes_dir) / f"{tag}.md"
    if not notes_path.is_file():
        raise ValueError(f"release notes file is missing: {notes_path}")
    return version


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate Tomato Agent release versions.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--tag", help="validate a stable release tag and its notes")
    mode.add_argument(
        "--check-current", action="store_true", help="validate checked-in app versions"
    )
    parser.add_argument(
        "--tauri-config", type=Path, default=ROOT / "desktop/src-tauri/tauri.conf.json"
    )
    parser.add_argument(
        "--cargo-manifest", type=Path, default=ROOT / "desktop/src-tauri/Cargo.toml"
    )
    parser.add_argument("--release-notes-dir", type=Path, default=ROOT / "docs/releases")
    args = parser.parse_args(argv)

    try:
        if args.check_current:
            version = validate_current_versions(args.tauri_config, args.cargo_manifest)
        else:
            version = validate_release_version(
                args.tag,
                args.tauri_config,
                args.cargo_manifest,
                args.release_notes_dir,
            )
    except ValueError as error:
        print(f"Release version validation failed: {error}", file=sys.stderr)
        return 1

    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
