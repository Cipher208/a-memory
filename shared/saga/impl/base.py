from __future__ import annotations

"""Compatibility shim — the saga implementation lives in sibling modules.

Historically everything (Saga engine, watchdog, state storage) lived in
this file. It was split into impl.saga / impl.watchdog / impl.storage;
this module re-exports the public names so existing
``from shared.saga.impl.base import ...`` imports keep working.
"""

from shared.saga.impl.storage import SAGA_DIR
from shared.saga.impl.saga import Saga, SagaStatus, SagaStep
from shared.saga.impl.watchdog import SagaWatchdog, saga_watchdog

__all__ = [
    "SAGA_DIR",
    "Saga",
    "SagaStatus",
    "SagaStep",
    "SagaWatchdog",
    "saga_watchdog",
]
