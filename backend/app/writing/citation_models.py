from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WritingCitation(BaseModel):
    """写作领域使用的证据引用 DTO。

    该模型与 Knowledge 的引用快照保持字段兼容，但不让 Writing 依赖
    Knowledge 的内部模型类型。转换只发生在研究适配器或写作入口边界。
    """

    model_config = ConfigDict(frozen=True)

    citation_id: str = Field(min_length=1)
    chunk_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    document_version: str = Field(min_length=1)
    source_path: str = Field(min_length=1)
    source_url: str | None = None
    title: str = Field(min_length=1)
    heading_path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_line_range(self) -> "WritingCitation":
        if self.end_line < self.start_line:
            raise ValueError("citation end_line must not precede start_line")
        return self

    def __eq__(self, other: object) -> bool:
        if isinstance(other, BaseModel):
            return self.model_dump(mode="python") == other.model_dump(mode="python")
        return NotImplemented

    @classmethod
    def from_knowledge_snapshot(cls, snapshot: Any) -> "WritingCitation":
        """将 Knowledge 边界对象或兼容字典转换为 Writing DTO。"""

        if isinstance(snapshot, cls):
            return snapshot
        if isinstance(snapshot, BaseModel):
            return cls.model_validate(snapshot.model_dump(mode="python"))
        if isinstance(snapshot, dict):
            return cls.model_validate(snapshot)
        raise TypeError("unsupported knowledge citation type")