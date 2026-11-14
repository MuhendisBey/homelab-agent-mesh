"""Configuration & secret isolation (12-factor).

Two layered sources are merged by ``pydantic-settings`` (highest priority first):

1. Process environment variables (e.g. ``LLM__MODEL_ID`` overrides ``llm.model_id``).
2. The ``.env`` file (secrets & endpoints only - never committed).
3. ``config/settings.yaml`` (domain topology, aliases, safety bounds, agent parameters).

There are intentionally **no environment-specific defaults** in this module: model IDs,
temperatures, timeouts, thresholds, URLs and entity IDs MUST come from ``.env`` /
``settings.yaml``. Missing values fail fast at startup with a validation error.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

DomainName = str
"""Bounded-context identifier used by the reflection store (e.g. ``"ha"`` / ``"infra"``)."""


class _StrictModel(BaseModel):
    """Base for YAML sections: unknown keys are rejected to catch typos early."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# YAML sections
# ---------------------------------------------------------------------------


class LLMSettings(_StrictModel):
    """Default LLM parameters shared by all agents (overridable per agent)."""

    model_id: str
    embedding_model_id: str
    temperature: float = Field(ge=0.0, le=1.0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    max_retries: int = Field(ge=0, le=10)
    retry_initial_delay_sec: int = Field(ge=0)
    retry_max_delay_sec: int = Field(ge=0)


class ClimateZone(_StrictModel):
    """Associates a thermostat/TRV with the window sensors that protect it."""

    climate_entity: str
    window_sensors: list[str] = Field(default_factory=list)


class SafetyBounds(_StrictModel):
    """Pre-condition guard clause parameters & human-approval policy."""

    min_hvac_temp: float
    max_hvac_temp: float
    window_open_guard_temp: float
    climate_zones: list[ClimateZone] = Field(default_factory=list)
    protected_containers: list[str] = Field(default_factory=list)
    protected_container_actions: list[str] = Field(min_length=1)
    protected_vm_ids: list[int] = Field(default_factory=list)
    protected_guest_actions: list[str] = Field(min_length=1)
    destructive_tools_requiring_approval: list[str] = Field(default_factory=list)
    approval_timeout_sec: int = Field(gt=0)
    max_tool_calls_per_task: int = Field(gt=0)
    max_loop_iterations: int = Field(gt=0)
    min_loop_delay_ms: int = Field(ge=0)
    max_loop_delay_ms: int = Field(gt=0)
    max_assert_timeout_sec: float = Field(gt=0)
    allowed_custom_event_patterns: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_ranges(self) -> SafetyBounds:
        if self.min_hvac_temp >= self.max_hvac_temp:
            raise ValueError("safety.min_hvac_temp must be lower than safety.max_hvac_temp")
        if self.min_loop_delay_ms > self.max_loop_delay_ms:
            raise ValueError("safety.min_loop_delay_ms must be <= safety.max_loop_delay_ms")
        return self


class CircuitBreakerSettings(_StrictModel):
    """Backend circuit breaker (HA WebSocket, Docker, Proxmox)."""

    failure_threshold: int = Field(gt=0)
    reset_timeout_sec: float = Field(gt=0)


class SubAgentSettings(_StrictModel):
    """Per sub-agent parameters. ``model_id`` / ``temperature`` fall back to ``llm.*``."""

    enabled: bool = True
    name: str
    tool_name: str = Field(pattern=r"^[a-zA-Z0-9_\-]{1,64}$")
    description: str
    domain: DomainName
    model_id: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=1.0)
    extra_instructions: str = ""


class SupervisorSettings(_StrictModel):
    """Root orchestrator parameters."""

    name: str
    window_size: int = Field(gt=0, description="Sliding window size (messages) of short-term memory.")
    model_id: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=1.0)
    extra_instructions: str = ""


class AgentsSettings(_StrictModel):
    """Agent topology. ``sub_agents`` keys must match a registered sub-agent plugin key."""

    supervisor: SupervisorSettings
    sub_agents: dict[str, SubAgentSettings]
    parallel_tool_execution: bool = False


class SeedLesson(_StrictModel):
    """A lesson inserted idempotently into the reflection store at startup."""

    domain: DomainName
    rule: str
    core: bool = False


class MemorySettings(_StrictModel):
    """3-tier reflection store parameters."""

    similarity_threshold: float = Field(ge=-1.0, le=1.0)
    embedding_backend: Literal["gemini", "local"]
    local_embedding_dimensions: int = Field(gt=0)
    max_rules_injected: int = Field(gt=0)
    allow_agent_core_invariants: bool = False
    agent_min_occurrences: int = Field(
        default=2,
        ge=1,
        description="Number of times an agent must observe a non-core lesson before it activates in retrieval.",
    )
    seed_lessons: list[SeedLesson] = Field(default_factory=list)


class SessionSettings(_StrictModel):
    """In-memory supervisor session registry."""

    max_sessions: int = Field(gt=0)
    idle_ttl_sec: int = Field(gt=0)


class HAMCPSettings(_StrictModel):
    """External ``ha-mcp`` server client parameters."""

    enabled: bool = True
    transport: Literal["sse", "streamable_http"]
    startup_timeout_sec: int = Field(gt=0)
    tool_allowlist: list[str] = Field(default_factory=list)
    tool_denylist: list[str] = Field(default_factory=list)


class HomeAssistantSettings(_StrictModel):
    """Home Assistant adapter parameters & topology."""

    ws_open_timeout_sec: float = Field(gt=0)
    ws_command_timeout_sec: float = Field(gt=0)
    rest_timeout_sec: float = Field(gt=0)
    default_assert_timeout_sec: float = Field(gt=0)
    default_poll_interval_ms: int = Field(gt=0)
    numeric_tolerance: float = Field(ge=0)
    verify_ssl: bool = True
    mutating_tools: list[str] = Field(
        default_factory=list,
        description="Tool names (native or ha-mcp) that change physical state and require verification.",
    )
    entity_aliases: dict[str, str] = Field(default_factory=dict)
    mcp: HAMCPSettings


class InfraMCPServerSettings(_StrictModel):
    """Standalone infra MCP server (optional - the infra agent uses the tools in-process)."""

    name: str
    transport: Literal["stdio", "sse", "streamable-http"]
    host: str
    port: int = Field(gt=0, lt=65536)
    expose_destructive_tools: bool = False


class InfraSettings(_StrictModel):
    """Host / Docker / Proxmox toolkit parameters."""

    disk_mounts: list[str] = Field(min_length=1)
    cpu_sample_interval_sec: float = Field(gt=0)
    docker_stats_concurrency: int = Field(gt=0)
    docker_timeout_sec: int = Field(gt=0)
    container_stop_timeout_sec: int = Field(ge=0)
    proxmox_timeout_sec: float = Field(gt=0)
    mcp_server: InfraMCPServerSettings


class ServerSettings(_StrictModel):
    """HTTP gateway parameters that are not secrets."""

    cors_origins: list[str] = Field(default_factory=list)
    sse_keepalive_sec: float = Field(gt=0)


class VoiceSettings(_StrictModel):
    """Voice command gateway, TTS, and HomePod AirPlay announcer parameters."""

    enabled: bool = True
    wake_words: list[str] = Field(default_factory=lambda: ["hey kezban", "kezban"])
    sync_timeout_sec: float = Field(default=8.0, gt=0.5, le=60.0)
    language: str = "tr"
    tts_entity: str = "tts.google_translate_en_com"
    tts_service: str = "tts.speak"
    default_speaker: str | None = None
    room_speakers: dict[str, str] = Field(default_factory=dict)
    fallback_notify_service: str | None = None
    notify_title: str = "Kezban"
    announce_scheduled_results: bool = True
    deferred_ack_text: str = "Üzerinde çalışıyorum, tamamlandığında sesli olarak bildireceğim."


# ---------------------------------------------------------------------------
# Root settings
# ---------------------------------------------------------------------------


class _BootstrapSettings(BaseSettings):
    """Resolves only the YAML path so the real settings can register the YAML source."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    yaml_config_path: Path = Path("config/settings.yaml")


class AppConfig(BaseSettings):
    """Root application configuration (``.env`` + ``settings.yaml``)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
        frozen=True,
    )

    # --- Secrets & endpoints (.env) -------------------------------------------------
    gemini_api_key: SecretStr
    ha_base_url: str
    ha_ws_url: str
    ha_access_token: SecretStr
    ha_mcp_sse_url: str | None = None
    ha_mcp_auth_token: SecretStr | None = None
    proxmox_api_url: str | None = None
    proxmox_token_id: str | None = None
    proxmox_token_secret: SecretStr | None = None
    proxmox_verify_ssl: bool = True
    api_bearer_token: SecretStr | None = None
    app_host: str
    app_port: int = Field(gt=0, lt=65536)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    sqlite_db_path: Path = Path("data/reflections.db")
    yaml_config_path: Path = Path("config/settings.yaml")

    # --- Structured configuration (settings.yaml) -------------------------------------
    llm: LLMSettings
    safety: SafetyBounds
    circuit_breaker: CircuitBreakerSettings
    agents: AgentsSettings
    memory: MemorySettings
    sessions: SessionSettings
    home_assistant: HomeAssistantSettings
    infra: InfraSettings
    server: ServerSettings
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    plugins: list[str] = Field(
        default_factory=list,
        description="Extra importable modules that register sub-agents / operation resolvers (OCP).",
    )

    @field_validator("ha_base_url", "proxmox_api_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str | None) -> str | None:
        return value.rstrip("/") if value else value

    @property
    def proxmox_enabled(self) -> bool:
        """True when all Proxmox API credentials are configured."""
        return bool(self.proxmox_api_url and self.proxmox_token_id and self.proxmox_token_secret)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        yaml_path = _BootstrapSettings().yaml_config_path
        if not yaml_path.exists():
            raise FileNotFoundError(
                f"Structured config '{yaml_path}' not found. Copy config/settings.example.yaml "
                "to config/settings.yaml (or set YAML_CONFIG_PATH) and adjust it."
            )
        yaml_source = YamlConfigSettingsSource(settings_cls, yaml_file=yaml_path, deep_merge=True)
        return (init_settings, env_settings, dotenv_settings, yaml_source, file_secret_settings)

    def agent_model_params(self, model_id: str | None, temperature: float | None) -> tuple[str, float]:
        """Resolve per-agent model overrides against the global LLM defaults."""
        return (model_id or self.llm.model_id, self.llm.temperature if temperature is None else temperature)


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """Load (once) and return the validated application configuration."""
    return AppConfig()  # type: ignore[call-arg]  # values are provided by env/.env/YAML sources
