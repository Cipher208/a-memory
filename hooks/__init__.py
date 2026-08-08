"""
Hooks Module - 24 hooks (12 user + 12 agent)
"""

from .agent_hooks import AgentHooks
from .loader import load_all_hooks
from .registry import hook_registry
from .user_hooks import UserHooks

# Trigger registration
load_all_hooks()

__all__ = ["AgentHooks", "UserHooks", "hook_registry", "load_all_hooks"]
