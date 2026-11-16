"""FastAPI SSE Entrypoint & Lifecycle Manager (`src/main.py`).

Wires together:
- `AppConfig` (12-Factor `.env` + `config/settings.yaml` loader)
- `ReflectionStore` (3-Tier SQLite + Semantic Embeddings) with idempotent startup seeding
- `HomeAssistantWSAdapter` (Native Async WebSocket + REST state verifier)
- `HomelabInfraToolkit` (Host telemetry via `psutil`, Docker Engine SDK, Proxmox VE API)
- `AgentFactory` & `SupervisorRuntime` (Hierarchical Strands Agents + HITL Interventions)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src import __version__
from src.agents.factory import AgentFactory
from src.agents.supervisor import SupervisorRuntime
from src.agents.thermal_agent import build_standalone_thermal_advisor_runner
from src.api.routes import router as api_router
from src.core.announcer import Announcer
from src.core.config import AppConfig, get_config
from src.core.entity_resolver import EntityResolver
from src.core.scheduler import ActionScheduler
from src.memory.base import BaseMemoryStore
from src.memory.reflection_store import ReflectionStore, build_embedder
from src.tools.ha_ws_adapter import HomeAssistantWSAdapter
from src.tools.infra_mcp_server import HomelabInfraToolkit
from src.tools.thermal_tools import ThermalTwinService

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
    entity_resolver: EntityResolver | None = None,
    scheduler: ActionScheduler | None = None,
    announcer: Announcer | None = None,
    thermal_service: ThermalTwinService | None = None,
) -> FastAPI:
    """Application factory supporting dependency injection for tests and dry-run modes."""
    cfg = config or get_config()
    configure_logging(cfg.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        embedder = build_embedder(cfg)
        store = memory_store or ReflectionStore(
            db_path=cfg.sqlite_db_path,
            embedder=embedder,
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
        resolver = entity_resolver or EntityResolver(
            config=cfg,
            db_path=cfg.sqlite_db_path,
            ha_backend=ha,
            embedder=embedder,
        )
        if ha.entity_resolver is None:
            ha.attach_entity_resolver(resolver)
        await resolver.start()

        sched = scheduler or ActionScheduler(
            db_path=cfg.sqlite_db_path,
            ha_adapter=ha,
        )
        await sched.start()

        ann = announcer or Announcer(
            config=cfg,
            ha_adapter=ha,
            entity_resolver=resolver,
        )
        sched.add_event_listener(ann.on_schedule_event)
        voice_tasks: set[asyncio.Task[Any]] = set()

        infra = infra_toolkit or HomelabInfraToolkit(cfg)
        thermal: ThermalTwinService | None = None
        if cfg.thermal.enabled:
            thermal = thermal_service or ThermalTwinService(
                config=cfg,
                ha_adapter=ha,
                entity_resolver=resolver,
            )
            await thermal.initialize()

        agent_factory = factory or AgentFactory(
            config=cfg,
            memory_store=store,
            ha_adapter=ha,
            infra_toolkit=infra,
            entity_resolver=resolver,
            scheduler=sched,
            thermal_service=thermal,
        )
        if thermal is not None:
            if thermal.advisor_runner is None:
                thermal.set_advisor_runner(
                    build_standalone_thermal_advisor_runner(cfg, agent_factory, thermal)
                )
            await thermal.start()

        runtime = SupervisorRuntime(
            config=cfg,
            factory=agent_factory,
            memory_store=store,
        )

        app.state.config = cfg
        app.state.memory_store = store
        app.state.ha_adapter = ha
        app.state.entity_resolver = resolver
        app.state.scheduler = sched
        app.state.announcer = ann
        app.state.voice_tasks = voice_tasks
        app.state.infra_toolkit = infra
        app.state.thermal_service = thermal
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
            for pending_task in list(voice_tasks):
                pending_task.cancel()
            if voice_tasks:
                await asyncio.gather(*voice_tasks, return_exceptions=True)
            if thermal is not None:
                await thermal.stop()
                await thermal.aclose()
            await sched.stop()
            await resolver.stop()
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

