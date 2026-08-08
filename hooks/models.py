from __future__ import annotations
from dataclasses import dataclass
from typing import Any
from collections.abc import Callable


@dataclass
class HookHandler:
    """Metadata for a registered hook handler."""

    func: Callable
    name: str
    layer: str
    is_async: bool
    takes_mem: bool
    instance: Any | None = None
