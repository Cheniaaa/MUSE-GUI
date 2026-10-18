from __future__ import annotations

import asyncio
import time
import json
from collections import deque
from dataclasses import dataclass, field
from threading import Event
from threading import Lock
from typing import Any

import httpx2
import websockets.exceptions as websockets_exceptions
from PySide6.QtCore import QObject, QThread, Signal

from src.config.ui_performance import LOG_MAX_BLOCKS
from src.controllers.gateway_client import GatewayClient
from src.models.runtime_session_state import BackendLifecycle, RuntimeSessionState


@dataclass(frozen=True)
class PendingGuiUpdateBatch:
    generation: int
    simulation_statuses: tuple[dict, ...]
    training_statuses: tuple[dict, ...]
    telemetry_snapshot: dict | None
    training_metric: dict | None
    system_logs: tuple[tuple[str, str | None], ...]
    training_logs: tuple[tuple[str, str | None], ...]
    dropped_system_logs: int
    dropped_training_logs: int


@dataclass
class _PendingGeneration:
    simulation_statuses: deque[dict] = field(default_factory=deque)
    training_statuses: deque[dict] = field(default_factory=deque)
    telemetry_snapshot: dict | None = None
    training_metric: dict | None = None
    system_logs: deque[tuple[str, str | None]] = field(default_factory=deque)
    training_logs: deque[tuple[str, str | None]] = field(default_factory=deque)
    dropped_system_logs: int = 0
    dropped_training_logs: int = 0


class _PendingGuiUpdates:
    def __init__(self, *, log_capacity: int = LOG_MAX_BLOCKS):
        self._lock = Lock()
        self._generations: dict[int, _PendingGeneration] = {}
        self._log_capacity = max(0, int(log_capacity))

    def publish(self, generation: int, stream_name: str, envelope: dict) -> None:
        if isinstance(generation, bool) or not isinstance(generation, int):
            return
        if not isinstance(envelope, dict):
            return
        message_type, data = _stream_envelope_data(envelope)
        with self._lock:
            pending = self._generations.setdefault(generation, _PendingGeneration())
            if message_type == "runtime_entity_states" and stream_name == "entities":
                pending.telemetry_snapshot = data
                return
            if message_type == "training_metric":
                pending.training_metric = data
                return
            if message_type == "training_status":
                pending.training_statuses.append(data)
                return
            if message_type == "simulation_status":
                pending.simulation_statuses.append(data)
                return
            if message_type == "training_log":
                self._append_log(pending.training_logs, "training", _log_record(data), pending)
                return
            if message_type in {"simulation_log", "simulation_event"}:
                self._append_log(pending.system_logs, "system", _log_record(data), pending)
                return
            fallback = (f"[{stream_name}] {_json_text(envelope)}", None)
            if stream_name == "training":
                self._append_log(pending.training_logs, "training", fallback, pending)
            else:
                self._append_log(pending.system_logs, "system", fallback, pending)

    def take(self, generation: int) -> PendingGuiUpdateBatch:
        with self._lock:
            self._drop_old_generations(generation)
            pending = self._generations.pop(generation, None)
        if pending is None:
            return _empty_pending_batch(generation)
        return PendingGuiUpdateBatch(
            generation=generation,
            simulation_statuses=tuple(pending.simulation_statuses),
            training_statuses=tuple(pending.training_statuses),
            telemetry_snapshot=pending.telemetry_snapshot,
            training_metric=pending.training_metric,
            system_logs=tuple(pending.system_logs),
            training_logs=tuple(pending.training_logs),
            dropped_system_logs=pending.dropped_system_logs,
            dropped_training_logs=pending.dropped_training_logs,
        )

    def clear_generation(self, generation: int) -> None:
        with self._lock:
            for pending_generation in list(self._generations):
                if pending_generation <= generation:
                    self._generations.pop(pending_generation, None)

    def _append_log(
        self,
        records: deque[tuple[str, str | None]],
        channel: str,
        record: tuple[str, str | None],
        pending: _PendingGeneration,
    ) -> None:
        if self._log_capacity == 0:
            self._increment_dropped(channel, pending)
            return
        records.append(record)
        if len(records) > self._log_capacity:
            records.popleft()
            self._increment_dropped(channel, pending)

    def _increment_dropped(self, channel: str, pending: _PendingGeneration) -> None:
        if channel == "training":
            pending.dropped_training_logs += 1
        else:
            pending.dropped_system_logs += 1

    def _drop_old_generations(self, generation: int) -> None:
        for pending_generation in list(self._generations):
            if pending_generation < generation:
                self._generations.pop(pending_generation, None)


class GatewayRequestWorker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self._operation = operation

    def run(self):
        try:
            self.succeeded.emit(self._operation())
        except (httpx2.HTTPError, OSError, json.JSONDecodeError) as error:
            self.failed.emit(str(error))


class GatewayStreamWorker(QThread):
    failed = Signal(str, str)

    def __init__(self, client: GatewayClient, stream_name: str, generation: int, pending_updates: _PendingGuiUpdates, parent=None):
        super().__init__(parent)
        self._client = client
        self._stream_name = stream_name
        self._generation = generation
        self._pending_updates = pending_updates
        self._stop_event = Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        try:
            asyncio.run(self._consume())
        except (
            websockets_exceptions.WebSocketException,
            OSError,
            json.JSONDecodeError,
            UnicodeDecodeError,
        ) as error:
            if not self._stop_event.is_set():
                self.failed.emit(self._stream_name, str(error))

    async def _consume(self):
        async for envelope in self._client.stream_envelopes(
            self._stream_name,
            stop_event=self._stop_event,
        ):
            if self._stop_event.is_set():
                break
            self._pending_updates.publish(self._generation, self._stream_name, envelope)


class GatewayController(QObject):
    connection_state_changed = Signal(str)
    connection_generation_changed = Signal(int)
    backend_state_changed = Signal(str)
    session_state_changed = Signal(object)
    response_received = Signal(str, object)
    error_occurred = Signal(str, str)

    def __init__(self, base_url: str = "http://127.0.0.1:8765", parent=None):
        super().__init__(parent)
        self._client = GatewayClient(base_url=base_url)
        self._request_workers: set[GatewayRequestWorker] = set()
        self._stream_workers: dict[str, GatewayStreamWorker] = {}
        self._closing = False
        self._session_state = RuntimeSessionState()
        self._cancel_backend_start_requested = False
        self._connection_generation = 0
        self._pending_gui_updates = _PendingGuiUpdates()

    @property
    def session_state(self) -> RuntimeSessionState:
        return self._session_state

    @property
    def connection_generation(self) -> int:
        return self._connection_generation

    def _set_session_state(self, state: RuntimeSessionState) -> None:
        if state == self._session_state:
            return
        self._session_state = state
        self.session_state_changed.emit(state)

    def _advance_connection_generation(self) -> int:
        self._connection_generation += 1
        self._pending_gui_updates.clear_generation(self._connection_generation)
        self.connection_generation_changed.emit(self._connection_generation)
        return self._connection_generation

    def take_pending_gui_updates(self, generation: int | None = None) -> PendingGuiUpdateBatch:
        active_generation = self._connection_generation if generation is None else generation
        batch = self._pending_gui_updates.take(active_generation)
        self._apply_pending_session_state(batch)
        return batch

    def clear_pending_gui_updates(self, generation: int | None = None) -> None:
        active_generation = self._connection_generation if generation is None else generation
        self._pending_gui_updates.clear_generation(active_generation)

    @property
    def base_url(self) -> str:
        return self._client.base_url

    def set_base_url(self, base_url: str):
        if self._closing:
            return False
        normalized = GatewayClient._normalize_base_url(base_url.strip())
        if normalized == self._client.base_url:
            return True
        if self._request_workers:
            self.error_occurred.emit("gateway.base_url", "request in progress")
            return False
        self.stop_streams()
        if self._stream_workers:
            self.error_occurred.emit("gateway.base_url", "stream shutdown in progress")
            return False
        self._advance_connection_generation()
        self._client.close()
        self._client = GatewayClient(base_url=normalized)
        self.connection_state_changed.emit("未连接")
        self._set_session_state(RuntimeSessionState())
        return True

    def close(self):
        self._closing = True
        self.stop_streams()
        if self._stream_workers:
            return False
        deadline = time.monotonic() + 1.5
        for worker in list(self._request_workers):
            remaining_ms = int(max(0.0, deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                break
            if worker.wait(remaining_ms) and worker in self._request_workers:
                self._request_workers.discard(worker)
                worker.deleteLater()
        if self._request_workers:
            return False
        self._client.close()
        return True

    def test_connection(self):
        if self._closing:
            return None
        self._run_request(
            "health",
            self._client.health,
            on_success=self._handle_health_result,
        )

    def bootstrap(self):
        if self._closing:
            return None
        self.connection_state_changed.emit("连接中")
        self.backend_state_changed.emit("等待连接")
        self._run_request(
            "health",
            self._client.health,
            on_success=self._handle_bootstrap_health_result,
        )

    def start_backend(self):
        if self._closing:
            return None
        self._set_session_state(
            self._session_state.with_backend_state(BackendLifecycle.STARTING)
        )
        self._cancel_backend_start_requested = False
        self.backend_state_changed.emit("启动中")
        self._run_request(
            "backend.start",
            self._client.start_backend,
            on_success=self._handle_backend_start_result,
        )

    def cancel_backend_start(self):
        if self._closing or self._session_state.backend_state is not BackendLifecycle.STARTING:
            return False
        self._cancel_backend_start_requested = True
        self._set_session_state(
            self._session_state.with_backend_state(BackendLifecycle.STOPPING)
        )
        self.backend_state_changed.emit("停止中")
        return True

    def stop_backend(self):
        if self._closing:
            return None
        self._set_session_state(
            self._session_state.with_backend_state(BackendLifecycle.STOPPING)
        )
        self._cancel_backend_start_requested = False
        self.backend_state_changed.emit("停止中")
        self._run_request(
            "backend.stop",
            self._client.stop_backend,
            on_success=self._handle_backend_stop_result,
        )

    def restart_backend(self):
        if self._closing:
            return None
        self._set_session_state(
            self._session_state.with_backend_state(BackendLifecycle.STOPPING)
        )
        self._cancel_backend_start_requested = False
        self.backend_state_changed.emit("停止中")
        self._run_request(
            "backend.stop",
            self._client.stop_backend,
            on_success=lambda _result: self.start_backend(),
        )

    def start_streams(self):
        if self._closing:
            return None
        if not self._stream_workers:
            self._advance_connection_generation()
        generation = self._connection_generation
        for stream_name in ("simulation", "training", "logs", "entities"):
            if stream_name in self._stream_workers:
                continue
            worker = GatewayStreamWorker(
                self._client,
                stream_name,
                generation,
                self._pending_gui_updates,
                parent=self,
            )
            worker.failed.connect(self._on_stream_failed)
            def _finish_stream_worker(name=stream_name, worker=worker):
                if self._stream_workers.pop(name, None) is worker:
                    worker.deleteLater()

            worker.finished.connect(_finish_stream_worker)
            self._stream_workers[stream_name] = worker
            worker.start()

    def stop_streams(self):
        workers = list(self._stream_workers.items())
        if workers:
            self._advance_connection_generation()
        for _, worker in workers:
            worker.stop()
        for name, worker in workers:
            if worker.wait(1500) and self._stream_workers.pop(name, None) is worker:
                worker.deleteLater()

    def get_runtime_entity_status(self):
        if self._closing:
            return None
        generation = self._connection_generation
        self._run_request(
            "runtime.entities.status",
            self._client.runtime_entity_status,
            on_success=lambda result: self.response_received.emit(
                "runtime.entities.status",
                {"generation": generation, "payload": result},
            ),
        )

    def list_environments(self):
        if self._closing:
            return None
        self._run_request(
            "simulation.list_environments",
            self._client.list_environments,
            on_success=lambda result: self.response_received.emit("simulation.list_environments", result),
        )

    def upload_environment(self, zip_path: str):
        if self._closing:
            return None
        self._run_request(
            "simulation.upload_environment",
            lambda: self._client.upload_environment(zip_path),
            on_success=lambda result: self.response_received.emit("simulation.upload_environment", result),
        )

    def get_simulation_environment_semantics(self, environment_id: str):
        if self._closing:
            return None
        self._run_request(
            "simulation.get_environment_semantics",
            lambda: self._client.get_simulation_environment_semantics(environment_id),
            on_success=lambda result: self.response_received.emit("simulation.get_environment_semantics", result),
        )

    def start_simulation(self, payload: dict):
        if self._closing:
            return None
        self._run_request(
            "simulation.start",
            lambda: self._client.start_simulation(payload),
            on_success=self._handle_simulation_start_result,
        )

    def stop_simulation(self):
        if self._closing:
            return None
        self._run_request(
            "simulation.stop",
            self._client.stop_simulation,
            on_success=self._handle_simulation_stop_result,
        )

    def list_training_packages(self):
        if self._closing:
            return None
        self._run_request(
            "training.list_packages",
            self._client.list_training_packages,
            on_success=lambda result: self.response_received.emit("training.list_packages", result),
        )

    def get_training_package(self, package_id: str):
        if self._closing:
            return None
        self._run_request(
            "training.get_package",
            lambda: self._client.get_training_package(package_id),
            on_success=lambda result: self.response_received.emit("training.get_package", result),
        )

    def upload_training_package(self, zip_path: str):
        if self._closing:
            return None
        self._run_request(
            "training.upload_package",
            lambda: self._client.upload_training_package(zip_path),
            on_success=lambda result: self.response_received.emit("training.upload_package", result),
        )

    def list_training_recipes(self):
        if self._closing:
            return None
        self._run_request(
            "training.list_recipes",
            self._client.list_training_recipes,
            on_success=lambda result: self.response_received.emit("training.list_recipes", result),
        )

    def get_training_recipe(self, recipe_id: str):
        if self._closing:
            return None
        self._run_request(
            "training.get_recipe",
            lambda: self._client.get_training_recipe(recipe_id),
            on_success=lambda result: self.response_received.emit("training.get_recipe", result),
        )

    def save_training_recipe(self, payload: dict):
        if self._closing:
            return None
        self._run_request(
            "training.save_recipe",
            lambda: self._client.register_training_recipe(payload),
            on_success=lambda result: self.response_received.emit("training.save_recipe", result),
        )

    def start_training_run(self, payload: dict):
        if self._closing:
            return None
        self._run_request(
            "training.start_run",
            lambda: self._client.start_training_run(payload),
            on_success=self._handle_training_start_result,
        )

    def stop_training_run(self, run_id: str):
        if self._closing or not run_id:
            return None
        self._run_request(
            "training.stop_run",
            lambda: self._client.stop_training_run(run_id),
            on_success=self._handle_training_stop_result,
        )

    def _run_request(self, action_name: str, operation, *, on_success=None):
        if self._closing:
            return None
        worker = GatewayRequestWorker(operation, parent=self)
        self._request_workers.add(worker)

        def _finish_worker():
            if worker in self._request_workers:
                self._request_workers.discard(worker)
                worker.deleteLater()

        def _handle_success(result):
            if on_success is not None:
                on_success(result)

        def _handle_failure(message):
            if action_name == "health":
                self._set_session_state(RuntimeSessionState())
            if action_name in {"backend.start", "backend.stop"}:
                self._set_session_state(
                    self._session_state.with_backend_state(BackendLifecycle.FAILED)
                )
            self.error_occurred.emit(action_name, message)

        worker.succeeded.connect(_handle_success)
        worker.failed.connect(_handle_failure)
        worker.finished.connect(_finish_worker)
        worker.start()
        return worker

    def _handle_backend_start_result(self, result):
        self.response_received.emit("backend.start", result)
        if self._cancel_backend_start_requested:
            self.stop_backend()
            return
        self._set_session_state(
            self._session_state
            .with_gateway_connection(True)
            .with_backend_state(BackendLifecycle.READY)
        )
        self.backend_state_changed.emit("Ready")
        self.connection_state_changed.emit("已连接")
        self.start_streams()
        self.list_environments()
        self.list_training_packages()
        self.get_runtime_entity_status()

    def _handle_bootstrap_health_result(self, result):
        self._set_session_state(self._session_state.with_gateway_connection(True))
        self.connection_state_changed.emit("已连接")
        self.response_received.emit("health", result)
        self.start_backend()

    def _handle_health_result(self, result):
        self._set_session_state(self._session_state.with_gateway_connection(True))
        self.connection_state_changed.emit("已连接")
        self.response_received.emit("health", result)

    def _handle_backend_stop_result(self, result):
        self._cancel_backend_start_requested = False
        self.stop_streams()
        self.clear_pending_gui_updates()
        self._set_session_state(
            self._session_state
            .with_gateway_connection(True)
            .with_backend_state(BackendLifecycle.STOPPED)
            .with_simulation_running(False)
            .with_training_status(None, None)
        )
        self.response_received.emit("backend.stop", result)
        self.backend_state_changed.emit("Stopped")
        self.connection_state_changed.emit("已连接")

    def _handle_simulation_start_result(self, result):
        self._set_session_state(self._session_state.with_simulation_running(True))
        self.response_received.emit("simulation.start", result)

    def _handle_simulation_stop_result(self, result):
        self._set_session_state(self._session_state.with_simulation_running(False))
        self.response_received.emit("simulation.stop", result)

    def _handle_training_start_result(self, result):
        run_id = str(result.get("run_id") or "").strip() if isinstance(result, dict) else ""
        if run_id:
            self._set_session_state(
                self._session_state.with_training_status("RUNNING", run_id)
            )
        self.response_received.emit("training.start_run", result)

    def _handle_training_stop_result(self, result):
        self._set_session_state(self._session_state.with_training_status(None, None))
        self.response_received.emit("training.stop_run", result)

    def _apply_pending_session_state(self, batch: PendingGuiUpdateBatch) -> None:
        for data in batch.simulation_statuses:
            if isinstance(data, dict):
                simulation_running = data.get("simulation_running")
                if isinstance(simulation_running, bool):
                    self._set_session_state(
                        self._session_state.with_simulation_running(simulation_running)
                    )
        for data in batch.training_statuses:
            self._apply_training_status_state(data)

    def _apply_training_status_state(self, data: dict) -> None:
        phase = str(data.get("phase") or "").strip()
        run_id = str(data.get("run_id") or "").strip()
        if phase:
            self._set_session_state(
                self._session_state.with_training_status(
                    phase,
                    run_id or self._session_state.training_run_id,
                )
            )

    def _on_stream_failed(self, stream_name: str, message: str):
        self.error_occurred.emit(stream_name, message)


def _stream_envelope_data(envelope: object) -> tuple[str, dict]:
    if not isinstance(envelope, dict):
        return "", {}
    message_type = str(envelope.get("type") or "").strip()
    data = envelope.get("data")
    if isinstance(data, dict):
        return message_type, data
    return message_type, {}


def _empty_pending_batch(generation: int) -> PendingGuiUpdateBatch:
    return PendingGuiUpdateBatch(
        generation=generation,
        simulation_statuses=(),
        training_statuses=(),
        telemetry_snapshot=None,
        training_metric=None,
        system_logs=(),
        training_logs=(),
        dropped_system_logs=0,
        dropped_training_logs=0,
    )


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _log_record(data: dict) -> tuple[str, str | None]:
    level = str(data.get("level") or "").strip()
    parts = []
    if level:
        parts.append(f"[{level}]")
    source = str(data.get("source") or "").strip()
    drone_namespace = str(data.get("drone_namespace") or "").strip()
    if source and drone_namespace:
        parts.append(f"{source}:{drone_namespace}")
    elif source:
        parts.append(source)
    message = str(data.get("message") or "").strip()
    if message:
        parts.append(message)
    if parts:
        return " ".join(parts), level or None
    return _json_text(data), level or None
