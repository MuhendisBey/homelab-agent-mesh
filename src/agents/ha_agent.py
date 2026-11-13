"""Home Assistant Specialist Sub-Agent.

- Stateless execution (`preserve_context=False`) so each task runs in an isolated context.
- Dynamically injects Home Assistant domain lessons learned (`domain="ha"`) from the
  3-Tier Reflection Store via `DomainMemoryAndCriticHooks`.
- Combines the custom async WebSocket adapter (`HomeAssistantWSAdapter`) with the optional
  external `ha-mcp` MCP client (`MCPClient`).
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.client.sse import sse_client
from strands.tools.mcp import MCPClient, ToolFilters
from strands.types.tools import AgentTool

from src.agents.factory import (
    SubAgentBuildContext,
    create_stateless_sub_agent,
    register_sub_agent,
)
from src.core.config import AppConfig, SubAgentSettings
from src.tools.schedule_tools import build_schedule_tools

logger = logging.getLogger(__name__)

_HA_BASE_SYSTEM_PROMPT = """
You are the Home Assistant Specialist Sub-Agent in a hierarchical smart-home orchestrator.

### Mandatory Operating Rules
1. **Truthful Actor-Critic Execution**:
   - After calling ANY state-changing service (`call_ha_service`, `execute_sequential_loop`,
     or an external MCP service tool), you MUST call `assert_entity_state` to verify that the
     physical device reached the expected state / attributes.
   - NEVER claim an action succeeded unless `assert_entity_state` returned
     `StateAssertionSuccess` (`"verified": true`). If it returns `StateAssertionFailure`,
     report the failure and actual state accurately.
2. **Rapid Sequential Loops**:
   - For repeating sub-second actions (e.g., toggle/blink N times with ms delays), ALWAYS use
     `execute_sequential_loop` once over WebSocket instead of calling `call_ha_service` repeatedly.
   - Example: "2 kere aç-kapa" (on-off twice) corresponds to 4 `toggle` iterations when starting
     from `off`. Check initial state if unsure, then call `execute_sequential_loop` and verify
     the final state with `assert_entity_state`.
3. **Safety & Heating Protection**:
   - Respect HVAC setpoint bounds and window-open frost protection.
   - If a tool call is rejected by a safety guard (e.g., `window_open_protection` or
     `safety_bound_violation`), DO NOT attempt to bypass it; report the guard reason clearly.
4. **Structured Output to Supervisor**:
   - Always summarize: (a) action executed, (b) entity_id, and (c) the `assert_entity_state`
     verification outcome.
""".strip()

_HA_RESOLVER_PROMPT = """

### Fast Entity Resolution Policy (EntityResolver Active)
- ALWAYS call `resolve_entity(query=..., domain_hint=...)` FIRST whenever you do not already have a verified canonical `entity_id`! It resolves cached memory and registry entities in <10ms without external round-trips.
- If `resolve_entity` returns `resolved: true` (`confidence >= 0.85`), use `top_entity_id` directly.
- If `resolve_entity` returns `ambiguous: true` with multiple plausible candidates in different rooms/fixtures, ask for clarification OR inspect `candidates` carefully before mutating physical hardware.
- If `resolve_entity` returns `resolved: false` (confidence too low or no match), fall back to `ha_mcp_ha_search(query=..., limit=50)` (if available).
- If the user corrects an entity mapping (e.g. "hayır, diğer oda/ışık"), call `remember_entity_alias(phrase=..., entity_id=..., wrong_entity_id=...)`.
""".rstrip()

_HA_SCHEDULER_PROMPT = """

### Deferred & Timed Actions Policy (ActionScheduler Active)
- For timed/deferred requests (e.g. "klimayı 10 dakika çalıştır sonra kapat" or "5 dakika sonra mutfak ışığını kapat"):
  1. Execute and verify (`assert_entity_state`) any IMMEDIATE action first (e.g. turn on the climate device now).
  2. Call `schedule_ha_action(delay_seconds=..., domain=..., service=..., entity_id=..., service_data=..., description=...)` for the future part instead of sleeping inside the conversation.
- Use `list_scheduled_actions` to inspect active timers and `cancel_scheduled_action` when asked to cancel a timer.
""".rstrip()

_HA_MCP_DISCOVERY_PROMPT = """

### Entity Discovery Fallback (HA-MCP Active)
- NEVER guess or brute-force `entity_id` values with `get_ha_entity_state`.
- When `resolve_entity` (if available) returns `resolved: false`, fall back to `ha_mcp_ha_search` (or `ha_mcp_ha_list_floors_areas` / `ha_mcp_ha_get_overview`).
- Search tips for speed and accuracy:
  - Always set `limit=50` in `ha_mcp_ha_search` (the default limit of 10 often fills up with
    `binary_sensor.*` or `sensor.*` entities).
  - If searching in a non-English home (e.g., Turkish: "mutfak", "oturma odası"), search with the
    original room/device keywords first (e.g., `query="mutfak"` or `query="mutfak isik"`).
  - Real-world lights or appliances may belong to `switch.*` as well as `light.*`; check both or
    omit `domain_filter` with `limit=50`.
  - `ha_mcp_ha_search` already returns each entity's current `state`, so you do not need an extra
    `get_ha_entity_state` call unless you specifically need detailed attributes.
""".rstrip()


def build_ha_mcp_client(config: AppConfig) -> MCPClient | None:
    """Create a resilient Strands `MCPClient` for `ha-mcp` when enabled and configured."""
    mcp_cfg = config.home_assistant.mcp
    url = (config.ha_mcp_sse_url or "").strip()
    if not mcp_cfg.enabled or not url:
        return None

    headers: dict[str, str] = {}
    if config.ha_mcp_auth_token is not None:
        token_val = config.ha_mcp_auth_token.get_secret_value().strip()
        if token_val:
            headers["Authorization"] = f"Bearer {token_val}"

    filters: ToolFilters = {}
    if mcp_cfg.tool_allowlist:
        filters["allowed"] = list(mcp_cfg.tool_allowlist)
    if mcp_cfg.tool_denylist:
        filters["rejected"] = list(mcp_cfg.tool_denylist)

    if mcp_cfg.transport == "sse":
        return MCPClient(
            transport_callable=lambda: sse_client(url=url, headers=headers or None),
            startup_timeout=mcp_cfg.startup_timeout_sec,
            tool_filters=filters or None,
            prefix="ha_mcp",
            continue_on_error=True,
        )
    return MCPClient(
        url=url,
        headers=headers or None,
        startup_timeout=mcp_cfg.startup_timeout_sec,
        tool_filters=filters or None,
        prefix="ha_mcp",
        continue_on_error=True,
    )


@register_sub_agent("ha")
def build_ha_sub_agent(settings: SubAgentSettings, ctx: SubAgentBuildContext) -> AgentTool:
    """Build the stateless Home Assistant sub-agent and expose it via `.as_tool()`."""
    if ctx.entity_resolver is not None and ctx.ha_adapter.entity_resolver is None:
        ctx.ha_adapter.attach_entity_resolver(ctx.entity_resolver)

    tools: list[Any] = list(ctx.ha_adapter.build_tools())
    prompt = _HA_BASE_SYSTEM_PROMPT
    if ctx.ha_adapter.entity_resolver is not None:
        prompt += _HA_RESOLVER_PROMPT

    if ctx.scheduler is not None:
        tools.extend(build_schedule_tools(ctx.scheduler))
        prompt += _HA_SCHEDULER_PROMPT

    mcp_client = build_ha_mcp_client(ctx.config)
    if mcp_client is not None:
        tools.append(mcp_client)
        prompt += _HA_MCP_DISCOVERY_PROMPT

    alias_map = ctx.config.home_assistant.entity_aliases
    if alias_map:
        alias_lines = "\n".join(f"- `{alias}` -> `{entity}`" for alias, entity in sorted(alias_map.items()))
        prompt += f"\n\n### Configured Entity Aliases\n{alias_lines}\n"

    return create_stateless_sub_agent(
        settings=settings,
        ctx=ctx,
        base_system_prompt=prompt,
        tools=tools,
        state_verifier=ctx.ha_adapter,
        mutating_tools=ctx.config.home_assistant.mutating_tools,
    )

