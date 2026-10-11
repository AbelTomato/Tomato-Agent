"""Server-owned capability metadata over the existing tool registry."""

from app.tools.registry import ToolRegistry
from .models import CapabilityDescriptor


class CapabilityRegistry(ToolRegistry):
    def __init__(self):
        super().__init__()
        self._descriptors = {}

    def register(self, tool, descriptor=None):
        if not isinstance(descriptor, CapabilityDescriptor):
            raise ValueError("capability descriptor required")
        if descriptor.name != tool.name or descriptor.parameters != tool.definition().parameters:
            raise ValueError("descriptor does not match tool")
        if descriptor.side_effect != descriptor.recovery_class:
            raise ValueError("side effect requires matching recovery classification")
        if tool.name in self._descriptors:
            raise ValueError("duplicate capability")
        self._descriptors[tool.name] = descriptor.model_copy(deep=True)
        super().register(tool)

    def resolve(self, name):
        self.get(name)
        return self._descriptors[name].model_copy(deep=True)

    def definitions(self):
        return [item for item in super().definitions() if self.resolve(item.name).available]