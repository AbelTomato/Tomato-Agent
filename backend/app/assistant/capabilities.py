"""Server-owned capability metadata over the existing tool registry."""

from copy import deepcopy

from app.tools.registry import ToolRegistry
from .models import CapabilityDescriptor


class CapabilityRegistry(ToolRegistry):
    def __init__(self):
        super().__init__()
        self._descriptors = {}
        self._migrations = {}

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

    def catalog(self, *, category=None, query=None):
        return [{**tool.model_dump(mode="json"), **self.resolve(tool.name).model_dump(mode="json")}
                for tool in super().definitions()
                if (category is None or self.resolve(tool.name).category == category)
                and (query is None or query.casefold() in tool.name.casefold()
                     or query.casefold() in tool.description.casefold())]

    def register_migration(self, name, source_version, migrate):
        """Register a server-reviewed, semantic-preserving migration to the current contract."""
        target = self.resolve(name).contract_version
        key = (name, source_version, target)
        if source_version == target or key in self._migrations or not callable(migrate):
            raise ValueError("invalid_contract_migration")
        self._migrations[key] = migrate

    def prepare_call(self, name, version, arguments):
        descriptor = self.resolve(name)
        if version is not None and version != descriptor.contract_version:
            migrate = self._migrations.get((name, version, descriptor.contract_version))
            if migrate is None:
                raise ValueError("stale_contract")
            arguments = migrate(deepcopy(arguments))
        return self.get(name).input_model.model_validate(arguments).model_dump(mode="json")