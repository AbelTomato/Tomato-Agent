import pytest

from app.agent.policies import CodeTaskCapabilityProfile
from app.agent.sandbox import SandboxRequest
from app.agent.sandbox_docker import DockerSandboxExecutor
from app.tools.code_tests import TestTargetRegistry
from app.workspaces.service import WorkspaceService


IMAGE = "python@sha256:" + "a" * 64


async def test_project_registry_validates_registered_paths(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "tests").mkdir()
    workspaces = WorkspaceService(tmp_path / "metadata", allowed_roots=[project])
    workspace = await workspaces.register_original(project, ["tests"])
    registry = TestTargetRegistry.for_workspace(workspaces, workspace.workspace_id)
    target = registry.resolve("unit")
    assert target.command == ("python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests")
    assert target.allowed_arguments == () and target.cwd == ""
    (project / "tests").rmdir()
    (project / "tests").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        TestTargetRegistry.for_workspace(workspaces, workspace.workspace_id)


def test_docker_uses_server_registry_never_request_claim(tmp_path):
    command = ("python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests")
    executor = DockerSandboxExecutor(IMAGE, registered_targets={str(tmp_path): {"unit": command}})
    profile = CodeTaskCapabilityProfile(allowed_paths=(str(tmp_path),), sandbox_backend="docker")
    request = SandboxRequest(tool_name="run_tests", request_id="registered",
                             arguments={"target": "unit", "argv": list(command), "cwd": str(tmp_path)})
    argv = executor.build_run_argv(request, profile, "tomato-agent-test")
    assert argv[argv.index("--entrypoint") + 1] == command[0]
    assert argv[-len(command) + 1:] == list(command[1:])

    for arguments in [{**request.arguments, "registered": True},
                      {**request.arguments, "argv": ["python", "-c", "print(1)"]}]:
        with pytest.raises(ValueError):
            executor.build_run_argv(request.model_copy(update={"arguments": arguments}), profile,
                                    "tomato-agent-test")
    with pytest.raises(ValueError):
        DockerSandboxExecutor(IMAGE).build_run_argv(request, profile, "tomato-agent-test")


@pytest.mark.parametrize("path", ["-q", "../outside", "/etc", ".env", "tests/link"])
async def test_unsafe_pytest_paths_rejected(tmp_path, path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "tests").mkdir()
    (project / "tests" / "link").symlink_to(tmp_path, target_is_directory=True)
    workspaces = WorkspaceService(tmp_path / "metadata", allowed_roots=[project])
    with pytest.raises(ValueError):
        await workspaces.register_original(project, [path])