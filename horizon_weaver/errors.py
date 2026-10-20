"""Failures exposed by component ports, independent of concrete adapters."""
from .core import Usage


class HarnessError(RuntimeError):
    pass


class ContextBudgetError(ValueError):
    pass


class CheckDefinitionError(ValueError):
    """A new executable check is malformed; no candidate was measured."""


class GatewayError(RuntimeError):
    def __init__(self, message: str, usage: Usage, *, retryable: bool = False):
        super().__init__(message)
        self.usage = usage
        self.retryable = retryable

