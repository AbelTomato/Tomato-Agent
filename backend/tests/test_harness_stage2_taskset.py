import json
from pathlib import Path

import pytest

from evals.harness_stage2 import build_task_set_sha256
from evals.harness_stage2_taskset import load_stage2_task_set


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "evals" / "harness_stage2_taskset.json"


def test_approved_task_set_loads_five_tasks_with_provenance() -> None:
    loaded = load_stage2_task_set(MANIFEST)

    assert loaded.split == "dev"
    assert [task.task_id for task in loaded.tasks] == [
        "research-alembic-core-001",
        "research-no-answer-001",
        "writing-alembic-flow-001",
        "writing-agent-stream-001",
        "writing-attention-001",
    ]
    assert len(build_task_set_sha256(loaded.tasks)) == 64
    no_answer = loaded.tasks[1]
    assert no_answer.expected_evidence_refs == ()
    snapshot = json.loads(no_answer.evidence_snapshot)
    assert snapshot["answerable"] is False
    assert snapshot["citations"] == []
    assert all("document_version" in json.loads(task.evidence_snapshot)["citations"][0] for task in loaded.tasks if task.expected_evidence_refs)


def test_task_set_rejects_tampered_manifest_hash(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest["dataset_sha256"] = "0" * 64
    tampered = tmp_path / "manifest.json"
    tampered.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="source dataset hash"):
        load_stage2_task_set(tampered)


def test_task_set_rejects_tampered_snapshot_hash(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest["knowledge_snapshot_sha256"] = "0" * 64
    tampered = tmp_path / "manifest.json"
    tampered.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="knowledge snapshot hash"):
        load_stage2_task_set(tampered)