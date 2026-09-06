class AgentError(Exception):
    code = "AGENT_ERROR"
    retryable = False


class ToolNotFoundError(AgentError):
    code = "TOOL_NOT_FOUND"


class ToolInputError(AgentError):
    code = "TOOL_INPUT_INVALID"


class ToolExecutionError(AgentError):
    code = "TOOL_EXECUTION_FAILED"


class ToolTimeoutError(AgentError):
    code = "TOOL_TIMEOUT"
    retryable = True


class UnsafePathError(ToolInputError):
    code = "UNSAFE_PATH"
