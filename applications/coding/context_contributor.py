"""Coding-owned runtime events contributed through the generic context port."""

from __future__ import annotations

import json

from applications.coding.orchestration.background_task import BG
from runtime.extensions import ContextContribution
from runtime.messaging.team_bus import BUS


class CodingRuntimeContributor:
    def contribute(self, *, session, profile) -> list[ContextContribution]:
        if str(getattr(profile, "tool_mode", "") or "") != "coding":
            return []
        metadata = getattr(session, "metadata", {}) or {}
        if metadata.get("kind") != "coding_application":
            return []
        notifications = BG.drain_notifications()
        inbox = BUS.read_inbox("lead")
        if not notifications and not inbox:
            return []
        payload = {
            "background_results": notifications,
            "inbox": inbox,
        }
        return [ContextContribution(
            name="coding_runtime_events",
            source="coding_application",
            content=(
                '<coding-runtime-events trust="context-only" instructions="false">\n'
                + json.dumps(payload, ensure_ascii=False, default=str)
                + "\n</coding-runtime-events>"
            ),
        )]


__all__ = ("CodingRuntimeContributor",)
