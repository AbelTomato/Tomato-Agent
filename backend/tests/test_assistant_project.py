from app.assistant.capabilities import CapabilityRegistry
from app.assistant.tools.project import register_project_reads
from app.tools.base import ToolContext
from test_application_lifecycle import make_app


async def test_only_read_tools_registered_and_no_workspace_authority(tmp_path):
    app = make_app(tmp_path)
    registry = CapabilityRegistry()
    register_project_reads(registry, app.state.workspace_service, app.state.artifact_service, app.state.run_repository)
    assert {item.name for item in registry.definitions()} == {"list_files", "read_file", "search_files", "request_input"}
    result = await registry.get("read_file").execute({"path": "a.py"}, ToolContext(session_id="s", run_id="r"))
    assert not result.success