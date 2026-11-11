import tempfile
import unittest
from pathlib import Path

from scripts.release_version import validate_current_versions, validate_release_version


class ReleaseVersionTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.tauri_config = self.root / "tauri.conf.json"
        self.cargo_manifest = self.root / "Cargo.toml"
        self.release_notes = self.root / "releases"
        self.release_notes.mkdir()
        self.write_metadata("1.2.3", "1.2.3")

    def tearDown(self):
        self.temporary_directory.cleanup()

    def write_metadata(self, tauri_version, cargo_version):
        self.tauri_config.write_text(
            '{"version": "' + tauri_version + '"}\n', encoding="utf-8"
        )
        self.cargo_manifest.write_text(
            '[package]\nname = "tomato-agent-desktop"\nversion = "'
            + cargo_version
            + '"\n',
            encoding="utf-8",
        )

    def test_accepts_matching_stable_semver_tag_with_notes(self):
        (self.release_notes / "v1.2.3.md").write_text("Release notes\n", encoding="utf-8")

        version = validate_release_version(
            "v1.2.3", self.tauri_config, self.cargo_manifest, self.release_notes
        )

        self.assertEqual(version, "1.2.3")

    def test_rejects_malformed_or_non_stable_tags(self):
        for tag in ("v01.2.3", "v1.2.3-rc.1", "1.2.3", "v1.2", "v1.2.3+build.1"):
            with self.subTest(tag=tag), self.assertRaisesRegex(ValueError, "stable SemVer"):
                validate_release_version(
                    tag, self.tauri_config, self.cargo_manifest, self.release_notes
                )

    def test_rejects_tauri_version_mismatch(self):
        self.write_metadata("1.2.4", "1.2.3")

        with self.assertRaisesRegex(ValueError, "Tauri version"):
            validate_release_version(
                "v1.2.3", self.tauri_config, self.cargo_manifest, self.release_notes
            )

    def test_rejects_cargo_version_mismatch(self):
        self.write_metadata("1.2.3", "1.2.4")

        with self.assertRaisesRegex(ValueError, "Cargo version"):
            validate_release_version(
                "v1.2.3", self.tauri_config, self.cargo_manifest, self.release_notes
            )

    def test_rejects_missing_release_notes(self):
        with self.assertRaisesRegex(ValueError, "release notes file is missing"):
            validate_release_version(
                "v1.2.3", self.tauri_config, self.cargo_manifest, self.release_notes
            )

    def test_current_metadata_requires_matching_versions(self):
        self.assertEqual(
            validate_current_versions(self.tauri_config, self.cargo_manifest), "1.2.3"
        )
        self.write_metadata("1.2.3", "1.2.4")

        with self.assertRaisesRegex(ValueError, "Cargo version"):
            validate_current_versions(self.tauri_config, self.cargo_manifest)


if __name__ == "__main__":
    unittest.main()
