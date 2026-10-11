class CodeAdapter:
    def __init__(self, service):
        self.service = service

    async def execute(self, run, lease, port):
        return await self.service.execute(str(run.id), execution_lease=lease)

    async def observe_recovery(self, run, snapshot):
        return self.service.recovery_observation(run)