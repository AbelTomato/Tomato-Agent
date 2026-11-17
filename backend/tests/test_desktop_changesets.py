import hashlib
import json

import pytest
from pydantic import ValidationError

from app.desktop.changesets import ChangeSet


def changeset(**overrides):
    value = {
        "change_id": "change-1",
        "run_id": "run-1",
        "session_id": "session-1",
        "project_id": "project-1",
        "authorization_version": "grant-1",
        "changes": [
            {
                "relative_path": "src/main.py",
                "operation": "modify",
                "expected_old_sha256": hashlib.sha256(b"old\n").hexdigest(),
                "base_content": "old\n",
                "content": "new\n",
            }
        ],
    }
    value.update(overrides)
    return ChangeSet.model_validate(value)


def test_changeset_digest_is_canonical_and_bound_to_all_content():
    proposed = changeset()
    expected = hashlib.sha256(
        json.dumps(proposed.canonical_payload(), ensure_ascii=False,
                   sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    assert proposed.digest == expected
    assert changeset().digest == proposed.digest
    assert changeset(run_id="run-2").digest != proposed.digest


@pytest.mark.parametrize("path", ["../outside", "/absolute", "a\\b", "", ".env"])
def test_changeset_rejects_unsafe_paths(path):
    item = {
        "relative_path": path,
        "operation": "add",
        "expected_old_sha256": None,
        "content": "ok",
    }
    with pytest.raises(ValidationError):
        changeset(changes=[item])


def test_changeset_rejects_duplicate_paths_and_invalid_add_modify_conditions():
    item = {
        "relative_path": "src/main.py",
        "operation": "add",
        "expected_old_sha256": None,
        "base_content": None,
        "content": "ok",
    }
    with pytest.raises(ValidationError):
        changeset(changes=[item, item])
    with pytest.raises(ValidationError):
        changeset(changes=[{**item, "operation": "modify"}])
    with pytest.raises(ValidationError):
        changeset(changes=[{**item, "operation": "add", "expected_old_sha256": "a" * 64}])


def test_changeset_binds_the_reviewed_old_content_to_its_expected_hash():
    with pytest.raises(ValidationError):
        changeset(changes=[{
            "relative_path": "src/main.py",
            "operation": "modify",
            "expected_old_sha256": hashlib.sha256(b"old\n").hexdigest(),
            "base_content": "different\n",
            "content": "new\n",
        }])
    with pytest.raises(ValidationError):
        changeset(changes=[{
            "relative_path": "new.py",
            "operation": "add",
            "expected_old_sha256": None,
            "base_content": "unexpected",
            "content": "new\n",
        }])


def test_changeset_rejects_oversized_content_and_unknown_fields():
    with pytest.raises(ValidationError):
        changeset(changes=[{
            "relative_path": "large.txt",
            "operation": "add",
            "expected_old_sha256": None,
            "content": "x" * (256 * 1024 + 1),
        }])
    with pytest.raises(ValidationError):
        changeset(unreviewed=True)
