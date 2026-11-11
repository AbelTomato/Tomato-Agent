"""Application lifecycle shared by the desktop engine and HTTP adapter."""


class ApplicationEngine:
    def __init__(self, dependencies, config):
        self.dependencies = dependencies
        self.config = config
        self._started = False

    async def start(self):
        if self._started:
            return
        dependencies = self.dependencies
        for component, method in (
            (dependencies.session_repository, "init"),
            (dependencies.memory_repository, "init"),
            (getattr(dependencies, "layered_memory_repository", None), "init"),
            (dependencies.project_service, "initialize"),
            (dependencies.run_repository, "init"),
            (dependencies.knowledge_repository, "init"),
            (dependencies.writing_repository, "init"),
            (dependencies.writing_execution_repository, "init"),
            (dependencies.code_task_service, "initialize"),
        ):
            if component is None:
                continue
            await getattr(component, method)()
        worker = dependencies.execution_worker
        try:
            if self.config.effective_task_worker_enabled:
                while True:
                    recovered = await worker.recover_on_startup()
                    if len(recovered) < worker.recovery_scan_limit:
                        break
                await worker.start()
            memory_worker = dependencies.memory_worker
            if memory_worker is not None:
                await memory_worker.start()
            layered_memory_worker = getattr(dependencies, "layered_memory_worker", None)
            if layered_memory_worker is not None:
                await layered_memory_worker.start()
        except BaseException:
            await worker.stop()
            memory_worker = dependencies.memory_worker
            if memory_worker is not None:
                await memory_worker.stop()
            layered_memory_worker = getattr(dependencies, "layered_memory_worker", None)
            if layered_memory_worker is not None:
                await layered_memory_worker.stop()
            raise
        self._started = True

    async def stop(self):
        if not self._started:
            return
        try:
            await self.dependencies.execution_worker.stop()
        finally:
            try:
                memory_worker = self.dependencies.memory_worker
                if memory_worker is not None:
                    await memory_worker.stop()
            finally:
                try:
                    layered_memory_worker = getattr(self.dependencies, "layered_memory_worker", None)
                    if layered_memory_worker is not None:
                        await layered_memory_worker.stop()
                finally:
                    self._started = False
