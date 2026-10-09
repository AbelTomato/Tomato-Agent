from uuid import UUID, uuid4

import pytest

from app.artifacts.models import ArtifactRef
from app.artifacts.service import ArtifactNotFoundError, ArtifactService
from app.workspaces.service import WorkspacePathError, WorkspaceService


@pytest.mark.asyncio
async def test_artifact_service_registers_text_and_file_with_digest(tmp_path):
    workspace_service = WorkspaceService(tmp_path / "workspaces")
    workspace = await workspace_service.create()
    await workspace_service.write_file(workspace.workspace_id, "src/main.py", "print('ok')\n")
    service = ArtifactService(
        tmp_path / "artifacts", workspace_service=workspace_service, max_bytes=100
    )
    await service.init()
    run_id = uuid4()

    file_ref = await service.register_file(
        run_id, workspace.workspace_id, "src/main.py", "file"
    )
    text_ref = await service.register_text(run_id, "test_report", "passed\n")

    assert isinstance(file_ref, ArtifactRef)
    assert isinstance(file_ref.artifact_id, UUID)
    assert file_ref.run_id == run_id
    assert file_ref.workspace_id == workspace.workspace_id
    assert file_ref.relative_path == "src/main.py"
    assert file_ref.kind == "file"
    assert file_ref.size_bytes == len(b"print('ok')\n")
    assert len(file_ref.sha256) == 64
    assert text_ref.workspace_id is None
    assert text_ref.relative_path is None
    assert await service.read(file_ref.artifact_id, 100) == b"print('ok')\n"
    assert await service.read(text_ref.artifact_id, 100) == b"passed\n"
    assert {item.artifact_id for item in await service.list_for_run(run_id)} == {
        file_ref.artifact_id,
        text_ref.artifact_id,
    }

    metadata = workspace.metadata_path.read_text(encoding="utf-8")
    assert str(file_ref.artifact_id) in metadata


@pytest.mark.asyncio
async def test_artifact_registration_is_idempotent_and_unknown_is_explicit(tmp_path):
    workspace_service = WorkspaceService(tmp_path / "workspaces")
    workspace = await workspace_service.create()
    await workspace_service.write_file(workspace.workspace_id, "report.txt", "same")
    service = ArtifactService(tmp_path / "artifacts", workspace_service=workspace_service)
    await service.init()
    run_id = uuid4()

    first = await service.register_file(run_id, workspace.workspace_id, "report.txt", "file")
    second = await service.register_file(run_id, workspace.workspace_id, "report.txt", "file")
    assert second == first

    text_first = await service.register_text(run_id, "diff", "same")
    text_second = await service.register_text(run_id, "diff", "same")
    assert text_second == text_first

    with pytest.raises(ArtifactNotFoundError):
        await service.get(uuid4())
    with pytest.raises(ArtifactNotFoundError):
        await service.read(uuid4(), 100)


@pytest.mark.asyncio
async def test_artifacts_enforce_size_and_workspace_boundaries(tmp_path):
    workspace_service = WorkspaceService(tmp_path / "workspaces")
    workspace = await workspace_service.create()
    await workspace_service.write_file(workspace.workspace_id, "large.txt", "123456")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    service = ArtifactService(
        tmp_path / "artifacts", workspace_service=workspace_service, max_bytes=5
    )
    await service.init()
    run_id = uuid4()

    with pytest.raises(ValueError, match="too large"):
        await service.register_file(run_id, workspace.workspace_id, "large.txt", "file")
    with pytest.raises(ValueError, match="too large"):
        await service.register_text(run_id, "test_report", "123456")
    with pytest.raises(WorkspacePathError):
        await service.register_file(run_id, workspace.workspace_id, str(outside), "file")
    with pytest.raises(WorkspacePathError):
        await service.register_file(run_id, workspace.workspace_id, "../outside.txt", "file")

    with pytest.raises(ValueError, match="max_bytes"):
        await service.read(uuid4(), 0)