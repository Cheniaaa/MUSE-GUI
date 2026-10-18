from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import yaml


RuntimeEntityKey = tuple[str, str]


def parse_optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def parse_optional_number(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


@dataclass(frozen=True)
class RuntimeEntityTelemetry:
    timestamp: int | None
    runtime_entity_id: str
    runtime_entity_kind: str
    position_valid: bool | None = None
    x_m: float | int | None = None
    y_m: float | int | None = None
    z_m: float | int | None = None
    velocity_valid: bool | None = None
    vx_mps: float | int | None = None
    vy_mps: float | int | None = None
    vz_mps: float | int | None = None
    attitude_valid: bool | None = None
    roll_rad: float | int | None = None
    pitch_rad: float | int | None = None
    yaw_rad: float | int | None = None
    global_position_valid: bool | None = None
    latitude_deg: float | int | None = None
    longitude_deg: float | int | None = None
    altitude_amsl_m: float | int | None = None
    gps_valid: bool | None = None
    gps_fix_type: float | int | None = None
    gps_satellites: float | int | None = None
    battery_valid: bool | None = None
    battery_remaining: float | int | None = None
    battery_voltage_v: float | int | None = None
    battery_current_valid: bool | None = None
    battery_current_a: float | int | None = None

    @property
    def key(self) -> RuntimeEntityKey:
        return (self.runtime_entity_id, self.runtime_entity_kind)

    @classmethod
    def from_payload(cls, payload: dict[str, Any], timestamp: int | None) -> "RuntimeEntityTelemetry | None":
        runtime_entity_id = payload.get("runtime_entity_id")
        runtime_entity_kind = payload.get("runtime_entity_kind")
        if not isinstance(runtime_entity_id, str) or not runtime_entity_id:
            return None
        if not isinstance(runtime_entity_kind, str) or not runtime_entity_kind:
            return None

        parsed_timestamp = parse_optional_number(timestamp)
        if parsed_timestamp is None or (isinstance(parsed_timestamp, float) and not parsed_timestamp.is_integer()):
            timestamp_value = None
        else:
            timestamp_value = int(parsed_timestamp)

        return cls(
            timestamp=timestamp_value,
            runtime_entity_id=runtime_entity_id,
            runtime_entity_kind=runtime_entity_kind,
            position_valid=parse_optional_bool(payload.get("position_valid")),
            x_m=parse_optional_number(payload.get("x_m")),
            y_m=parse_optional_number(payload.get("y_m")),
            z_m=parse_optional_number(payload.get("z_m")),
            velocity_valid=parse_optional_bool(payload.get("velocity_valid")),
            vx_mps=parse_optional_number(payload.get("vx_mps")),
            vy_mps=parse_optional_number(payload.get("vy_mps")),
            vz_mps=parse_optional_number(payload.get("vz_mps")),
            attitude_valid=parse_optional_bool(payload.get("attitude_valid")),
            roll_rad=parse_optional_number(payload.get("roll_rad")),
            pitch_rad=parse_optional_number(payload.get("pitch_rad")),
            yaw_rad=parse_optional_number(payload.get("yaw_rad")),
            global_position_valid=parse_optional_bool(payload.get("global_position_valid")),
            latitude_deg=parse_optional_number(payload.get("latitude_deg")),
            longitude_deg=parse_optional_number(payload.get("longitude_deg")),
            altitude_amsl_m=parse_optional_number(payload.get("altitude_amsl_m")),
            gps_valid=parse_optional_bool(payload.get("gps_valid")),
            gps_fix_type=parse_optional_number(payload.get("gps_fix_type")),
            gps_satellites=parse_optional_number(payload.get("gps_satellites")),
            battery_valid=parse_optional_bool(payload.get("battery_valid")),
            battery_remaining=parse_optional_number(payload.get("battery_remaining")),
            battery_voltage_v=parse_optional_number(payload.get("battery_voltage_v")),
            battery_current_valid=parse_optional_bool(payload.get("battery_current_valid")),
            battery_current_a=parse_optional_number(payload.get("battery_current_a")),
        )


@dataclass(frozen=True)
class SimulationDroneStatus:
    connected: bool | None = None
    initialized: bool | None = None
    local_position_valid: bool | None = None
    px4_armed: bool | None = None
    px4_nav_state_name: str | None = None
    lifecycle_state: str | None = None

    @property
    def ready(self) -> bool:
        return self.connected is True and self.initialized is True and self.local_position_valid is True

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "SimulationDroneStatus":
        px4_nav_state_name = payload.get("px4_nav_state_name")
        lifecycle_state = payload.get("lifecycle_state")
        return cls(
            connected=parse_optional_bool(payload.get("connected")),
            initialized=parse_optional_bool(payload.get("initialized")),
            local_position_valid=parse_optional_bool(payload.get("local_position_valid")),
            px4_armed=parse_optional_bool(payload.get("px4_armed")),
            px4_nav_state_name=px4_nav_state_name.strip() if isinstance(px4_nav_state_name, str) and px4_nav_state_name.strip() else None,
            lifecycle_state=lifecycle_state.strip() if isinstance(lifecycle_state, str) and lifecycle_state.strip() else None,
        )


@dataclass(frozen=True)
class RuntimeEntityDisplay:
    key: RuntimeEntityKey
    label: str
    telemetry: RuntimeEntityTelemetry
    status: SimulationDroneStatus | None


@dataclass(frozen=True)
class _RuntimeEntityMetadata:
    key: RuntimeEntityKey
    display_name: str | None
    source_namespace: str | None


class RuntimeTelemetryStore:
    def __init__(self) -> None:
        self._telemetry_by_key: dict[RuntimeEntityKey, RuntimeEntityTelemetry] = {}
        self._metadata_by_key: dict[RuntimeEntityKey, _RuntimeEntityMetadata] = {}
        self._duplicate_metadata_keys: set[RuntimeEntityKey] = set()
        self._status_by_source_namespace: dict[str, SimulationDroneStatus] = {}
        self._duplicate_source_namespaces: set[str] = set()
        self._ready_training_entity_options: list[dict[str, object]] = []
        self._active_generation: int | None = None
        self._live_snapshot_seen = False

    def clear(self) -> None:
        self.clear_contents()
        self._active_generation = None
        self._live_snapshot_seen = False

    def clear_contents(self) -> None:
        self._telemetry_by_key.clear()
        self._clear_simulation_status()

    def update_telemetry_snapshot(self, payload: dict[str, Any]) -> None:
        timestamp = payload.get("timestamp")
        entities = payload.get("entities")
        self._telemetry_by_key = {}
        if not isinstance(entities, list):
            return

        for entity_payload in entities:
            if not isinstance(entity_payload, dict):
                continue
            telemetry = RuntimeEntityTelemetry.from_payload(entity_payload, timestamp)
            if telemetry is None:
                continue
            self._telemetry_by_key[telemetry.key] = telemetry

    def begin_generation(self, generation: int) -> None:
        self._active_generation = generation
        self._live_snapshot_seen = False

    def apply_bootstrap_snapshot(self, generation: int, payload: dict[str, Any]) -> bool:
        if generation != self._active_generation or self._live_snapshot_seen:
            return False
        self.update_telemetry_snapshot(payload)
        return True

    def apply_live_snapshot(self, generation: int, payload: dict[str, Any]) -> bool:
        if generation != self._active_generation:
            return False
        self.update_telemetry_snapshot(payload)
        self._live_snapshot_seen = True
        return True

    def update_simulation_status(self, payload: dict[str, Any]) -> None:
        active_session = payload.get("active_session")
        if not isinstance(active_session, dict):
            self._clear_simulation_status()
            return

        environment = active_session.get("environment")
        if not isinstance(environment, dict):
            self._clear_simulation_status()
            return

        runtime_entities_yaml = environment.get("runtime_entities_yaml")
        runtime_entities = self._load_runtime_entities(runtime_entities_yaml)
        if not runtime_entities:
            self._clear_simulation_status()
            return

        self._replace_metadata(runtime_entities)
        self._replace_drone_status(payload.get("drones"))
        self._refresh_ready_training_entity_options()

    def entities(self) -> list[RuntimeEntityDisplay]:
        displays = [self._display_for(telemetry) for telemetry in self._telemetry_by_key.values()]
        return sorted(displays, key=lambda display: (display.label, display.key))

    def selected_view(self, key: RuntimeEntityKey) -> RuntimeEntityDisplay | None:
        telemetry = self._telemetry_by_key.get(key)
        if telemetry is None:
            return None
        return self._display_for(telemetry)

    def ready_training_entity_options(self) -> list[dict[str, object]]:
        return [dict(option) for option in self._ready_training_entity_options]

    def _clear_simulation_status(self) -> None:
        self._metadata_by_key.clear()
        self._duplicate_metadata_keys.clear()
        self._status_by_source_namespace.clear()
        self._duplicate_source_namespaces.clear()
        self._ready_training_entity_options = []

    def _replace_metadata(self, runtime_entities: list[Any]) -> None:
        metadata_by_key: dict[RuntimeEntityKey, _RuntimeEntityMetadata] = {}
        duplicate_metadata_keys: set[RuntimeEntityKey] = set()
        source_namespace_counts: dict[str, int] = {}

        for item in runtime_entities:
            if not isinstance(item, dict):
                continue
            entity_id = item.get("entity_id")
            entity_kind = item.get("entity_kind")
            if not isinstance(entity_id, str) or not entity_id:
                continue
            if not isinstance(entity_kind, str) or not entity_kind:
                continue

            key = (entity_id, entity_kind)
            display_name = item.get("display_name")
            source_namespace = item.get("source_namespace")
            metadata = _RuntimeEntityMetadata(
                key=key,
                display_name=display_name if isinstance(display_name, str) and display_name else None,
                source_namespace=source_namespace if isinstance(source_namespace, str) and source_namespace else None,
            )

            if key in metadata_by_key:
                duplicate_metadata_keys.add(key)
                metadata_by_key.pop(key, None)
            elif key not in duplicate_metadata_keys:
                metadata_by_key[key] = metadata

            if metadata.source_namespace is not None:
                source_namespace_counts[metadata.source_namespace] = source_namespace_counts.get(metadata.source_namespace, 0) + 1

        self._metadata_by_key = metadata_by_key
        self._duplicate_metadata_keys = duplicate_metadata_keys
        self._duplicate_source_namespaces = {
            source_namespace for source_namespace, count in source_namespace_counts.items() if count > 1
        }

    def _replace_drone_status(self, drones: Any) -> None:
        status_by_source_namespace: dict[str, SimulationDroneStatus] = {}
        duplicate_status_namespaces: set[str] = set()
        if isinstance(drones, list):
            for drone in drones:
                if not isinstance(drone, dict):
                    continue
                source_namespace = drone.get("source_namespace")
                if not isinstance(source_namespace, str) or not source_namespace:
                    continue
                if source_namespace in status_by_source_namespace:
                    duplicate_status_namespaces.add(source_namespace)
                    status_by_source_namespace.pop(source_namespace, None)
                    continue
                if source_namespace not in duplicate_status_namespaces:
                    status_by_source_namespace[source_namespace] = SimulationDroneStatus.from_payload(drone)

        self._status_by_source_namespace = status_by_source_namespace
        self._duplicate_source_namespaces = self._duplicate_source_namespaces.union(duplicate_status_namespaces)

    def _refresh_ready_training_entity_options(self) -> None:
        options: list[dict[str, object]] = []
        for metadata in self._metadata_by_key.values():
            if metadata.key in self._duplicate_metadata_keys:
                continue
            if metadata.key[1] != "drone":
                continue
            if metadata.source_namespace is None:
                continue
            if metadata.source_namespace in self._duplicate_source_namespaces:
                continue

            status = self._status_by_source_namespace.get(metadata.source_namespace)
            ready = status.ready if status is not None else False
            label_name = metadata.display_name or metadata.key[0]
            options.append(
                {
                    "label": f"{label_name} ({metadata.key[0]}, {'READY' if ready else 'NOT READY'})",
                    "runtime_entity_id": metadata.key[0],
                    "runtime_entity_kind": metadata.key[1],
                    "ready": ready,
                    "source_namespace": metadata.source_namespace,
                }
            )

        self._ready_training_entity_options = options

    def _display_for(self, telemetry: RuntimeEntityTelemetry) -> RuntimeEntityDisplay:
        metadata = self._metadata_by_key.get(telemetry.key)
        return RuntimeEntityDisplay(
            key=telemetry.key,
            label=self._label_for(telemetry, metadata),
            telemetry=telemetry,
            status=self._status_for(telemetry.key, metadata),
        )

    def _label_for(
        self, telemetry: RuntimeEntityTelemetry, metadata: _RuntimeEntityMetadata | None
    ) -> str:
        runtime_entity_id = telemetry.runtime_entity_id
        runtime_entity_kind = telemetry.runtime_entity_kind
        if metadata is not None and metadata.display_name and metadata.display_name != runtime_entity_id:
            label = f"{runtime_entity_id} ({metadata.display_name})"
        else:
            label = runtime_entity_id

        if runtime_entity_kind != "drone":
            label = f"{label} [{runtime_entity_kind}]"
        return label

    def _status_for(
        self, key: RuntimeEntityKey, metadata: _RuntimeEntityMetadata | None
    ) -> SimulationDroneStatus | None:
        if key in self._duplicate_metadata_keys:
            return None
        if metadata is None or metadata.source_namespace is None:
            return None
        if metadata.source_namespace in self._duplicate_source_namespaces:
            return None
        return self._status_by_source_namespace.get(metadata.source_namespace)

    def _load_runtime_entities(self, runtime_entities_yaml: Any) -> list[Any]:
        if not isinstance(runtime_entities_yaml, str) or not runtime_entities_yaml.strip():
            return []
        try:
            loaded = yaml.safe_load(runtime_entities_yaml)
        except yaml.YAMLError:
            return []
        return loaded if isinstance(loaded, list) else []
