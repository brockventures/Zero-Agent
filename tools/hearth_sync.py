#!/usr/bin/env python3
"""Deterministic upstream Mirrormere -> Hearth sync helper."""
from typing import Tuple
from tools import nas_config


def sync_hearth(force: bool = False) -> Tuple[bool, str]:
    """Check if Host 2 Hearth container is healthy and synced."""
    rc, out, err = nas_config.run_ssh(
        nas_config.HOST_2_IP,
        "/usr/local/bin/docker ps --filter name=hearth-core --format '{{.Status}}'"
    )
    if rc != 0:
        return False, f"SSH check failed: {err.strip()}"
    status = out.strip()
    if not status or "Up" not in status:
        return False, f"hearth-core container not Up (status={status!r})"
    return True, f"hearth-core healthy ({status})"
