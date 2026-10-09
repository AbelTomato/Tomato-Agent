import pytest
from pydantic import TypeAdapter, ValidationError

from app.agent import models, policies
from app.settings import Settings


@pytest.fixture
def CodeTaskRequest():
    assert hasattr(models, "CodeTaskRequest"), "CodeTaskRequest contract is missing"
    return models.CodeTaskRequest


@pytest.fixture
def CodeTaskBudget():
    assert hasattr(models, "CodeTaskBudget"), "CodeTaskBudget contract is missing"
    return models.CodeTaskBudget


@pytest.fixture
def CodeTaskCapabilityProfile():
    assert hasattr(policies, "CodeTaskCapabilityProfile"), "Server profile is missing"
    return policies.CodeTaskCapabilityProfile


def test_code_task_request_requires_non_empty_task_and_forbids_policy_fields(CodeTaskRequest):
    request = CodeTaskRequest(task="Fix the failing test")

    assert request.task == "Fix the failing test"

    with pytest.raises(ValidationError):
        CodeTaskRequest(task="   ")
    with pytest.raises(ValidationError):
        CodeTaskRequest(task="Fix it", capability_profile={"allow_network": True})
    with pytest.raises(ValidationError):
        CodeTaskRequest(task="Fix it", allowed_tools=["write_file"])


def test_code_task_budget_is_strict_and_rejects_non_positive_values(CodeTaskBudget):
    budget = CodeTaskBudget(
        max_loops=2,
        max_tool_calls=3,
        max_duration_seconds=10.0,
        max_context_tokens=1000,
        max_response_chars=2000,
    )
    assert budget.max_loops == 2

    with pytest.raises(ValidationError):
        CodeTaskBudget(
            max_loops=0,
            max_tool_calls=1,
            max_duration_seconds=1.0,
            max_context_tokens=1,
            max_response_chars=1,
        )
    with pytest.raises(ValidationError):
        CodeTaskBudget(
            max_loops=1,
            max_tool_calls=-1,
            max_duration_seconds=1.0,
            max_context_tokens=1,
            max_response_chars=1,
        )
    with pytest.raises(ValidationError):
        CodeTaskBudget(
            max_loops=1,
            max_tool_calls=1,
            max_duration_seconds=1.0,
            max_context_tokens=1,
            max_response_chars=1,
            extra="reject",
        )


def test_code_task_capability_profile_is_server_side_and_denies_network_credentials(CodeTaskCapabilityProfile):
    profile = CodeTaskCapabilityProfile(allowed_paths=("/workspace/repo",))

    assert profile.allow_network is False
    assert profile.allow_process is False
    assert profile.credential_names == ()
    assert profile.allowed_tools == frozenset(
        {
            "list_files",
            "read_file",
            "search_files",
            "write_file",
            "apply_patch",
            "run_tests",
            "get_diff",
            "collect_artifact",
        }
    )

    with pytest.raises(ValidationError):
        CodeTaskCapabilityProfile(allow_network=True)
    with pytest.raises(ValidationError):
        CodeTaskCapabilityProfile(credential_names=("API_KEY",))
    with pytest.raises(ValidationError):
        CodeTaskCapabilityProfile(allowed_tools=frozenset({"shell"}))


def test_task_run_status_is_fixed_literal_and_settings_default_to_safe_limits():
    assert hasattr(models, "TaskRunStatus"), "TaskRunStatus contract is missing"
    adapter = TypeAdapter(models.TaskRunStatus)
    for status in ("queued", "running", "waiting", "completed", "failed", "cancelled", "timed_out"):
        assert adapter.validate_python(status) == status
    with pytest.raises(ValidationError):
        adapter.validate_python("paused")

    settings = Settings(_env_file=None)
    assert settings.code_task_allow_network is False
    assert settings.code_task_allow_credentials is False
    assert settings.code_task_max_tool_calls > 0


@pytest.mark.parametrize("field", ["budget", "allowed_paths", "allow_network", "credential_names", "allow_process", "timeout_seconds", "unknown"])
def test_request_rejects_server_policy_overrides(CodeTaskRequest, field):
    with pytest.raises(ValidationError):
        CodeTaskRequest.model_validate({"task": "Fix it", field: {}})


@pytest.mark.parametrize("field", ["max_loops", "max_tool_calls", "max_duration_seconds", "max_context_tokens", "max_response_chars"])
@pytest.mark.parametrize("value", [-1, 0, True, "2", float("inf"), float("nan")])
def test_budget_rejects_invalid_limits(CodeTaskBudget, field, value):
    with pytest.raises(ValidationError):
        CodeTaskBudget(**{field: value})


def test_settings_generate_profile_and_budget_from_server_limits(tmp_path):
    settings = Settings(_env_file=None, code_task_max_tool_calls=3, code_task_tool_timeout_seconds=4.0)
    assert hasattr(settings, "code_task_budget"), "Server budget is missing"
    assert settings.code_task_budget.max_tool_calls == 3
    profile = settings.code_task_capability_profile(tmp_path)
    assert profile.allowed_paths == (str(tmp_path.resolve()),)
    assert profile.timeout_seconds == 4.0
    assert not profile.allow_network
    assert not profile.credential_names


@pytest.mark.parametrize("field", ["code_task_max_tool_calls", "code_task_max_file_bytes", "code_task_max_artifact_bytes", "code_task_max_tool_result_chars"])
def test_settings_reject_invalid_limits(field):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: 0})
