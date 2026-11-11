"""FastAPI SSE Entrypoint & Lifecycle Manager (`src/main.py`).

Wires together:
- `AppConfig` (12-Factor `.env` + `config/settings.yaml` loader)
- `ReflectionStore` (3-Tier SQLite + Semantic Embeddings) with idempotent startup seeding
- `HomeAssistantWSAdapter` (Native Async WebSocket + REST state verifier)
- `HomelabInfraToolkit` (Host telemetry via `psutil`, Docker Engine SDK, Proxmox VE API)
- `AgentFactory` & `SupervisorRuntime` (Hierarchical Strands Agents + HITL Interventions)
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src import __version__
from src.agents.factory import AgentFactory
from src.agents.supervisor import SupervisorRuntime
from src.api.routes import router as api_router
from src.core.config import AppConfig, get_config
from src.memory.base import BaseMemoryStore
from src.memory.reflection_store import ReflectionStore, build_embedder
from src.tools.ha_ws_adapter import HomeAssistantWSAdapter
from src.tools.infra_mcp_server import HomelabInfraToolkit

logger = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )


def create_app(
    config: AppConfig | None = None,
    memory_store: BaseMemoryStore | None = None,
    ha_adapter: HomeAssistantWSAdapter | None = None,
    infra_toolkit: HomelabInfraToolkit | None = None,
    factory: AgentFactory | None = None,
) -> FastAPI:
    """Application factory supporting dependency injection for tests and dry-run modes."""
    cfg = config or get_config()
    configure_logging(cfg.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        store = memory_store or ReflectionStore(
            db_path=cfg.sqlite_db_path,
            embedder=build_embedder(cfg),
            similarity_threshold=cfg.memory.similarity_threshold,
            max_rules_injected=cfg.memory.max_rules_injected,
            local_fallback_dimensions=cfg.memory.local_embedding_dimensions,
        )
        await store.initialize()

        if cfg.memory.seed_lessons:
            seed_tuples = [
                (item.domain, item.rule, item.core) for item in cfg.memory.seed_lessons
            ]
            seeded = await store.seed(seed_tuples)
            if seeded:
                logger.info("reflection_store | seeded %d new lesson(s) from settings.yaml", seeded)

        ha = ha_adapter or HomeAssistantWSAdapter(cfg)
        infra = infra_toolkit or HomelabInfraToolkit(cfg)
        agent_factory = factory or AgentFactory(
            config=cfg,
            memory_store=store,
            ha_adapter=ha,
            infra_toolkit=infra,
        )
        runtime = SupervisorRuntime(
            config=cfg,
            factory=agent_factory,
            memory_store=store,
        )

        app.state.config = cfg
        app.state.memory_store = store
        app.state.ha_adapter = ha
        app.state.infra_toolkit = infra
        app.state.factory = agent_factory
        app.state.runtime = runtime

        logger.info(
            "homelab-agent-mesh v%s started | model=%s | sub_agents=%s",
            __version__,
            cfg.llm.model_id,
            list(cfg.agents.sub_agents.keys()),
        )
        try:
            yield
        finally:
            await ha.aclose()
            await infra.aclose()
            logger.info("homelab-agent-mesh shutdown complete")

    app = FastAPI(
        title="Homelab & Smart Home Multi-Agent Orchestrator",
        version=__version__,
        lifespan=lifespan,
    )

    if cfg.server.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cfg.server.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(api_router)
    return app


def run() -> None:
    """CLI entrypoint (`homelab-orchestrator`)."""
    cfg = get_config()
    uvicorn.run(
        "src.main:create_app",
        factory=True,
        host=cfg.app_host,
        port=cfg.app_port,
        log_level=cfg.log_level.lower(),
    )


if __name__ == "__main__":
    run()

