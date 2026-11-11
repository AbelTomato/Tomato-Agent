from pathlib import Path

from app.memory.extraction_evaluation import run_extraction_policy_evaluation


DATASET = Path(__file__).parents[1] / "evals" / "memory_extraction_policy_v1.json"


def test_extraction_policy_gate_meets_fixed_offline_boundary_cases():
    report = run_extraction_policy_evaluation(DATASET)

    assert report["dataset_id"] == "personal-assistant-memory-extraction-policy-v1"
    assert len(report["dataset_sha256"]) == 64
    assert report["summary"]["case_count"] == 40
    assert report["summary"]["precision"] >= 0.95
    assert report["summary"]["recall"] >= 0.95
    assert report["summary"]["false_positive"] == 0
    assert "not measured" in report["scope"]
