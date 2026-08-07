"""
Hooks Module - 24 hooks (12 user + 12 agent)
"""

from .agent_hooks import AgentHooks
from .registry import HookRegistry
from .user_hooks import UserHooks

__all__ = ["AgentHooks", "HookRegistry", "UserHooks"]
