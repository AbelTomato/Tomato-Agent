import json
from pathlib import Path

import pytest

from app.knowledge.evaluation import load_ablation_configs


CONFIG_PATH = Path(__file__).parents[1] / "evals" / "blog_retrieval_ablation.json"


def test_fixed_ablation_config_contains_reproducible_dev_strategies():
    configs = load_ablation_configs(CONFIG_PATH)

    assert [config["name"] for config in configs] == [
        "baseline",
        "wide-candidate",
        "wide-plus-rerank",
        "coverage",
        "query-decomposition",
    ]
    assert all(config["split"] == "dev" for config in configs)
    assert all(config["mode"] == "hybrid" for config in configs)
    assert all(config["candidate_limit"] >= config["final_limit"] for config in configs)
    assert all(config["final_limit"] > 0 for config in configs)
    assert all(0 <= config["candidate_min_vector_similarity"] <= 1 for config in configs)


def test_ablation_config_rejects_test_split_and_unknown_fields(tmp_path: Path):
    path = tmp_path / "invalid.json"
    path.write_text(
        json.dumps(
            [
                {
                    "name": "bad",
                    "split": "test",
                    "mode": "hybrid",
                    "candidate_limit": 30,
                    "candidate_min_vector_similarity": 0.2,
                    "final_limit": 5,
                    "query_planning": "conditional",
                    "rerank": "noop",
                    "evidence_selection": "coverage-aware",
                    "answerability": "coverage-v1",
                    "unexpected": True,
                }
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="split|unknown"):
        load_ablation_configs(path)