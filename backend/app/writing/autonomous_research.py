from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent.context import ContextManager
from app.agent.harness import Decision, Harness
from app.agent.harness_models import Budget, HarnessState, ToolPolicy
from app.agent.models import ContextState, LLMResponse, Message
from app.agent.policies import ToolDeclaration
from app.agent.interfaces import LLMClient
from app.tools.base import ToolContext, ToolResult
from app.tools.registry import ToolRegistry
from app.writing.execution_models import ExecutionConfig, ResearchBundle
from app.writing.research import WritingResearcher

ResearchMode = Literal["deterministic", "constrained_autonomous"]


class ResearchState(HarnessState):
    pass


class _Action(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["search", "read", "stop"]
    query: str | None = Field(default=None, min_length=1, max_length=500)
    chunk_id: str | None = Field(default=None, min_length=1, max_length=200)


class _KnowledgeTool:
    description = "Read-only local knowledge evidence tool."

    def __init__(self, name: str, search: Callable[[str, int], Any], read: Callable[[str], Any]):
        self.name = name
        self.search = search
        self.read = read

    @property
    def input_model(self):
        return _SearchInput if self.name == "search_knowledge" else _ReadInput

    def definition(self):
        from app.agent.models import ToolDefinition

        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=self.input_model.model_json_schema(),
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        parsed = self.input_model.model_validate(arguments)
        if self.name == "search_knowledge":
            result = self.search(parsed.query, parsed.top_k)
        else:
            result = self.read(parsed.chunk_id)
        if hasattr(result, "__await__"):
            result = await result
        return ToolResult(success=True, data=result)


class _SearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=5, ge=1, le=10)


class _ReadInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    chunk_id: str = Field(min_length=1, max_length=200)


class _CapturingLLM:
    def __init__(self, delegate: LLMClient):
        self.delegate = delegate
        self.last_response: LLMResponse | None = None

    async def complete(self, messages: list[Message], tools: list[Any]) -> LLMResponse:
        response = await self.delegate.complete(messages, tools)
        self.last_response = response
        return response


class ConstrainedResearchStrategy:
    def __init__(self, llm: _CapturingLLM, evidence: dict[str, Any], max_evidence: int):
        self.llm = llm
        self.evidence = evidence
        self.max_evidence = max_evidence

    def decide(self, state: ResearchState, tools: tuple[ToolDeclaration, ...]) -> Decision:
        response = self.llm.last_response
        self.llm.last_response = None
        if response is None or response.kind != "final" or not response.content:
            return Decision(kind="stop", reason="invalid research decision")
        try:
            action = _Action.model_validate_json(response.content)
        except (ValidationError, ValueError):
            return Decision(kind="stop", reason="invalid research decision")

        allowed_names = {item.name for item in tools} & {"search_knowledge", "read_knowledge"}
        if action.action == "stop":
            return Decision(kind="stop", reason="strategy stopped")
        if action.action == "search" and action.query and "search_knowledge" in allowed_names:
            return Decision(
                kind="call_tool",
                tool_name="search_knowledge",
                arguments={"query": action.query, "top_k": self.max_evidence},
            )
        if action.action == "read" and action.chunk_id and "read_knowledge" in allowed_names:
            if action.chunk_id not in self.evidence:
                return Decision(kind="stop", reason="read target is not approved evidence")
            return Decision(
                kind="call_tool", tool_name="read_knowledge", arguments={"chunk_id": action.chunk_id}
            )
        return Decision(kind="stop", reason="unsupported research action")


class AutonomousResearcher:
    """Optional, bounded research loop; deterministic retrieval remains the default."""

    def __init__(
        self,
        deterministic: WritingResearcher,
        *,
        llm: LLMClient | None = None,
        autonomous_search: Callable[[str, int], Any] | None = None,
        autonomous_read: Callable[[str], Any] | None = None,
    ) -> None:
        self.deterministic = deterministic
        self.llm = llm
        self.autonomous_search = autonomous_search or self._search_from_service
        self.autonomous_read = autonomous_read or self._read_from_service

    async def _search_from_service(self, query: str, limit: int):
        answer = await self.deterministic.knowledge_service.retrieve(
            query, mode="keyword", limit=limit
        )
        return answer.citations

    async def _read_from_service(self, chunk_id: str):
        repository = self.deterministic.knowledge_service.repository
        return await repository.get_chunk(chunk_id)

    async def collect(
        self,
        topic: str,
        *,
        config: ExecutionConfig,
        mode: ResearchMode = "deterministic",
    ) -> ResearchBundle:
        if mode == "deterministic":
            return await self.deterministic.collect(topic, config=config)
        if mode != "constrained_autonomous":
            raise ValueError("unsupported research mode")
        if self.llm is None:
            return ResearchBundle(
                evidence_status="no_results",
                citations=[],
                retrieval_mode="keyword",
                retrieval_fallback_reason="autonomous_model_unconfigured",
            )

        evidence: dict[str, Any] = {}

        async def resolve(value):
            return await value if hasattr(value, "__await__") else value

        async def search(query: str, limit: int):
            values = await resolve(self.autonomous_search(query, limit))
            citations = values if isinstance(values, list | tuple) else getattr(values, "citations", [])
            for item in citations:
                if isinstance(item, dict):
                    try:
                        from app.knowledge.service import CitationSnapshot
                        item = CitationSnapshot.model_validate(item)
                    except ValidationError:
                        continue
                if hasattr(item, "citation_id") and hasattr(item, "text"):
                    evidence[item.citation_id] = item
            return {"citation_ids": list(evidence)}

        async def read(chunk_id: str):
            value = await resolve(self.autonomous_read(chunk_id))
            if hasattr(value, "citation_id") and hasattr(value, "text"):
                evidence[value.citation_id] = value
                return {"citation_id": value.citation_id, "text": value.text}
            existing = evidence.get(chunk_id)
            if existing is not None and isinstance(getattr(value, "text", None), str):
                evidence[chunk_id] = existing.model_copy(update={"text": value.text})
                return {"citation_id": chunk_id, "text": value.text}
            if isinstance(value, dict):
                if existing is not None and isinstance(value.get("text"), str):
                    evidence[chunk_id] = existing.model_copy(update={"text": value["text"]})
                return value
            return {"status": "not_found"}

        registry = ToolRegistry([
            _KnowledgeTool("search_knowledge", search, read),
            _KnowledgeTool("read_knowledge", search, read),
        ])
        capturing_llm = _CapturingLLM(self.llm)
        strategy = ConstrainedResearchStrategy(capturing_llm, evidence, config.max_evidence)
        harness = Harness(ContextManager(max_tokens=config.max_context_tokens, token_counter=lambda text: len(text)))
        budget = Budget(
            max_loops=4,
            max_tool_calls=3,
            max_duration_seconds=config.timeout_seconds,
            max_context_tokens=config.max_context_tokens,
            max_response_chars=config.max_response_chars,
        )
        policy = ToolPolicy(
            allowed_tools=frozenset({"search_knowledge", "read_knowledge"}),
            max_calls=3,
            timeout_seconds=config.timeout_seconds,
            max_result_chars=config.max_response_chars,
        )
        initial_state = ResearchState(
            context_state=ContextState(
                summary=f'<research-topic data="untrusted">{topic}</research-topic>'
            ),
            structured_state={"topic": topic},
        )
        execution = await harness.run(
            initial_state,
            strategy,
            llm=capturing_llm,
            tools=registry,
            policy=policy,
            budget=budget,
        )
        if execution.status == "failed":
            return ResearchBundle(
                evidence_status="no_results",
                citations=[],
                retrieval_mode="keyword",
                retrieval_fallback_reason=execution.stop_reason or "autonomous_research_failed",
            )
        citations = list(evidence.values())[: config.max_evidence]
        status = "supported" if citations else "no_results"
        return ResearchBundle(
            evidence_status=status,
            citations=citations,
            retrieval_mode="keyword",
        )