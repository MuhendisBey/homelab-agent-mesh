# Contributing to homelab-agent-mesh

Thank you for your interest in contributing to **`homelab-agent-mesh`**! 

This project is an open-source, production-hardened **Hierarchical Multi-Agent Orchestrator** for modern smart homes and self-hosted homelabs. It manages physical hardware, Docker containers, Proxmox virtualization, and thermodynamic digital twins using **Python 3.12+ (`asyncio`)**, the **Strands Agents SDK**, and **FastAPI Server-Sent Events (SSE)**.

Whether you are a human engineer, an open-source contributor, or an **AI coding agent** collaborating on this codebase, this document outlines our engineering standards, design invariants, and contribution workflows.

---

## 1. Core Engineering Principles & Invariants

Every change to this codebase must adhere to these five non-negotiable principles:

1. **12-Factor App & Zero Hardcoding:**
   - **Never** hardcode IP addresses, ports, API tokens, entity IDs, model names, temperature boundaries, room names, or container names in Python code.
   - All secrets and network endpoints load via `pydantic-settings` from `.env`.
   - All topology, agent parameters, safety bounds, and aliases load from `config/settings.yaml`.
   - Documentation and examples must exclusively use **RFC 5737 TEST-NET-1** placeholder IPs (`192.0.2.x`).

2. **Defense in Depth (Guard Clauses & HITL):**
   - Safety checks operate at both the local tool level (`src/core/guards.py`) and the Strands agent intervention level (`src/agents/factory.py`).
   - Destructive operations (`restart_container`, `reboot_node`, `delete_automation`, etc.) **must** trigger Strands `Confirm` interventions and require human approval via `/api/v1/interventions/approve`.
   - HVAC setpoints outside safe ranges (`15.0°C` – `28.0°C`) or attempted heating when windows are open must fail fast with custom domain exceptions.

3. **Actor-Critic Physical Verification (`assert_entity_state`):**
   - When commanding physical or infrastructural state changes, a tool must not report success simply because an API call succeeded.
   - Physical state must be verified using `assert_entity_state` before returning a success verdict (`StateAssertionSuccess` / `StateAssertionFailure`).

4. **Lean Resource Footprint (<110 MB RSS RAM):**
   - The runtime is optimized for lightweight edge nodes (e.g. Proxmox LXC on GMKtec NucBox 3).
   - Avoid heavy dependencies, unnecessary sidecars, or heavy vector databases. We use optimized SQLite WAL with LRU caching and hashed n-gram fallbacks.

5. **Dual-Repository Separation & Strict Secret Hygiene:**
   - The public repository (`MuhendisBey/homelab-agent-mesh`) contains **only** application code, sanitized configs, unit tests, and CI/CD pipelines.
   - Private homelab topology, real device registries, SOPS/age encrypted secrets (`secrets.env.sops`), and `compose.yaml` live in the private operations repository (`MuhendisBey/homelab-ops`).
   - Never commit `.env`, `config/settings.yaml`, `config/thermal_overrides.yaml`, `data/*.db`, or `data/neonplan.json`.

---

## 2. Note for AI Coding Agents & Pair-Programmers

This repository is **designed from the ground up to be developed, maintained, and operated with AI coding agents** (Antigravity, Cursor, Claude Code, Codex, etc.).

If you are an AI agent working in this codebase:

> [!IMPORTANT]
> **Read `AGENTS.md` First:**  
> The file [`AGENTS.md`](AGENTS.md) is the living operational manual and architectural ground truth. Review it at the start of every session to understand module contracts, recent features, and current session goals.

### Agent Guidelines:
- **Do Not Guess Entity IDs or IPs:** Never synthesize imaginary entity IDs or real LAN IPs. Use `EntityResolver` and environment-driven configurations.
- **Strict Typing:** All new code must be fully type-annotated to pass `mypy --strict src tests` with zero errors.
- **Run the Test Suite:** Always execute `pytest -v` and `ruff check src tests` before concluding your turn.
- **Preserve Clean Commits:** Use Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`, `perf:`, `test:`).
- **Sub-Agent Extensions:** When adding new capabilities, register them modularly using the `@register_sub_agent` decorator in `src/agents/` rather than bloating the `Supervisor`.

---

## 3. Development Setup

### Prerequisites
- Python 3.12 or newer
- Git
- Docker & Docker Compose (optional, for containerized testing)

### Local Environment Setup

```bash
# 1. Clone the repository
git clone https://github.com/MuhendisBey/homelab-agent-mesh.git
cd homelab-agent-mesh

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies in editable mode with development tools
pip install --upgrade pip
pip install -e . pytest pytest-asyncio ruff mypy types-PyYAML

# 4. Prepare local configuration templates
cp .env.example .env
cp config/settings.example.yaml config/settings.yaml
cp config/thermal_overrides.example.yaml config/thermal_overrides.yaml
```

---

## 4. Quality Verification & Testing

Every pull request and commit is verified by GitHub Actions (`.github/workflows/ci.yml`). Ensure your local branch passes all checks before submitting:

```bash
# Run Ruff linter and code formatter checks
ruff check --select E,F,I,UP --ignore E501 src tests

# Run Mypy strict type checking
mypy --strict src tests

# Run the full automated test suite
pytest -v

# Run secret hygiene check
forbidden=$(git ls-files | grep -E '(^|/)\.env(\.[^/]+)?$|(^|/)config/settings\.yaml$|\.(db|sqlite|sqlite3)$' | grep -vE '\.env\.example$|settings\.example\.yaml$' || true)
if [ -n "$forbidden" ]; then
  echo "Forbidden secret or database file tracked: $forbidden"
  exit 1
fi
```

---

## 5. Architectural Directory Guide

When contributing, place new logic in the appropriate layer:

- `src/core/`: Protocol interfaces, config loaders, custom exceptions, safety guard clauses, persistent scheduler, Turkish morphology (`text_tr.py`), and psychrometric/thermal physics engines.
- `src/memory/`: 3-tier `ReflectionStore`, CoALA semantic memory, and SQLite WAL `ThermalStore`.
- `src/tools/`: Adapters and Strands toolkits (`HomeAssistantWSAdapter`, `InfraMcpServer`, `ScheduleTools`, `ThermalTwinService`, `HAHistoryClient`).
- `src/agents/`: Multi-agent definitions:
  - `supervisor.py`: Root decomposition and HITL orchestrator.
  - `ha_agent.py`: Home Assistant specialist.
  - `infra_agent.py`: Docker/Proxmox homelab infrastructure specialist.
  - `thermal_agent.py`: Thermodynamic spatial digital twin specialist.
  - `factory.py`: `@register_sub_agent` registry and agent builders.
- `src/api/`: FastAPI SSE endpoints (`/chat/stream`, `/voice`, `/interventions`, `/entities`, `/schedules`) and Pydantic schemas.
- `tests/`: End-to-end integration tests and physics/morphology unit tests.

---

## 6. Submitting a Pull Request

1. Fork the repo and create a feature branch (`git checkout -b feat/my-new-feature`).
2. Write clean, modular code with accompanying unit tests in `tests/`.
3. Verify that all tests, type checks, and linters pass (`pytest -v`, `mypy --strict src tests`, `ruff check`).
4. Commit your changes following Conventional Commits format.
5. Push your branch and open a Pull Request against `main`.
6. Ensure GitHub Actions CI passes completely.

Thank you for helping make open-source homelabs smarter, safer, and more autonomous! 🚀
