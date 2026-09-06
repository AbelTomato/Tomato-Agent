from pathlib import Path

from pydantic import BaseModel, Field

from app.agent.models import ToolDefinition
from app.errors import UnsafePathError
from .base import ToolContext, ToolResult


class ReadDocsInput(BaseModel):
    path: str = Field(min_length=1, max_length=300)
    start_line: int = Field(default=1, ge=1)
    end_line: int = Field(default=100, ge=1, le=500)


class ReadDocs:
    name = "read_docs"
    description = "Read a bounded range from an allowed project documentation file."
    input_model = ReadDocsInput
    allowed_extensions = {".md", ".txt", ".rst"}

    def __init__(self, docs_root: Path, max_bytes: int = 100_000):
        self.docs_root, self.max_bytes = docs_root.resolve(), max_bytes

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=ReadDocsInput.model_json_schema(),
        )

    def _safe_path(self, raw: str) -> Path:
        candidate = (self.docs_root / raw).resolve()
        if (
            self.docs_root not in candidate.parents
            or candidate.suffix.lower() not in self.allowed_extensions
        ):
            raise UnsafePathError("Path is outside the documentation allowlist")
        return candidate

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        data = self.input_model.model_validate(arguments)
        path = self._safe_path(data.path)
        if not path.is_file():
            return ToolResult(success=False, error="Document not found")
        if path.stat().st_size > self.max_bytes:
            return ToolResult(success=False, error="Document is too large")
        lines = path.read_text(encoding="utf-8").splitlines()
        selected = lines[data.start_line - 1 : data.end_line]
        return ToolResult(
            success=True,
            data={
                "path": data.path,
                "start_line": data.start_line,
                "content": "\n".join(selected),
            },
        )
