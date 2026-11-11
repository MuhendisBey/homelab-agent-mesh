"""End-to-end verification tests for homelab-agent-mesh v1.0."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from strands.models.model import Model

from src.agents.factory import AgentFactory
from src.core.config import AppConfig
from src.core.exceptions import (
    ProtectedResourceViolationError,
    SafetyBoundViolationError,
    WindowOpenProtectionError,
)
from src.core.guards import GuardRails, HvacSetpointGuard, ServiceCallIntent
from src.core.interfaces import (
    BaseStateVerifier,
    EntityState,
    StateAssertionFailure,
    StateAssertionResult,
    StateAssertionSuccess,
)
from src.main import create_app
from src.memory.reflection_store import LocalHashedNgramEmbedder, ReflectionStore
from src.tools.ha_ws_adapter import HomeAssistantWSAdapter
from src.tools.infra_mcp_server import HomelabInfraToolkit


class FakeStateVerifier(BaseStateVerifier):
    def __init__(self, states: dict[str, EntityState]) -> None:
        self.states = states

    async def get_state(self, entity_id: str) -> EntityState | None:
        return self.states.get(entity_id)

    async def assert_state(
        self,
        entity_id: str,
        expected_state: str | None,
        timeout_sec: float,
        poll_interval_ms: int,
        expected_attributes: Any = None,
    ) -> StateAssertionResult:
        st = self.states.get(entity_id)
        if st and (expected_state is None or st.state == expected_state):
            return StateAssertionSuccess(
                entity_id=entity_id,
                expected_state=expected_state,
                actual_state=st.state,
                elapsed_ms=5,
                method="polling",
            )
        return StateAssertionFailure(
            entity_id=entity_id,
            expected_state=expected_state,
            actual_state=st.state if st else None,
            elapsed_ms=5,
            method="polling",
            reason="mismatch",
        )


def _load_test_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppConfig:
    repo_root = Path(__file__).resolve().parents[1]
    example_yaml = repo_root / "config" / "settings.example.yaml"
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("HA_BASE_URL", "http://192.0.2.10:8123")
    monkeypatch.setenv("HA_WS_URL", "ws://192.0.2.10:8123/api/websocket")
    monkeypatch.setenv("HA_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("HA_MCP_SSE_URL", "")
    monkeypatch.setenv("APP_HOST", "127.0.0.1")
    monkeypatch.setenv("APP_PORT", "8080")
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "reflections.db"))
    monkeypatch.setenv("YAML_CONFIG_PATH", str(example_yaml))
    monkeypatch.setenv("MEMORY__EMBEDDING_BACKEND", "local")
    monkeypatch.setenv("HOME_ASSISTANT__MCP__ENABLED", "false")
    return AppConfig()  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_reflection_store_three_tier_retrieval(tmp_path: Path) -> None:
    embedder = LocalHashedNgramEmbedder(dimensions=128)
    store = ReflectionStore(
        db_path=tmp_path / "test_mem.db",
        embedder=embedder,
        similarity_threshold=0.35,
        local_fallback_dimensions=128,
    )
    await store.initialize()

    # Core invariant in 'ha', semantic rule in 'ha', and core invariant in 'infra'
    await store.record_lesson("ha", "Never override 7C frost protection when window is open.", is_core_invariant=True)
    await store.record_lesson("ha", "Use media_player.living_room_tv for living room TV commands.", is_core_invariant=False)
    await store.record_lesson("infra", "Pause ML worker container during high CPU spikes.", is_core_invariant=True)

    ha_rules = await store.get_relevant_rules("ha", "turn on the living room tv")
    # Tier 1: 'infra' rule must NOT leak into 'ha'
    assert all("ML worker" not in r for r in ha_rules)
    # Tier 2: Core invariant MUST always be returned even for an unrelated TV query
    assert any(r.startswith("[CORE INVARIANT] Never override 7C") for r in ha_rules)
    # Tier 3: Semantic match on living room TV must be returned
    assert any("media_player.living_room_tv" in r for r in ha_rules)


@pytest.mark.asyncio
async def test_hvac_and_protected_container_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _load_test_config(tmp_path, monkeypatch)
    guards = GuardRails(cfg.safety)

    # Protected container must raise ProtectedResourceViolationError
    with pytest.raises(ProtectedResourceViolationError):
        guards.ensure_container_action_allowed("network-vpn", "restart")

    # Non-protected container is allowed
    guards.ensure_container_action_allowed("photo-ml-worker", "pause")

    # HVAC bounds & window-open protection
    verifier = FakeStateVerifier(
        {
            "climate.main_thermostat": EntityState(
                entity_id="climate.main_thermostat",
                state="heat",
                attributes={"temperature": 7.0},  # <= window_open_guard_temp (8.0)
            ),
            "binary_sensor.living_room_window_contact": EntityState(
                entity_id="binary_sensor.living_room_window_contact",
                state="off",
                attributes={},
            ),
        }
    )
    hvac_guard = HvacSetpointGuard(cfg.safety, state_reader=verifier)

    # Out-of-bounds setpoint (35C > max 28C)
    with pytest.raises(SafetyBoundViolationError):
        await hvac_guard.validate_intent(
            ServiceCallIntent(
                domain="climate",
                service="set_temperature",
                entity_ids=("climate.main_thermostat",),
                data={"temperature": 35.0},
            )
        )

    # Window-open guard triggered because current setpoint is 7.0C (<= 8.0C)
    with pytest.raises(WindowOpenProtectionError):
        await hvac_guard.validate_intent(
            ServiceCallIntent(
                domain="climate",
                service="set_temperature",
                entity_ids=("climate.main_thermostat",),
                data={"temperature": 21.5},
            )
        )


class ScriptedModel(Model):
    """Deterministic Strands Model for testing Supervisor -> SubAgent -> HITL flows."""

    def __init__(self, steps: list[Any]) -> None:
        self._steps = list(steps)
        self._cfg: dict[str, Any] = {"model_id": "scripted"}

    def update_config(self, **model_config: Any) -> None:
        self._cfg.update(model_config)

    def get_config(self) -> Any:
        return self._cfg

    async def structured_output(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        raise NotImplementedError
        yield {}

    async def stream(self, messages: Any, tool_specs: Any = None, system_prompt: Any = None, **kwargs: Any):  # type: ignore[override]
        step = self._steps.pop(0)
        yield {"messageStart": {"role": "assistant"}}
        if isinstance(step, str):
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": step}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        else:
            name, args, tid = step
            yield {"contentBlockStart": {"start": {"toolUse": {"name": name, "toolUseId": tid}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(args)}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}


def test_sse_chat_and_two_phase_commit_intervention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _load_test_config(tmp_path, monkeypatch)

    class FakeDockerContainer:
        name = "photo-ml"
        short_id = "abc1234"
        status = "running"

        def restart(self, timeout: int = 10) -> None:
            self.status = "running"

        def reload(self) -> None:
            pass

    class FakeDockerClient:
        class containers:
            @staticmethod
            def get(name: str) -> FakeDockerContainer:
                return FakeDockerContainer()

    embedder = LocalHashedNgramEmbedder(dimensions=128)
    store = ReflectionStore(
        db_path=tmp_path / "api_mem.db",
        embedder=embedder,
        similarity_threshold=0.5,
        local_fallback_dimensions=128,
    )
    ha_adapter = HomeAssistantWSAdapter(cfg)
    infra_toolkit = HomelabInfraToolkit(cfg, docker_client=FakeDockerClient())

    # Model scripts: Supervisor calls infrastructure_agent -> InfraSubAgent calls manage_container(restart)
    scripts = {
        "Supervisor": ScriptedModel(
            [
                ("infrastructure_agent", {"input": "restart photo-ml container"}, "sup-call-1"),
                "Container photo-ml was restarted after your approval.",
            ]
        ),
        "InfraSubAgent": ScriptedModel(
            [
                ("manage_container", {"container_name": "photo-ml", "action": "restart"}, "sub-call-1"),
                "photo-ml restarted and verified running.",
            ]
        ),
        "HASubAgent": ScriptedModel(["noop"]),
    }
    call_order = ["Supervisor", "HASubAgent", "InfraSubAgent"]

    def fake_model_factory(model_id: str | None, temperature: float | None) -> Model:
        role = call_order.pop(0) if call_order else "Supervisor"
        return scripts[role]

    # Note: SupervisorRuntime._build_session builds sub-agents first (ha, infra) then Supervisor
    call_order[:] = ["HASubAgent", "InfraSubAgent", "Supervisor"]

    factory = AgentFactory(
        config=cfg,
        memory_store=store,
        ha_adapter=ha_adapter,
        infra_toolkit=infra_toolkit,
        custom_model_factory=fake_model_factory,
    )
    app = create_app(
        config=cfg,
        memory_store=store,
        ha_adapter=ha_adapter,
        infra_toolkit=infra_toolkit,
        factory=factory,
    )

    with TestClient(app) as client:
        # 1. Initial chat stream should pause with intervention_required
        resp = client.post("/api/v1/chat/stream", json={"prompt": "Restart photo-ml container"})
        assert resp.status_code == 200
        events = [
            json.loads(line.removeprefix("data: "))
            for line in resp.text.splitlines()
            if line.startswith("data: ")
        ]
        event_types = [e["type"] for e in events]
        assert "plan_step" in event_types
        assert "intervention_required" in event_types
        intervention_ev = next(e for e in events if e["type"] == "intervention_required")
        approval_id = intervention_ev["approval_id"]
        assert intervention_ev["operation"] == "restart_container"

        # 2. Approve the intervention and verify completion
        approve_resp = client.post(
            "/api/v1/interventions/approve",
            json={"approval_id": approval_id, "approved": True},
        )
        assert approve_resp.status_code == 200
        resume_events = [
            json.loads(line.removeprefix("data: "))
            for line in approve_resp.text.splitlines()
            if line.startswith("data: ")
        ]
        resume_types = [e["type"] for e in resume_events]
        assert "intervention_resolved" in resume_types
        assert "content_delta" in resume_types
        done_ev = next(e for e in resume_events if e["type"] == "done")
        assert done_ev["stop_reason"] == "end_turn"
        assert done_ev["pending_approvals"] == []

