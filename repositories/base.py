from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from shared.write_dispatch import WriteDispatch


class BaseRepo:
    """Base for all repositories; holds the shared WriteDispatch."""

    def __init__(self, dispatch: WriteDispatch) -> None:
        self._dispatch = dispatch
