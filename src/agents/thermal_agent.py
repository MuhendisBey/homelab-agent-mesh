"""Thermal Digital Twin Specialist Sub-Agent.

- Stateless execution (`preserve_context=False`) so each thermal diagnostic runs in an isolated
  context.
- Dynamically injects `domain="thermal"` lessons learned from the 3-Tier Reflection Store via
  `DomainMemoryAndCriticHooks`.
- Strictly enforces "Python Does the Physics, LLM Does the Reasoning": all RC cooling, eTRV bias,
  psychrometric, ventilation rebound, and hydraulic balance math runs deterministically in
  `ThermalPhysicsEngine`; the agent interprets tool outputs and updates the Home Assistant
  advisor dashboard.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from strands import Agent
from strands.tools.executors import SequentialToolExecutor
from strands.types.tools import AgentTool

from src.agents.factory import (
    AgentFactory,
    SafetyAndApprovalGate,
    SubAgentBuildContext,
    create_stateless_sub_agent,
    register_sub_agent,
)
from src.core.config import AppConfig, SubAgentSettings
from src.tools.thermal_tools import ThermalTwinService, build_thermal_tools

_THERMAL_BASE_SYSTEM_PROMPT = """
You are the Spatial Thermal Digital Twin Specialist Sub-Agent in a hierarchical orchestrator.

### Mandatory Operating Rules
1. **Python Does the Physics, LLM Does the Reasoning**:
   - NEVER compute differential equations, Newton cooling curves, psychrometric conversions, or
     estimated room temperatures by hand or from raw guesswork.
   - ALWAYS call `get_thermal_twin_snapshot`, `analyze_zone_history`, or
     `simulate_thermal_scenario` first and ground every quantitative statement in the returned
     JSON fields.
2. **Frost & Window-Open Protection Semantics**:
   - When `frost_protection_active` is `true`, `window_open` is `true`, or `target_setpoint_c` is
     at or below 8.0 °C, NEVER classify low temperature or idle heating as a radiator/boiler
     malfunction — that is intentional window-open frost protection.
3. **Clear Diagnostic Explanations**:
   - Respond in the user's language (Turkish by default for Turkish prompts) and explain:
     - **Radiator eTRV Bias**: contrast `raw_etrv_temp_c` vs. `true_ambient_temp_c` (plus
       `spatial_gradient_c`, `valve_position_pct`, `etrv_bias_factor`, `static_offset_c`).
     - **Envelope RC Constants**: interpret `k_loss` (1/h) and `tau_hours` (thermal time constant)
       from nighttime cooling curves.
     - **Moisture & Dew-Point Health**: explain `rel_humidity_pct`, `absolute_humidity_g_m3`,
       `dew_point_c`, `enthalpy_kj_kg`, and `mold_risk` (`low`/`moderate`/`high`).
     - **Shock Ventilation & Rebound**: cite `optimal_ventilation_min`, observed `knee_point_min`,
       and post-closure `rebound_temp_c` / `rebound_delta_c`.
     - **Overnight Projections & Hydraulic Balancing**: summarize `projected_min_temp_c`,
       `estimated_warmup_min`, and `hydraulic_balance` classifications (`fast_heating`,
       `balanced`, `slow_heating`, `stalled`) with `recommended_max_valve_pct` when applicable.
4. **Home Assistant Dashboard Publishing**:
   - When asked to generate a morning briefing, anomaly alert, or dashboard report, ALWAYS call
     `publish_thermal_advisor_report(summary_state=..., report_markdown=...)` with a concise
     status string and a well-structured Markdown report.
""".strip()


@register_sub_agent("thermal")
def build_thermal_sub_agent(settings: SubAgentSettings, ctx: SubAgentBuildContext) -> AgentTool:
    """Build the stateless Thermal Digital Twin sub-agent and expose it via `.as_tool()`."""
    service = ctx.thermal_service or ThermalTwinService(
        config=ctx.config,
        ha_adapter=ctx.ha_adapter,
        entity_resolver=ctx.entity_resolver,
    )
    tools: list[Any] = list(build_thermal_tools(service))
    return create_stateless_sub_agent(
        settings=settings,
        ctx=ctx,
        base_system_prompt=_THERMAL_BASE_SYSTEM_PROMPT,
        tools=tools,
        state_verifier=None,
        mutating_tools=[],
    )


def build_standalone_thermal_advisor_runner(
    config: AppConfig,
    factory: AgentFactory,
    service: ThermalTwinService,
) -> Callable[[str], Awaitable[str]]:
    """Create an async advisor callback that synthesizes thermal JSON and updates HA dashboard."""
    sub_cfg = config.agents.sub_agents.get("thermal")
    agent_name = sub_cfg.name if sub_cfg is not None else "ThermalSubAgent"
    model_id = sub_cfg.model_id if sub_cfg is not None else None
    temperature = sub_cfg.temperature if sub_cfg is not None else None

    prompt = _THERMAL_BASE_SYSTEM_PROMPT
    if sub_cfg is not None and sub_cfg.extra_instructions.strip():
        prompt += f"\n\n## Site-Specific Instructions\n{sub_cfg.extra_instructions.strip()}\n"

    tools: list[Any] = list(build_thermal_tools(service))

    async def _run_advisor(briefing_json: str) -> str:
        advisor_max_tokens = max(config.llm.max_output_tokens or 4096, 8192)
        advisor_agent = Agent(
            name=agent_name,
            model=factory.build_model(
                model_id,
                temperature,
                max_output_tokens=advisor_max_tokens,
            ),
            system_prompt=prompt,
            tools=tools,
            interventions=[SafetyAndApprovalGate(agent_name=agent_name, config=config)],
            retry_strategy=factory.build_retry_strategy(),
            tool_executor=SequentialToolExecutor(),
            callback_handler=None,
        )
        instruction = (
            "Aşağıdaki yapılandırılmış termal ikiz JSON verisini yorumla; gerçek ortam "
            "sıcaklıklarını, eTRV sapmalarını, soğuma sabitlerini (k_loss, tau_hours), "
            "nem/çiy noktası/küf riskini, şok havalandırma bulgularını, gece "
            "projeksiyonlarını, max vana / hidrolik denge önerilerini ve anomalileri özetleyen "
            "KISA ve yüksek sinyalli bir Türkçe Markdown raporu hazırla (en fazla ~400 kelime, "
            "kompakt özet tablo + kısa madde işaretleri). Ardından mutlaka "
            "`publish_thermal_advisor_report` aracını çağırarak Home Assistant panosunu güncelle. "
            "Araç çağrısı tamamlandıktan sonra tüm raporu cevabında tekrar etme; yalnızca tek "
            "cümlelik kısa bir onay yaz.\n\n"
            f"{briefing_json}"
        )
        result = await advisor_agent.invoke_async(instruction)
        return str(result).strip()

    return _run_advisor
