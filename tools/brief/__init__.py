"""BRIEF (Background Routing, Inbox & Execution Filter) package."""

from .config import (
    BriefConfig,
    ensure_memory_scaffold,
    is_kill_switch_active,
    set_kill_switch,
    touch_heartbeat,
)
from .queue import BriefQueue, list_sessions

__all__ = [
    "BriefConfig",
    "BriefQueue",
    "ensure_memory_scaffold",
    "is_kill_switch_active",
    "list_sessions",
    "set_kill_switch",
    "touch_heartbeat",
]
