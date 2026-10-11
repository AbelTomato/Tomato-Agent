from dataclasses import dataclass
from pathlib import Path

from app.agent.context import ContextManager
from app.agent.interfaces import LLMClient
from app.agent.models import LLMResponse, Message, ToolDefinition
from app.agent.runtime import AgentRuntime
from app.agent.code_task import CodeTaskService, CodeTaskStrategy
from app.artifacts.service import ArtifactService
from app.knowledge.embeddings import EmbeddingClient
from app.knowledge.pipeline_factory import (
    build_knowledge_retrieval_service,
    build_knowledge_service,
    create_embedding_client as create_knowledge_embedding_client,
    runtime_config_from_settings,
)
from app.knowledge.repository import KnowledgeRepository
from app.knowledge.service import KnowledgeService
from app.llm.compatible_client import OpenAICompatibleClient
from app.runs.repository import RunRepository
from app.sessions.repository import SessionRepository
from app.settings import Settings
from app.tools.calculator import Calculator
from app.tools.code_tests import RegisteredTestTarget, RunTestsTool, TestTargetRegistry
from app.agent.sandbox import LocalSandboxExecutor
from app.agent.sandbox_docker import DockerSandboxExecutor
from app.agent.sandbox_openshell import OpenShellSandboxExecutor
from app.tools.read_docs import ReadDocs
from app.tools.read_knowledge import ReadKnowledge
from app.tools.registry import ToolRegistry
from app.tools.search import Search
from app.tools.search_knowledge import SearchKnowledge
from app.writing.draft import DraftGenerator
from app.writing.execution_models import ExecutionConfig
from app.writing.execution_repository import WritingExecutionRepository
from app.writing.executor import WritingTaskExecutor
from app.writing.outline import OutlineGenerator
from app.writing.repository import WritingRepository
from app.writing.research import WritingResearchQueryPort, WritingResearcher
from app.writing.service import WritingService
from app.workspaces.service import WorkspaceService
from app.execution.repository import ExecutionRepository
from app.execution.worker import ExecutionWorker


class UnconfiguredLLM:
    async def complete(self, messages: list[Message], tools: list[ToolDefinition]) -> LLMResponse:
        raise RuntimeError("No LLMClient has been configured")


@dataclass
class AppDependencies:
    session_repository: SessionRepository
    run_repository: RunRepository
    knowledge_repository: KnowledgeRepository
    writing_repository: WritingRepository
    writing_execution_repository: WritingExecutionRepository
    tool_registry: ToolRegistry
    llm_client: LLMClient
    knowledge_service: KnowledgeService
    writing_service: WritingService
    writing_research_service: WritingResearchQueryPort
    writing_executor: WritingTaskExecutor
    workspace_service: WorkspaceService
    artifact_service: ArtifactService
    code_task_service: CodeTaskService
    execution_repository: ExecutionRepository
    execution_worker: ExecutionWorker
    task_service: object


def create_llm_client(config: Settings) -> LLMClient:
    if not config.llm_api_key:
        return UnconfiguredLLM()
    return OpenAICompatibleClient(
        api_key=config.llm_api_key,
        base_url=config.llm_base_url,
        model=config.llm_model,
        timeout_seconds=config.llm_timeout_seconds,
    )


def create_embedding_client(config: Settings) -> EmbeddingClient | None:
    """Compatibility facade delegating embedding construction to the factory."""

    return create_knowledge_embedding_client(runtime_config_from_settings(config))


def create_knowledge_service(
    repository: KnowledgeRepository,
    config: Settings,
    *,
    embedding_client: EmbeddingClient | None = None,
) -> KnowledgeService:
    return build_knowledge_service(
        repository,
        runtime_config_from_settings(config),
        embedding_client=embedding_client,
    )


def build_app_dependencies(config: Settings) -> AppDependencies:
    session_repository = SessionRepository(config.database_path)
    run_repository = RunRepository(config.database_path)
    knowledge_repository = KnowledgeRepository(config.database_path)
    writing_repository = WritingRepository(config.database_path)
    writing_execution_repository = WritingExecutionRepository(config.database_path)
    llm_client = create_llm_client(config)
    embedding_client = create_embedding_client(config)
    tool_registry = ToolRegistry(
        [
            Calculator(),
            Search(),
            ReadDocs(Path(config.docs_root), max_bytes=config.max_doc_bytes),
            SearchKnowledge(
                knowledge_repository,
                max_result_chars=config.max_tool_result_chars,
            ),
            ReadKnowledge(
                knowledge_repository,
                max_result_chars=config.max_tool_result_chars,
            ),
        ]
    )
    runtime_config = runtime_config_from_settings(config)
    knowledge_service = build_knowledge_service(
        knowledge_repository,
        runtime_config,
        embedding_client=embedding_client,
    )
    writing_service = WritingService(
        writing_repository,
        draft_directory=config.draft_directory,
    )
    writing_research_service = build_knowledge_retrieval_service(
        knowledge_repository,
        runtime_config,
        embedding_client=embedding_client,
    )
    writing_executor = WritingTaskExecutor(
        writing_service,
        writing_execution_repository,
        WritingResearcher(writing_research_service),
        OutlineGenerator(llm_client),
        draft_generator=DraftGenerator(llm_client),
        config=ExecutionConfig(
            retrieval_mode=config.writing_retrieval_mode,
            max_evidence=config.writing_max_evidence,
            max_context_tokens=config.writing_max_context_tokens,
            max_response_chars=config.writing_max_response_chars,
            timeout_seconds=config.writing_timeout_seconds,
        ),
        model_id=config.llm_model if config.llm_api_key.strip() else "",
    )
    workspace_service = WorkspaceService(
        config.code_task_workspace_root,
        max_file_bytes=config.code_task_max_file_bytes,
        allowed_roots=config.workspace_allowed_roots,
    )
    artifact_service = ArtifactService(
        config.code_task_artifact_root,
        workspace_service,
        max_bytes=config.code_task_max_artifact_bytes,
    )
    if config.code_task_sandbox_backend == "docker":
        sandbox_executor = DockerSandboxExecutor(
            config.code_task_docker_image,
            docker_binary=config.code_task_docker_binary,
        )
    elif config.code_task_sandbox_backend == "openshell":
        sandbox_executor = OpenShellSandboxExecutor(
            config.code_task_openshell_image,
            openshell_binary=config.code_task_openshell_binary,
        ) if config.code_task_openshell_image else LocalSandboxExecutor()
    else:
        sandbox_executor = LocalSandboxExecutor()
    code_task_test_tool = RunTestsTool(
        TestTargetRegistry([RegisteredTestTarget(
            name="unit", command=("python", "-m", "pytest", "-q", "test_calculator.py"), cwd=""
        )]),
        sandbox_executor,
        workspace_service,
        artifact_service,
        run_repository,
    )
    code_task_service = CodeTaskService(
        run_repository,
        workspace_service,
        artifact_service,
        llm=llm_client if config.llm_api_key.strip() else None,
        strategy=CodeTaskStrategy() if config.llm_api_key.strip() else None,
        test_tool=code_task_test_tool,
        budget=config.code_task_budget,
        capability_profile_factory=lambda repo: config.code_task_capability_profile(repo),
        execution_repository=ExecutionRepository(config.database_path),
    )
    from app.tasks.service import TaskService
    from app.tasks.adapters.chat import ChatAdapter
    from app.tasks.adapters.code import CodeAdapter
    from app.tasks.adapters.knowledge import KnowledgeAdapter
    from app.tasks.adapters.writing import WritingAdapter
    from app.knowledge.conversation import KnowledgeConversation
    from app.execution.ports import canonical_digest
    from app.assistant.service import AssistantService

    runtime = AgentRuntime(llm_client, ContextManager(), tool_registry, session_repository,
                           "You are a concise assistant.", config.runtime_config)
    task_service = TaskService(run_repository, code_task_service.execution_repository,
        session_repository, {
            "code": CodeAdapter(code_task_service),
            "chat": ChatAdapter(runtime, session_repository),
            "knowledge": KnowledgeAdapter(KnowledgeConversation(session_repository, knowledge_service, llm_client)),
            "writing": WritingAdapter(writing_service, writing_executor, code_task_service.execution_repository),
        }, enabled=config.effective_task_worker_enabled, model_available=bool(config.llm_api_key.strip()),
        assistant=AssistantService(run_repository, code_task_service.execution_repository, session_repository,
            workspace_service, artifact_service, knowledge_service, llm_client, config),
        config_digest=canonical_digest({"model": config.llm_model, "base_url": config.llm_base_url,
            "runtime": config.runtime_config.model_dump(mode="json"),
            "writing": {"retrieval_mode": config.writing_retrieval_mode,
                        "max_evidence": config.writing_max_evidence,
                        "max_context_tokens": config.writing_max_context_tokens,
                        "max_response_chars": config.writing_max_response_chars,
                        "timeout_seconds": config.writing_timeout_seconds},
            "knowledge": {key: value for key, value in config.model_dump(mode="json").items()
                          if key.startswith("knowledge_") and not key.endswith("api_key")},
            "embedding": {"model": config.embedding_model, "dimensions": config.embedding_dimensions,
                          "base_url": config.embedding_base_url}}))

    async def observe_recovery(candidate):
        run = await run_repository.get_run(candidate.run_id)
        if run.task_type == "assistant":
            from app.execution.recovery import RecoveryObservation
            snapshot = await code_task_service.execution_repository.get_snapshot(run.id)
            digest = task_service.assistant.policy_digest
            if snapshot.config_digest != task_service.config_digest or not config.assistant_enabled:
                digest = canonical_digest({"policy": digest, "config_changed": True})
            return RecoveryObservation(policy_digest=digest)
        return code_task_service.recovery_observation(run)

    execution_worker = ExecutionWorker(
        code_task_service.execution_repository, task_service.execute,
        poll_interval_seconds=config.code_task_worker_poll_interval_seconds,
        max_concurrency=config.code_task_worker_max_concurrency,
        recovery_observation_factory=observe_recovery,
    )
    execution_worker.stage_recovery_factory = task_service.recover_stage
    return AppDependencies(
        session_repository=session_repository,
        run_repository=run_repository,
        knowledge_repository=knowledge_repository,
        writing_repository=writing_repository,
        writing_execution_repository=writing_execution_repository,
        tool_registry=tool_registry,
        llm_client=llm_client,
        knowledge_service=knowledge_service,
        writing_service=writing_service,
        writing_research_service=writing_research_service,
        writing_executor=writing_executor,
        workspace_service=workspace_service,
        artifact_service=artifact_service,
        code_task_service=code_task_service,
        execution_repository=code_task_service.execution_repository,
        execution_worker=execution_worker,
        task_service=task_service,
    )


def create_runtime(config: Settings, dependencies: AppDependencies) -> AgentRuntime:
    return AgentRuntime(
        llm=dependencies.llm_client,
        context_manager=ContextManager(),
        tool_registry=dependencies.tool_registry,
        repository=dependencies.session_repository,
        system_instruction="You are a concise assistant.",
        config=config.runtime_config,
    )