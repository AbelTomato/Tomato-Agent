import pytest
from pydantic import ValidationError

from app.agent.config import RuntimeConfig
from app.agent.context import ContextManager
from app.agent.models import Message, ToolCall
from app.agent.runtime import AgentRuntime
from app.settings import Settings
from app.sessions.repository import SessionRepository
from app.tools.registry import ToolRegistry


def test_runtime_config_has_safe_execution_budgets():
    config = RuntimeConfig()

    assert config.max_loops == 12
    assert config.max_tool_calls == 8
    assert config.max_duration_seconds == 120.0
    assert config.tool_timeout_seconds == 20.0


def test_runtime_config_rejects_non_positive_budgets():
    with pytest.raises(ValidationError):
        RuntimeConfig(max_loops=0)
    with pytest.raises(ValidationError):
        RuntimeConfig(tool_timeout_seconds=-1)


def test_settings_maps_environment_fields_to_runtime_config():
    settings = Settings(
        runtime_max_loops=3,
        runtime_max_tool_calls=2,
        runtime_max_duration_seconds=15,
        runtime_tool_timeout_seconds=4,
    )

    assert settings.runtime_config == RuntimeConfig(
        max_loops=3,
        max_tool_calls=2,
        max_duration_seconds=15,
        tool_timeout_seconds=4,
    )


def test_message_preserves_structured_tool_calls():
    message = Message(
        role="assistant",
        tool_calls=[ToolCall(name="calculator", arguments={"expression": "1 + 1"})],
    )

    assert message.content == ""
    assert message.tool_calls[0].name == "calculator"


def test_agent_runtime_keeps_explicit_runtime_config(tmp_path):
    config = RuntimeConfig(max_loops=3)
    runtime = AgentRuntime(
        llm=object(),
        context_manager=ContextManager(token_counter=len),
        tool_registry=ToolRegistry(),
        repository=SessionRepository(tmp_path / "agent.db"),
        system_instruction="system",
        config=config,
    )

    assert runtime.config is config
