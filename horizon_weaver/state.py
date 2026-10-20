"""Compatibility imports for the v1.0 state module.

The v1.1 implementation has separate event storage, state representation and graph modules.
"""
from .event_store import EventStore, StoreError
from .state_graph import StateGraph

__all__ = ["EventStore", "StateGraph", "StoreError"]
