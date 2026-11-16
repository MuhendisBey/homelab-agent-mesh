"""Dynamic Strands Agent factory, InterventionHandlers, and Actor-Critic lifecycle hooks.

Implements:
- **Open/Closed Sub-Agent Plugin Registry**: new domain sub-agents register via
  `@register_sub_agent("key")` without modifying Supervisor logic.
- **3-Tier Dynamic Reflection Injection**: `DomainMemoryInjector` queries `BaseMemoryStore`
  on `BeforeInvocationEvent` and updates the stateless sub-agent's system prompt before the
  first model turn.
- **Two-Phase Commit (Human-in-the-Loop) & Pre-Condition Guards**: `SafetyAndApprovalGate`
  (`InterventionHandler`) enforces HVAC/window/protected-resource guards and returns
  `Confirm(...)` for tools matching `safety.destructive_tools_requiring_approval`.
- **Actor-Critic Telemetry**: `SubAgentTelemetryHooks` emits `tool_call_start`,
  `tool_call_end`, `state_assertion`, and `critic_verdict` SSE events.
"""

from __future__ import annotations

import importlib
import json
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from google import genai
from strands import Agent, InterventionHandler, ModelRetryStrategy
from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import (
    AfterInvocationEvent,
    AfterToolCallEvent,
    BeforeInvocationEvent,
    BeforeToolCallEvent,
)
from strands.interventions import Confirm, Deny, Proceed
from strands.models.gemini import GeminiModel
from strands.models.model import Model
from strands.tools.executors import ConcurrentToolExecutor, SequentialToolExecutor
from strands.types.tools import AgentTool

from src.core.config import AppConfig, SubAgentSettings
from src.core.entity_resolver import EntityResolver
from src.core.events import (
    CriticVerdictEvent,
    MemoryInjectedEvent,
    StateAssertionEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
)
from src.core.exceptions import HomelabAgentError
from src.core.guards import GuardRails, HvacSetpointGuard, extract_service_call, operation_registry
from src.core.interfaces import BaseStateVerifier, EventEmitter
from src.core.scheduler import ActionScheduler
from src.memory.base import BaseMemoryStore
from src.tools.ha_ws_adapter import HomeAssistantWSAdapter
from src.tools.infra_mcp_server import HomelabInfraToolkit
from src.tools.thermal_tools import ThermalTwinService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sub-Agent Open/Closed Registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubAgentBuildContext:
    """Shared dependencies passed to sub-agent builders (DIP)."""

    config: AppConfig
    memory_store: BaseMemoryStore
    ha_adapter: HomeAssistantWSAdapter
    infra_toolkit: HomelabInfraToolkit
    emitter: EventEmitter
    model_factory: Callable[[str | None, float | None], Model]
    entity_resolver: EntityResolver | None = None
    scheduler: ActionScheduler | None = None
    thermal_service: ThermalTwinService | None = None


SubAgentBuilder = Callable[[SubAgentSettings, SubAgentBuildContext], AgentTool]


@dataclass(frozen=True)
class SubAgentRegistration:
    key: str
    builder: SubAgentBuilder


_SUB_AGENT_REGISTRY: dict[str, SubAgentBuilder] = {}


def register_sub_agent(key: str) -> Callable[[SubAgentBuilder], SubAgentBuilder]:
    """Decorator registering a sub-agent builder under ``config.agents.sub_agents[key]``."""

    def _decorator(fn: SubAgentBuilder) -> SubAgentBuilder:
        _SUB_AGENT_REGISTRY[key] = fn
        return fn

    return _decorator


def get_registered_sub_agents() -> Mapping[str, SubAgentBuilder]:
    return dict(_SUB_AGENT_REGISTRY)


# ---------------------------------------------------------------------------
# Intervention Handler: Guard Clauses + Two-Phase Commit (Confirm) + Budget
# ---------------------------------------------------------------------------


def _evaluate_approval(response: Any) -> bool:
    if response is True:
        return True
    if isinstance(response, Mapping):
        return bool(response.get("approved") is True)
    if isinstance(response, str):
        return response.strip().lower() in {"y", "yes", "true", "approved", "approve"}
    return False


class SafetyAndApprovalGate(InterventionHandler):
    """Enforces pre-condition guards, tool-call budgets, and HITL Two-Phase Commit."""

    name = "safety-and-approval-gate"

    def __init__(
        self,
        agent_name: str,
        config: AppConfig,
        state_verifier: BaseStateVerifier | None = None,
    ) -> None:
        self._agent_name = agent_name
        self._config = config
        self._safety = config.safety
        self._guards = GuardRails(config.safety)
        self._hvac_guard = HvacSetpointGuard(config.safety, state_reader=state_verifier)
        self._call_count = 0

    def before_invocation(self, event: BeforeInvocationEvent, **kwargs: Any) -> Proceed:
        self._call_count = 0
        return Proceed()

    async def before_tool_call(
        self,
        event: BeforeToolCallEvent,
        **kwargs: Any,
    ) -> Proceed | Deny | Confirm:
        tool_name = str(event.tool_use.get("name", ""))
        raw_input = event.tool_use.get("input")
        tool_args: dict[str, Any] = dict(raw_input) if isinstance(raw_input, Mapping) else {"input": raw_input}

        # 1. Circuit-breaker tool budget per task
        self._call_count += 1
        if self._call_count > self._safety.max_tool_calls_per_task:
            return Deny(
                reason=(
                    f"Tool budget exceeded ({self._safety.max_tool_calls_per_task} calls per task). "
                    "Stop retrying and report the current state to the Supervisor."
                )
            )

        # 2. Pre-condition guard clauses (apply to both native and external MCP tools)
        try:
            if tool_name == "manage_container":
                self._guards.ensure_container_action_allowed(
                    container_name=str(tool_args.get("container_name", "")),
                    action=str(tool_args.get("action", "")),
                )
            elif tool_name == "manage_proxmox_guest":
                self._guards.ensure_guest_action_allowed(
                    vmid=int(tool_args.get("vmid", -1)),
                    action=str(tool_args.get("action", "")),
                )
            elif tool_name == "fire_custom_ha_event":
                self._guards.validate_event_type(str(tool_args.get("event_type", "")))
            elif tool_name == "execute_sequential_loop":
                self._guards.validate_loop(
                    iterations=int(tool_args.get("iterations", 0)),
                    delay_ms=int(tool_args.get("delay_ms", 0)),
                )

            intent = extract_service_call(tool_args)
            if intent is not None:
                await self._hvac_guard.validate_intent(intent)
        except HomelabAgentError as exc:
            logger.warning("agent=<%s> tool=<%s> | guard denied: %s", self._agent_name, tool_name, exc.message)
            return Deny(reason=json.dumps(exc.to_dict(), ensure_ascii=False))

        # 3. Two-Phase Commit (Human-in-the-Loop Intervention)
        matched_op = operation_registry.requires_approval(
            tool_name=tool_name,
            args=tool_args,
            destructive=self._safety.destructive_tools_requiring_approval,
        )
        if matched_op is not None:
            reason_str = (
                f"Operation '{matched_op}' via tool '{tool_name}' is classified as destructive "
                "and requires explicit human approval."
            )
            payload: dict[str, Any] = {
                "agent": self._agent_name,
                "tool": tool_name,
                "operation": matched_op,
                "args": tool_args,
                "reason": reason_str,
            }
            return Confirm(
                prompt=json.dumps(payload, ensure_ascii=False),
                reason=reason_str,
                evaluate=_evaluate_approval,
            )

        return Proceed()


# ---------------------------------------------------------------------------
# Sub-Agent Hooks: Dynamic 3-Tier Memory Injection + Actor-Critic Telemetry
# ---------------------------------------------------------------------------


def _extract_latest_user_text(event: BeforeInvocationEvent) -> str:
    messages = event.messages or event.agent.messages
    for msg in reversed(messages or []):
        if msg.get("role") == "user":
            parts: list[str] = []
            for block in msg.get("content", []):
                if isinstance(block, Mapping) and "text" in block:
                    parts.append(str(block["text"]))
            if parts:
                return " ".join(parts)
    return ""


class DomainMemoryAndCriticHooks(HookProvider):
    """Injects 3-Tier Reflection rules before execution and audits StateAssertions after."""

    def __init__(
        self,
        agent_name: str,
        domain: str,
        base_system_prompt: str,
        memory_store: BaseMemoryStore,
        emitter: EventEmitter,
        mutating_tools: Iterable[str] = (),
    ) -> None:
        self._agent_name = agent_name
        self._domain = domain
        self._base_prompt = base_system_prompt
        self._memory_store = memory_store
        self._emitter = emitter
        self._mutating_tools = set(mutating_tools)
        self._mutated_entities: list[str] = []
        self._verified_count = 0
        self._failed_count = 0
        self._asserted_entities: set[str] = set()

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self.on_before_invocation)
        registry.add_callback(BeforeToolCallEvent, self.on_before_tool_call)
        registry.add_callback(AfterToolCallEvent, self.on_after_tool_call)
        registry.add_callback(AfterInvocationEvent, self.on_after_invocation)

    async def on_before_invocation(self, event: BeforeInvocationEvent) -> None:
        self._mutated_entities.clear()
        self._verified_count = 0
        self._failed_count = 0
        self._asserted_entities.clear()

        query = _extract_latest_user_text(event)
        retrieved = await self._memory_store.retrieve(domain=self._domain, query=query)
        event.agent.system_prompt = self._base_prompt + retrieved.render()
        self._emitter.emit(
            MemoryInjectedEvent(
                agent=self._agent_name,
                domain=self._domain,
                core_rules=len(retrieved.core),
                semantic_rules=len(retrieved.semantic),
                degraded=retrieved.degraded,
            )
        )

    def on_before_tool_call(self, event: BeforeToolCallEvent) -> None:
        tool_name = str(event.tool_use.get("name", ""))
        tool_use_id = str(event.tool_use.get("toolUseId", ""))
        raw_input = event.tool_use.get("input")
        args = dict(raw_input) if isinstance(raw_input, Mapping) else {"input": raw_input}
        self._emitter.emit(
            ToolCallStartEvent(
                agent=self._agent_name,
                tool=tool_name,
                tool_use_id=tool_use_id,
                args=args,
            )
        )

    def on_after_tool_call(self, event: AfterToolCallEvent) -> None:
        tool_name = str(event.tool_use.get("name", ""))
        tool_use_id = str(event.tool_use.get("toolUseId", ""))
        raw_input = event.tool_use.get("input")
        args = dict(raw_input) if isinstance(raw_input, Mapping) else {}

        status_raw = str(event.result.get("status", "success"))
        if event.cancel_message:
            status: Any = "cancelled"
        elif status_raw == "error" or event.exception is not None:
            status = "error"
        else:
            status = "success"

        duration_ms = int(event.duration * 1000) if event.duration is not None else None
        text_payload = ""
        for block in event.result.get("content", []):
            if isinstance(block, Mapping) and "text" in block:
                text_payload = str(block["text"])
                break

        self._emitter.emit(
            ToolCallEndEvent(
                agent=self._agent_name,
                tool=tool_name,
                tool_use_id=tool_use_id,
                status=status,
                duration_ms=duration_ms,
                detail=event.cancel_message or (text_payload[:300] if status == "error" else None),
            )
        )

        if status != "success":
            return

        if tool_name in self._mutating_tools:
            entity_id = str(args.get("entity_id", "")).strip()
            if entity_id:
                self._mutated_entities.append(entity_id)

        if tool_name == "assert_entity_state" and text_payload:
            try:
                parsed = json.loads(text_payload)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, Mapping) and "verified" in parsed:
                verified = bool(parsed.get("verified"))
                entity = str(parsed.get("entity_id") or args.get("entity_id") or "")
                self._asserted_entities.add(entity)
                if verified:
                    self._verified_count += 1
                else:
                    self._failed_count += 1
                self._emitter.emit(
                    StateAssertionEvent(
                        agent=self._agent_name,
                        entity=entity,
                        verified=verified,
                        expected_state=(
                            str(parsed["expected_state"])
                            if parsed.get("expected_state") is not None
                            else None
                        ),
                        actual_state=(
                            str(parsed["actual_state"])
                            if parsed.get("actual_state") is not None
                            else None
                        ),
                        reason=str(parsed["reason"]) if parsed.get("reason") else None,
                    )
                )

    def on_after_invocation(self, event: AfterInvocationEvent) -> None:
        if not self._mutated_entities and self._verified_count == 0 and self._failed_count == 0:
            return
        unverified = [e for e in self._mutated_entities if e not in self._asserted_entities]
        self._emitter.emit(
            CriticVerdictEvent(
                agent=self._agent_name,
                mutations=len(self._mutated_entities),
                verified=self._verified_count,
                failed=self._failed_count,
                unverified_mutations=unverified,
            )
        )


# ---------------------------------------------------------------------------
# Agent Factory
# ---------------------------------------------------------------------------


class AgentFactory:
    """Builds Gemini models, registered specialist sub-agents, and tool executors."""

    def __init__(
        self,
        config: AppConfig,
        memory_store: BaseMemoryStore,
        ha_adapter: HomeAssistantWSAdapter,
        infra_toolkit: HomelabInfraToolkit,
        genai_client: genai.Client | None = None,
        custom_model_factory: Callable[[str | None, float | None], Model] | None = None,
        entity_resolver: EntityResolver | None = None,
        scheduler: ActionScheduler | None = None,
        thermal_service: ThermalTwinService | None = None,
    ) -> None:
        self._config = config
        self._memory_store = memory_store
        self._ha_adapter = ha_adapter
        self._infra_toolkit = infra_toolkit
        self._entity_resolver = entity_resolver
        self._scheduler = scheduler
        self._thermal_service = thermal_service
        self._genai_client = genai_client or genai.Client(
            api_key=config.gemini_api_key.get_secret_value()
        )
        self._custom_model_factory = custom_model_factory
        self._load_configured_plugins()

    def _load_configured_plugins(self) -> None:
        for module_path in self._config.plugins:
            importlib.import_module(module_path)

    def build_model(
        self,
        model_id: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> Model:
        """Create a Strands Model (or test double when injected)."""
        if self._custom_model_factory is not None:
            return self._custom_model_factory(model_id, temperature)
        resolved_model, resolved_temp = self._config.agent_model_params(model_id, temperature)
        params: dict[str, Any] = {"temperature": resolved_temp}
        effective_max_tokens = (
            max_output_tokens
            if max_output_tokens is not None
            else self._config.llm.max_output_tokens
        )
        if effective_max_tokens is not None:
            params["max_output_tokens"] = effective_max_tokens
        return GeminiModel(
            client=self._genai_client,
            model_id=resolved_model,
            params=params,
        )

    def build_retry_strategy(self) -> ModelRetryStrategy:
        return ModelRetryStrategy(
            max_attempts=max(1, self._config.llm.max_retries + 1),
            initial_delay=self._config.llm.retry_initial_delay_sec,
            max_delay=self._config.llm.retry_max_delay_sec,
        )

    def build_tool_executor(self) -> ConcurrentToolExecutor | SequentialToolExecutor:
        if self._config.agents.parallel_tool_execution:
            return ConcurrentToolExecutor()
        return SequentialToolExecutor()

    def build_sub_agent_tools(self, emitter: EventEmitter) -> list[AgentTool]:
        """Instantiate all enabled sub-agents from `config.agents.sub_agents` via `.as_tool()`."""
        # Ensure built-in sub-agents are registered
        import src.agents.ha_agent
        import src.agents.infra_agent  # noqa: F401
        importlib.import_module("src.agents.thermal_agent")

        ctx = SubAgentBuildContext(
            config=self._config,
            memory_store=self._memory_store,
            ha_adapter=self._ha_adapter,
            infra_toolkit=self._infra_toolkit,
            emitter=emitter,
            model_factory=self.build_model,
            entity_resolver=self._entity_resolver,
            scheduler=self._scheduler,
            thermal_service=self._thermal_service,
        )
        tools: list[AgentTool] = []
        for key, sub_cfg in self._config.agents.sub_agents.items():
            if not sub_cfg.enabled:
                continue
            if key == "thermal" and not self._config.thermal.enabled:
                continue
            builder = _SUB_AGENT_REGISTRY.get(key)
            if builder is None:
                raise KeyError(
                    f"Sub-agent key '{key}' in settings.yaml has no registered builder. "
                    f"Registered keys: {sorted(_SUB_AGENT_REGISTRY)}"
                )
            tools.append(builder(sub_cfg, ctx))
        return tools


def create_stateless_sub_agent(
    settings: SubAgentSettings,
    ctx: SubAgentBuildContext,
    base_system_prompt: str,
    tools: list[Any],
    state_verifier: BaseStateVerifier | None = None,
    mutating_tools: list[str] | None = None,
) -> AgentTool:
    """Helper constructing a stateless Strands sub-agent and wrapping it via `.as_tool()`."""
    full_base_prompt = base_system_prompt.strip()
    if settings.extra_instructions.strip():
        full_base_prompt += f"\n\n## Site-Specific Instructions\n{settings.extra_instructions.strip()}\n"

    hooks = DomainMemoryAndCriticHooks(
        agent_name=settings.name,
        domain=settings.domain,
        base_system_prompt=full_base_prompt,
        memory_store=ctx.memory_store,
        emitter=ctx.emitter,
        mutating_tools=mutating_tools or [],
    )
    gate = SafetyAndApprovalGate(
        agent_name=settings.name,
        config=ctx.config,
        state_verifier=state_verifier,
    )
    retry = ModelRetryStrategy(
        max_attempts=max(1, ctx.config.llm.max_retries + 1),
        initial_delay=ctx.config.llm.retry_initial_delay_sec,
        max_delay=ctx.config.llm.retry_max_delay_sec,
    )
    sub_agent = Agent(
        name=settings.name,
        description=settings.description,
        model=ctx.model_factory(settings.model_id, settings.temperature),
        system_prompt=full_base_prompt,
        tools=tools,
        hooks=[hooks],
        interventions=[gate],
        retry_strategy=retry,
        tool_executor=SequentialToolExecutor(),
        callback_handler=None,
    )
    # preserve_context=False enforces stateless execution per task AND persists interrupted
    # turns inside the parent Supervisor so HITL approvals resume cleanly.
    return sub_agent.as_tool(
        name=settings.tool_name,
        description=settings.description,
        preserve_context=False,
    )
