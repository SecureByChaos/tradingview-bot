from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


HEALTHY = "Healthy"
WARNING = "Warning"
CRITICAL = "Critical"
DISABLED = "Disabled"


@dataclass
class HealthResult:
    status: str
    message: str = ""
    latency_ms: float | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

