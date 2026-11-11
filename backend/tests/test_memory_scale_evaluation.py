import pytest

from app.memory.scale_evaluation import run_memory_scale_evaluation


@pytest.mark.asyncio
async def test_scale_evaluation_reports_each_concurrency_level_and_hits_target():
    report = await run_memory_scale_evaluation(
        item_count=25, serial_queries=2, concurrency_levels=[1, 3],
    )

    assert report["provider_mode"] == "keyword_only_no_provider"
    assert report["dataset"]["item_count"] == 25
    assert report["serial"]["hit_count"] == 2
    assert report["concurrent_by_level"]["1"]["count"] == 1
    assert report["concurrent_by_level"]["1"]["hit_count"] == 1
    assert report["concurrent_by_level"]["3"]["count"] == 3
    assert report["concurrent_by_level"]["3"]["hit_count"] == 3


@pytest.mark.asyncio
async def test_scale_evaluation_can_measure_cached_fake_hybrid_path_without_provider():
    report = await run_memory_scale_evaluation(
        item_count=25, serial_queries=2, concurrency_levels=[2],
        embedding_mode="cached-fake",
    )

    assert report["provider_mode"] == "synthetic_cached_embedding_no_provider"
    assert report["warmup"]["hit"] is True
    assert report["serial"]["hit_count"] == 2
    assert report["concurrent_by_level"]["2"]["hit_count"] == 2


@pytest.mark.asyncio
async def test_scale_evaluation_reports_cold_fake_provider_overlap_without_provider():
    report = await run_memory_scale_evaluation(
        item_count=25, serial_queries=1, concurrency_levels=[2],
        embedding_mode="cold-fake",
    )

    cold = report["cold_cache_concurrent"]
    assert report["provider_mode"] == "synthetic_cold_embedding_no_provider"
    assert cold["concurrency"] == 2
    assert cold["hit_count"] == 2
    assert cold["max_provider_concurrency"] >= 2
    assert cold["provider_requests"] > 0
    assert cold["provider_inputs"] <= 2 * (64 + 1)


@pytest.mark.asyncio
async def test_scale_evaluation_reports_background_embedding_index_completion():
    report = await run_memory_scale_evaluation(
        item_count=25, serial_queries=2, concurrency_levels=[2],
        embedding_mode="worker-fake",
    )

    indexing = report["background_indexing"]
    assert report["provider_mode"] == "synthetic_background_embedding_no_provider"
    assert indexing["provider_inputs"] == 25
    assert indexing["remaining_missing"] == 0
    assert report["warmup"]["hit"] is True
    assert report["serial"]["hit_count"] == 2
