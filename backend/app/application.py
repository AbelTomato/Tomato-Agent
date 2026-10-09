from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.api.knowledge import router as knowledge_router
from app.api.sessions import router as sessions_router
from app.api.writing import router as writing_router
from app.api.code_tasks import router as code_tasks_router
from app.dependencies import build_app_dependencies, create_runtime
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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await dependencies.session_repository.init()
        await dependencies.run_repository.init()
        await dependencies.knowledge_repository.init()
        await dependencies.writing_repository.init()
        await dependencies.writing_execution_repository.init()
        await dependencies.code_task_service.initialize()
        yield

    app = FastAPI(title="Tomato Agent Infrastructure", lifespan=lifespan)
    app.state.config = app_settings
    app.state.dependencies = dependencies
    for name, value in vars(dependencies).items():
        setattr(app.state, name, value)
    app.state.runtime = runtime
    app.state.code_task_llm = dependencies.code_task_service.llm
    app.state.code_task_strategy = dependencies.code_task_service.strategy
    app.state.code_task_test_tool = dependencies.code_task_service.test_tool
    app.include_router(health_router)
    app.include_router(sessions_router)
    app.include_router(knowledge_router)
    app.include_router(writing_router)
    app.include_router(code_tasks_router)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
    )
    return app