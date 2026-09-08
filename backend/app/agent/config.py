from pydantic import BaseModel, Field


class RuntimeConfig(BaseModel):
    """Agent Runtime 的执行预算；不包含 Loop 决策逻辑。"""

    max_loops: int = Field(default=12, gt=0)
    max_tool_calls: int = Field(default=8, gt=0)
    max_duration_seconds: float = Field(default=120.0, gt=0)
    tool_timeout_seconds: float = Field(default=20.0, gt=0)
