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
2. **Safety & Heating Protection**:
   - Respect HVAC setpoint bounds and window-open frost protection.
   - If a tool call is rejected by a safety guard (e.g., `window_open_protection` or
     `safety_bound_violation`), DO NOT attempt to bypass it; report the guard reason clearly.
3. **Structured Output to Supervisor**:
   - Always summarize: (a) action executed, (b) entity_id, and (c) the `assert_entity_state`
     verification outcome.
""".strip()


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
    tools: list[Any] = list(ctx.ha_adapter.build_tools())
    mcp_client = build_ha_mcp_client(ctx.config)
    if mcp_client is not None:
        tools.append(mcp_client)

    alias_map = ctx.config.home_assistant.entity_aliases
    prompt = _HA_BASE_SYSTEM_PROMPT
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

