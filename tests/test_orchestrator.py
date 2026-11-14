"""End-to-end verification tests for homelab-agent-mesh v1.0."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from strands.models.model import Model

from src.agents.factory import AgentFactory
from src.core.announcer import Announcer, clean_for_tts, prepare_prompt_context, strip_wake_prefix
from src.core.config import AppConfig
from src.core.entity_resolver import EntityResolver, RegistryEntityRecord
from src.core.events import ScheduleResultEvent
from src.core.exceptions import (
    HomeAssistantError,
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
from src.core.scheduler import ActionScheduler
from src.core.text_tr import infer_domain_hints, normalize_tr, stem_tr_phrase
from src.main import create_app
from src.memory.reflection_store import (
    GeminiEmbedder,
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


def _make_registry_record(
    entity_id: str,
    friendly_name: str,
    area_id: str | None,
    area_name: str | None,
    state: str = "off",
    aliases: tuple[str, ...] = (),
) -> RegistryEntityRecord:
    domain = entity_id.split(".", 1)[0]
    obj_part = entity_id.split(".", 1)[-1].replace("_", " ")
    combined = normalize_tr(f"{friendly_name} {area_name or ''} {obj_part} {' '.join(aliases)}")
    _, search_stems = stem_tr_phrase(combined, strip_stopwords=False)
    _, area_stems = stem_tr_phrase(area_name or "", strip_stopwords=False)
    return RegistryEntityRecord(
        entity_id=entity_id,
        domain=domain,
        friendly_name=friendly_name,
        area_id=area_id,
        area_name=area_name,
        aliases=aliases,
        state=state,
        search_text_norm=combined,
        search_stems=search_stems,
        area_stems=area_stems,
    )


@pytest.mark.asyncio
async def test_turkish_stemmer_and_entity_resolver_grounding_and_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1. Turkish morphology, I/ı casefolding, softening reversal, and domain hints
    assert normalize_tr("Mutfaktaki IŞIKLARI aç") == "mutfaktaki isiklari ac"
    _, mutfak_stems = stem_tr_phrase("Mutfaktaki IŞIKLARI aç")
    assert set(mutfak_stems) == {"mutfak", "isik"}
    assert infer_domain_hints("Mutfaktaki ışıkları aç") == ("light", "switch")

    _, klima_stems = stem_tr_phrase("Oturma odasındaki klimayı çalıştır")
    assert set(klima_stems) == {"oturma", "oda", "klima"}
    assert infer_domain_hints("Oturma odasındaki klimayı") == ("climate",)

    # 2. Grounding Index resolution & ambiguity handling
    cfg = _load_test_config(tmp_path, monkeypatch)
    records = [
        _make_registry_record("switch.mutfak_isik", "Mutfak Işık", "mutfak", "Mutfak"),
        _make_registry_record(
            "sensor.mutfak_isik_guc", "Mutfak Işık Güç", "mutfak", "Mutfak", state="0"
        ),
        _make_registry_record(
            "climate.yatak_odasi", "Yatak Odası Klima", "yatak_odasi", "Yatak Odası", state="off"
        ),
        _make_registry_record("light.salon_tavan", "Salon Tavan Işık", "salon", "Salon"),
        _make_registry_record("light.salon_abajur", "Salon Abajur Işık", "salon", "Salon"),
    ]

    def ha_http_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/states/switch.mutfak_isik":
            return httpx.Response(
                200,
                json={"entity_id": "switch.mutfak_isik", "state": "on", "attributes": {}},
            )
        return httpx.Response(404, json={"message": "Entity not found"})

    resolver = EntityResolver(
        config=cfg,
        db_path=cfg.sqlite_db_path,
        embedder=LocalHashedNgramEmbedder(dimensions=128),
        enable_ws_listener=False,
    )
    resolver.seed_records_for_test(records)

    res_mutfak = await resolver.resolve("mutfaktaki ışıkları")
    assert res_mutfak.resolved is True
    assert res_mutfak.ambiguous is False
    assert res_mutfak.top_entity_id == "switch.mutfak_isik"
    assert res_mutfak.candidates[0].confidence >= 0.85
    assert res_mutfak.elapsed_ms < 50.0

    # Multiple fixtures in Salon -> must detect ambiguity and NOT auto-resolve
    res_salon_amb = await resolver.resolve("salon ışıkları")
    assert res_salon_amb.ambiguous is True
    assert res_salon_amb.resolved is False
    assert {c.entity_id for c in res_salon_amb.candidates[:2]} == {
        "light.salon_tavan",
        "light.salon_abajur",
    }

    res_salon_tavan = await resolver.resolve("salon tavan ışığı")
    assert res_salon_tavan.resolved is True
    assert res_salon_tavan.top_entity_id == "light.salon_tavan"

    # 3. Verified action learning via HA adapter assert_state -> CoALA entity_memory
    ha = HomeAssistantWSAdapter(
        cfg,
        http_client=httpx.AsyncClient(
            base_url=cfg.ha_base_url,
            transport=httpx.MockTransport(ha_http_handler),
        ),
        entity_resolver=resolver,
    )
    try:
        # Resolve once (registers pending phrase), then verify state via assert_state
        await resolver.resolve("yemek tarafı ampulü")
        # Not yet learned before assert_state
        assert await resolver.list_memory(status="active") == []

        # Associate 'mutfak ampulü' -> switch.mutfak_isik and assert physical state
        await resolver.resolve("mutfaktaki ışıkları")
        assertion = await ha.assert_state("switch.mutfak_isik", expected_state="on")
        assert assertion.verified is True

        learned = await resolver.list_memory(status="active")
        assert len(learned) == 1
        assert learned[0].entity_id == "switch.mutfak_isik"
        assert learned[0].source == "verified_action"
        assert learned[0].success_count == 1

        # Exact memory hit must be used on repeat query
        repeat = await resolver.resolve("mutfaktaki ışıkları")
        assert repeat.resolved is True
        assert repeat.candidates[0].match_source == "entity_memory_exact"
        assert ha.resolve_entity_id("mutfaktaki ışıkları") == "switch.mutfak_isik"

        # 4. User correction marks wrong mapping rejected and activates corrected one
        await resolver.record_verified_resolution("salon lambam", "light.salon_abajur")
        await resolver.record_user_correction(
            phrase="salon lambam",
            correct_entity_id="light.salon_tavan",
            wrong_entity_id="light.salon_abajur",
        )
        corrected = await resolver.resolve("salon lambam")
        assert corrected.top_entity_id == "light.salon_tavan"
        rejected = await resolver.list_memory(status="rejected")
        assert any(r.entity_id == "light.salon_abajur" and r.failure_count == 1 for r in rejected)

        # 5. Bidirectional HA event sync: rename migrates memory, remove marks stale
        await resolver.handle_registry_event(
            "entity_registry_updated",
            {
                "action": "update",
                "entity_id": "switch.mutfak_ana_isik",
                "old_entity_id": "switch.mutfak_isik",
            },
        )
        migrated_rows = await resolver.list_memory(status="active")
        assert any(r.entity_id == "switch.mutfak_ana_isik" for r in migrated_rows)
        assert not any(r.entity_id == "switch.mutfak_isik" for r in migrated_rows)

        await resolver.handle_registry_event(
            "entity_registry_updated",
            {"action": "remove", "entity_id": "switch.mutfak_ana_isik"},
        )
        stale_rows = await resolver.list_memory(status="stale")
        assert any(r.entity_id == "switch.mutfak_ana_isik" for r in stale_rows)
    finally:
        await resolver.stop()
        await ha.aclose()


@pytest.mark.asyncio
async def test_action_scheduler_persistence_misfire_and_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _load_test_config(tmp_path, monkeypatch)
    now_holder = [datetime(2026, 11, 13, 12, 0, 0, tzinfo=UTC)]

    def fake_clock() -> datetime:
        return now_holder[0]

    class FakeScheduleHA:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str, dict[str, Any]]] = []
            self.fail_first_assert_for: set[str] = {"climate.bedroom"}
            self.guard_error_for: set[str] = {"climate.protected"}

        def resolve_entity_id(self, entity_or_alias: str) -> str:
            return entity_or_alias

        async def call_service(
            self,
            domain: str,
            service: str,
            entity_id: str,
            service_data: Mapping[str, Any] | None = None,
        ) -> dict[str, Any]:
            if entity_id in self.guard_error_for:
                raise WindowOpenProtectionError("Window open frost guard active.")
            self.calls.append((domain, service, entity_id, dict(service_data or {})))
            return {"status": "dispatched"}

        async def assert_state(
            self,
            entity_id: str,
            expected_state: str | None,
            timeout_sec: float | None = None,
            poll_interval_ms: int | None = None,
            expected_attributes: Mapping[str, Any] | None = None,
        ) -> StateAssertionResult:
            if entity_id in self.fail_first_assert_for:
                self.fail_first_assert_for.remove(entity_id)
                return StateAssertionFailure(
                    entity_id=entity_id,
                    expected_state=expected_state,
                    actual_state="heat",
                    elapsed_ms=5,
                    method="polling",
                    reason="transient lag",
                )
            return StateAssertionSuccess(
                entity_id=entity_id,
                expected_state=expected_state,
                actual_state=expected_state or "ok",
                elapsed_ms=5,
                method="polling",
            )

    backend = FakeScheduleHA()
    db_file = tmp_path / "scheduler.db"
    events_seen: list[Any] = []

    sched1 = ActionScheduler(
        db_path=db_file,
        ha_adapter=backend,
        clock=fake_clock,
        retry_delay_sec=0.0,
    )
    sched1.add_event_listener(lambda ev: events_seen.append(ev))

    job_retry = await sched1.schedule_action(
        domain="climate",
        service="set_hvac_mode",
        entity_id="climate.bedroom",
        delay_seconds=30,
        service_data={"hvac_mode": "off"},
        description="Turn off bedroom AC after 30s",
    )
    assert job_retry.expected_state == "off"
    assert job_retry.status == "pending"

    # Simulate crash while job was 'running' before restart
    with sqlite3.connect(str(db_file)) as conn:
        conn.execute(
            "UPDATE scheduled_actions SET status = 'running' WHERE schedule_id = ?",
            (job_retry.schedule_id,),
        )

    # Restart recovery in a new ActionScheduler instance on the same SQLite DB
    sched2 = ActionScheduler(
        db_path=db_file,
        ha_adapter=backend,
        clock=fake_clock,
        retry_delay_sec=0.0,
    )
    sched2.add_event_listener(lambda ev: events_seen.append(ev))
    await sched2.start()
    try:
        recovered = await sched2.get_action(job_retry.schedule_id)
        assert recovered.status == "pending"

        # Advance fake clock past 30s and execute -> 1st assert fails, automatic retry succeeds!
        now_holder[0] += timedelta(seconds=35)
        await sched2.run_due_once()
        finished = await sched2.get_action(job_retry.schedule_id)
        assert finished.status == "done"
        assert finished.attempts == 2
        assert len([c for c in backend.calls if c[2] == "climate.bedroom"]) == 2

        # Safe-direction misfire policy:
        # Overdue turn_off (600s late > 300s grace) MUST still run
        overdue_off = await sched2.schedule_action(
            domain="switch",
            service="turn_off",
            entity_id="switch.mutfak_isik",
            run_at_utc=now_holder[0] - timedelta(seconds=600),
            misfire_policy="safe_direction",
            misfire_grace_sec=300,
        )
        # Overdue turn_on (600s late > 300s grace) MUST be skipped safely
        overdue_on = await sched2.schedule_action(
            domain="switch",
            service="turn_on",
            entity_id="switch.salon_heater",
            run_at_utc=now_holder[0] - timedelta(seconds=600),
            misfire_policy="safe_direction",
            misfire_grace_sec=300,
        )
        # GuardViolationError must fail immediately WITHOUT retry (attempts == 1)
        guarded_job = await sched2.schedule_action(
            domain="climate",
            service="set_temperature",
            entity_id="climate.protected",
            delay_seconds=1,
            service_data={"temperature": 22.0},
        )
        now_holder[0] += timedelta(seconds=2)
        await sched2.run_due_once()

        assert (await sched2.get_action(overdue_off.schedule_id)).status == "done"
        skipped_on = await sched2.get_action(overdue_on.schedule_id)
        assert skipped_on.status == "skipped"
        assert not any(c[2] == "switch.salon_heater" for c in backend.calls)

        failed_guard = await sched2.get_action(guarded_job.schedule_id)
        assert failed_guard.status == "failed"
        assert failed_guard.attempts == 1
        assert failed_guard.result is not None
        assert failed_guard.result.get("error") == "window_open_protection"
    finally:
        await sched2.stop()

    assert any(getattr(ev, "type", None) == "schedule_created" for ev in events_seen)
    assert any(getattr(ev, "type", None) == "schedule_result" and ev.status == "done" for ev in events_seen)

    # Verify FastAPI REST endpoints for entities & schedules
    api_resolver = EntityResolver(
        config=cfg,
        db_path=tmp_path / "api_mem.db",
        embedder=LocalHashedNgramEmbedder(dimensions=128),
        enable_ws_listener=False,
    )
    api_resolver.seed_records_for_test(
        [_make_registry_record("switch.mutfak_isik", "Mutfak Işık", "mutfak", "Mutfak")]
    )
    api_ha = HomeAssistantWSAdapter(cfg, entity_resolver=api_resolver)
    api_app = create_app(config=cfg, ha_adapter=api_ha, entity_resolver=api_resolver)
    with TestClient(api_app) as client:
        mem_resp = client.post(
            "/api/v1/entities/memory",
            json={"phrase": "akşam lambası", "entity_id": "switch.mutfak_isik"},
        )
        assert mem_resp.status_code == 201

        res_resp = client.get("/api/v1/entities/resolve", params={"q": "akşam lambası"})
        assert res_resp.status_code == 200
        res_body = res_resp.json()
        assert res_body["resolved"] is True
        assert res_body["top_entity_id"] == "switch.mutfak_isik"

        sch_resp = client.post(
            "/api/v1/schedules",
            json={
                "domain": "switch",
                "service": "turn_off",
                "entity_id": "switch.mutfak_isik",
                "delay_seconds": 120,
                "description": "API timer test",
            },
        )
        assert sch_resp.status_code == 201
        sid = sch_resp.json()["schedule_id"]

        list_resp = client.get("/api/v1/schedules", params={"status": "pending"})
        assert list_resp.status_code == 200
        assert any(item["schedule_id"] == sid for item in list_resp.json())

        del_resp = client.delete(f"/api/v1/schedules/{sid}")
        assert del_resp.status_code == 200
        assert del_resp.json()["status"] == "cancelled"

        # Cancelling again returns 409; unknown id returns 404
        assert client.delete(f"/api/v1/schedules/{sid}").status_code == 409
        assert client.delete("/api/v1/schedules/nonexistent").status_code == 404


class DelayedScriptedModel(ScriptedModel):
    """Scripted model double that sleeps before yielding tokens to test timeout handoff."""

    def __init__(self, steps: list[Any], delay_sec: float = 0.0) -> None:
        super().__init__(steps)
        self._delay_sec = delay_sec

    async def stream(
        self,
        messages: Any,
        tool_specs: Any = None,
        system_prompt: Any = None,
        **kwargs: Any,
    ) -> Any:
        if self._delay_sec > 0:
            await anyio.sleep(self._delay_sec)
        async for chunk in super().stream(
            messages=messages,
            tool_specs=tool_specs,
            system_prompt=system_prompt,
            **kwargs,
        ):
            yield chunk


@pytest.mark.asyncio
async def test_voice_gateway_fast_completion_timeout_handoff_and_announcer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _load_test_config(tmp_path, monkeypatch)

    # 1. Wake-word stripping (Turkish I/i safe) & Markdown cleanup
    assert strip_wake_prefix("Kezban, mutfak ışıklarını aç", cfg.voice.wake_words) == (
        "mutfak ışıklarını aç",
        True,
    )
    assert strip_wake_prefix("Hey Kezban: klimayı kapat", cfg.voice.wake_words) == (
        "klimayı kapat",
        True,
    )
    assert strip_wake_prefix("HEY KEZBAN! yatak odası sıcaklığı kaç?", cfg.voice.wake_words) == (
        "yatak odası sıcaklığı kaç?",
        True,
    )
    assert strip_wake_prefix("İyi akşamlar, ışıkları kapat", cfg.voice.wake_words) == (
        "İyi akşamlar, ışıkları kapat",
        False,
    )
    assert strip_wake_prefix("Kezban", cfg.voice.wake_words) == ("Kezban", True)

    assert clean_for_tts("## Sonuç\n- **Mutfak Işığı** açıldı (`switch.mutfak_isik`).") == (
        "Sonuç Mutfak Işığı açıldı (switch.mutfak_isik)."
    )

    # 2. Announcer speaker routing, tts.speak, fallback to google_translate_say, & scheduler alerts
    recorded_calls: list[tuple[str, str, str, dict[str, Any]]] = []
    fail_speak_once = [False]

    class FakeTTSHAAdapter(HomeAssistantWSAdapter):
        async def call_service(
            self,
            domain: str,
            service: str,
            entity_id: str,
            service_data: Mapping[str, Any] | None = None,
        ) -> dict[str, Any]:
            if domain == "tts" and service == "speak" and fail_speak_once[0]:
                fail_speak_once[0] = False
                raise HomeAssistantError("tts.speak unsupported in mock HA")
            recorded_calls.append((domain, service, entity_id, dict(service_data or {})))
            return {"status": "dispatched"}

    resolver = EntityResolver(
        config=cfg,
        db_path=tmp_path / "voice_resolver.db",
        embedder=LocalHashedNgramEmbedder(dimensions=128),
        enable_ws_listener=False,
    )
    resolver.seed_records_for_test(
        [
            _make_registry_record("switch.mutfak_isik", "Mutfak Işığı", "mutfak", "Mutfak"),
            _make_registry_record(
                "media_player.kitchen_homepod",
                "Mutfak HomePod",
                "mutfak",
                "Mutfak",
            ),
            _make_registry_record(
                "media_player.living_room_homepod",
                "Oturma Odası HomePod",
                "oturma_odasi",
                "Oturma Odası",
            ),
        ]
    )
    ha_adapter = FakeTTSHAAdapter(cfg, entity_resolver=resolver)
    announcer = Announcer(config=cfg, ha_adapter=ha_adapter, entity_resolver=resolver)

    assert await announcer.resolve_speaker(room="mutfak") == "media_player.kitchen_homepod"
    assert await announcer.resolve_speaker(room="Oturma Odası") == "media_player.living_room_homepod"

    res_speak = await announcer.announce("**Mutfak** ışığı açıldı.", room="mutfak")
    assert res_speak.delivered is True
    assert res_speak.tts_service == "tts.speak"
    assert res_speak.speaker_entity == "media_player.kitchen_homepod"
    assert res_speak.message == "Mutfak ışığı açıldı."
    assert recorded_calls[-1] == (
        "tts",
        "speak",
        "tts.google_translate_en_com",
        {
            "media_player_entity_id": "media_player.kitchen_homepod",
            "message": "Mutfak ışığı açıldı.",
            "language": "tr",
        },
    )

    fail_speak_once[0] = True
    res_fallback = await announcer.announce("Yedek servis testi", room="mutfak")
    assert res_fallback.delivered is True
    assert res_fallback.tts_service == "tts.google_translate_say"
    assert recorded_calls[-1] == (
        "tts",
        "google_translate_say",
        "media_player.kitchen_homepod",
        {"message": "Yedek servis testi", "language": "tr"},
    )

    # Scheduler lifecycle result announces to the entity's area speaker (Mutfak), ignoring cancelled
    calls_before_sched = len(recorded_calls)
    await announcer.on_schedule_event(
        ScheduleResultEvent(
            schedule_id="job-cancel",
            entity_id="switch.mutfak_isik",
            status="cancelled",
            verified=False,
        )
    )
    assert len(recorded_calls) == calls_before_sched

    await announcer.on_schedule_event(
        ScheduleResultEvent(
            schedule_id="job-done",
            entity_id="switch.mutfak_isik",
            status="done",
            verified=True,
            detail="Verified.",
        )
    )
    assert len(recorded_calls) == calls_before_sched + 1
    assert recorded_calls[-1][3]["media_player_entity_id"] == "media_player.kitchen_homepod"
    assert "Mutfak Işığı" in recorded_calls[-1][3]["message"]

    # 3. FastAPI /api/v1/voice fast path & slow timeout handoff -> background announcement
    embedder = LocalHashedNgramEmbedder(dimensions=128)
    store = ReflectionStore(
        db_path=tmp_path / "voice_mem.db",
        embedder=embedder,
        similarity_threshold=0.5,
        local_fallback_dimensions=128,
    )
    infra_toolkit = HomelabInfraToolkit(cfg)

    session_models = [
        # Session 1 (fast <8s) builds: HASubAgent, InfraSubAgent, Supervisor
        ScriptedModel(["noop"]),
        ScriptedModel(["noop"]),
        ScriptedModel(["**Mutfak** ışığı açıldı."]),
        # Session 2 (slow >timeout_sec=0.15) builds: HASubAgent, InfraSubAgent, Supervisor
        ScriptedModel(["noop"]),
        ScriptedModel(["noop"]),
        DelayedScriptedModel(["Oturma odası sistem özeti hazır: her şey normal."], delay_sec=0.35),
    ]

    def fake_model_factory(model_id: str | None, temperature: float | None) -> Model:
        return session_models.pop(0)

    factory = AgentFactory(
        config=cfg,
        memory_store=store,
        ha_adapter=ha_adapter,
        infra_toolkit=infra_toolkit,
        custom_model_factory=fake_model_factory,
        entity_resolver=resolver,
    )
    app = create_app(
        config=cfg,
        memory_store=store,
        ha_adapter=ha_adapter,
        infra_toolkit=infra_toolkit,
        factory=factory,
        entity_resolver=resolver,
        announcer=announcer,
    )

    with TestClient(app) as client:
        # 3a. Direct /voice/announce endpoint
        ann_resp = client.post(
            "/api/v1/voice/announce",
            json={"message": "`Kezban` hazır.", "room": "oturma odası"},
        )
        assert ann_resp.status_code == 200
        ann_body = ann_resp.json()
        assert ann_body["delivered"] is True
        assert ann_body["speaker_entity"] == "media_player.living_room_homepod"
        assert ann_body["message"] == "Kezban hazır."

        # 3b. Fast synchronous completion within budget
        fast_resp = client.post(
            "/api/v1/voice",
            json={
                "text": "Kezban, mutfak ışığını aç",
                "room": "mutfak",
                "announce": True,
                "session_id": "voice-fast",
            },
        )
        assert fast_resp.status_code == 200
        fast_body = fast_resp.json()
        assert fast_body["status"] == "completed"
        assert fast_body["timed_out"] is False
        assert fast_body["wake_word_detected"] is True
        assert fast_body["cleaned_prompt"] == "mutfak ışığını aç"
        assert fast_body["speech"] == "Mutfak ışığı açıldı."
        assert fast_body["announcement"]["delivered"] is True
        assert fast_body["announcement"]["speaker_entity"] == "media_player.kitchen_homepod"

        # 3c. Slow agent exceeding 0.15s budget -> immediate accepted_background + async HomePod alert
        calls_before_slow = len(recorded_calls)
        slow_resp = client.post(
            "/api/v1/voice",
            json={
                "text": "Hey Kezban, tüm homelab durumunu derinlemesine analiz et",
                "room": "oturma odası",
                "timeout_sec": 0.15,
                "session_id": "voice-slow",
            },
        )
        assert slow_resp.status_code == 200
        slow_body = slow_resp.json()
        assert slow_body["status"] == "accepted_background"
        assert slow_body["timed_out"] is True
        assert slow_body["wake_word_detected"] is True
        assert slow_body["speech"] == cfg.voice.deferred_ack_text
        assert slow_body["elapsed_ms"] < 300

        # Wait up to 2.0s for the background task + watcher to finish and dispatch HomePod TTS
        deadline = time.monotonic() + 2.0
        while len(recorded_calls) == calls_before_slow and time.monotonic() < deadline:
            await anyio.sleep(0.05)

        assert len(recorded_calls) == calls_before_slow + 1
        assert recorded_calls[-1][3]["media_player_entity_id"] == "media_player.living_room_homepod"
        assert "Oturma odası sistem özeti hazır" in recorded_calls[-1][3]["message"]


@pytest.mark.asyncio
async def test_pre_grounding_prompt_unification_and_gemini_embedder_lru_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _load_test_config(tmp_path, monkeypatch)

    # 1. GeminiEmbedder LRU cache: stem-normalized hits, failure bypass, and LRU eviction
    api_calls: list[str] = []
    fail_next = [False]

    class FakeModels:
        async def embed_content(self, model: str, contents: str, config: Any = None) -> Any:
            api_calls.append(contents)
            if fail_next[0]:
                fail_next[0] = False
                raise RuntimeError("transient embedding failure")

            class _Emb:
                values = [3.0, 4.0]

            class _Resp:
                embeddings = [_Emb()]

            return _Resp()

    class FakeAio:
        models = FakeModels()

    class FakeGenAIClient:
        aio = FakeAio()

    gemini_emb = GeminiEmbedder(
        api_key="test-key",
        embedding_model_id="gemini-embedding-2",
        fallback_dimensions=128,
        client=FakeGenAIClient(),  # type: ignore[arg-type]
        cache_size=2,
    )
    vec1 = await gemini_emb.embed_text("Mutfak ışığının durumu nedir?")
    vec2 = await gemini_emb.embed_text("mutfak ışığı durumu ne")
    assert vec1 == [pytest.approx(0.6), pytest.approx(0.8)]
    assert vec2 == vec1
    assert len(api_calls) == 1
    assert gemini_emb.cache_hits == 1
    assert gemini_emb.cache_misses == 1

    # Failed API call must NOT be cached
    fail_next[0] = True
    with pytest.raises(RuntimeError):
        await gemini_emb.embed_text("oturma odası kliması")
    vec3 = await gemini_emb.embed_text("oturma odası kliması")
    assert len(vec3) == 2
    assert len(api_calls) == 3

    # Fill capacity (size=2) with another phrase to evict oldest ('mutfak isik')
    await gemini_emb.embed_text("salon perdesi")
    await gemini_emb.embed_text("mutfak ışığı")
    assert len(api_calls) == 5

    # 2. prepare_prompt_context: room-scoped pre-grounding, live state injection, learn_pending=False
    resolver = EntityResolver(
        config=cfg,
        db_path=tmp_path / "preground_resolver.db",
        embedder=LocalHashedNgramEmbedder(dimensions=128),
        enable_ws_listener=False,
    )
    resolver.seed_records_for_test(
        [
            _make_registry_record("switch.mutfak_isik", "Mutfak Işık", "mutfak", "Mutfak", state="off"),
            _make_registry_record("light.salon_tavan", "Salon Tavan Işık", "salon", "Salon", state="off"),
        ]
    )

    class FakePreGroundHA(HomeAssistantWSAdapter):
        def __init__(self) -> None:
            super().__init__(cfg, entity_resolver=resolver)
            self.live = {
                "switch.mutfak_isik": EntityState(
                    entity_id="switch.mutfak_isik",
                    state="on",
                    attributes={"brightness": 180},
                ),
                "light.salon_tavan": EntityState(
                    entity_id="light.salon_tavan",
                    state="off",
                    attributes={},
                ),
            }

        async def get_state(self, entity_id: str) -> EntityState | None:
            return self.live.get(entity_id)

    ha_mock = FakePreGroundHA()

    prep_room = await prepare_prompt_context(
        text="Kezban, ışığın durumu nedir?",
        config=cfg,
        entity_resolver=resolver,
        ha_adapter=ha_mock,
        room="mutfak",
        response_style="voice",
    )
    assert prep_room.wake_word_detected is True
    assert prep_room.cleaned_prompt == "ışığın durumu nedir?"
    assert prep_room.pre_grounded_entities == ["switch.mutfak_isik"]
    assert "[SESLİ KOMUT MODU]" in prep_room.augmented_prompt
    assert "[ÖN-ÇÖZÜMLENMİŞ VARLIKLAR (Canlı Home Assistant Verisi)]" in prep_room.augmented_prompt
    assert "switch.mutfak_isik" in prep_room.augmented_prompt
    assert "canlı_durum='on'" in prep_room.augmented_prompt
    assert "kesin_eşleşme=evet" in prep_room.augmented_prompt
    assert resolver.pop_pending_phrase("switch.mutfak_isik") is None

    # Cross-room prompt should resolve the explicitly named room instead of stalling on caller room
    prep_cross = await prepare_prompt_context(
        text="Salon tavan ışığını aç",
        config=cfg,
        entity_resolver=resolver,
        ha_adapter=ha_mock,
        room="mutfak",
        response_style="markdown",
    )
    assert prep_cross.pre_grounded_entities == ["light.salon_tavan"]
    assert "[ODA BAĞLAMI] Kullanıcının bulunduğu oda: mutfak." in prep_cross.augmented_prompt
    assert "light.salon_tavan" in prep_cross.augmented_prompt

    # Unrelated chat prompt without room/entities preserves plain text
    prep_plain = await prepare_prompt_context(
        text="Sen kimsin ve neler yapabilirsin?",
        config=cfg,
        entity_resolver=resolver,
        ha_adapter=ha_mock,
    )
    assert prep_plain.pre_grounded_entities == []
    assert prep_plain.augmented_prompt == "Sen kimsin ve neler yapabilirsin?"

    # 3. /api/v1/chat/stream unified room + response_style='voice' + pre-grounding delivery
    captured_prompts: list[str] = []

    class CapturingModel(ScriptedModel):
        async def stream(
            self,
            messages: Any,
            tool_specs: Any = None,
            system_prompt: Any = None,
            **kwargs: Any,
        ) -> Any:
            for msg in messages or []:
                if msg.get("role") == "user":
                    for block in msg.get("content", []):
                        if isinstance(block, Mapping) and "text" in block:
                            captured_prompts.append(str(block["text"]))
            async for chunk in super().stream(
                messages=messages,
                tool_specs=tool_specs,
                system_prompt=system_prompt,
                **kwargs,
            ):
                yield chunk

    store = ReflectionStore(
        db_path=tmp_path / "unified_mem.db",
        embedder=LocalHashedNgramEmbedder(dimensions=128),
        similarity_threshold=0.5,
        local_fallback_dimensions=128,
    )
    infra_toolkit = HomelabInfraToolkit(cfg)
    models_seq: list[Model] = [
        ScriptedModel(["noop"]),
        ScriptedModel(["noop"]),
        CapturingModel(["Mutfak ışığı şu anda açık durumda."]),
    ]
    factory = AgentFactory(
        config=cfg,
        memory_store=store,
        ha_adapter=ha_mock,
        infra_toolkit=infra_toolkit,
        custom_model_factory=lambda _m, _t: models_seq.pop(0),
        entity_resolver=resolver,
    )
    app = create_app(
        config=cfg,
        memory_store=store,
        ha_adapter=ha_mock,
        infra_toolkit=infra_toolkit,
        factory=factory,
        entity_resolver=resolver,
    )

    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/chat/stream",
            json={
                "prompt": "Kezban, ışığın durumu nedir?",
                "room": "mutfak",
                "response_style": "voice",
            },
        )
        assert resp.status_code == 200
        assert "Mutfak ışığı şu anda açık durumda." in resp.text

    assert len(captured_prompts) == 1
    assert "[SESLİ KOMUT MODU]" in captured_prompts[0]
    assert "switch.mutfak_isik" in captured_prompts[0]
    assert "canlı_durum='on'" in captured_prompts[0]
    assert "Kullanıcı komutu: ışığın durumu nedir?" in captured_prompts[0]


@pytest.mark.asyncio
async def test_plural_possessive_stemming_and_announcer_notify_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.core.text_tr import infer_domain_hints, stem_tr_token

    # 1. Turkish plural+possessive suffixes strip cleanly down to device roots without breaking -i roots
    assert stem_tr_token("ışıklarının") == "isik"
    assert stem_tr_token("ışıklarında") == "isik"
    assert stem_tr_token("lambalarının") == "lamba"
    assert stem_tr_token("vanalarının") == "vana"
    assert stem_tr_token("kapının") == "kapi"
    assert stem_tr_token("kombinin") == "kombi"
    assert infer_domain_hints("Mutfak ışıklarının durumu nedir?") == ("light", "switch")

    cfg = _load_test_config(tmp_path, monkeypatch)
    resolver = EntityResolver(
        config=cfg,
        db_path=tmp_path / "plural_resolver.db",
        embedder=LocalHashedNgramEmbedder(dimensions=128),
        enable_ws_listener=False,
    )
    resolver.seed_records_for_test(
        [
            _make_registry_record("switch.mutfak_isik", "Mutfak Işık", "kitchen", "Mutfak"),
            _make_registry_record("cover.mutfak_guneslik", "Mutfak Güneşlik", "kitchen", "Mutfak"),
        ]
    )
    res = await resolver.resolve("Mutfak ışıklarının durumu nedir?", limit=3, learn_pending=False)
    assert res.resolved is True
    assert res.top_entity_id == "switch.mutfak_isik"

    # 2. Announcer falls back to mobile push notify when no speaker is configured, omitting WS target
    cfg_notify = cfg.model_copy(
        update={
            "voice": cfg.voice.model_copy(
                update={
                    "default_speaker": None,
                    "room_speakers": {},
                    "fallback_notify_service": "notify.mobile_app_iphone",
                    "notify_title": "Kezban",
                }
            )
        }
    )

    sent_ws_payloads: list[dict[str, Any]] = []

    from contextlib import asynccontextmanager

    class FakeWSHAAdapter(HomeAssistantWSAdapter):
        @asynccontextmanager
        async def _ws_session(self) -> Any:
            yield object()

        async def _send_ws_command(
            self,
            ws: Any,
            msg_id: int,
            payload: dict[str, Any],
        ) -> dict[str, Any]:
            sent_ws_payloads.append(dict(payload))
            return {"id": msg_id, "type": "result", "success": True}

    ws_ha = FakeWSHAAdapter(cfg_notify)
    announcer = Announcer(config=cfg_notify, ha_adapter=ws_ha, entity_resolver=resolver)
    notify_res = await announcer.announce("Mutfak ışığı şu anda **kapalı**.", room="mutfak")

    assert notify_res.delivered is True
    assert notify_res.tts_service == "notify.mobile_app_iphone"
    assert notify_res.reason == "fallback_notify"
    assert len(sent_ws_payloads) == 1
    assert sent_ws_payloads[0] == {
        "type": "call_service",
        "domain": "notify",
        "service": "mobile_app_iphone",
        "service_data": {
            "title": "Kezban",
            "message": "Mutfak ışığı şu anda kapalı.",
        },
    }
    assert "target" not in sent_ws_payloads[0]




