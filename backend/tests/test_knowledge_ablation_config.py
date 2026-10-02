import json
from pathlib import Path

import pytest

from app.knowledge.evaluation import (
    PRODUCTION_EQUIVALENT_PROFILE,
    apply_ablation_config,
    apply_production_equivalent_profile,
    build_parser,
    load_ablation_configs,
    load_production_equivalent_profile,
)


CONFIG_PATH = Path(__file__).parents[1] / "evals" / "blog_retrieval_ablation.json"
TASK3_CONFIG_PATH = Path(__file__).parents[1] / "evals" / "blog_retrieval_ablation_task3.json"
PRODUCTION_EQUIVALENT_PROFILE_PATH = (
    Path(__file__).parents[1] / "evals" / "blog_retrieval_production_equivalent_dev.json"
)


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


def test_task3_config_declares_only_approved_provider_free_strategies():
    configs = load_ablation_configs(TASK3_CONFIG_PATH)

    assert [config["name"] for config in configs] == [
        "baseline",
        "wide-candidate",
        "coverage-aware",
    ]
    assert all(config["split"] == "dev" for config in configs)
    assert all(config["mode"] == "hybrid" for config in configs)
    assert all(config["query_planning"] == "disabled" for config in configs)
    assert all(config["rerank"] == "noop" for config in configs)
    assert all(config["candidate_limit"] >= config["final_limit"] for config in configs)
    assert all(config["final_limit"] > 0 for config in configs)
    assert all(0 <= config["candidate_min_vector_similarity"] <= 1 for config in configs)

    assert configs[0]["evidence_selection"] == "baseline"
    assert configs[0]["answerability"] == "baseline"
    assert configs[1]["evidence_selection"] == "baseline"
    assert configs[1]["answerability"] == "baseline"
    assert configs[2]["evidence_selection"] == "coverage-aware"
    assert configs[2]["answerability"] == "coverage-v1"


def test_task3_strategies_apply_their_effective_config_without_implicit_variants():
    configs = load_ablation_configs(TASK3_CONFIG_PATH)

    for config in configs:
        args = build_parser().parse_args(
            [
                "--dataset",
                "questions.jsonl",
                "--split",
                "dev",
                "--mode",
                "keyword",
                "--output",
                "report.json",
            ]
        )
        apply_ablation_config(args, config)

        assert args.ablation_name == config["name"]
        assert args.mode == config["mode"]
        assert args.candidate_limit == config["candidate_limit"]
        assert args.candidate_min_vector_similarity == config["candidate_min_vector_similarity"]
        assert args.final_limit == config["final_limit"]
        assert args.query_planning == "disabled"
        assert args.rerank == "noop"
        assert args.evidence_selection == config["evidence_selection"]
        assert args.answerability == config["answerability"]


def test_production_equivalent_profile_is_fixed_to_the_approved_dev_chain():
    profile = load_production_equivalent_profile(PRODUCTION_EQUIVALENT_PROFILE_PATH)

    assert profile["name"] == PRODUCTION_EQUIVALENT_PROFILE
    assert profile["split"] == "dev"
    assert profile["mode"] == "hybrid"
    assert profile["candidate_limit"] == 30
    assert profile["candidate_min_vector_similarity"] == pytest.approx(0.2)
    assert profile["final_limit"] == 5
    assert profile["query_planning"] == "conditional"
    assert profile["rerank"] == "provider"
    assert profile["evidence_selection"] == "coverage-aware"
    assert profile["answerability"] == "coverage-v1"
    assert profile["allow_insufficient_llm"] is False


def test_production_equivalent_profile_rejects_non_dev_or_non_provider_variants(tmp_path: Path):
    path = tmp_path / "invalid-profile.json"
    path.write_text(
        json.dumps(
            {
                "name": PRODUCTION_EQUIVALENT_PROFILE,
                "split": "test",
                "mode": "keyword",
                "candidate_limit": 30,
                "candidate_min_vector_similarity": 0.2,
                "final_limit": 5,
                "query_planning": "disabled",
                "rerank": "noop",
                "evidence_selection": "baseline",
                "answerability": "baseline",
                "allow_insufficient_llm": True,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="production-equivalent|split|mode|rerank"):
        load_production_equivalent_profile(path)


def test_production_equivalent_profile_overrides_legacy_cli_values():
    args = build_parser().parse_args(
        [
            "--dataset",
            "questions.jsonl",
            "--split",
            "dev",
            "--mode",
            "keyword",
            "--output",
            "report.json",
            "--profile",
            PRODUCTION_EQUIVALENT_PROFILE,
        ]
    )

    apply_production_equivalent_profile(args, load_production_equivalent_profile(PRODUCTION_EQUIVALENT_PROFILE_PATH))

    assert args.profile == PRODUCTION_EQUIVALENT_PROFILE
    assert args.mode == "hybrid"
    assert args.candidate_limit == 30
    assert args.candidate_min_vector_similarity == pytest.approx(0.2)
    assert args.final_limit == 5
    assert args.query_planning == "conditional"
    assert args.rerank == "provider"
    assert args.evidence_selection == "coverage-aware"
    assert args.answerability == "coverage-v1"
    assert args.allow_insufficient_llm is False


def test_legacy_evaluation_parser_defaults_remain_keyword_and_noop():
    args = build_parser().parse_args(
        [
            "--dataset",
            "questions.jsonl",
            "--split",
            "dev",
            "--mode",
            "keyword",
            "--output",
            "report.json",
        ]
    )

    assert args.mode == "keyword"
    assert args.query_planning == "disabled"
    assert args.rerank == "noop"
    assert args.evidence_selection == "baseline"
    assert args.answerability == "baseline"