# Homelab & Smart Home Multi-Agent Orchestrator (`homelab-agent-mesh` v1.0)

Production-grade, modular, 12-Factor compliant **Hierarchical Multi-Agent Homelab & Smart Home Orchestrator** built on **Python 3.12+ (`asyncio`)**, **Strands Agents SDK**, and **FastAPI Server-Sent Events (SSE)**.

## Key Architectural Highlights

- **Zero Hardcoding & Public-Repo Safe (12-Factor App):**
  - All secrets and endpoints are loaded via `pydantic-settings` from `.env` (gitignored).
  - All model IDs, temperatures, HVAC safety bounds, protected containers/VMs, entity aliases, and seed reflection rules live in `config/settings.yaml` (gitignored).
  - Sanitized `.env.example` and `config/settings.example.yaml` use RFC 5737 (`192.0.2.x`) placeholders so the repo can be safely published to GitHub.
- **Hierarchical Supervisor & Agent-as-a-Tool (`.as_tool()`):**
  - Root `Supervisor` agent maintains a sliding window conversation buffer (`SlidingWindowConversationManager`) and delegates domain tasks to stateless specialist sub-agents (`HASubAgent` and `InfraSubAgent`).
  - Open/Closed plugin registry (`@register_sub_agent`) allows adding new specialist agents without modifying core orchestrator code.
- **3-Tier Zero-Miss Reflection Store (SQLite + Embeddings):**
  - **Tier 1 (Bounded Context Isolation):** Filters lessons by domain (`ha` vs `infra`).
  - **Tier 2 (Core Invariants):** Lessons marked `is_core_invariant = 1` are **always** injected (0% miss rate).
  - **Tier 3 (Semantic Similarity):** Cosine similarity search via Google GenAI embeddings (`text-embedding-004`) with an automatic zero-RAM local hashed n-gram fallback.
- **Actor-Critic & Deterministic Post-Action State Assertion:**
  - `HomeAssistantWSAdapter` provides rapid sub-second sequential WebSocket loops (`execute_sequential_loop`), allow-listed raw event firing (`fire_custom_ha_event`), and post-action verification (`assert_entity_state` -> `StateAssertionSuccess` / `StateAssertionFailure`).
- **Pre-Condition Guard Clauses & Two-Phase Commit (Human-in-the-Loop):**
  - Blocks out-of-range HVAC setpoints, respects window-open frost protection (`window_open_guard_temp` + contact sensors), and prevents stopping/restarting protected containers or VMs (`ProtectedResourceViolationError`).
  - Intercepts operations listed in `safety.destructive_tools_requiring_approval` via Strands `Confirm` interventions, streams an `intervention_required` SSE packet with `approval_id`, and pauses execution until confirmed via `POST /api/v1/interventions/approve`.

## Directory Structure

```text
homelab-agent-mesh/
├── .env.example
├── .gitignore
├── pyproject.toml
├── docker-compose.yml
├── Dockerfile
├── README.md
├── config/
│   └── settings.example.yaml
└── src/
    ├── __init__.py
    ├── main.py                      # FastAPI SSE entrypoint & lifecycle manager
    ├── core/
    │   ├── __init__.py
    │   ├── config.py                # Pydantic BaseSettings (.env + YAML loader)
    │   ├── events.py                # Typed SSE event models & async queue sink
    │   ├── exceptions.py            # Custom domain & circuit-breaker exceptions
    │   ├── guards.py                # Pre-condition Guard Clauses & safety validators
    │   └── interfaces.py            # Abstract interfaces (BaseStateVerifier, EventEmitter)
    ├── memory/
    │   ├── __init__.py
    │   ├── base.py                  # Abstract Memory Store interface (DIP)
    │   └── reflection_store.py      # 3-Tier SQLite + Semantic Embedding store
    ├── tools/
    │   ├── __init__.py
    │   ├── ha_ws_adapter.py         # Custom Async WebSocket adapter & state asserter
    │   └── infra_mcp_server.py      # FastMCP / MCPServer for Host, Docker & Proxmox
    ├── agents/
    │   ├── __init__.py
    │   ├── factory.py               # Dynamic Strands Agent builder & InterventionHandlers
    │   ├── ha_agent.py              # Home Assistant specialist sub-agent
    │   ├── infra_agent.py           # Homelab Infrastructure specialist sub-agent
    │   └── supervisor.py            # Root Orchestrator with Interventions & Streaming
    └── api/
        ├── __init__.py
        ├── schemas.py               # SSE event & request/response Pydantic models
        └── routes.py                # Streaming /chat/stream & /interventions/approve endpoints
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

