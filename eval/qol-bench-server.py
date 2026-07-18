from __future__ import annotations

from fastmcp import FastMCP
from pydantic import BaseModel

mcp = FastMCP("qol-benchmark")


class Incident(BaseModel):
    id: str
    service: str
    severity: int
    status: str


class Metrics(BaseModel):
    service: str
    p95_latency_ms: int
    error_rate: float


class Owner(BaseModel):
    service: str
    team: str


class Event(BaseModel):
    id: str
    service: str
    kind: str
    latency_ms: int


@mcp.tool
def list_incidents(service: str, limit: int = 20) -> list[Incident]:
    """List deterministic incidents for a service."""
    prefix = service[:3].upper()
    return [
        Incident(
            id=f"{prefix}-{index:03d}",
            service=service,
            severity=(index % 5) + 1,
            status="closed" if index % 3 == 0 else "open",
        )
        for index in range(min(limit, 50))
    ]


@mcp.tool
def service_metrics(service: str) -> Metrics:
    """Return deterministic health metrics for a service."""
    values = {
        "api": (180, 0.02),
        "payments": (240, 0.01),
        "web": (120, 0.005),
    }
    latency, error_rate = values.get(service, (150, 0.015))
    return Metrics(service=service, p95_latency_ms=latency, error_rate=error_rate)


@mcp.tool
def get_owner(service: str) -> Owner:
    """Return the owning team for a service."""
    return Owner(service=service, team=f"team-{service}")


@mcp.tool
def get_dependencies(service: str) -> list[str]:
    """Return direct service dependencies."""
    dependencies = {
        "web": ["api", "payments"],
        "api": ["payments"],
        "payments": [],
    }
    return dependencies.get(service, [])


@mcp.tool
def get_events(service: str, count: int = 100) -> list[Event]:
    """Return a potentially large deterministic event list."""
    kinds = ["request", "retry", "timeout"]
    return [
        Event(
            id=f"event-{index:04d}",
            service=service,
            kind=kinds[index % len(kinds)],
            latency_ms=20 + (index % 17) * 7,
        )
        for index in range(min(count, 500))
    ]


if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False, log_level="ERROR")
