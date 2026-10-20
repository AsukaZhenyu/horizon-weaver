"""Static composition and resource ownership; no business event bus."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable, Mapping


@dataclass(frozen=True)
class Component:
    name: str
    provides: tuple[str, ...]
    requires: tuple[str, ...]
    factory: Callable[[Mapping[str, object], ExitStack], Mapping[str, object]]


class Runtime:
    def __init__(self, components: tuple[Component, ...]):
        self.components = components
        self._stack: ExitStack | None = None
        self._services: Mapping[str, object] = MappingProxyType({})

    def _order(self) -> tuple[Component, ...]:
        names: set[str] = set()
        providers: dict[str, Component] = {}
        for component in self.components:
            if component.name in names:
                raise ValueError(f"duplicate component: {component.name}")
            names.add(component.name)
            for service in component.provides:
                if service in providers:
                    raise ValueError(f"duplicate provider: {service}")
                providers[service] = component
        visiting: set[str] = set()
        visited: set[str] = set()
        order: list[Component] = []

        def visit(component: Component):
            if component.name in visiting:
                raise ValueError(f"dependency cycle at {component.name}")
            if component.name in visited:
                return
            visiting.add(component.name)
            for required in component.requires:
                if required not in providers:
                    raise ValueError(f"missing provider: {required}")
                visit(providers[required])
            visiting.remove(component.name)
            visited.add(component.name)
            order.append(component)

        for component in self.components:
            visit(component)
        return tuple(order)

    def start(self) -> Mapping[str, object]:
        if self._stack is not None:
            raise RuntimeError("runtime is already started")
        order = self._order()  # Validate the complete graph before allocating resources.
        stack = ExitStack()
        services: dict[str, object] = {}
        try:
            for component in order:
                provided = dict(component.factory(MappingProxyType(services), stack))
                if set(provided) != set(component.provides):
                    raise ValueError(f"provider contract mismatch: {component.name}")
                services.update(provided)
        except BaseException:
            stack.close()  # Also cleans resources acquired by a partially failed factory.
            raise
        self._stack = stack
        self._services = MappingProxyType(services)
        return self._services

    def stop(self) -> None:
        stack, self._stack = self._stack, None
        self._services = MappingProxyType({})
        if stack is not None:
            stack.close()

    def __enter__(self) -> Mapping[str, object]:
        return self.start()

    def __exit__(self, *_):
        self.stop()

