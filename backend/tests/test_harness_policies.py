import asyncio

import pytest
from pydantic import BaseModel, ValidationError

from app.agent.harness_models import ToolPolicy
from app.agent.policies import (
    CapabilityProfile,
    ToolDeclaration,
    ToolExecutionError,
    map_tool_exception,
    map_tool_result,
    validate_capability_profile,
    validate_tool_call,
)
from app.errors import ToolInputError, ToolNotFoundError, ToolTimeoutError
from app.tools.base import ToolResult


class Input(BaseModel):
    query: str


def declaration(**overrides) -> ToolDeclaration:
    values = {
        "name": "read_knowledge",
        "description": "Read approved knowledge.",
        "input_schema": Input.model_json_schema(),
        "capabilities": frozenset({"resource"}),
        "side_effect": "read",
    }
    values.update(overrides)
    return ToolDeclaration(**values)


def policy(**overrides) -> ToolPolicy:
    values = {
        "allowed_tools": frozenset({"read_knowledge"}),
        "max_calls": 2,
        "timeout_seconds": 1.0,
        "max_result_chars": 100,
    }
    values.update(overrides)
    return ToolPolicy(**values)


def test_tool_declaration_is_strict_and_rejects_unknown_capabilities():
    value = declaration()
    assert value.side_effect == "read"
    assert value.capabilities == frozenset({"resource"})

    with pytest.raises(ValidationError):
        declaration(capabilities=frozenset({"network"}), extra="reject")
    with pytest.raises(ValidationError):
        declaration(capabilities=frozenset({"host"}))


def test_validate_tool_call_checks_allowlist_and_schema():
    validate_tool_call(declaration(), {"query": "topic"}, policy())

    with pytest.raises(ToolExecutionError) as exc_info:
        validate_tool_call(declaration(), {"query": "topic"}, policy(allowed_tools=frozenset()))
    assert exc_info.value.code == "unknown_tool"
    with pytest.raises(ToolExecutionError) as exc_info:
        validate_tool_call(declaration(), {"wrong": "value"}, policy())
    assert exc_info.value.code == "invalid_arguments"
    with pytest.raises(ToolExecutionError) as exc_info:
        validate_tool_call(
            declaration(capabilities=frozenset({"network"})),
            {"query": "topic"},
            policy(),
        )
    assert exc_info.value.code == "permission_denied"


def test_capability_profile_defaults_to_deny_and_rejects_unsafe_values():
    profile = CapabilityProfile()
    validate_capability_profile(profile)
    assert profile.allow_process is False
    assert profile.allow_network is False
    assert profile.allowed_paths == ()
    assert profile.credential_names == ()

    with pytest.raises(ValidationError):
        CapabilityProfile(allow_process=True)
    with pytest.raises(ValidationError):
        CapabilityProfile(allow_network=True)
    with pytest.raises(ValidationError):
        CapabilityProfile(credential_names=("API_KEY",))
    with pytest.raises(ValidationError):
        CapabilityProfile(allowed_paths=("/",))


def test_capability_profile_allows_only_explicit_server_safe_values():
    profile = CapabilityProfile(
        allowed_paths=("/srv/approved",),
        timeout_seconds=2.0,
        max_output_chars=500,
    )
    validate_capability_profile(profile)
    assert profile.allowed_paths == ("/srv/approved",)

    with pytest.raises(ValidationError):
        CapabilityProfile(allowed_paths=("relative/path",))


def test_error_mapping_is_stable_and_does_not_expose_internal_details():
    assert map_tool_exception(ToolNotFoundError("secret internal name")).code == "unknown_tool"
    assert map_tool_exception(ToolInputError("password=secret")).code == "invalid_arguments"
    assert map_tool_exception(ToolTimeoutError("stack trace secret")).code == "timeout"
    mapped = map_tool_exception(RuntimeError("Authorization: bearer secret"))
    assert mapped.code == "execution_failed"
    assert "secret" not in mapped.public_message
    assert mapped.retryable is False

    assert map_tool_result(ToolResult(success=True, data={"text": "ok"})) is None
    limited = map_tool_result(ToolResult(success=True, data="123456"), max_output_chars=3)
    assert limited.code == "output_limit"
    assert limited.retryable is False


@pytest.mark.asyncio
async def test_timeout_error_mapping_from_cancelled_execution():
    mapped = map_tool_exception(asyncio.TimeoutError())
    assert mapped.code == "timeout"