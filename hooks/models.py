from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass
class HookHandler:
    """Metadata for a registered hook handler."""
    func: Callable
    name: str
    layer: str
    is_async: bool
    takes_mem: bool
    instance: Optional[Any] = None
