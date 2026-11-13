"""Strands AgentTool wrappers for the persistent :class:`ActionScheduler`."""

from __future__ import annotations

from typing import Any, Literal

from strands import tool
from strands.types.tools import AgentTool

from src.core.scheduler import ActionScheduler, ScheduleStatus


def build_schedule_tools(scheduler: ActionScheduler) -> list[AgentTool]:
    """Return Strands tools backed by ``scheduler`` for Home Assistant sub-agents."""

    @tool(
        name="schedule_ha_action",
        description=(
            "Schedule a deferred Home Assistant service call to run automatically after "
            "`delay_seconds` (1..86400). Persists across restarts and verifies the physical "
            "state with Actor-Critic assertion when executed."
        ),
    )
    async def schedule_ha_action(
        delay_seconds: int,
        domain: str,
        service: str,
        entity_id: str,
        service_data: dict[str, Any] | None = None,
        expected_state: str | None = None,
        description: str = "",
        misfire_policy: Literal["safe_direction", "execute_always", "skip_if_late"] = "safe_direction",
    ) -> dict[str, Any]:
        """Schedule a future Home Assistant service execution.

        Args:
            delay_seconds: Delay in seconds from now before execution (1 to 86400).
            domain: Home Assistant domain (e.g. 'light', 'switch', 'climate', 'cover').
            service: Service name to run later (e.g. 'turn_off', 'turn_on', 'set_hvac_mode').
            entity_id: Canonical Home Assistant entity_id or configured alias.
            service_data: Optional service parameters (e.g. {'hvac_mode': 'off'}).
            expected_state: Optional expected state after running (auto-inferred for on/off/cover).
            description: Human-readable reason for the timer.
            misfire_policy: Behavior if delayed past grace period ('safe_direction' recommended).
        """
        rec = await scheduler.schedule_action(
            domain=domain,
            service=service,
            entity_id=entity_id,
            delay_seconds=delay_seconds,
            service_data=service_data,
            expected_state=expected_state,
            description=description,
            misfire_policy=misfire_policy,
            created_by="ha_agent",
        )
        return rec.model_dump(mode="json")

    @tool(
        name="list_scheduled_actions",
        description="List scheduled Home Assistant actions filtered by status (default 'pending') or entity_id.",
    )
    async def list_scheduled_actions(
        status: ScheduleStatus | None = "pending",
        entity_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List persistent scheduled Home Assistant actions.

        Args:
            status: Filter by status ('pending', 'done', 'failed', 'skipped', 'cancelled', or None for all).
            entity_id: Optional entity_id to filter by.
        """
        items = await scheduler.list_actions(status=status, entity_id=entity_id)
        return [item.model_dump(mode="json") for item in items]

    @tool(
        name="cancel_scheduled_action",
        description="Cancel a pending scheduled Home Assistant action by its schedule_id.",
    )
    async def cancel_scheduled_action(schedule_id: str) -> dict[str, Any]:
        """Cancel a pending deferred Home Assistant action.

        Args:
            schedule_id: Unique ID returned by `schedule_ha_action` or `list_scheduled_actions`.
        """
        rec = await scheduler.cancel_action(schedule_id)
        return rec.model_dump(mode="json")

    return [schedule_ha_action, list_scheduled_actions, cancel_scheduled_action]

