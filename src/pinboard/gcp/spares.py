"""Spare sandboxes: creating one ahead of time. The bookkeeping (which spare exists, who claimed it, when to delete it) is in the state
store and the service; this module only knows how to make a ready-to-use sandbox and leave it running."""
from __future__ import annotations

import logging

from pinboard.core.config import Settings
from pinboard.gcp.sandbox import SandboxManager
from pinboard.gcp.skills import sync_skills

log = logging.getLogger(__name__)


def create_spare_sandbox(settings: Settings, *, client=None, storage_client=None) -> str:
    """Create a sandbox, wait until it answers, copy the skills in (the same work a session would wait for), and leave it running.
    Returns its name. If anything fails the half-made sandbox is deleted."""
    manager = SandboxManager(settings, client=client)
    sandbox = manager.open()  # waits until ready; deletes the sandbox itself if it never gets ready
    try:
        if settings.skills_source:
            log.info("spare sandbox skills synced: %s", sync_skills(sandbox, settings.skills_source, settings.skills_path, storage_client).skills)
    except BaseException:
        manager.close()
        raise
    return manager.detach()
