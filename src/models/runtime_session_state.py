from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum


class BackendLifecycle(Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    FAILED = "failed"


_TERMINAL_TRAINING_PHASES = {"COMPLETED", "CANCELLED", "FAILED"}
_ACTIVE_TRAINING_PHASES = {"STARTING", "RUNNING"}


@dataclass(frozen=True)
class RuntimeSessionState:
    gateway_connected: bool = False
    backend_state: BackendLifecycle = BackendLifecycle.STOPPED
    simulation_running: bool = False
    training_phase: str | None = None
    training_run_id: str | None = None

    def with_gateway_connection(self, connected: bool) -> "RuntimeSessionState":
        return replace(self, gateway_connected=bool(connected))

    def with_backend_state(self, state: BackendLifecycle) -> "RuntimeSessionState":
        return replace(self, backend_state=state)

    def with_simulation_running(self, running: bool) -> "RuntimeSessionState":
        return replace(self, simulation_running=bool(running))

    def with_training_status(
        self,
        phase: str | None,
        run_id: str | None,
    ) -> "RuntimeSessionState":
        normalized_phase = _normalized_text(phase)
        normalized_run_id = _normalized_text(run_id)
        if normalized_phase in _TERMINAL_TRAINING_PHASES:
            normalized_run_id = None
        return replace(
            self,
            training_phase=normalized_phase,
            training_run_id=normalized_run_id,
        )

    @property
    def training_active(self) -> bool:
        return self.training_phase in _ACTIVE_TRAINING_PHASES and bool(self.training_run_id)


def _normalized_text(value: str | None) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None
