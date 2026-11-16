"""Integration and unit tests for HAHistoryClient, ThermalTwinService, ThermalSubAgent, and FastAPI."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from strands.models.model import Model

from src.agents.factory import AgentFactory
from src.core.config import AppConfig
from src.core.entity_resolver import EntityResolver, RegistryEntityRecord
from src.core.exceptions import CircuitOpenError, HomeAssistantError
from src.core.text_tr import normalize_tr, stem_tr_phrase
from src.main import create_app
from src.memory.reflection_store import LocalHashedNgramEmbedder, ReflectionStore
from src.tools.ha_history_client import HAHistoryClient
from src.tools.ha_ws_adapter import HomeAssistantWSAdapter
from src.tools.infra_mcp_server import HomelabInfraToolkit
from src.tools.thermal_tools import ThermalTwinService


class ScriptedModel(Model):
    """Deterministic Strands Model double for testing Supervisor -> ThermalSubAgent flows."""

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

    async def stream(
        self,
        messages: Any,
        tool_specs: Any = None,
        system_prompt: Any = None,
        **kwargs: Any,
    ) -> Any:
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


class InspectingScriptedModel(ScriptedModel):
    """ScriptedModel capturing every prompt/tool payload seen by the LLM."""

    def __init__(self, steps: list[Any]) -> None:
        super().__init__(steps)
        self.observed_calls: list[list[dict[str, Any]]] = []

    async def stream(
        self,
        messages: Any,
        tool_specs: Any = None,
        system_prompt: Any = None,
        **kwargs: Any,
    ) -> Any:
        snapshot: list[dict[str, Any]] = []
        for msg in messages or []:
            if isinstance(msg, Mapping):
                snapshot.append(dict(msg))
        self.observed_calls.append(snapshot)
        async for chunk in super().stream(
            messages=messages,
            tool_specs=tool_specs,
            system_prompt=system_prompt,
            **kwargs,
        ):
            yield chunk


def _write_synthetic_floorplan(path: Path) -> Path:
    payload: dict[str, Any] = {
        "building": {
            "version": 1,
            "weather_entity": "weather.home",
            "outdoor_temp_sensors": ["sensor.outdoor_temperature"],
            "floors": [
                {
                    "id": "floor_1",
                    "name": "First Floor",
                    "height": 2.5,
                    "rooms": [
                        {
                            "id": "room_bedroom",
                            "name": "Bedroom",
                            "area_id": "bedroom",
                            "points": [[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0]],
                            "climate": {
                                "temperature": "sensor.bedroom_ambient_temp",
                                "humidity": "sensor.bedroom_humidity",
                            },
                        },
                        {
                            "id": "room_study",
                            "name": "Study",
                            "area_id": "study",
                            "points": [[4.0, 0.0], [7.0, 0.0], [7.0, 3.0], [4.0, 3.0]],
                        },
                    ],
                    "openings": [
                        {
                            "id": "win_bedroom",
                            "room_id": "room_bedroom",
                            "type": "window",
                            "width": 2.0,
                            "height": 1.4,
                            "contact": "binary_sensor.bedroom_window",
                        }
                    ],
                    "furniture": [
                        {
                            "id": "rad_bedroom",
                            "type": "radiator",
                            "x": 2.0,
                            "z": 0.15,
                            "entity": "climate.bedroom_trv",
                            "valve_entity": "sensor.bedroom_valve",
                        },
                        {
                            "id": "rad_study",
                            "type": "radiator",
                            "x": 5.5,
                            "z": 0.15,
                            "entity": "climate.study_trv",
                            "valve_entity": "sensor.study_valve",
                        },
                    ],
                    "placements": [
                        {
                            "entity_id": "sensor.bedroom_ambient_temp",
                            "x": 2.0,
                            "z": 3.0,
                            "y": 1.5,
                        }
                    ],
                }
            ],
        }
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _load_thermal_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    plan_path: Path | None = None,
) -> AppConfig:
    repo_root = Path(__file__).resolve().parents[1]
    example_yaml = repo_root / "config" / "settings.example.yaml"
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("HA_BASE_URL", "http://192.0.2.10:8123")
    monkeypatch.setenv("HA_WS_URL", "ws://192.0.2.10:8123/api/websocket")
    monkeypatch.setenv("HA_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("HA_MCP_SSE_URL", "")
    monkeypatch.setenv("APP_HOST", "127.0.0.1")
    monkeypatch.setenv("APP_PORT", "8080")
    monkeypatch.setenv("SQLITE_DB_PATH", str(tmp_path / "thermal_mesh.db"))
    monkeypatch.setenv("YAML_CONFIG_PATH", str(example_yaml))
    monkeypatch.setenv("NEONPLAN_JSON_PATH", str(plan_path or (tmp_path / "neonplan.json")))
    monkeypatch.setenv("THERMAL_OVERRIDES_PATH", str(tmp_path / "thermal_overrides.yaml"))
    monkeypatch.setenv("MEMORY__EMBEDDING_BACKEND", "local")
    monkeypatch.setenv("HOME_ASSISTANT__MCP__ENABLED", "false")
    monkeypatch.setenv("THERMAL__ENABLED", "true")
    monkeypatch.setenv("THERMAL__TICK_INTERVAL_SEC", "3600")
    monkeypatch.setenv("AGENTS__SUB_AGENTS__THERMAL__ENABLED", "true")
    return AppConfig()  # type: ignore[call-arg]


def _make_registry_record(entity_id: str, friendly_name: str, area_id: str, area_name: str) -> RegistryEntityRecord:
    domain = entity_id.split(".", 1)[0]
    combined = normalize_tr(f"{friendly_name} {area_name} {entity_id.split('.', 1)[-1]}")
    _, stems = stem_tr_phrase(combined, strip_stopwords=False)
    _, area_stems = stem_tr_phrase(area_name, strip_stopwords=False)
    return RegistryEntityRecord(
        entity_id=entity_id,
        domain=domain,
        friendly_name=friendly_name,
        area_id=area_id,
        area_name=area_name,
        search_text_norm=combined,
        search_stems=stems,
        area_stems=area_stems,
    )


@pytest.mark.asyncio
async def test_ha_history_client_query_splitting_circuit_breaker_and_virtual_sensors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify climate vs sensor query params, CircuitBreaker tripping, and state truncation <= 245 chars."""
    cfg = _load_thermal_config(tmp_path, monkeypatch)
    recorded_requests: list[tuple[str, str, dict[str, str], dict[str, Any] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        recorded_requests.append((request.method, request.url.path, params, body))
        if request.method == "GET" and request.url.path.startswith("/api/history/period/"):
            filter_ids = params.get("filter_entity_id", "").split(",")
            payload = []
            for eid in filter_ids:
                if eid == "climate.bedroom_trv":
                    payload.append(
                        [
                            {
                                "entity_id": eid,
                                "state": "heat",
                                "attributes": {"current_temperature": 21.0, "hvac_action": "idle"},
                                "last_updated": "2026-11-15T06:00:00Z",
                            }
                        ]
                    )
                else:
                    payload.append(
                        [
                            {
                                "entity_id": eid,
                                "state": "19.8",
                                "last_updated": "2026-11-15T06:00:00Z",
                            }
                        ]
                    )
            return httpx.Response(200, json=payload)
        if request.method == "POST" and request.url.path.startswith("/api/states/"):
            return httpx.Response(200, json=body or {})
        return httpx.Response(404, json={"message": "not found"})

    http_client = httpx.AsyncClient(
        base_url=cfg.ha_base_url,
        transport=httpx.MockTransport(handler),
    )
    client = HAHistoryClient(config=cfg, http_client=http_client)
    try:
        start = datetime(2026, 11, 15, 0, 0, tzinfo=UTC)
        end = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)

        # 1. Mixed climate + sensor query splits into attribute-rich vs minimal batches
        hist = await client.fetch_history(
            entity_ids=["climate.bedroom_trv", "sensor.bedroom_ambient_temp"],
            start_utc=start,
            end_utc=end,
        )
        assert len(recorded_requests) == 2
        rich_req = next(r for r in recorded_requests if r[2]["filter_entity_id"] == "climate.bedroom_trv")
        min_req = next(r for r in recorded_requests if r[2]["filter_entity_id"] == "sensor.bedroom_ambient_temp")
        assert "minimal_response" not in rich_req[2]
        assert "no_attributes" not in rich_req[2]
        assert min_req[2]["minimal_response"] == "1"
        assert min_req[2]["no_attributes"] == "1"
        assert hist["climate.bedroom_trv"][0]["attributes"]["current_temperature"] == pytest.approx(21.0)

        # 2. Pure sensor query uses minimal flags only
        recorded_requests.clear()
        await client.fetch_history(["sensor.bedroom_ambient_temp"], start_utc=start, end_utc=end)
        assert len(recorded_requests) == 1
        assert recorded_requests[0][2]["minimal_response"] == "1"
        assert recorded_requests[0][2]["no_attributes"] == "1"

        # 3. Virtual sensor truncation <= 245 chars and full report_markdown attribute
        long_state = "A" * 350
        await client.publish_virtual_sensor(
            entity_id="sensor.thermal_twin_bedroom_stats",
            state=long_state,
            attributes={"unit_of_measurement": "°C/h"},
        )
        posted_stats = recorded_requests[-1][3]
        assert posted_stats is not None
        assert len(posted_stats["state"]) <= 245

        full_md = "# Thermal Briefing\n\n" + ("- Bedroom nominal\n" * 30)
        await client.publish_advisor_report(
            summary_state="S" * 320,
            report_markdown=full_md,
        )
        posted_report = recorded_requests[-1][3]
        assert posted_report is not None
        assert len(posted_report["state"]) <= 245
        assert posted_report["attributes"]["report_markdown"] == full_md
    finally:
        await http_client.aclose()

    # 4. CircuitBreaker opens after consecutive 5xx failures
    call_count = 0

    def failing_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(503, json={"error": "upstream unavailable"})

    failing_http = httpx.AsyncClient(
        base_url=cfg.ha_base_url,
        transport=httpx.MockTransport(failing_handler),
    )
    failing_client = HAHistoryClient(config=cfg, http_client=failing_http)
    try:
        for _ in range(cfg.circuit_breaker.failure_threshold):
            with pytest.raises(HomeAssistantError):
                await failing_client.fetch_all_states()
        with pytest.raises(CircuitOpenError):
            await failing_client.fetch_all_states()
        assert call_count == cfg.circuit_breaker.failure_threshold
    finally:
        await failing_http.aclose()


@pytest.mark.asyncio
async def test_thermal_service_tick_history_and_virtual_sensor_publishing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify ThermalTwinService coordinates topology, live publishing, history analysis, and simulation."""
    plan_path = _write_synthetic_floorplan(tmp_path / "neonplan.json")
    cfg = _load_thermal_config(tmp_path, monkeypatch, plan_path=plan_path)
    published_sensors: dict[str, dict[str, Any]] = {}

    t_base = datetime(2026, 11, 15, 0, 0, tzinfo=UTC)
    live_states = [
        {"entity_id": "sensor.outdoor_temperature", "state": "3.0", "attributes": {}},
        {"entity_id": "weather.home", "state": "cloudy", "attributes": {"temperature": 3.0, "humidity": 82}},
        {
            "entity_id": "climate.bedroom_trv",
            "state": "heat",
            "attributes": {"current_temperature": 21.5, "temperature": 20.5, "hvac_action": "heating"},
        },
        {"entity_id": "sensor.bedroom_valve", "state": "50.0", "attributes": {}},
        {"entity_id": "sensor.bedroom_ambient_temp", "state": "19.8", "attributes": {}},
        {"entity_id": "sensor.bedroom_humidity", "state": "56.0", "attributes": {}},
        {"entity_id": "binary_sensor.bedroom_window", "state": "off", "attributes": {}},
        {
            "entity_id": "climate.study_trv",
            "state": "heat",
            "attributes": {"current_temperature": 20.8, "temperature": 20.0, "hvac_action": "idle"},
        },
        {"entity_id": "sensor.study_valve", "state": "0.0", "attributes": {}},
    ]

    def ha_rest_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/states":
            return httpx.Response(200, json=live_states)
        if request.method == "POST" and request.url.path.startswith("/api/states/"):
            eid = request.url.path.removeprefix("/api/states/")
            published_sensors[eid] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json=published_sensors[eid])
        if request.method == "GET" and request.url.path.startswith("/api/history/period/"):
            eids = request.url.params.get("filter_entity_id", "").split(",")
            series_list: list[list[dict[str, Any]]] = []
            for eid in eids:
                if eid == "sensor.bedroom_ambient_temp":
                    series_list.append(
                        [
                            {
                                "entity_id": eid,
                                "state": f"{20.8 - 0.15 * i:.2f}",
                                "last_updated": (t_base + timedelta(hours=i)).isoformat(),
                            }
                            for i in range(6)
                        ]
                    )
                elif eid == "climate.bedroom_trv":
                    series_list.append(
                        [
                            {
                                "entity_id": eid,
                                "state": "heat",
                                "attributes": {"current_temperature": 21.0 - 0.15 * i, "hvac_action": "idle"},
                                "last_updated": (t_base + timedelta(hours=i)).isoformat(),
                            }
                            for i in range(6)
                        ]
                    )
                elif eid == "sensor.bedroom_valve":
                    series_list.append(
                        [{"entity_id": eid, "state": "0.0", "last_updated": t_base.isoformat()}]
                    )
                elif eid == "sensor.outdoor_temperature":
                    series_list.append(
                        [{"entity_id": eid, "state": "4.0", "last_updated": t_base.isoformat()}]
                    )
            return httpx.Response(200, json=series_list)
        return httpx.Response(404, json={"message": "not found"})

    http_client = httpx.AsyncClient(
        base_url=cfg.ha_base_url,
        transport=httpx.MockTransport(ha_rest_handler),
    )
    history_client = HAHistoryClient(config=cfg, http_client=http_client)
    resolver = EntityResolver(
        config=cfg,
        db_path=cfg.sqlite_db_path,
        embedder=LocalHashedNgramEmbedder(dimensions=128),
        enable_ws_listener=False,
    )
    resolver.seed_records_for_test(
        [
            _make_registry_record("sensor.study_humidity", "Study Humidity", "study", "Study"),
        ]
    )
    service = ThermalTwinService(
        config=cfg,
        entity_resolver=resolver,
        history_client=history_client,
    )
    try:
        await service.initialize()
        assert "study" in service.topology.zones
        assert "sensor.study_humidity" in service.topology.zones["study"].humidity_sensors

        snap = await service.run_live_tick(publish=True)
        assert snap.monitored_zone_count == 2
        assert "sensor.thermal_twin_bedroom_true_temp" in published_sensors
        assert "sensor.thermal_twin_bedroom_stats" in published_sensors
        assert len(published_sensors["sensor.thermal_twin_bedroom_true_temp"]["state"]) <= 245

        hist_res = await service.run_history_analysis(zone_id="bedroom", lookback_hours=24)
        assert "bedroom" in hist_res["night_cooling"]

        sim_res = await service.simulate(zone_id="bedroom", hours_ahead=8.0, outdoor_temp_c=1.0)
        assert "bedroom" in sim_res["projections"]

        report_md = await service.run_daily_briefing()
        assert "Günlük Termal İkiz Özeti" in report_md
        assert "sensor.thermal_twin_advisor_report" in published_sensors
        advisor_post = published_sensors["sensor.thermal_twin_advisor_report"]
        assert len(advisor_post["state"]) <= 245
        assert advisor_post["attributes"]["report_markdown"] == report_md

        async def _failing_advisor(_: str) -> str:
            raise RuntimeError("simulated MaxTokensReachedException")

        service.set_advisor_runner(_failing_advisor)
        fallback_md = await service.run_daily_briefing()
        assert "Günlük Termal İkiz Özeti" in fallback_md
        assert published_sensors["sensor.thermal_twin_advisor_report"]["attributes"]["report_markdown"] == fallback_md
    finally:
        await service.aclose()
        await http_client.aclose()


def test_thermal_subagent_supervisor_and_fastapi_integration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify Supervisor routes thermal queries to thermal_twin_agent with stateless context & compact tools."""
    plan_path = _write_synthetic_floorplan(tmp_path / "neonplan.json")
    cfg = _load_thermal_config(tmp_path, monkeypatch, plan_path=plan_path)
    published_sensors: dict[str, dict[str, Any]] = {}

    t_base = datetime(2026, 11, 15, 6, 0, tzinfo=UTC)
    live_states = [
        {"entity_id": "sensor.outdoor_temperature", "state": "2.0", "attributes": {}},
        {"entity_id": "weather.home", "state": "cloudy", "attributes": {"temperature": 2.0, "humidity": 78}},
        {
            "entity_id": "climate.bedroom_trv",
            "state": "heat",
            "attributes": {"current_temperature": 20.5, "temperature": 20.0, "hvac_action": "idle"},
        },
        {"entity_id": "sensor.bedroom_valve", "state": "0.0", "attributes": {}},
        {"entity_id": "sensor.bedroom_ambient_temp", "state": "19.5", "attributes": {}},
        {"entity_id": "sensor.bedroom_humidity", "state": "58.0", "attributes": {}},
        {"entity_id": "binary_sensor.bedroom_window", "state": "off", "attributes": {}},
        {
            "entity_id": "climate.study_trv",
            "state": "heat",
            "attributes": {"current_temperature": 20.0, "temperature": 20.0, "hvac_action": "idle"},
        },
        {"entity_id": "sensor.study_valve", "state": "0.0", "attributes": {}},
    ]

    # Synthetic 15-min morning ventilation episode (08:00 -> 08:15)
    v_open = t_base + timedelta(hours=2)
    v_close = v_open + timedelta(minutes=15)

    def ha_rest_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/states":
            return httpx.Response(200, json=live_states)
        if request.method == "POST" and request.url.path.startswith("/api/states/"):
            eid = request.url.path.removeprefix("/api/states/")
            published_sensors[eid] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(200, json=published_sensors[eid])
        if request.method == "GET" and request.url.path.startswith("/api/history/period/"):
            eids = request.url.params.get("filter_entity_id", "").split(",")
            out: list[list[dict[str, Any]]] = []
            for eid in eids:
                if eid == "binary_sensor.bedroom_window":
                    out.append(
                        [
                            {"entity_id": eid, "state": "off", "last_updated": t_base.isoformat()},
                            {"entity_id": eid, "state": "on", "last_updated": v_open.isoformat()},
                            {"entity_id": eid, "state": "off", "last_updated": v_close.isoformat()},
                        ]
                    )
                elif eid == "sensor.bedroom_ambient_temp":
                    out.append(
                        [
                            {"entity_id": eid, "state": "20.2", "last_updated": v_open.isoformat()},
                            {"entity_id": eid, "state": "18.6", "last_updated": (v_open + timedelta(minutes=5)).isoformat()},
                            {"entity_id": eid, "state": "17.8", "last_updated": v_close.isoformat()},
                            {"entity_id": eid, "state": "19.1", "last_updated": (v_close + timedelta(minutes=20)).isoformat()},
                        ]
                    )
                elif eid == "sensor.bedroom_humidity":
                    out.append(
                        [
                            {"entity_id": eid, "state": "64.0", "last_updated": v_open.isoformat()},
                            {"entity_id": eid, "state": "52.0", "last_updated": (v_open + timedelta(minutes=5)).isoformat()},
                            {"entity_id": eid, "state": "48.0", "last_updated": v_close.isoformat()},
                            {"entity_id": eid, "state": "49.0", "last_updated": (v_close + timedelta(minutes=20)).isoformat()},
                        ]
                    )
                elif eid == "climate.bedroom_trv":
                    out.append(
                        [
                            {
                                "entity_id": eid,
                                "state": "heat",
                                "attributes": {"current_temperature": 20.5, "temperature": 7.0, "hvac_action": "idle"},
                                "last_updated": v_open.isoformat(),
                            }
                        ]
                    )
                elif eid == "sensor.bedroom_valve":
                    out.append([{"entity_id": eid, "state": "0.0", "last_updated": t_base.isoformat()}])
                elif eid == "sensor.outdoor_temperature":
                    out.append([{"entity_id": eid, "state": "2.0", "last_updated": t_base.isoformat()}])
                elif eid == "weather.home":
                    out.append(
                        [
                            {
                                "entity_id": eid,
                                "state": "cloudy",
                                "attributes": {"temperature": 2.0, "humidity": 78.0},
                                "last_updated": v_open.isoformat(),
                            }
                        ]
                    )
            return httpx.Response(200, json=out)
        return httpx.Response(404, json={"message": "not found"})

    http_client = httpx.AsyncClient(
        base_url=cfg.ha_base_url,
        transport=httpx.MockTransport(ha_rest_handler),
    )
    history_client = HAHistoryClient(config=cfg, http_client=http_client)
    store = ReflectionStore(
        db_path=cfg.sqlite_db_path,
        embedder=LocalHashedNgramEmbedder(dimensions=128),
        similarity_threshold=0.5,
        local_fallback_dimensions=128,
    )
    resolver = EntityResolver(
        config=cfg,
        db_path=cfg.sqlite_db_path,
        embedder=LocalHashedNgramEmbedder(dimensions=128),
        enable_ws_listener=False,
    )
    ha_adapter = HomeAssistantWSAdapter(cfg, entity_resolver=resolver)
    infra_toolkit = HomelabInfraToolkit(cfg)
    thermal_service = ThermalTwinService(
        config=cfg,
        ha_adapter=ha_adapter,
        entity_resolver=resolver,
        history_client=history_client,
    )

    thermal_model = InspectingScriptedModel(
        [
            # Turn 1 (overnight bedroom projection)
            ("get_thermal_twin_snapshot", {"zone_id": "bedroom", "refresh": True}, "th-1"),
            ("simulate_thermal_scenario", {"zone_id": "bedroom", "hours_ahead": 8.0}, "th-2"),
            "Bedroom is currently 19.5°C true ambient and projected to reach ~11.2°C overnight.",
            # Turn 2 (morning ventilation + publish report; stateless preserve_context=False!)
            ("analyze_zone_history", {"zone_id": "bedroom", "lookback_hours": 24}, "th-3"),
            (
                "publish_thermal_advisor_report",
                {
                    "summary_state": "Bedroom morning ventilation optimal (~10 min)",
                    "report_markdown": "## Bedroom Ventilation Report\n\n- Knee point ~10 min, rebound to 19.1°C.",
                },
                "th-4",
            ),
            "Morning ventilation purged moisture at knee point and dashboard report was published.",
        ]
    )
    supervisor_model = InspectingScriptedModel(
        [
            ("thermal_twin_agent", {"input": "How cold will the bedroom get tonight?"}, "sup-th-1"),
            "The bedroom true temperature is 19.5°C and is projected to cool overnight if unheated.",
            ("thermal_twin_agent", {"input": "Analyze today's morning ventilation and publish report"}, "sup-th-2"),
            "Morning ventilation analysis is complete and published to the Home Assistant dashboard.",
        ]
    )
    models_by_key: list[Model] = [
        ScriptedModel(["noop"]),  # HASubAgent
        ScriptedModel(["noop"]),  # InfraSubAgent
        thermal_model,            # ThermalSubAgent
        supervisor_model,         # Supervisor
    ]
    factory = AgentFactory(
        config=cfg,
        memory_store=store,
        ha_adapter=ha_adapter,
        infra_toolkit=infra_toolkit,
        custom_model_factory=lambda _m, _t: models_by_key.pop(0),
        entity_resolver=resolver,
        thermal_service=thermal_service,
    )
    app = create_app(
        config=cfg,
        memory_store=store,
        ha_adapter=ha_adapter,
        infra_toolkit=infra_toolkit,
        factory=factory,
        entity_resolver=resolver,
        thermal_service=thermal_service,
    )

    with TestClient(app) as client:
        health = client.get("/api/v1/health")
        assert health.status_code == 200
        assert "thermal_twin_agent" in health.json()["active_sub_agents"]

        r1 = client.post(
            "/api/v1/chat/stream",
            json={"prompt": "How cold will the bedroom get tonight?", "session_id": "thermal-sess"},
        )
        assert r1.status_code == 200
        ev1 = [
            json.loads(line.removeprefix("data: "))
            for line in r1.text.splitlines()
            if line.startswith("data: ")
        ]
        assert any(
            e["type"] == "tool_call_start" and e["tool"] == "get_thermal_twin_snapshot"
            for e in ev1
        )
        assert any(
            e["type"] == "tool_call_start" and e["tool"] == "simulate_thermal_scenario"
            for e in ev1
        )
        assert next(e for e in ev1 if e["type"] == "done")["stop_reason"] == "end_turn"

        r2 = client.post(
            "/api/v1/chat/stream",
            json={"prompt": "Analyze today's morning ventilation", "session_id": "thermal-sess"},
        )
        assert r2.status_code == 200
        ev2 = [
            json.loads(line.removeprefix("data: "))
            for line in r2.text.splitlines()
            if line.startswith("data: ")
        ]
        assert any(
            e["type"] == "tool_call_start" and e["tool"] == "analyze_zone_history"
            for e in ev2
        )
        assert any(
            e["type"] == "tool_call_start" and e["tool"] == "publish_thermal_advisor_report"
            for e in ev2
        )
        assert "sensor.thermal_twin_advisor_report" in published_sensors
        assert "Bedroom Ventilation Report" in (
            published_sensors["sensor.thermal_twin_advisor_report"]["attributes"]["report_markdown"]
        )

    # Verify preserve_context=False: turn 2's initial call to ThermalSubAgent has only 1 user message
    # (calls 0,1,2 belong to prompt 1; calls 3,4,5 belong to prompt 2)
    assert len(thermal_model.observed_calls) == 6
    turn2_initial_msgs = thermal_model.observed_calls[3]
    user_msgs_in_turn2 = [m for m in turn2_initial_msgs if m.get("role") == "user"]
    assert len(user_msgs_in_turn2) == 1

    # Verify no raw HA time-series arrays (e.g. raw 'last_updated' payloads) ever reached the LLM
    serialized_llm_inputs = json.dumps(thermal_model.observed_calls)
    assert "last_updated" not in serialized_llm_inputs
    assert "last_changed" not in serialized_llm_inputs
    assert "ventilation_events" in serialized_llm_inputs
    assert "rebound_temp_c" in serialized_llm_inputs
