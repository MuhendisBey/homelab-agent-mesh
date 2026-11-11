# Architecture Specification & Engineering Guide (v1.0)
*Homelab & Smart Home Multi-Agent Mesh (`homelab-agent-mesh`)*

---

## 1. System Overview & Context (C4 Level 1)

`homelab-agent-mesh` is a production-grade, hierarchical multi-agent orchestrator designed to safely and deterministically control a private smart home and homelab cluster. It integrates large language models (**Strands Agents SDK + Google Gemini**), physical home automation (**Home Assistant via Native WebSocket + REST + MCP**), and system infrastructure (**Docker Engine + Host Telemetry + Proxmox VE**).

```mermaid
flowchart TD
    User["User / Mobile Client / Automation"] -->|"HTTP POST /chat/stream"| Gateway["FastAPI Streaming Gateway"]
    Gateway -->|"Real-Time SSE Events"| User

    subgraph Mesh["homelab-agent-mesh Container (GMKtec NucBox 3)"]
        Gateway --> Supervisor["Supervisor Orchestrator (Strands Harness)"]
        Supervisor --> Memory[("3-Tier Reflection Store\n(SQLite + Embeddings)")]
        Supervisor --> HASubAgent["Home Assistant Sub-Agent"]
        Supervisor --> InfraSubAgent["Infrastructure Sub-Agent"]
    end

    HASubAgent -->|"WebSocket (ws://) & REST"| HA["Home Assistant Core"]
    HASubAgent -.->|"SSE Stream"| HAMCP["External ha-mcp Server"]
    InfraSubAgent -->|"Unix Socket"| DockerSock["/var/run/docker.sock"]
    InfraSubAgent -->|"REST HTTPS API"| PVE["Proxmox VE Cluster"]
    Supervisor -->|"Google GenAI API"| Gemini["Google Gemini API (AI Studio)"]
```

---

## 2. Deployment Architecture (C4 Level 2)

The mesh runs inside an isolated Docker container on a **GMKtec NucBox 3** (or Proxmox LXC container) with strict resource reservations and read-only volume mounts adhering to the **12-Factor App** methodology.

```mermaid
flowchart LR
    subgraph Host["Physical Host (GMKtec NucBox 3 / Proxmox LXC)"]
        subgraph Filesystem["Host Mounts & Sockets"]
            DockerSock["/var/run/docker.sock\n(Docker Daemon Socket)"]
            YAMLConf["config/settings.yaml\n(Topology & Safety Bounds)"]
            DataDir["data/\n(reflections.db & WAL journal)"]
            EnvFile[".env\n(Secrets & Endpoints)"]
        end

        subgraph Container["homelab-agent-mesh Container"]
            direction TB
            Runtime["Python 3.12+ (asyncio)\nStrands Agents SDK & FastAPI"]
            Limits["Resource Limits:\n• Memory Limit: 128 MB RAM\n• Target RSS: <80 MB RAM\n• CPU: Unconstrained"]
        end

        DockerSock -->|"Mounted (ro)"| Container
        YAMLConf -->|"Mounted (ro)"| Container
        DataDir -->|"Mounted (rw)"| Container
        EnvFile -.->|"Injected via env_file"| Container
    end

    Container -->|"Port 8080 (or 8028)"| LAN["Local Subnet / Tailscale Mesh"]
```

---

## 3. Component Architecture & Task Decomposition (C4 Level 3)

The architecture strictly follows the **Supervisor / Agent-as-a-Tool** pattern. Specialist sub-agents are exposed to the root orchestrator via Strands' native `.as_tool(preserve_context=False)` adapter, enforcing stateless, isolated turns while propagating human intervention interruptions to the parent.

```mermaid
classDiagram
    class SupervisorAgent {
        +SlidingWindowConversationManager memory
        +SafetyAndApprovalGate gate
        +SupervisorLifecycleHooks hooks
        +stream_async(prompt)
    }

    class HASubAgent {
        +DomainMemoryAndCriticHooks memory_hooks
        +SafetyAndApprovalGate safety_gate
        +execute_sequential_loop()
        +call_ha_service()
        +assert_entity_state()
        +fire_custom_ha_event()
    }

    class InfraSubAgent {
        +DomainMemoryAndCriticHooks memory_hooks
        +SafetyAndApprovalGate safety_gate
        +get_host_telemetry()
        +list_docker_containers()
        +manage_container()
        +list_proxmox_resources()
        +manage_proxmox_guest()
    }

    class BaseStateVerifier {
        <<interface>>
        +get_state(entity_id) EntityState
        +assert_state(entity_id, expected_state) StateAssertionResult
    }

    class HomeAssistantWSAdapter {
        -ClientConnection ws_session
        -httpx.AsyncClient http_client
        -CircuitBreaker breaker
        +execute_sequential_loop()
        +assert_state()
    }

    class HomelabInfraToolkit {
        -DockerClient docker
        -httpx.AsyncClient proxmox_client
        -CircuitBreaker docker_breaker
        +get_host_telemetry()
        +manage_container()
    }

    SupervisorAgent --> HASubAgent : "delegates via .as_tool()"
    SupervisorAgent --> InfraSubAgent : "delegates via .as_tool()"
    HASubAgent ..|> BaseStateVerifier : "validates post-execution"
    HASubAgent --> HomeAssistantWSAdapter : "executes commands"
    InfraSubAgent --> HomelabInfraToolkit : "executes telemetry/control"
    HomeAssistantWSAdapter ..|> BaseStateVerifier : "implements"
```

---

## 4. 3-Tier Zero-Miss Reflection Store Architecture

To eliminate LLM amnesia and prevent repeat failures (such as fighting frost protection automations or hitting dead endpoints), reflections are persisted into an ACID-compliant SQLite store with a **3-Tier retrieval pipeline**:

```mermaid
flowchart TD
    Query["Incoming Task / Sub-Agent Prompt"] --> Tier1["Tier 1: Bounded-Context Domain Filter\n(WHERE domain = 'ha' OR domain = 'infra')"]
    
    Tier1 --> Split{"Partition Rules"}
    
    Split -->|"is_core_invariant = 1"| Tier2["Tier 2: Core Invariants\n(Always Injected unconditionally • 0% Miss Rate)"]
    Split -->|"is_core_invariant = 0"| Tier3["Tier 3: Semantic Vector Match\n(Embed Query via Gemini text-embedding / Local N-gram)"]
    
    Tier3 --> Cosine["Compute Cosine Similarity:\nsim = (Q · R) / (|Q| · |R|)"]
    Cosine --> Threshold{"Similarity >= threshold?\n(e.g. >= 0.70)"}
    Threshold -->|"Yes"| SemanticMatches["Ranked Semantic Matches"]
    Threshold -->|"No"| Discard["Discarded"]
    
    Tier2 --> Assembler["Prompt Assembler & Deduplicator"]
    SemanticMatches --> Assembler
    Assembler --> SystemPrompt["Sub-Agent Injected System Prompt:\n## Lessons learned (MANDATORY)\n- [CORE INVARIANT] ...\n- Semantic rule ..."]
```

### Entity-Relationship Diagram

```mermaid
erDiagram
    LESSONS_LEARNED {
        int id PK "Auto Increment"
        string domain "Bounded context ('ha' | 'infra')"
        int is_core_invariant "1 = Core Invariant, 0 = Semantic"
        string rule_content "Actionable rule text"
        string embedding_model "Model tag (e.g. 'gemini-embedding-001')"
        text embedding_json "Normalized vector array [float]"
        timestamp created_at "Default CURRENT_TIMESTAMP"
    }
```

---

## 5. End-to-End Sequence Diagrams

### 5.1. Multi-Intent Decomposition & Actor-Critic Execution

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant Gateway as FastAPI Gateway (/chat/stream)
    participant Supervisor as Supervisor Agent
    participant HASub as Home Assistant Sub-Agent
    participant Adapter as HomeAssistantWSAdapter
    participant HA as Home Assistant Core

    User->>Gateway: POST /chat/stream ("Turn on living room TV & verify")
    Gateway-->>User: SSE: {"type": "session", "session_id": "..."}
    
    Gateway->>Supervisor: stream_async(prompt)
    Supervisor->>Supervisor: Plan Step 1: Delegate to home_assistant_agent
    Supervisor-->>Gateway: SSE: {"type": "plan_step", "step": 1, "detail": "Delegating to home_assistant_agent..."}
    Gateway-->>User: SSE: {"type": "plan_step", ...}

    Supervisor->>HASub: invoke as tool (prompt)
    HASub->>HASub: Hook: Inject domain='ha' memory (Tier 1+2+3)
    HASub-->>Gateway: SSE: {"type": "memory_injected", "core_rules": 1, ...}
    Gateway-->>User: SSE: {"type": "memory_injected", ...}

    HASub->>Adapter: call_ha_service(domain="media_player", service="turn_on", entity_id="media_player.living_room_tv")
    Adapter->>HA: WS Command: {"type": "call_service", ...}
    HA-->>Adapter: WS Result: {"success": true}
    Adapter-->>HASub: {"status": "dispatched"}

    Note over HASub,Adapter: Actor-Critic Mandatory Post-Execution Verification
    HASub->>Adapter: assert_entity_state(entity_id="media_player.living_room_tv", expected_state="on")
    Adapter->>HA: REST GET /api/states/media_player.living_room_tv
    HA-->>Adapter: {"state": "on", ...}
    Adapter-->>HASub: StateAssertionSuccess(verified=True, actual_state="on")
    HASub-->>Gateway: SSE: {"type": "state_assertion", "verified": true, "actual_state": "on"}
    Gateway-->>User: SSE: {"type": "state_assertion", ...}

    HASub-->>Supervisor: Tool Result: "TV turned on and verified in state 'on'."
    Supervisor-->>Gateway: SSE: {"type": "content_delta", "text": "Living room TV was turned on and confirmed."}
    Gateway-->>User: SSE: {"type": "content_delta", ...}
    Supervisor-->>Gateway: SSE: {"type": "done", "stop_reason": "end_turn"}
    Gateway-->>User: SSE: {"type": "done", ...}
```

---

### 5.2. Two-Phase Commit: Human-in-the-Loop Intervention Flow

Destructive operations (defined in `safety.destructive_tools_requiring_approval`) cannot proceed autonomously. Execution is suspended in-flight, an authorization token is issued, and execution resumes only upon explicit approval.

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant Gateway as FastAPI Gateway
    participant Supervisor as Supervisor Agent
    participant InfraSub as Infrastructure Sub-Agent
    participant Gate as SafetyAndApprovalGate (Intervention)
    participant Toolkit as HomelabInfraToolkit
    participant Docker as Docker Engine

    User->>Gateway: POST /chat/stream ("Restart immich container")
    Gateway->>Supervisor: stream_async(prompt)
    Supervisor->>InfraSub: invoke("Restart immich container")
    InfraSub->>Gate: before_tool_call(manage_container, action="restart")
    
    Gate->>Gate: Match operation against destructive_tools_requiring_approval
    Note over Gate: Matched: "restart_container" -> Suspend Execution!
    Gate-->>InfraSub: Confirm(prompt=JSON, evaluate=_evaluate_approval)
    InfraSub-->>Supervisor: ToolInterruptEvent (propagated from sub-agent)
    
    Supervisor->>Supervisor: Pause event loop & issue approval_id
    Supervisor-->>Gateway: SSE: {"type": "intervention_required", "approval_id": "token_abc123", "operation": "restart_container"}
    Supervisor-->>Gateway: SSE: {"type": "done", "stop_reason": "interrupt", "pending_approvals": ["token_abc123"]}
    Gateway-->>User: SSE: {"type": "intervention_required", ...}

    Note over User,Gateway: Human Decision Window (Expires in safety.approval_timeout_sec)

    User->>Gateway: POST /interventions/approve {"approval_id": "token_abc123", "approved": true}
    Gateway->>Supervisor: resume from interrupt {"approved": true}
    Supervisor->>InfraSub: resume turn
    Gate-->>InfraSub: Proceed (confirmation approved)
    
    InfraSub->>Toolkit: manage_container(container_name="immich", action="restart")
    Toolkit->>Docker: container.restart(timeout=10)
    Docker-->>Toolkit: OK
    Toolkit-->>InfraSub: {"container": "immich", "action": "restart", "current_status": "running", "verified": true}
    
    InfraSub-->>Supervisor: Tool Result: "immich was restarted and verified running."
    Supervisor-->>Gateway: SSE: {"type": "content_delta", "text": "Container immich was restarted successfully after your approval."}
    Gateway-->>User: Real-Time SSE Completion
```

---

### 5.3. Pre-Condition Guard Clause Short-Circuit (Window Open Protection)

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant Gateway as FastAPI Gateway
    participant Supervisor as Supervisor Agent
    participant HASub as Home Assistant Sub-Agent
    participant Guard as HvacSetpointGuard
    participant Adapter as HomeAssistantWSAdapter

    User->>Gateway: POST /chat/stream ("Set living room heating to 22.5C")
    Gateway->>Supervisor: stream_async(prompt)
    Supervisor->>HASub: invoke("Set living room heating to 22.5C")
    
    HASub->>Guard: validate_intent(climate.main_thermostat, temp=22.5)
    Guard->>Adapter: get_state("binary_sensor.living_room_window_contact")
    Adapter-->>Guard: EntityState(state="on") [WINDOW OPEN]
    
    Note over Guard: Window is OPEN: override would waste energy & break Memento!
    Guard-->>HASub: raise WindowOpenProtectionError("Window sensor is open; heating changes blocked.")
    
    HASub-->>Supervisor: Tool Result Error: "WindowOpenProtectionError"
    Supervisor-->>Gateway: SSE: {"type": "content_delta", "text": "Cannot adjust heating: Living room window is currently open (frost protection active)."}
    Gateway-->>User: Final safe response (No physical command was dispatched)
```

---

## 6. Circuit Breaker & Resilience State Machine

To prevent infinite retry loops or network storms when Home Assistant, Docker, or Proxmox VE become temporarily unreachable, all external adapters run behind a `CircuitBreaker`.

```mermaid
stateDiagram-v2
    [*] --> Closed : System Boot

    Closed --> Closed : Successful Call (failures = 0)
    Closed --> Open : Consecutive Failures >= failure_threshold (e.g. 3)

    state Open {
        [*] --> Cooldown : Start Timer (reset_timeout_sec)
        Cooldown --> Cooldown : Fast Fail (raise CircuitOpenError immediately)
    }

    Open --> HalfOpen : Cooldown Expired (t >= reset_timeout_sec)

    state HalfOpen {
        [*] --> Probe : Allow 1 Trial Request
        Probe --> ProbeSuccess : Success
        Probe --> ProbeFailure : Failure
    }

    ProbeSuccess --> Closed : Circuit Closed (failures reset to 0)
    ProbeFailure --> Open : Probe Failed (Timer restarted)
```

---

## 7. Configuration Sourcing & Precedence (12-Factor App)

Configuration loading is strictly ordered by precedence using `pydantic-settings`:

```mermaid
flowchart TD
    EnvVars["1. Process Environment Variables\n(e.g. LLM__TEMPERATURE=0.0)"] -->|"Overrides"| DotEnv[".env File (Gitignored)\nSecrets: GEMINI_API_KEY, HA_ACCESS_TOKEN, PROXMOX_SECRET"]
    DotEnv -->|"Overrides"| YAML["config/settings.yaml (Gitignored)\nTopology: climate_zones, protected_containers, aliases"]
    YAML -->|"Overrides"| Defaults["Code Base Models / Safety Bounds"]
    
    Defaults --> AppConfig["Validated AppConfig Singleton (get_config())"]
```

---

## 8. Summary of Non-Negotiable Architectural Guarantees

| Guarantee | Mechanism | Verification Method |
| :--- | :--- | :--- |
| **Zero Secret Leakage** | `pydantic-settings` `SecretStr` + strict `.gitignore` | Automated diff scan + pre-push audit |
| **No Unauthorized Destructive Ops** | Strands `InterventionHandler` Two-Phase Commit | Automated test `test_sse_chat_and_two_phase_commit_intervention` |
| **Zero-Miss Critical Rules** | SQLite Reflection Store with `is_core_invariant = 1` | Automated test `test_reflection_store_three_tier_retrieval` |
| **No Hallucinated Actions** | Post-execution `assert_entity_state` polling/subscription | `StateAssertionSuccess` / `StateAssertionFailure` audit |
| **Runaway Action Prevention** | Loop bounds (`max_loop_iterations`, `min_loop_delay_ms`) | Pre-condition checks in `GuardRails` |
| **Low Resource Footprint** | Multi-stage Docker build, zero heavy vector DB, local embed fallback | Running RSS benchmark (<80 MB RAM) |
