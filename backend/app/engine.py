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
            (dependencies.project_service, "initialize"),
            (dependencies.run_repository, "init"),
            (dependencies.knowledge_repository, "init"),
            (dependencies.writing_repository, "init"),
            (dependencies.writing_execution_repository, "init"),
            (dependencies.code_task_service, "initialize"),
        ):
            await getattr(component, method)()
        worker = dependencies.execution_worker
        try:
            if self.config.effective_task_worker_enabled:
                while True:
                    recovered = await worker.recover_on_startup()
                    if len(recovered) < worker.recovery_scan_limit:
                        break
                await worker.start()
        except BaseException:
            await worker.stop()
            raise
        self._started = True

    async def stop(self):
        if not self._started:
            return
        try:
            await self.dependencies.execution_worker.stop()
        finally:
            self._started = False
