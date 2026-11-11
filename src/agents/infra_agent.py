"""Homelab Infrastructure Specialist Sub-Agent.

- Stateless execution (`preserve_context=False`).
- Dynamically injects `domain="infra"` lessons learned (Core Invariants + Semantic matches)
  from the 3-Tier Reflection Store before every task.
- Wraps `HomelabInfraToolkit` (Host telemetry via `psutil`, Docker socket control, and
  optional Proxmox VE LXC/VM status & power management) with strict Guard Clauses and
  Two-Phase Commit interventions for destructive operations.
"""

from __future__ import annotations

from strands.types.tools import AgentTool

from src.agents.factory import (
    SubAgentBuildContext,
    create_stateless_sub_agent,
    register_sub_agent,
)
from src.core.config import SubAgentSettings

_INFRA_BASE_SYSTEM_PROMPT = """
You are the Homelab Infrastructure Specialist Sub-Agent in a hierarchical orchestrator.

### Mandatory Operating Rules
1. **Inspect Before Acting**:
   - When asked to relieve host load or investigate resource usage, call `get_host_telemetry`
     and `list_docker_containers` first so your decision is grounded in live telemetry.
2. **Protected Resource Guards**:
   - Protected containers (`safety.protected_containers`) and protected VMs/LXCs
     (`safety.protected_vm_ids`) must NEVER be stopped or restarted. If a guard raises
     `protected_resource_violation`, report it immediately without retrying.
3. **Truthful Verification**:
   - Always report the container/guest `previous_status`, `current_status`, and `verified`
     fields returned by `manage_container` / `manage_proxmox_guest`.
""".strip()


@register_sub_agent("infra")
def build_infra_sub_agent(settings: SubAgentSettings, ctx: SubAgentBuildContext) -> AgentTool:
    """Build the stateless Infrastructure sub-agent and expose it via `.as_tool()`."""
    protected_list = ctx.config.safety.protected_containers
    prompt = _INFRA_BASE_SYSTEM_PROMPT
    if protected_list:
        joined = ", ".join(f"`{c}`" for c in protected_list)
        prompt += f"\n\n### Protected Containers (Never stop/restart)\n{joined}\n"

    return create_stateless_sub_agent(
        settings=settings,
        ctx=ctx,
        base_system_prompt=prompt,
        tools=list(ctx.infra_toolkit.build_tools()),
        state_verifier=None,
        mutating_tools=["manage_container", "manage_proxmox_guest"],
    )

