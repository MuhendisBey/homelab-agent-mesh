# Homelab & Smart Home Multi-Agent Orchestrator (`homelab-agent-mesh` v1.3.0)

Production-grade, modular, 12-Factor compliant **Hierarchical Multi-Agent Homelab & Smart Home Orchestrator** built on **Python 3.12+ (`asyncio`)**, **Strands Agents SDK**, and **FastAPI Server-Sent Events (SSE)**.

> 📖 **Documentation:**  
> - **Architecture & C4 / Sequence Diagrams:** **[docs/architecture.md](docs/architecture.md)**  
> - **GitOps & Zero-Secret Deployment Strategy:** **[docs/deployment.md](docs/deployment.md)**  
> - **v2.0 Plugin SDK & Diagnostics Roadmap:** **[docs/architecture_roadmap.md](docs/architecture_roadmap.md)**

## Key Architectural Highlights

- **Zero Hardcoding & Public-Repo Safe (12-Factor App):**
  - All secrets and endpoints are loaded via `pydantic-settings` from `.env` (gitignored).
  - All model IDs, temperatures, HVAC safety bounds, protected containers/VMs, entity aliases, and seed reflection rules live in `config/settings.yaml` (gitignored).
  - Sanitized `.env.example` and `config/settings.example.yaml` use RFC 5737 (`192.0.2.x`) placeholders so the repo can be safely published to GitHub.
- **Hierarchical Supervisor & Agent-as-a-Tool (`.as_tool()`):**
  - Root `Supervisor` agent maintains a sliding window conversation buffer (`SlidingWindowConversationManager`) and delegates domain tasks to stateless specialist sub-agents (`HASubAgent` and `InfraSubAgent`).
  - Open/Closed plugin registry (`@register_sub_agent`) allows adding new specialist agents without modifying core orchestrator code.
- **3-Tier Zero-Miss Reflection Store (SQLite + Embeddings) & Memory Drift Guard:**
  - **Tier 1 (Bounded Context Isolation):** Filters lessons by domain (`ha` vs `infra`).
  - **Tier 2 (Core Invariants):** Lessons marked `is_core_invariant = 1` are **always** injected (0% miss rate).
  - **Tier 3 (Semantic Similarity):** Cosine similarity search via Google GenAI embeddings (`text-embedding-004`) with an automatic zero-RAM local hashed n-gram fallback.
  - **Anti-Poisoning & Repetition Threshold:** Rejects transient network/timeout/circuit-breaker failures (`MemoryPoisoningError`) and stages agent lessons until observed `agent_min_occurrences` (default: 2) times before activation.
- **Actor-Critic & Deterministic Post-Action State Assertion:**
  - `HomeAssistantWSAdapter` provides rapid sub-second sequential WebSocket loops (`execute_sequential_loop`), allow-listed raw event firing (`fire_custom_ha_event`), and post-action verification (`assert_entity_state` -> `StateAssertionSuccess` / `StateAssertionFailure`).
- **Event-Driven Entity Grounding & CoALA Semantic Memory (P1):**
  - Sub-15ms Turkish/English entity resolution (`EntityResolver`) combining a real-time Home Assistant WebSocket registry Grounding Index, dependency-free Turkish agglutinative morphology (`text_tr.py`), and CoALA `entity_memory` backed by SQLite WAL + Beta-Bernoulli confidence scoring (`verified_action` and user corrections).
- **Persistent Action Scheduler (P2):**
  - Zero-dependency `asyncio` + SQLite (`scheduled_actions`) deferred execution engine (`ActionScheduler`) with crash recovery, `safe_direction` misfire policy, runtime guard enforcement, and Actor-Critic post-action verification (`assert_state` + automatic retry).
- **Voice Command Gateway & HomePod AirPlay TTS Announcer (P4):**
  - `POST /api/v1/voice` strips Turkish/English wake prefixes (`Hey Kezban`, `Kezban`), enforces a synchronous voice timeout budget (`voice.sync_timeout_sec`, default `8.0s`), and seamlessly hands off longer tasks to finish in the background and speak results on HomePods via Home Assistant TTS (`tts.speak` with `tts.google_translate_say` fallback) plus automatic announcements for completed `ActionScheduler` tasks.
- **Pre-Condition Guard Clauses, Parallel Telemetry & Multi-Interrupt Two-Phase Commit:**
  - Blocks out-of-range HVAC setpoints, respects window-open frost protection (`window_open_guard_temp` + contact sensors), and prevents stopping/restarting protected containers or VMs (`ProtectedResourceViolationError`).
  - Parallelizes Docker container telemetry stats via `ThreadPoolExecutor` and `infra.docker_stats_concurrency`.
  - Intercepts operations listed in `safety.destructive_tools_requiring_approval` via Strands `Confirm` interventions, supports sequential and batch multi-interrupt resumption (`resolved_interrupt_responses`), streams an `intervention_required` SSE packet with `approval_id`, and pauses execution until confirmed via `POST /api/v1/interventions/approve`.

## Directory Structure

```text
homelab-agent-mesh/
├── .dockerignore
├── .env.example
├── .gitignore
├── pyproject.toml
├── docker-compose.yml
├── Dockerfile
├── README.md
├── docs/
│   ├── architecture.md
│   ├── architecture_roadmap.md
│   └── deployment.md                # Dual-repo GitOps, GHCR & SOPS+age deployment guide
├── config/
│   └── settings.example.yaml
├── tests/
│   └── test_orchestrator.py
└── src/
    ├── __init__.py
    ├── main.py                      # FastAPI SSE entrypoint & lifecycle manager
    ├── core/
    │   ├── __init__.py
    │   ├── announcer.py             # HomePod AirPlay TTS Announcer & wake-word stripper
    │   ├── config.py                # Pydantic BaseSettings (.env + YAML loader)
    │   ├── entity_resolver.py       # Event-driven HA Grounding Index & CoALA entity_memory
    │   ├── events.py                # Typed SSE event models & async queue sink
    │   ├── exceptions.py            # Custom domain & circuit-breaker exceptions
    │   ├── guards.py                # Pre-condition Guard Clauses & safety validators
    │   ├── interfaces.py            # Abstract interfaces (BaseStateVerifier, EventEmitter)
    │   ├── scheduler.py             # Persistent asyncio + SQLite ActionScheduler
    │   └── text_tr.py               # Turkish normalization, stemming & domain hints
    ├── memory/
    │   ├── __init__.py
    │   ├── base.py                  # Abstract Memory Store interface (DIP)
    │   └── reflection_store.py      # 3-Tier SQLite + Semantic Embedding store
    ├── tools/
    │   ├── __init__.py
    │   ├── ha_ws_adapter.py         # Custom Async WebSocket adapter & state asserter
    │   ├── infra_mcp_server.py      # FastMCP / MCPServer for Host, Docker & Proxmox
    │   └── schedule_tools.py        # Strands tools for deferred HA scheduling
    ├── agents/
    │   ├── __init__.py
    │   ├── factory.py               # Dynamic Strands Agent builder & InterventionHandlers
    │   ├── ha_agent.py              # Home Assistant specialist sub-agent
    │   ├── infra_agent.py           # Homelab Infrastructure specialist sub-agent
    │   └── supervisor.py            # Root Orchestrator with Interventions & Streaming
    └── api/
        ├── __init__.py
        ├── schemas.py               # SSE event & request/response Pydantic models
        └── routes.py                # /chat/stream, /entities/*, /schedules, /voice/* & /health endpoints
```

## Quick Start

```bash
cp .env.example .env
cp config/settings.example.yaml config/settings.yaml
# Edit .env and config/settings.yaml with your local endpoints and credentials

docker compose up --build -d
```

### Example SSE Stream Request

```bash
curl -N -X POST http://localhost:8080/api/v1/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Turn on living_room_tv, set main_thermostat to 21.5C, toggle dining_light 3 times with 800ms delay, and pause heavy ML containers if CPU is high."}'
```

### Approving a Paused Destructive Operation (Two-Phase Commit)

```bash
curl -N -X POST http://localhost:8080/api/v1/interventions/approve \
  -H "Content-Type: application/json" \
  -d '{"approval_id": "<approval_id_from_sse>", "approved": true}'
```

### Sending a Voice Command (P4)

```bash
curl -X POST http://localhost:8080/api/v1/voice \
  -H "Content-Type: application/json" \
  -d '{"text": "Kezban, mutfak ışıklarını aç", "room": "mutfak", "announce": false}'
```

