"""End-to-end verification tests for homelab-agent-mesh v1.0."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from strands.models.model import Model

from src.agents.factory import AgentFactory
from src.core.config import AppConfig
from src.core.exceptions import (
    MemoryPoisoningError,
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
from src.memory.reflection_store import (
    LocalHashedNgramEmbedder,
    ReflectionStore,
    create_record_lesson_tool,
)
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

    async def structured_output(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError
        yield {}

    async def stream(self, messages: Any, tool_specs: Any = None, system_prompt: Any = None, **kwargs: Any) -> Any:
        step = self._steps.pop(0)
        yield {"messageStart": {"role": "assistant"}}
        if isinstance(step, str):
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": step}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        else:
            tool_calls = step if isinstance(step, list) else [step]
            for name, args, tid in tool_calls:
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


@pytest.mark.asyncio
async def test_reflection_store_anti_poisoning_and_repetition_threshold(tmp_path: Path) -> None:
    embedder = LocalHashedNgramEmbedder(dimensions=128)
    store = ReflectionStore(
        db_path=tmp_path / "hardened_mem.db",
        embedder=embedder,
        similarity_threshold=0.35,
        local_fallback_dimensions=128,
    )
    await store.initialize()

    # 1. Transient network / timeout / circuit-breaker errors must never poison reflection store
    poison_rules = [
        "TimeoutError: device unreachable, do not try again",
        "ConnectionError on Proxmox node, bir daha deneme",
        "Bu cihaza erişilemiyor, bir daha deneme",
        "Circuit open on home_assistant backend",
    ]
    for bad_rule in poison_rules:
        with pytest.raises(MemoryPoisoningError):
            await store.record_lesson("infra", bad_rule)

    # 2. Agent tool enforces repetition threshold (agent_min_occurrences=2)
    record_tool: Any = create_record_lesson_tool(
        store=store,
        allowed_domains=["ha", "infra"],
        allow_core_invariants=False,
        agent_min_occurrences=2,
    )
    with pytest.raises(MemoryPoisoningError):
        await record_tool(
            domain="ha",
            rule_content="Zaman aşımı nedeniyle lambaya erişilemiyor, bir daha deneme",
        )

    first = await record_tool(
        domain="ha",
        rule_content="Use media_player.living_room_tv for living room TV commands",
    )
    assert first["created"] is True
    assert first["occurrence_count"] == 1
    assert first["active"] is False

    # Staged rule (1/2) must NOT yet be injected into sub-agent prompts
    assert await store.get_relevant_rules("ha", "turn on the living room tv") == []

    # 2nd observation (even with slightly rephrased wording) activates the lesson
    second = await record_tool(
        domain="ha",
        rule_content="For living room TV commands use media_player.living_room_tv",
    )
    assert second["created"] is False
    assert second["occurrence_count"] == 2
    assert second["active"] is True

    active_rules = await store.get_relevant_rules("ha", "turn on the living room tv")
    assert any("media_player.living_room_tv" in r for r in active_rules)


@pytest.mark.asyncio
async def test_infra_toolkit_parallel_docker_stats(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INFRA__DOCKER_STATS_CONCURRENCY", "4")
    cfg = _load_test_config(tmp_path, monkeypatch)

    class SlowStatsContainer:
        def __init__(self, name: str) -> None:
            self.name = name
            self.short_id = name[:7]
            self.status = "running"

        def stats(self, stream: bool = False) -> dict[str, Any]:
            time.sleep(0.12)
            return {
                "cpu_stats": {"cpu_usage": {"total_usage": 200}, "system_cpu_usage": 1000, "online_cpus": 2},
                "precpu_stats": {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 500},
                "memory_stats": {"usage": 64 * 1024 * 1024, "limit": 512 * 1024 * 1024},
            }

    class FakeDockerClient:
        class containers:
            @staticmethod
            def list(all: bool = False) -> list[SlowStatsContainer]:
                return [SlowStatsContainer(f"worker-{i}") for i in range(4)]

    toolkit = HomelabInfraToolkit(cfg, docker_client=FakeDockerClient())
    start = time.monotonic()
    listed = await toolkit.list_docker_containers()
    elapsed = time.monotonic() - start

    assert len(listed) == 4
    assert all(item["cpu_percent"] == 40.0 for item in listed)
    # 4 * 0.12s = 0.48s sequential; parallel with concurrency=4 completes near ~0.12s
    assert elapsed < 0.35


def _parse_sse_events(response_text: str) -> list[dict[str, Any]]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response_text.splitlines()
        if line.startswith("data: ")
    ]


def test_multi_interrupt_sequential_and_batch_resumption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # --- Part A: Sequential Multi-Interrupt (1 prompt -> 2 destructive operations) ---
    monkeypatch.setenv("PROXMOX_API_URL", "https://192.0.2.20:8006")
    monkeypatch.setenv("PROXMOX_TOKEN_ID", "root@pam!test")
    monkeypatch.setenv("PROXMOX_TOKEN_SECRET", "test-secret")
    cfg = _load_test_config(tmp_path / "seq", monkeypatch)

    executed_actions: list[str] = []

    class FakeDockerContainer:
        name = "immich"
        short_id = "imm1234"
        status = "running"

        def restart(self, timeout: int = 10) -> None:
            executed_actions.append("docker_restart:immich")
            self.status = "running"

        def reload(self) -> None:
            pass

    class FakeDockerClient:
        class containers:
            @staticmethod
            def get(name: str) -> FakeDockerContainer:
                return FakeDockerContainer()

    def proxmox_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api2/json/nodes/pve1/lxc/102/status/reboot":
            executed_actions.append("proxmox_reboot:102")
            return httpx.Response(200, json={"data": "UPID:pve1:reboot:102"})
        return httpx.Response(404, json={"data": None})

    store = ReflectionStore(
        db_path=tmp_path / "seq" / "mem.db",
        embedder=LocalHashedNgramEmbedder(dimensions=128),
        similarity_threshold=0.5,
        local_fallback_dimensions=128,
    )
    ha_adapter = HomeAssistantWSAdapter(cfg)
    infra_toolkit = HomelabInfraToolkit(
        cfg,
        docker_client=FakeDockerClient(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(proxmox_handler)),
    )

    seq_scripts = {
        "HASubAgent": ScriptedModel(["noop"]),
        "InfraSubAgent": ScriptedModel(
            [
                ("manage_container", {"container_name": "immich", "action": "restart"}, "sub-seq-1"),
                ("manage_proxmox_guest", {"node": "pve1", "vmid": 102, "guest_type": "lxc", "action": "reboot"}, "sub-seq-2"),
                "Immich restarted and LXC 102 rebooted.",
            ]
        ),
        "Supervisor": ScriptedModel(
            [
                ("infrastructure_agent", {"input": "restart immich and reboot LXC 102"}, "sup-seq-1"),
                "Both Immich and LXC 102 were restarted after your approvals.",
            ]
        ),
    }
    seq_order = ["HASubAgent", "InfraSubAgent", "Supervisor"]
    seq_factory = AgentFactory(
        config=cfg,
        memory_store=store,
        ha_adapter=ha_adapter,
        infra_toolkit=infra_toolkit,
        custom_model_factory=lambda *_: seq_scripts[seq_order.pop(0)],
    )
    seq_app = create_app(
        config=cfg,
        memory_store=store,
        ha_adapter=ha_adapter,
        infra_toolkit=infra_toolkit,
        factory=seq_factory,
    )

    with TestClient(seq_app) as client:
        r1 = client.post(
            "/api/v1/chat/stream",
            json={"prompt": "Immich konteynerini yeniden başlat ve LXC 102'yi reboot et"},
        )
        assert r1.status_code == 200
        ev1 = _parse_sse_events(r1.text)
        intr1 = next(e for e in ev1 if e["type"] == "intervention_required")
        assert intr1["operation"] == "restart_container"
        assert executed_actions == []

        # Approve first step -> executes docker restart, then pauses at LXC 102 reboot
        r2 = client.post(
            "/api/v1/interventions/approve",
            json={"approval_id": intr1["approval_id"], "approved": True},
        )
        assert r2.status_code == 200
        ev2 = _parse_sse_events(r2.text)
        intr2 = next(e for e in ev2 if e["type"] == "intervention_required")
        done2 = next(e for e in ev2 if e["type"] == "done")
        assert intr2["operation"] == "reboot_node"
        assert done2["stop_reason"] == "interrupt"
        assert done2["pending_approvals"] == [intr2["approval_id"]]
        assert executed_actions == ["docker_restart:immich"]

        # Approve second step -> executes Proxmox LXC reboot and finishes turn
        r3 = client.post(
            "/api/v1/interventions/approve",
            json={"approval_id": intr2["approval_id"], "approved": True},
        )
        assert r3.status_code == 200
        ev3 = _parse_sse_events(r3.text)
        done3 = next(e for e in ev3 if e["type"] == "done")
        assert done3["stop_reason"] == "end_turn"
        assert done3["pending_approvals"] == []
        assert executed_actions == ["docker_restart:immich", "proxmox_reboot:102"]

    # --- Part B: Simultaneous Batch Interrupts (2 approvals in 1 turn) ---
    monkeypatch.setenv("AGENTS__PARALLEL_TOOL_EXECUTION", "true")
    monkeypatch.setenv(
        "SAFETY__DESTRUCTIVE_TOOLS_REQUIRING_APPROVAL",
        '["restart_container","get_ha_entity_state"]',
    )
    batch_cfg = _load_test_config(tmp_path / "batch", monkeypatch)
    batch_actions: list[str] = []

    class BatchDockerContainer:
        name = "immich"
        short_id = "imm1234"
        status = "running"

        def restart(self, timeout: int = 10) -> None:
            batch_actions.append("docker_restart:immich")

        def reload(self) -> None:
            pass

    class BatchDockerClient:
        class containers:
            @staticmethod
            def get(name: str) -> BatchDockerContainer:
                return BatchDockerContainer()

    def ha_handler(request: httpx.Request) -> httpx.Response:
        batch_actions.append("ha_get_state")
        return httpx.Response(200, json={"entity_id": "light.dining_room_main", "state": "on", "attributes": {}})

    batch_store = ReflectionStore(
        db_path=tmp_path / "batch" / "mem.db",
        embedder=LocalHashedNgramEmbedder(dimensions=128),
        similarity_threshold=0.5,
        local_fallback_dimensions=128,
    )
    batch_ha = HomeAssistantWSAdapter(
        batch_cfg,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(ha_handler)),
    )
    batch_infra = HomelabInfraToolkit(batch_cfg, docker_client=BatchDockerClient())
    batch_scripts = {
        "HASubAgent": ScriptedModel(
            [
                ("get_ha_entity_state", {"entity_id": "light.dining_room_main"}, "ha-batch-1"),
                "light state checked.",
            ]
        ),
        "InfraSubAgent": ScriptedModel(
            [
                ("manage_container", {"container_name": "immich", "action": "restart"}, "infra-batch-1"),
                "immich restarted.",
            ]
        ),
        "Supervisor": ScriptedModel(
            [
                [
                    ("home_assistant_agent", {"input": "check dining light"}, "sup-b-1"),
                    ("infrastructure_agent", {"input": "restart immich"}, "sup-b-2"),
                ],
                "Both parallel approvals completed.",
            ]
        ),
    }
    batch_order = ["HASubAgent", "InfraSubAgent", "Supervisor"]
    batch_factory = AgentFactory(
        config=batch_cfg,
        memory_store=batch_store,
        ha_adapter=batch_ha,
        infra_toolkit=batch_infra,
        custom_model_factory=lambda *_: batch_scripts[batch_order.pop(0)],
    )
    batch_app = create_app(
        config=batch_cfg,
        memory_store=batch_store,
        ha_adapter=batch_ha,
        infra_toolkit=batch_infra,
        factory=batch_factory,
    )

    with TestClient(batch_app) as client:
        br1 = client.post("/api/v1/chat/stream", json={"prompt": "Check light and restart immich"})
        assert br1.status_code == 200
        bev1 = _parse_sse_events(br1.text)
        interventions = [e for e in bev1 if e["type"] == "intervention_required"]
        assert len(interventions) == 2
        bdone1 = next(e for e in bev1 if e["type"] == "done")
        assert bdone1["stop_reason"] == "interrupt"
        assert len(bdone1["pending_approvals"]) == 2
        assert batch_actions == []

        first_id = interventions[0]["approval_id"]
        second_id = interventions[1]["approval_id"]

        # Resolving only 1st approval must NOT resume early; 2nd must remain pending
        br2 = client.post(
            "/api/v1/interventions/approve",
            json={"approval_id": first_id, "approved": True},
        )
        assert br2.status_code == 200
        bev2 = _parse_sse_events(br2.text)
        bdone2 = next(e for e in bev2 if e["type"] == "done")
        assert bdone2["stop_reason"] == "interrupt"
        assert bdone2["pending_approvals"] == [second_id]
        assert batch_actions == []

        # Resolving 2nd approval executes both pending tools and completes turn
        br3 = client.post(
            "/api/v1/interventions/approve",
            json={"approval_id": second_id, "approved": True},
        )
        assert br3.status_code == 200
        bev3 = _parse_sse_events(br3.text)
        bdone3 = next(e for e in bev3 if e["type"] == "done")
        assert bdone3["stop_reason"] == "end_turn"
        assert bdone3["pending_approvals"] == []
        assert set(batch_actions) == {"ha_get_state", "docker_restart:immich"}

