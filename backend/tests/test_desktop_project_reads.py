import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.agent.policies import CodeTaskCapabilityProfile
from app.assistant.tools.project import FileArguments, ProjectRead, ProjectReadAuthorizationRequired
from app.tools.base import ToolContext


class FakeBridge:
    def __init__(self):
        self.calls = []

    async def call(self, operation, payload, **identity):
        self.calls.append((operation, payload, identity))
        if operation == "read_file":
            return {"path": "README.md", "content": "private project text", "bytes": 20}
        raise AssertionError(operation)


class FakeRuns:
    def __init__(self, session_id):
        self.run = SimpleNamespace(session_id=session_id)

    async def get_run(self, _run_id):
        return self.run


class FakeSessions:
    async def get_session(self, _session_id):
        return SimpleNamespace(project_id="project-1")


class FakeTool:
    name = "read_file"

    async def preflight_only(self):
        return None


@pytest.mark.asyncio
async def test_desktop_project_read_requires_run_grant_before_broker_call():
    session_id = str(uuid4())
    tool = SimpleNamespace(name="read_file", runs=FakeRuns(session_id), input_model=FileArguments)
    bridge = FakeBridge()
    project = ProjectRead(tool, FakeSessions())
    project.bridge = bridge
    context = ToolContext(session_id=session_id, run_id="run-1", project_id="project-1",
        profile=CodeTaskCapabilityProfile())

    with pytest.raises(ProjectReadAuthorizationRequired):
        await project.preflight({"path": "README.md"}, context)
    assert bridge.calls == []

    context.project_read_grant = "host-issued-grant"
    await project.preflight({"path": "README.md"}, context)
    result = await project.execute({"path": "README.md"}, context)
    assert result.success
    assert json.loads(result.data["result"]["answer"]) == {
        "content": "private project text", "missing": False,
    }
    assert bridge.calls == [("read_file", {"relative_path": "README.md", "max_bytes": 131072}, {
        "grant": "host-issued-grant", "session_id": session_id, "run_id": "run-1", "project_id": "project-1",
    })]
