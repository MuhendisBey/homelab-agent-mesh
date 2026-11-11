"""Homelab Infrastructure Toolkit & MCP Server (Host Telemetry, Docker Socket & Proxmox VE).

Enforces strict Guard Clauses:
- Protected containers (`safety.protected_containers`) cannot be stopped, restarted, or paused
  when those actions are in `safety.protected_container_actions` -> raises `ProtectedResourceViolationError`.
- Protected Proxmox VM/LXC IDs (`safety.protected_vm_ids`) cannot be stopped/rebooted
  -> raises `ProtectedResourceViolationError`.
- Registers operation resolvers in `operation_registry` so destructive actions (`restart_container`,
  `stop_container`, `reboot_node`, etc.) trigger the Supervisor/Sub-agent Two-Phase Commit intervention.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any, Literal

import docker
import httpx
import psutil
from docker.errors import DockerException, NotFound
from mcp.server.mcpserver import MCPServer
from strands import tool
from strands.types.tools import AgentTool

from src.core.config import AppConfig, get_config
from src.core.exceptions import (
    DockerBackendError,
    GuardViolationError,
    ProxmoxBackendError,
    ResourceNotFoundError,
)
from src.core.guards import CircuitBreaker, GuardRails, operation_registry

logger = logging.getLogger(__name__)

ContainerAction = Literal["pause", "unpause", "restart", "stop", "start"]
ProxmoxGuestAction = Literal["status", "start", "stop", "shutdown", "reboot"]


def _resolve_manage_container_op(args: Mapping[str, Any]) -> str:
    action = str(args.get("action", "")).strip().lower()
    return f"{action}_container" if action else "manage_container"


def _resolve_proxmox_guest_op(args: Mapping[str, Any]) -> str:
    action = str(args.get("action", "")).strip().lower()
    if action in {"reboot", "stop", "shutdown"}:
        return "reboot_node" if action == "reboot" else f"{action}_guest"
    return f"proxmox_{action}" if action else "manage_proxmox_guest"


operation_registry.register("manage_container", _resolve_manage_container_op)
operation_registry.register("manage_proxmox_guest", _resolve_proxmox_guest_op)


def _compute_cpu_percent(stats: Mapping[str, Any]) -> float:
    cpu_stats = stats.get("cpu_stats") or {}
    precpu_stats = stats.get("precpu_stats") or {}
    cpu_usage = (cpu_stats.get("cpu_usage") or {}).get("total_usage", 0)
    precpu_usage = (precpu_stats.get("cpu_usage") or {}).get("total_usage", 0)
    system_usage = cpu_stats.get("system_cpu_usage", 0)
    presystem_usage = precpu_stats.get("system_cpu_usage", 0)
    cpu_delta = float(cpu_usage - precpu_usage)
    sys_delta = float(system_usage - presystem_usage)
    online_cpus = int(cpu_stats.get("online_cpus") or 1)
    if sys_delta > 0.0 and cpu_delta > 0.0:
        return round((cpu_delta / sys_delta) * online_cpus * 100.0, 2)
    return 0.0


class HomelabInfraToolkit:
    """Adapter over host telemetry (`psutil`), Docker Engine SDK, and Proxmox VE REST API."""

    def __init__(
        self,
        config: AppConfig,
        docker_client: Any | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._infra_cfg = config.infra
        self._guards = GuardRails(config.safety)
        self._docker_client = docker_client
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self._docker_breaker = CircuitBreaker(
            name="docker",
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_sec=config.circuit_breaker.reset_timeout_sec,
        )
        self._proxmox_breaker = CircuitBreaker(
            name="proxmox",
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_sec=config.circuit_breaker.reset_timeout_sec,
        )

    async def aclose(self) -> None:
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    def _get_docker(self) -> Any:
        if self._docker_client is None:
            try:
                self._docker_client = docker.from_env(timeout=self._infra_cfg.docker_timeout_sec)
            except DockerException as exc:
                raise DockerBackendError(f"Unable to connect to Docker socket: {exc}") from exc
        return self._docker_client

    # ------------------------------------------------------------------
    # Tool 1: Host telemetry via psutil
    # ------------------------------------------------------------------

    async def get_host_telemetry(self) -> dict[str, Any]:
        """Collect CPU%, load averages, RAM, swap, disk usage, and thermal sensors."""
        return await asyncio.to_thread(self._collect_host_telemetry_sync)

    def _collect_host_telemetry_sync(self) -> dict[str, Any]:
        cpu_pct = float(psutil.cpu_percent(interval=self._infra_cfg.cpu_sample_interval_sec))
        vm = psutil.virtual_memory()
        swap = psutil.swap_memory()

        try:
            load1, load5, load15 = psutil.getloadavg()
            load_avg: dict[str, float] | None = {
                "1m": round(load1, 2),
                "5m": round(load5, 2),
                "15m": round(load15, 2),
            }
        except (AttributeError, OSError):
            load_avg = None

        disks: dict[str, dict[str, float]] = {}
        for mount in self._infra_cfg.disk_mounts:
            try:
                usage = psutil.disk_usage(mount)
                disks[mount] = {
                    "total_gb": round(usage.total / (1024**3), 2),
                    "used_gb": round(usage.used / (1024**3), 2),
                    "free_gb": round(usage.free / (1024**3), 2),
                    "percent": float(usage.percent),
                }
            except OSError as exc:
                logger.debug("mount=<%s> | disk_usage skipped: %s", mount, exc)

        thermals: dict[str, list[dict[str, Any]]] = {}
        sensors_fn = getattr(psutil, "sensors_temperatures", None)
        if callable(sensors_fn):
            try:
                raw_temps = sensors_fn() or {}
                for chip, entries in raw_temps.items():
                    thermals[chip] = [
                        {
                            "label": entry.label or chip,
                            "current_c": float(entry.current),
                            "high_c": float(entry.high) if entry.high is not None else None,
                            "critical_c": float(entry.critical) if entry.critical is not None else None,
                        }
                        for entry in entries
                    ]
            except Exception as exc:
                logger.debug("sensors_temperatures unavailable: %s", exc)

        return {
            "cpu_percent": cpu_pct,
            "cpu_count_logical": psutil.cpu_count(logical=True),
            "load_avg": load_avg,
            "memory": {
                "total_mb": round(vm.total / (1024**2), 1),
                "available_mb": round(vm.available / (1024**2), 1),
                "used_mb": round(vm.used / (1024**2), 1),
                "percent": float(vm.percent),
            },
            "swap": {
                "total_mb": round(swap.total / (1024**2), 1),
                "used_mb": round(swap.used / (1024**2), 1),
                "percent": float(swap.percent),
            },
            "disks": disks,
            "thermals": thermals,
        }

    # ------------------------------------------------------------------
    # Tool 2: List Docker containers + resource stats
    # ------------------------------------------------------------------

    async def list_docker_containers(self, include_stopped: bool = False) -> list[dict[str, Any]]:
        """List Docker containers with status, CPU%, memory usage, and protection flag."""
        async with self._docker_breaker.guard():
            return await asyncio.to_thread(self._list_containers_sync, include_stopped)

    def _list_containers_sync(self, include_stopped: bool) -> list[dict[str, Any]]:
        client = self._get_docker()
        try:
            containers = client.containers.list(all=include_stopped)
        except DockerException as exc:
            raise DockerBackendError(f"Failed to list Docker containers: {exc}") from exc

        results: list[dict[str, Any]] = []
        for container in containers:
            name = str(getattr(container, "name", "")).lstrip("/")
            status = str(getattr(container, "status", "unknown"))
            cpu_pct = 0.0
            mem_mb = 0.0
            mem_limit_mb = 0.0
            if status == "running":
                try:
                    stats = container.stats(stream=False)
                    cpu_pct = _compute_cpu_percent(stats)
                    mem_stats = stats.get("memory_stats") or {}
                    mem_mb = round(float(mem_stats.get("usage", 0)) / (1024**2), 2)
                    mem_limit_mb = round(float(mem_stats.get("limit", 0)) / (1024**2), 2)
                except Exception as exc:
                    logger.debug("container=<%s> | stats fetch skipped: %s", name, exc)

            results.append(
                {
                    "name": name,
                    "short_id": getattr(container, "short_id", ""),
                    "status": status,
                    "cpu_percent": cpu_pct,
                    "memory_mb": mem_mb,
                    "memory_limit_mb": mem_limit_mb,
                    "protected": self._guards.is_protected_container(name),
                }
            )
        return results

    # ------------------------------------------------------------------
    # Tool 3: Manage container with ProtectedResourceViolationError guard
    # ------------------------------------------------------------------

    async def manage_container(self, container_name: str, action: ContainerAction) -> dict[str, Any]:
        """Pause, unpause, restart, stop, or start a container after enforcing protection guards."""
        clean_name = container_name.strip().lstrip("/")
        clean_action = action.strip().lower()
        if clean_action not in {"pause", "unpause", "restart", "stop", "start"}:
            raise GuardViolationError(
                f"Unsupported container action '{action}'. Allowed: pause, unpause, restart, stop, start.",
                container=clean_name,
                action=action,
            )

        # Mandatory Pre-Condition Guard Clause
        self._guards.ensure_container_action_allowed(clean_name, clean_action)

        async with self._docker_breaker.guard():
            return await asyncio.to_thread(self._manage_container_sync, clean_name, clean_action)

    def _manage_container_sync(self, container_name: str, action: str) -> dict[str, Any]:
        client = self._get_docker()
        try:
            container = client.containers.get(container_name)
        except NotFound as exc:
            raise ResourceNotFoundError(
                f"Docker container '{container_name}' was not found.",
                container=container_name,
            ) from exc
        except DockerException as exc:
            raise DockerBackendError(f"Failed to inspect container '{container_name}': {exc}") from exc

        previous_status = str(getattr(container, "status", "unknown"))
        try:
            if action == "pause":
                container.pause()
            elif action == "unpause":
                container.unpause()
            elif action == "restart":
                container.restart(timeout=self._infra_cfg.container_stop_timeout_sec)
            elif action == "stop":
                container.stop(timeout=self._infra_cfg.container_stop_timeout_sec)
            elif action == "start":
                container.start()
            container.reload()
        except DockerException as exc:
            raise DockerBackendError(
                f"Docker action '{action}' on '{container_name}' failed: {exc}",
                container=container_name,
                action=action,
            ) from exc

        current_status = str(getattr(container, "status", "unknown"))
        expected_map = {
            "pause": "paused",
            "unpause": "running",
            "restart": "running",
            "stop": "exited",
            "start": "running",
        }
        expected_status = expected_map[action]
        return {
            "container": container_name,
            "action": action,
            "previous_status": previous_status,
            "current_status": current_status,
            "verified": current_status == expected_status,
        }

    # ------------------------------------------------------------------
    # Tool 4: Proxmox VE LXC / VM status & guarded power control
    # ------------------------------------------------------------------

    def _get_proxmox_http(self) -> httpx.AsyncClient:
        if not self._config.proxmox_enabled or not self._config.proxmox_api_url:
            raise ProxmoxBackendError("Proxmox VE credentials are not configured in .env.")
        if self._http_client is None:
            secret = (
                self._config.proxmox_token_secret.get_secret_value()
                if self._config.proxmox_token_secret
                else ""
            )
            self._http_client = httpx.AsyncClient(
                base_url=self._config.proxmox_api_url,
                headers={
                    "Authorization": f"PVEAPIToken={self._config.proxmox_token_id}={secret}",
                },
                timeout=self._infra_cfg.proxmox_timeout_sec,
                verify=self._config.proxmox_verify_ssl,
            )
        return self._http_client

    async def list_proxmox_resources(self) -> list[dict[str, Any]]:
        """Query cluster-wide Proxmox LXC/VM status via `/api2/json/cluster/resources`."""
        async with self._proxmox_breaker.guard():
            client = self._get_proxmox_http()
            try:
                resp = await client.get("/api2/json/cluster/resources", params={"type": "vm"})
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise ProxmoxBackendError(f"Proxmox API request failed: {exc}") from exc
            items = (resp.json() or {}).get("data") or []
            protected_ids = set(self._config.safety.protected_vm_ids)
            return [
                {
                    "vmid": int(item.get("vmid", 0)),
                    "name": item.get("name"),
                    "type": item.get("type"),
                    "node": item.get("node"),
                    "status": item.get("status"),
                    "cpu_fraction": round(float(item.get("cpu", 0.0)), 4),
                    "mem_mb": round(float(item.get("mem", 0)) / (1024**2), 1),
                    "maxmem_mb": round(float(item.get("maxmem", 0)) / (1024**2), 1),
                    "protected": int(item.get("vmid", 0)) in protected_ids,
                }
                for item in items
            ]

    async def manage_proxmox_guest(
        self,
        node: str,
        vmid: int,
        guest_type: Literal["lxc", "qemu"],
        action: ProxmoxGuestAction,
    ) -> dict[str, Any]:
        """Query or control a Proxmox LXC/QEMU guest with `protected_vm_ids` guard enforcement."""
        clean_action = action.strip().lower()
        if clean_action not in {"status", "start", "stop", "shutdown", "reboot"}:
            raise GuardViolationError(f"Unsupported Proxmox action '{action}'.", vmid=vmid, action=action)

        self._guards.ensure_guest_action_allowed(vmid=vmid, action=clean_action)

        async with self._proxmox_breaker.guard():
            client = self._get_proxmox_http()
            base_path = f"/api2/json/nodes/{node}/{guest_type}/{vmid}/status"
            try:
                if clean_action == "status":
                    resp = await client.get(f"{base_path}/current")
                else:
                    resp = await client.post(f"{base_path}/{clean_action}")
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise ProxmoxBackendError(
                    f"Proxmox action '{clean_action}' on {guest_type}/{vmid} failed: {exc}",
                    vmid=vmid,
                    action=clean_action,
                ) from exc
            return {
                "node": node,
                "vmid": vmid,
                "guest_type": guest_type,
                "action": clean_action,
                "data": (resp.json() or {}).get("data"),
            }

    # ------------------------------------------------------------------
    # Strands AgentTool builders
    # ------------------------------------------------------------------

    def build_tools(self) -> list[AgentTool]:
        """Build Strands tools for the Infrastructure Sub-Agent."""
        toolkit = self

        @tool(
            name="get_host_telemetry",
            description="Read host CPU%, load average, RAM, swap, disk usage, and thermal sensors via psutil.",
        )
        async def get_host_telemetry() -> dict[str, Any]:
            """Collect live host CPU, memory, swap, disk and temperature telemetry."""
            return await toolkit.get_host_telemetry()

        @tool(
            name="list_docker_containers",
            description="List running (or all) Docker containers with status, CPU%, memory usage, and protection status.",
        )
        async def list_docker_containers(include_stopped: bool = False) -> list[dict[str, Any]]:
            """List Docker containers via the local Docker Engine socket.

            Args:
                include_stopped: When True, include exited/paused containers as well as running ones.
            """
            return await toolkit.list_docker_containers(include_stopped=include_stopped)

        @tool(
            name="manage_container",
            description=(
                "Manage a Docker container (pause, unpause, restart, stop, start). "
                "Protected containers defined in safety.protected_containers are strictly blocked."
            ),
        )
        async def manage_container(container_name: str, action: ContainerAction) -> dict[str, Any]:
            """Execute a lifecycle action on a Docker container.

            Args:
                container_name: Target Docker container name.
                action: One of 'pause', 'unpause', 'restart', 'stop', 'start'.
            """
            return await toolkit.manage_container(container_name=container_name, action=action)

        tools_list: list[AgentTool] = [
            get_host_telemetry,
            list_docker_containers,
            manage_container,
        ]

        if self._config.proxmox_enabled:

            @tool(
                name="list_proxmox_resources",
                description="List Proxmox VE LXC containers and QEMU VMs across the cluster with status and resource usage.",
            )
            async def list_proxmox_resources() -> list[dict[str, Any]]:
                """List all Proxmox VE LXC and QEMU guests."""
                return await toolkit.list_proxmox_resources()

            @tool(
                name="manage_proxmox_guest",
                description="Inspect status or execute start/stop/shutdown/reboot on a Proxmox LXC or QEMU guest.",
            )
            async def manage_proxmox_guest(
                node: str,
                vmid: int,
                guest_type: Literal["lxc", "qemu"],
                action: ProxmoxGuestAction,
            ) -> dict[str, Any]:
                """Control or query a Proxmox VE LXC/VM guest (guarded by safety.protected_vm_ids).

                Args:
                    node: Proxmox node name.
                    vmid: Numeric VM / LXC ID.
                    guest_type: Either 'lxc' or 'qemu'.
                    action: One of 'status', 'start', 'stop', 'shutdown', 'reboot'.
                """
                return await toolkit.manage_proxmox_guest(
                    node=node,
                    vmid=vmid,
                    guest_type=guest_type,
                    action=action,
                )

            tools_list.extend([list_proxmox_resources, manage_proxmox_guest])

        return tools_list


def create_infra_mcp_server(
    config: AppConfig | None = None,
    toolkit: HomelabInfraToolkit | None = None,
) -> MCPServer:
    """Build a standalone MCP server (MCP SDK v2 `MCPServer`, successor to `FastMCP`)."""
    cfg = config or get_config()
    kit = toolkit or HomelabInfraToolkit(cfg)
    mcp_cfg = cfg.infra.mcp_server
    server = MCPServer(name=mcp_cfg.name)

    @server.tool(
        name="get_host_telemetry",
        description="Read CPU, RAM, swap, disk, and thermal telemetry via psutil.",
    )
    async def mcp_get_host_telemetry() -> dict[str, Any]:
        return await kit.get_host_telemetry()

    @server.tool(
        name="list_docker_containers",
        description="List Docker containers with CPU%, RAM usage, and protection status.",
    )
    async def mcp_list_docker_containers(include_stopped: bool = False) -> list[dict[str, Any]]:
        return await kit.list_docker_containers(include_stopped=include_stopped)

    if mcp_cfg.expose_destructive_tools:

        @server.tool(
            name="manage_container",
            description="Pause, unpause, restart, stop, or start a non-protected Docker container.",
        )
        async def mcp_manage_container(container_name: str, action: ContainerAction) -> dict[str, Any]:
            return await kit.manage_container(container_name=container_name, action=action)

    return server


if __name__ == "__main__":
    app_cfg = get_config()
    mcp_srv = create_infra_mcp_server(app_cfg)
    srv_cfg = app_cfg.infra.mcp_server
    if srv_cfg.transport == "stdio":
        mcp_srv.run(transport="stdio")
    else:
        mcp_srv.run(transport=srv_cfg.transport, host=srv_cfg.host, port=srv_cfg.port)

