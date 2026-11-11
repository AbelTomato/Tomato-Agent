from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request

from app.api.memory import router as memory_router
from app.api.layered_memory import router as layered_memory_router
from app.api.knowledge import router as knowledge_router
from app.api.sessions import router as sessions_router
from app.api.writing import router as writing_router
from app.api.code_tasks import router as code_tasks_router
from app.api.workspaces import router as workspaces_router
from app.api.projects import router as projects_router
from app.api.tasks import router as tasks_router
from app.dependencies import build_app_dependencies, create_runtime
from app.engine import ApplicationEngine
from app.settings import Settings, settings

health_router = APIRouter()


@health_router.get("/health")
async def health():
    return {"status": "ok", "runtime": "runtime-implemented"}


@health_router.get("/api/tools")
async def tools(request: Request):
    return {
        "tools": [
            item.model_dump()
            for item in request.app.state.tool_registry.definitions()
        ]
    }


def create_app(config: Settings | None = None) -> FastAPI:
    app_settings = config or settings
    dependencies = build_app_dependencies(app_settings)
    runtime = create_runtime(app_settings, dependencies)
    engine = ApplicationEngine(dependencies, app_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await engine.start()
        try:
            yield
        finally:
            await engine.stop()

    app = FastAPI(title="Tomato Agent Engine", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = app_settings
    app.state.dependencies = dependencies
    for name, value in vars(dependencies).items():
        setattr(app.state, name, value)
    app.state.runtime = runtime
    app.state.engine = engine
    app.state.code_task_llm = dependencies.code_task_service.llm
    app.state.code_task_strategy = dependencies.code_task_service.strategy
    app.state.code_task_test_tool = dependencies.code_task_service.test_tool
    app.include_router(health_router)
    app.include_router(memory_router)
    app.include_router(layered_memory_router)
    app.include_router(sessions_router)
    app.include_router(knowledge_router)
    app.include_router(writing_router)
    app.include_router(code_tasks_router)
    app.include_router(workspaces_router)
    app.include_router(projects_router)
    app.include_router(tasks_router)
    return app
