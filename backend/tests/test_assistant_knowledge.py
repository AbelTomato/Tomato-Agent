from types import SimpleNamespace

from app.assistant.models import CapabilityOutcome
from app.assistant.tools.knowledge import KnowledgeCapability
from app.knowledge.service import KnowledgeService
from app.tools.base import ToolContext


class EmptyRepository:
    async def search_chunks(self, query, limit):
        return []


async def test_empty_evidence_is_explicit_and_does_not_call_model():
    service = KnowledgeService(EmptyRepository())
    context = ToolContext(session_id="session", run_id="run", model_client=object())
    for name in ("search_knowledge", "answer_knowledge"):
        result = await KnowledgeCapability(name, service, mode="keyword").execute({"query": "missing"}, context)
        outcome = CapabilityOutcome.model_validate(result.data)
        assert result.success and outcome.result.citations == []
        assert outcome.result.answer


async def test_pipeline_receives_run_scoped_client():
    seen = []

    class Pipeline:
        async def run(self, query, **kwargs):
            seen.append(kwargs["llm_client"])
            return SimpleNamespace(selection=SimpleNamespace(selected=[]), decision=SimpleNamespace(status="no_results"))

    service = KnowledgeService(EmptyRepository())
    service.pipeline = Pipeline()
    client = object()
    await KnowledgeCapability("search_knowledge", service).execute({"query": "missing"},
        ToolContext(session_id="session", run_id="run", model_client=client))
    assert seen == [client]