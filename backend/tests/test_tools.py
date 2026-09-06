from pathlib import Path
import pytest
from app.tools.base import ToolContext
from app.tools.calculator import Calculator
from app.tools.read_docs import ReadDocs
from app.errors import UnsafePathError

ctx = ToolContext(session_id="s", run_id="r")


@pytest.mark.asyncio
async def test_calculator():
    result = await Calculator().execute({"expression": "(12 + 8) * 3 / 2"}, ctx)
    assert result.success and result.data["value"] == 30


@pytest.mark.asyncio
async def test_calculator_rejects_calls():
    result = await Calculator().execute({"expression": '__import__("os")'}, ctx)
    assert not result.success


@pytest.mark.asyncio
async def test_read_docs_allowlist(tmp_path: Path):
    (tmp_path / "ok.md").write_text("one\ntwo", encoding="utf-8")
    result = await ReadDocs(tmp_path).execute({"path": "ok.md"}, ctx)
    assert result.success and result.data["content"] == "one\ntwo"
    with pytest.raises(UnsafePathError):
        await ReadDocs(tmp_path).execute({"path": "../secret.txt"}, ctx)
