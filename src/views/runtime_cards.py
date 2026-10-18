from __future__ import annotations

import yaml

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    ComboBox,
    FluentIcon,
    IconWidget,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    SubtitleLabel,
)

from src.models.runtime_session_state import BackendLifecycle, RuntimeSessionState
from src.models.training_recipe_draft import TrainingRecipeDraft
from src.controllers.gateway_payloads import (
    build_simulation_start_payload,
    build_training_run_payload,
    generated_run_name,
)


class Separator(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.HLine)
        self.setFrameShadow(QFrame.Sunken)
        self.setStyleSheet(
            "background-color: rgba(255, 255, 255, 0.1); border: none; max-height: 1px;"
        )


ROLE_ORDER = ("task", "observation", "action", "reward", "algorithm", "policy_model")
SEMANTICS_REF_TARGET_KEYS = {
    "world_points": "world_point",
    "world_entities": "world_entity",
    "navigation_graphs": "navigation_graph",
    "regions": "region",
}


def response_items(response: object, key: str) -> list[dict]:
    if not isinstance(response, dict):
        return []
    value = response.get(key)
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _id_value(item: dict, *names: str) -> str:
    for name in names:
        value = item.get(name)
        if value:
            return str(value)
    return ""


def _display_value(item: dict, id_name: str, *names: str) -> str:
    for name in names:
        value = item.get(name)
        if value:
            return str(value)
    return str(item.get(id_name, ""))


class GatewayStatusCard(CardWidget):
    retry_requested = Signal()
    backend_lifecycle_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.v_layout = QVBoxLayout(self)
        self.v_layout.setSpacing(12)
        self.v_layout.setContentsMargins(20, 20, 20, 20)
        self._session_state = RuntimeSessionState()
        self._backend_lifecycle_action = ""
        self._init_widgets()
        self._init_layout()

    def _init_widgets(self):
        self.head_icon = IconWidget(FluentIcon.LINK)
        self.head_icon.setFixedSize(24, 24)
        self.head_title = SubtitleLabel("平台状态")
        self.btn_backend_lifecycle = PushButton(FluentIcon.PLAY, "等待 Gateway")
        self.btn_backend_lifecycle.setEnabled(False)
        self.btn_retry = PushButton(FluentIcon.SYNC, "重试连接")

        self.base_url_edit = LineEdit()
        self.base_url_edit.setText("http://127.0.0.1:8765")
        self.base_url_edit.setPlaceholderText("http://127.0.0.1:8765")

        self.lbl_gateway = BodyLabel("Gateway: 未连接")
        self.lbl_backend = BodyLabel("Backend: 未就绪")
        self.lbl_simulation = BodyLabel("Simulation: 未启动")
        self.lbl_training = BodyLabel("Training: 未启动")

    def _init_layout(self):
        title_row = QHBoxLayout()
        title_row.addWidget(self.head_icon)
        title_row.addWidget(self.head_title)
        title_row.addStretch()
        title_row.addWidget(self.btn_backend_lifecycle)
        title_row.addWidget(self.btn_retry)
        self.v_layout.addLayout(title_row)

        url_row = QHBoxLayout()
        url_row.setSpacing(10)
        url_row.addWidget(CaptionLabel("Base URL"))
        url_row.addWidget(self.base_url_edit)
        self.v_layout.addLayout(url_row)

        grid = QGridLayout()
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(8)
        grid.addWidget(self.lbl_gateway, 0, 0)
        grid.addWidget(self.lbl_backend, 0, 1)
        grid.addWidget(self.lbl_simulation, 1, 0)
        grid.addWidget(self.lbl_training, 1, 1)
        self.v_layout.addLayout(grid)

        self.btn_retry.clicked.connect(self.retry_requested.emit)
        self.btn_backend_lifecycle.clicked.connect(self._emit_backend_lifecycle_requested)

    def set_session_state(self, state: RuntimeSessionState):
        self._session_state = state
        gateway_text = "已连接" if state.gateway_connected else "未连接"
        backend_text = {
            BackendLifecycle.STOPPED: "Stopped",
            BackendLifecycle.STARTING: "启动中",
            BackendLifecycle.READY: "Ready",
            BackendLifecycle.STOPPING: "停止中",
            BackendLifecycle.FAILED: "失败",
        }[state.backend_state]
        if not state.gateway_connected:
            self._set_label(self.lbl_gateway, "Gateway: 未连接", ok=False)
        else:
            self._set_label(self.lbl_gateway, f"Gateway: {gateway_text}", ok=True)
        self._set_label(self.lbl_backend, f"Backend: {backend_text}", ok=state.backend_state is BackendLifecycle.READY)
        simulation_text = "Running" if state.simulation_running else "Stopped"
        self._set_label(self.lbl_simulation, f"Simulation: {simulation_text}", ok=state.simulation_running)
        training_text = state.training_phase or "Stopped"
        self._set_label(
            self.lbl_training,
            f"Training: {training_text}",
            ok=state.training_active or training_text == "COMPLETED",
        )
        self._update_backend_lifecycle_button()

    def _set_label(self, label: BodyLabel, text: str, *, ok: bool):
        color = "#1a7f37" if ok else "#888888"
        if "失败" in text or "错误" in text:
            color = "#c62828"
        label.setText(text)
        label.setStyleSheet(f"color: {color}; font-weight: bold;")

    def _emit_backend_lifecycle_requested(self):
        if self._backend_lifecycle_action:
            self.backend_lifecycle_requested.emit(self._backend_lifecycle_action)

    def _set_backend_lifecycle_button(self, icon, text: str, action: str, enabled: bool):
        self.btn_backend_lifecycle.setIcon(icon)
        self.btn_backend_lifecycle.setText(text)
        self.btn_backend_lifecycle.setEnabled(enabled)
        self._backend_lifecycle_action = action if enabled else ""

    def _update_backend_lifecycle_button(self):
        state = self._session_state.backend_state
        if not self._session_state.gateway_connected:
            self._set_backend_lifecycle_button(FluentIcon.SYNC, "等待 Gateway", "", False)
            return
        if state is BackendLifecycle.STARTING:
            self._set_backend_lifecycle_button(FluentIcon.PAUSE, "取消启动", "cancel_start", True)
            return
        if state is BackendLifecycle.STOPPING:
            self._set_backend_lifecycle_button(FluentIcon.PAUSE, "停止中...", "", False)
            return
        if state is BackendLifecycle.READY:
            self._set_backend_lifecycle_button(FluentIcon.PAUSE, "停止后端", "stop", True)
            return
        if state is BackendLifecycle.FAILED:
            self._set_backend_lifecycle_button(FluentIcon.SYNC, "重新启动后端", "restart", True)
            return
        self._set_backend_lifecycle_button(FluentIcon.PLAY, "启动后端", "start", True)


class SimulationConfigCard(CardWidget):
    upload_environment_requested = Signal()
    start_simulation_requested = Signal(dict)
    stop_simulation_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.v_layout = QVBoxLayout(self)
        self.v_layout.setSpacing(16)
        self.v_layout.setContentsMargins(24, 24, 24, 24)
        self._environments: list[dict] = []
        self._row_widgets: list[dict] = []
        self._session_state = RuntimeSessionState()
        self._init_widgets()
        self._init_layout()
        self._connect_signal_slot()

    def _init_widgets(self):
        self.head_icon = IconWidget(FluentIcon.SETTING)
        self.head_icon.setFixedSize(24, 24)
        self.head_title = SubtitleLabel("仿真配置")

        self.env_combo = ComboBox()
        self.env_combo.setPlaceholderText("等待环境列表...")
        self.btn_upload_env = PushButton(FluentIcon.FOLDER_ADD, "导入环境")

        self.count_edit = LineEdit()
        self.count_edit.setText("1")
        self.count_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.count_edit.setFixedWidth(80)

        self.drone_rows_container = QWidget()
        self.drone_rows_layout = QVBoxLayout(self.drone_rows_container)
        self.drone_rows_layout.setContentsMargins(0, 0, 0, 0)
        self.drone_rows_layout.setSpacing(10)

        self.lbl_status = CaptionLabel("等待 backend ready")
        self.lbl_status.setStyleSheet("color: #888;")
        self.btn_start_simulation = PrimaryPushButton(FluentIcon.PLAY, "启动仿真")
        self.btn_stop_simulation = PushButton(FluentIcon.PAUSE, "停止仿真")
        self.btn_start_simulation.setEnabled(False)
        self.btn_stop_simulation.setEnabled(False)

    def _init_layout(self):
        title_row = QHBoxLayout()
        title_row.addWidget(self.head_icon)
        title_row.addWidget(self.head_title)
        title_row.addStretch()
        title_row.addWidget(self.lbl_status)
        self.v_layout.addLayout(title_row)

        grid = QGridLayout()
        grid.setHorizontalSpacing(15)
        grid.setVerticalSpacing(14)
        grid.setColumnStretch(1, 4)
        grid.addWidget(BodyLabel("仿真环境:"), 0, 0)
        env_row = QHBoxLayout()
        env_row.setSpacing(10)
        env_row.addWidget(self.env_combo)
        env_row.addWidget(self.btn_upload_env)
        grid.addLayout(env_row, 0, 1)
        grid.addWidget(BodyLabel("无人机数量:"), 0, 2)
        grid.addWidget(self.count_edit, 0, 3)
        self.v_layout.addLayout(grid)

        self.v_layout.addWidget(Separator())
        self.v_layout.addWidget(self.drone_rows_container)

        action_row = QHBoxLayout()
        action_row.addStretch()
        action_row.addWidget(self.btn_start_simulation)
        action_row.addWidget(self.btn_stop_simulation)
        self.v_layout.addLayout(action_row)

    def _connect_signal_slot(self):
        self.env_combo.currentIndexChanged.connect(lambda _index: self._on_environment_changed())
        self.count_edit.textChanged.connect(lambda _text: self._rebuild_drone_rows())
        self.btn_upload_env.clicked.connect(self.upload_environment_requested.emit)
        self.btn_start_simulation.clicked.connect(self._emit_start)
        self.btn_stop_simulation.clicked.connect(self.stop_simulation_requested.emit)

    def set_environments(self, response: object):
        self._environments = response_items(response, "environments")
        self.env_combo.blockSignals(True)
        self.env_combo.clear()
        for env in self._environments:
            label = _display_value(env, "environment_template_id", "display_name")
            self.env_combo.addItem(label)
        self.env_combo.blockSignals(False)
        if self._environments:
            self.env_combo.setCurrentIndex(0)
            self._on_environment_changed()
            self.set_status("环境列表已加载", ok=True)
        else:
            self.set_status("无可用环境", ok=False)
            self._rebuild_drone_rows()
        self._update_buttons()

    def set_status(self, text: str, *, ok: bool = False):
        color = "#1a7f37" if ok else "#888888"
        if "失败" in text or "错误" in text:
            color = "#c62828"
        self.lbl_status.setText(text)
        self.lbl_status.setStyleSheet(f"color: {color};")

    def set_session_state(self, state: RuntimeSessionState):
        self._session_state = state
        self._update_buttons()

    def selected_environment(self) -> dict | None:
        index = self.env_combo.currentIndex()
        if 0 <= index < len(self._environments):
            return self._environments[index]
        return None

    def _on_environment_changed(self):
        env = self.selected_environment()
        if env is not None:
            default_count = int(env.get("default_drone_count") or env.get("min_drone_count") or 1)
            self.count_edit.setText(str(max(default_count, 1)))
        self._rebuild_drone_rows()

    def _rebuild_drone_rows(self):
        while self.drone_rows_layout.count():
            item = self.drone_rows_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._row_widgets = []

        env = self.selected_environment()
        try:
            count = max(int(self.count_edit.text()), 1)
        except ValueError:
            count = 1
        if env is None:
            self._update_buttons()
            return

        drone_options = [item for item in env.get("drone_type_options", []) if isinstance(item, dict)]
        placements = [item for item in env.get("placements", []) if isinstance(item, dict) and item.get("type") == "pose"]
        for index in range(count):
            row = QFrame()
            row.setStyleSheet(".QFrame{background-color: rgba(255,255,255,0.04); border-radius: 8px;}")
            layout = QHBoxLayout(row)
            layout.setContentsMargins(14, 10, 14, 10)
            layout.setSpacing(12)
            label = StrongBodyLabel(f"Drone {index + 1}")
            type_combo = ComboBox()
            placement_combo = ComboBox()
            for option in drone_options:
                type_combo.addItem(_display_value(option, "drone_type_option_id", "display_name"))
            for placement in placements:
                placement_combo.addItem(_display_value(placement, "placement_id"))
            layout.addWidget(label)
            layout.addWidget(CaptionLabel("类型"))
            layout.addWidget(type_combo)
            layout.addWidget(CaptionLabel("起点"))
            layout.addWidget(placement_combo)
            layout.addStretch()
            self.drone_rows_layout.addWidget(row)
            self._row_widgets.append(
                {"type_combo": type_combo, "placement_combo": placement_combo, "types": drone_options, "placements": placements}
            )
        self._update_buttons()

    def _emit_start(self):
        env = self.selected_environment()
        if env is None:
            return
        drone_rows = []
        for row in self._row_widgets:
            type_index = row["type_combo"].currentIndex()
            placement_index = row["placement_combo"].currentIndex()
            if type_index < 0 or placement_index < 0:
                return
            drone_rows.append(
                {
                    "drone_type_option_id": row["types"][type_index]["drone_type_option_id"],
                    "startup_placement_id": row["placements"][placement_index]["placement_id"],
                }
            )
        payload = build_simulation_start_payload(env["environment_template_id"], drone_rows)
        self.start_simulation_requested.emit(payload)

    def _update_buttons(self):
        can_start = bool(
            self._environments
            and self._row_widgets
            and all(self._row_can_start(row) for row in self._row_widgets)
            and not self._session_state.simulation_running
            and self._session_state.gateway_connected
            and self._session_state.backend_state is BackendLifecycle.READY
        )
        self.btn_start_simulation.setEnabled(can_start)
        self.btn_stop_simulation.setEnabled(self._session_state.simulation_running)

    def _row_can_start(self, row: dict) -> bool:
        types = row.get("types")
        placements = row.get("placements")
        if not isinstance(types, list) or not isinstance(placements, list):
            return False
        type_index = row["type_combo"].currentIndex()
        placement_index = row["placement_combo"].currentIndex()
        return bool(
            types
            and placements
            and 0 <= type_index < len(types)
            and 0 <= placement_index < len(placements)
        )


class AgentConfigWidget(QFrame):
    binding_changed = Signal()
    config_changed = Signal()

    def __init__(
        self,
        agent_id: str,
        template: dict,
        config: dict,
        runtime_entities: list[dict],
        *,
        agent_ids: list[str] | None = None,
        environment_semantics: dict[str, list[str]] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.agent_id = agent_id
        self.template = template
        self.config = dict(config)
        self.runtime_entities = list(runtime_entities)
        self.agent_ids = [str(item).strip() for item in (agent_ids or []) if str(item).strip()]
        if not self.agent_ids:
            self.agent_ids = [self.agent_id]
        self.environment_semantics = {
            str(key): [str(item).strip() for item in value if str(item).strip()]
            for key, value in (environment_semantics or {}).items()
            if isinstance(value, list)
        }
        self.role_combos: dict[str, ComboBox] = {}
        self.param_widgets: dict[str, QWidget] = {}
        self.setStyleSheet(".QFrame{background-color: rgba(255,255,255,0.04); border-radius: 8px;}")
        self.v_layout = QVBoxLayout(self)
        self.v_layout.setContentsMargins(16, 12, 16, 12)
        self.v_layout.setSpacing(12)
        self._init_widgets()
        self._init_layout()

    def _init_widgets(self):
        self.entity_combo = ComboBox()
        self.entity_combo.setMinimumWidth(220)
        self.entity_combo.blockSignals(True)
        for entity in self.runtime_entities:
            self.entity_combo.addItem(entity["label"])
        selected = self.config.get("entity", {}).get("runtime_entity_id", "")
        selected_index = -1
        for index, entity in enumerate(self.runtime_entities):
            if entity.get("runtime_entity_id") == selected:
                selected_index = index
                break
        if not selected and self.runtime_entities:
            selected_index = 0
        self.entity_combo.setCurrentIndex(selected_index)
        self.entity_combo.blockSignals(False)
        self.entity_combo.currentIndexChanged.connect(lambda _index: self.binding_changed.emit())

        refs = self.template.get("refs", {}) if isinstance(self.template.get("refs"), dict) else {}
        selections = self.config.get("selections", {}) if isinstance(self.config.get("selections"), dict) else {}
        for role in ROLE_ORDER:
            options = refs.get(role, []) if isinstance(refs.get(role, []), list) else []
            if not options:
                continue
            combo = ComboBox()
            for option in options:
                combo.addItem(_display_value(option, "component_id", "name"))
            selected_component = selections.get(role, "")
            for index, option in enumerate(options):
                if option.get("component_id") == selected_component:
                    combo.setCurrentIndex(index)
                    break
            combo.currentIndexChanged.connect(lambda _index: self._on_role_changed())
            combo._flysim_options = options
            self.role_combos[role] = combo

        self.params_frame = QFrame()
        self.params_layout = QHBoxLayout(self.params_frame)
        self.params_layout.setContentsMargins(0, 0, 0, 0)
        self.params_layout.setSpacing(12)
        self._rebuild_task_params()

    def _reference_options_for_target(self, ref_target: str) -> list[str]:
        if ref_target == "agent":
            return list(self.agent_ids)
        return list(self.environment_semantics.get(ref_target, []))

    def _init_layout(self):
        info_row = QHBoxLayout()
        info_row.addWidget(CaptionLabel("绑定实体"))
        info_row.addWidget(self.entity_combo)
        info_row.addStretch()
        self.v_layout.addLayout(info_row)

        if self.role_combos:
            role_grid = QGridLayout()
            role_grid.setHorizontalSpacing(15)
            role_grid.setVerticalSpacing(10)
            for index, (role, combo) in enumerate(self.role_combos.items()):
                row = index // 3
                col = (index % 3) * 2
                role_grid.addWidget(CaptionLabel(role.replace("_", " ").title()), row, col)
                role_grid.addWidget(combo, row, col + 1)
            self.v_layout.addWidget(Separator())
            self.v_layout.addLayout(role_grid)

        self.v_layout.addWidget(Separator())
        self.v_layout.addWidget(self.params_frame)

    def runtime_binding(self) -> dict | None:
        entity_index = self.entity_combo.currentIndex()
        if 0 <= entity_index < len(self.runtime_entities):
            source = self.runtime_entities[entity_index]
            return {
                "runtime_entity_id": source["runtime_entity_id"],
                "runtime_entity_kind": source["runtime_entity_kind"],
                "ready": source.get("ready", False),
            }
        return None

    def save_config(self) -> dict:
        selections = {}
        for role, combo in self.role_combos.items():
            options = getattr(combo, "_flysim_options", [])
            index = combo.currentIndex()
            if 0 <= index < len(options):
                selections[role] = options[index].get("component_id", "")
        task_parameters = {}
        task_schema = self._selected_task_schema()
        for name, widget in self.param_widgets.items():
            if isinstance(widget, ComboBox):
                raw_value = widget.currentText()
            elif isinstance(widget, LineEdit):
                raw_value = widget.text()
            else:
                continue
            include, value = self._coerce_task_param_value(raw_value, task_schema.get(name, {}))
            if include:
                task_parameters[name] = value
        params = dict(self.config.get("params", {}) if isinstance(self.config.get("params"), dict) else {})
        params["task"] = task_parameters
        saved = dict(self.config)
        saved["selections"] = selections
        saved["params"] = params
        saved.pop("task_parameters", None)
        return saved

    def _coerce_task_param_value(self, value: object, spec: dict) -> tuple[bool, object]:
        if not isinstance(spec, dict):
            return True, value
        value_type = str(spec.get("type") or "").strip()
        if isinstance(value, str) and not value.strip() and not bool(spec.get("required", False)):
            return False, None
        if value_type == "number":
            try:
                return True, float(value)
            except (TypeError, ValueError):
                return True, value
        if value_type == "int":
            try:
                return True, int(value)
            except (TypeError, ValueError):
                return True, value
        if value_type == "bool":
            if isinstance(value, bool):
                return True, value
            return True, str(value).strip().lower() == "true"
        if value_type == "list" and isinstance(value, str):
            try:
                parsed = yaml.safe_load(value)
            except yaml.YAMLError:
                return True, value
            return True, parsed if isinstance(parsed, list) else value
        return True, value

    def _on_role_changed(self):
        self._rebuild_task_params()
        self.config_changed.emit()

    def _rebuild_task_params(self):
        while self.params_layout.count():
            item = self.params_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.param_widgets = {}
        schema = self._selected_task_schema()
        if not schema:
            self.params_layout.addWidget(CaptionLabel("当前 task 无参数"))
            self.params_layout.addStretch()
            return
        params = self.config.get("params", {}) if isinstance(self.config.get("params"), dict) else {}
        existing = params.get("task", {}) if isinstance(params.get("task"), dict) else {}
        if not existing and isinstance(self.config.get("task_parameters"), dict):
            existing = self.config.get("task_parameters", {})
        for name, spec in schema.items():
            if not isinstance(spec, dict):
                continue
            self.params_layout.addWidget(CaptionLabel(name))
            value = existing.get(name, spec.get("default", ""))
            if spec.get("type") == "enum" and isinstance(spec.get("options"), list):
                widget = ComboBox()
                widget.addItems([str(option) for option in spec["options"]])
                index = widget.findText(str(value))
                if index >= 0:
                    widget.setCurrentIndex(index)
                widget.currentIndexChanged.connect(lambda _index: self.config_changed.emit())
            elif spec.get("type") == "bool":
                widget = ComboBox()
                widget.addItems(["false", "true"])
                widget.setCurrentText("true" if bool(value) else "false")
                widget.currentIndexChanged.connect(lambda _index: self.config_changed.emit())
            elif spec.get("type") == "reference":
                options = self._reference_options_for_target(str(spec.get("ref_target", "")).strip())
                if options:
                    widget = ComboBox()
                    widget.addItems(options)
                    index = widget.findText(str(value))
                    if index >= 0:
                        widget.setCurrentIndex(index)
                    widget.currentIndexChanged.connect(lambda _index: self.config_changed.emit())
                else:
                    widget = LineEdit()
                    widget.setText(str(value))
                    widget.textChanged.connect(lambda _text: self.config_changed.emit())
            else:
                widget = LineEdit()
                widget.setText(str(value))
                widget.textChanged.connect(lambda _text: self.config_changed.emit())
            self.param_widgets[name] = widget
            self.params_layout.addWidget(widget)
        self.params_layout.addStretch()

    def _selected_task_schema(self) -> dict:
        task_combo = self.role_combos.get("task")
        if task_combo is None:
            return {}
        options = getattr(task_combo, "_flysim_options", [])
        index = task_combo.currentIndex()
        if not (0 <= index < len(options)):
            return {}
        config_schema = options[index].get("config_schema", {})
        if not isinstance(config_schema, dict):
            return {}
        if isinstance(config_schema.get("task_parameters"), dict):
            return config_schema["task_parameters"]
        if isinstance(config_schema.get("properties"), dict):
            return config_schema["properties"]
        return {key: value for key, value in config_schema.items() if isinstance(value, dict)}


class TrainingConfigCard(CardWidget):
    upload_package_requested = Signal()
    package_selected = Signal(str)
    recipe_selected = Signal(str)
    save_recipe_requested = Signal(dict)
    start_training_requested = Signal(dict)
    stop_training_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.v_layout = QVBoxLayout(self)
        self.v_layout.setSpacing(16)
        self.v_layout.setContentsMargins(24, 24, 24, 24)
        self._packages: list[dict] = []
        self._recipes: list[dict] = []
        self._manifest: dict = {}
        self._agent_templates: list[dict] = []
        self._current_agent_index = 0
        self._current_agent_widget: AgentConfigWidget | None = None
        self._current_recipe: dict | None = None
        self._recipe_draft: TrainingRecipeDraft | None = None
        self._runtime_entities: list[dict] = []
        self._environment_semantics: dict[str, list[str]] = {}
        self._default_recipe_id = ""
        self._loaded_package_id = ""
        self._session_state = RuntimeSessionState()
        self._init_widgets()
        self._init_layout()
        self._connect_signal_slot()
        self._update_buttons()

    def _init_widgets(self):
        self.head_icon = IconWidget(FluentIcon.ROBOT)
        self.head_icon.setFixedSize(24, 24)
        self.head_title = SubtitleLabel("训练配置")
        self.lbl_status = CaptionLabel("等待训练包")
        self.lbl_status.setStyleSheet("color: #888;")

        self.package_combo = ComboBox()
        self.package_combo.setPlaceholderText("选择训练包...")
        self.btn_upload_package = PushButton(FluentIcon.ZIP_FOLDER, "上传训练包")
        self.recipe_combo = ComboBox()
        self.recipe_combo.setPlaceholderText("选择 recipe...")
        self.recipe_combo.setEnabled(False)
        self.btn_save_recipe = PushButton(FluentIcon.SAVE, "保存 recipe")
        self.btn_save_recipe.setEnabled(False)

        self.agent_selector = ComboBox()
        self.agent_selector.setFixedWidth(160)
        self.agent_config_container = QWidget()
        self.agent_config_layout = QVBoxLayout(self.agent_config_container)
        self.agent_config_layout.setContentsMargins(0, 0, 0, 0)

        self.btn_start_training = PrimaryPushButton(FluentIcon.PLAY, "启动训练")
        self.btn_stop_training = PushButton(FluentIcon.PAUSE, "停止训练")
        self.btn_start_training.setEnabled(False)
        self.btn_stop_training.setEnabled(False)

    def _init_layout(self):
        title_row = QHBoxLayout()
        title_row.addWidget(self.head_icon)
        title_row.addWidget(self.head_title)
        title_row.addStretch()
        title_row.addWidget(self.lbl_status)
        self.v_layout.addLayout(title_row)

        grid = QGridLayout()
        grid.setHorizontalSpacing(15)
        grid.setVerticalSpacing(14)
        grid.setColumnStretch(1, 3)
        grid.setColumnStretch(3, 3)
        package_row = QHBoxLayout()
        package_row.setSpacing(10)
        package_row.addWidget(self.package_combo)
        package_row.addWidget(self.btn_upload_package)
        grid.addWidget(BodyLabel("训练包:"), 0, 0)
        grid.addLayout(package_row, 0, 1)
        recipe_row = QHBoxLayout()
        recipe_row.setSpacing(10)
        recipe_row.addWidget(self.recipe_combo)
        recipe_row.addWidget(self.btn_save_recipe)
        grid.addWidget(BodyLabel("Recipe:"), 0, 2)
        grid.addLayout(recipe_row, 0, 3)
        self.v_layout.addLayout(grid)

        selector_row = QHBoxLayout()
        selector_row.addWidget(BodyLabel("选择 Agent:"))
        selector_row.addWidget(self.agent_selector)
        selector_row.addStretch()
        self.v_layout.addWidget(Separator())
        self.v_layout.addLayout(selector_row)
        self.v_layout.addWidget(self.agent_config_container)

        action_row = QHBoxLayout()
        action_row.addStretch()
        action_row.addWidget(self.btn_start_training)
        action_row.addWidget(self.btn_stop_training)
        self.v_layout.addLayout(action_row)

    def _connect_signal_slot(self):
        self.btn_upload_package.clicked.connect(self.upload_package_requested.emit)
        self.package_combo.currentIndexChanged.connect(lambda _index: self._emit_package_selected())
        self.recipe_combo.currentIndexChanged.connect(lambda _index: self._emit_recipe_selected())
        self.agent_selector.currentIndexChanged.connect(self._on_agent_selected)
        self.btn_save_recipe.clicked.connect(self._emit_save_recipe)
        self.btn_start_training.clicked.connect(self._emit_start_training)
        self.btn_stop_training.clicked.connect(lambda: self.stop_training_requested.emit(self._session_state.training_run_id or ""))

    def set_packages(self, response: object):
        current_package_id = self.current_package_id()
        self._packages = response_items(response, "packages")
        self.package_combo.blockSignals(True)
        self.package_combo.clear()
        selected_index = 0
        for index, package in enumerate(self._packages):
            package_id = _id_value(package, "package_id")
            if package_id == current_package_id:
                selected_index = index
            self.package_combo.addItem(_display_value(package, "package_id", "package_name"))
        if self._packages:
            self.package_combo.setCurrentIndex(selected_index)
        self.package_combo.blockSignals(False)
        if self._packages:
            self._emit_package_selected()
        else:
            self.set_status("无训练包")
        self._update_buttons()

    def load_package(self, package: dict, *, default_recipe_id: str = ""):
        package_id = _id_value(package, "package_id")
        if not package_id:
            return
        for index, item in enumerate(self._packages):
            if _id_value(item, "package_id") == package_id:
                self.package_combo.blockSignals(True)
                self.package_combo.setCurrentIndex(index)
                self.package_combo.blockSignals(False)
                self._packages[index] = package
                break
        package_changed = package_id != self._loaded_package_id
        self._loaded_package_id = package_id
        if package_changed or default_recipe_id:
            self._default_recipe_id = default_recipe_id
        self._manifest = yaml.safe_load(package.get("package_manifest_yaml") or "{}") or {}
        self._agent_templates = self._agent_templates_from_manifest(self._manifest)
        self.set_status("训练包已加载", ok=True)
        self._rebuild_agent_selector()
        self._update_buttons()

    def set_environment_semantics(self, semantics: dict):
        normalized: dict[str, list[str]] = {}
        if isinstance(semantics, dict):
            for key, value in semantics.items():
                if isinstance(value, list):
                    normalized_key = SEMANTICS_REF_TARGET_KEYS.get(str(key), str(key))
                    normalized[normalized_key] = [str(item).strip() for item in value if str(item).strip()]
        if normalized == self._environment_semantics:
            return
        self._environment_semantics = normalized
        if self._recipe_draft is not None and self._agent_templates:
            self._save_current_agent_binding()
            self._rebuild_agent_panel(self._current_agent_index)
            if self._recipe_draft.dirty:
                self.set_status("Recipe 有未保存修改")
            self._update_buttons()

    def select_package(self, package_id: str) -> bool:
        for index, package in enumerate(self._packages):
            if _id_value(package, "package_id") == package_id:
                self.package_combo.setCurrentIndex(index)
                return True
        return False

    def set_recipes(self, response: object):
        package_id = self.current_package_id()
        recipes = response_items(response, "recipes")
        self._recipes = [
            recipe for recipe in recipes
            if recipe.get("owner_type") == "package" and recipe.get("owner_id") == package_id
        ]
        self.recipe_combo.blockSignals(True)
        self.recipe_combo.clear()
        selected_index = 0
        default_recipe_id = self._default_recipe_id
        for index, recipe in enumerate(self._recipes):
            recipe_id = _id_value(recipe, "recipe_id")
            if recipe_id == default_recipe_id:
                selected_index = index
            self.recipe_combo.addItem(_display_value(recipe, "recipe_id", "name"))
        if self._recipes:
            self.recipe_combo.setCurrentIndex(selected_index)
        self.recipe_combo.setEnabled(bool(self._recipes))
        self.recipe_combo.blockSignals(False)
        if self._recipes:
            self._emit_recipe_selected()
        else:
            self._current_recipe = None
            self._recipe_draft = None
            self.set_status("当前训练包无 recipe")
        self._update_buttons()

    def load_recipe(self, recipe: dict):
        if "recipe" in recipe and isinstance(recipe["recipe"], dict):
            recipe = recipe["recipe"]
        self._current_recipe = recipe
        self._recipe_draft = TrainingRecipeDraft.from_recipe_payload(recipe)
        self.set_status("Recipe 已加载", ok=True)
        self._rebuild_agent_selector()
        self._rebuild_agent_panel(0)
        self._save_current_agent_binding()
        self._update_buttons()

    def set_runtime_entities(self, options: list[dict]):
        if options == self._runtime_entities:
            return
        self._save_current_agent_binding()
        self._runtime_entities = list(options)
        self._rebuild_agent_panel(self._current_agent_index)
        self._save_current_agent_binding()
        self._update_buttons()

    def set_session_state(self, state: RuntimeSessionState):
        self._session_state = state
        self._update_buttons()

    def mark_recipe_saved(self):
        if self._recipe_draft is not None:
            revision = self._recipe_draft.pending_save_revision
            if revision is not None:
                self._recipe_draft.finish_save(revision, True)
        if self._recipe_draft is not None and self._recipe_draft.dirty:
            self.set_status("Recipe 有未保存修改")
        else:
            self.set_status("Recipe 已保存", ok=True)
        self._update_buttons()

    def mark_recipe_save_failed(self):
        if self._recipe_draft is not None:
            revision = self._recipe_draft.pending_save_revision
            if revision is not None:
                self._recipe_draft.finish_save(revision, False)
        self.set_status("Recipe 保存失败，仍有未保存修改")
        self._update_buttons()

    def set_status(self, text: str, *, ok: bool = False):
        color = "#1a7f37" if ok else "#888888"
        if "失败" in text or "错误" in text:
            color = "#c62828"
        self.lbl_status.setText(text)
        self.lbl_status.setStyleSheet(f"color: {color};")

    def current_package_id(self) -> str:
        index = self.package_combo.currentIndex()
        if 0 <= index < len(self._packages):
            return _id_value(self._packages[index], "package_id")
        return ""

    def current_package(self) -> dict | None:
        index = self.package_combo.currentIndex()
        if 0 <= index < len(self._packages):
            return self._packages[index]
        return None

    def _emit_package_selected(self):
        package_id = self.current_package_id()
        if package_id:
            self.package_selected.emit(package_id)

    def _emit_recipe_selected(self):
        index = self.recipe_combo.currentIndex()
        if 0 <= index < len(self._recipes):
            recipe_id = _id_value(self._recipes[index], "recipe_id")
            if recipe_id:
                self.recipe_selected.emit(recipe_id)

    def _emit_save_recipe(self):
        self._save_current_agent_binding()
        if self._recipe_draft is None:
            return
        self._recipe_draft.begin_save()
        self.set_status("Recipe 保存中...")
        self._update_buttons()
        self.save_recipe_requested.emit(self._recipe_draft.save_payload())

    def _emit_start_training(self):
        self._save_current_agent_binding()
        binding = self._first_ready_binding()
        if binding is None or not self._can_start_training(binding):
            return
        payload = build_training_run_payload(
            package_id=self.current_package_id(),
            recipe_id=_id_value(self._current_recipe or {}, "recipe_id"),
            run_name=self._generated_run_name(),
            agent_bindings=[binding],
        )
        self.start_training_requested.emit(payload)

    def _generated_run_name(self) -> str:
        package = self.current_package() or {}
        recipe = self._current_recipe or {}
        package_name = _display_value(package, "package_id", "package_name")
        recipe_name = _display_value(recipe, "recipe_id", "name")
        run_name = generated_run_name(package_name, recipe_name)
        if run_name:
            return run_name
        return generated_run_name(_id_value(package, "package_id"), _id_value(recipe, "recipe_id"))

    def _on_agent_selected(self, index: int):
        self._save_current_agent_binding()
        self._current_agent_index = max(index, 0)
        self._rebuild_agent_panel(self._current_agent_index)

    def _save_current_agent_config(self):
        if self._current_agent_widget is None or self._recipe_draft is None:
            return
        saved_config = self._current_agent_widget.save_config()
        self._recipe_draft.set_agent_config(self._current_agent_widget.agent_id, saved_config)

    def _save_current_agent_binding(self):
        if self._current_agent_widget is None or self._recipe_draft is None:
            return
        binding = self._current_agent_widget.runtime_binding()
        if binding is None:
            return
        self._recipe_draft.set_runtime_binding(self._current_agent_widget.agent_id, binding)

    def _rebuild_agent_selector(self):
        current = self.agent_selector.currentText()
        self.agent_selector.blockSignals(True)
        self.agent_selector.clear()
        for agent in self._agent_templates:
            self.agent_selector.addItem(agent["agent_id"])
        index = self.agent_selector.findText(current)
        self.agent_selector.setCurrentIndex(max(index, 0) if self._agent_templates else -1)
        self.agent_selector.blockSignals(False)

    def _rebuild_agent_panel(self, index: int):
        while self.agent_config_layout.count():
            item = self.agent_config_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._current_agent_widget = None
        if not (0 <= index < len(self._agent_templates)):
            self.agent_config_layout.addWidget(CaptionLabel("选择训练包和 recipe 后配置 agent"))
            return
        agent = self._agent_templates[index]
        config = self._agent_config_for_widget(agent["agent_id"])
        widget = AgentConfigWidget(
            agent["agent_id"],
            agent,
            config,
            self._runtime_entities,
            agent_ids=[item["agent_id"] for item in self._agent_templates],
            environment_semantics=self._environment_semantics,
        )
        widget.binding_changed.connect(self._on_agent_binding_changed)
        widget.config_changed.connect(self._on_agent_config_changed)
        self._current_agent_widget = widget
        self.agent_config_layout.addWidget(widget)

    def _on_agent_binding_changed(self):
        self._save_current_agent_binding()
        self._update_buttons()

    def _on_agent_config_changed(self):
        self._save_current_agent_config()
        if self._recipe_draft is not None and self._recipe_draft.dirty:
            self.set_status("Recipe 有未保存修改")
        self._update_buttons()

    def _agent_config_for_widget(self, agent_id: str) -> dict:
        if self._recipe_draft is None:
            return {}
        existing = self._recipe_draft.agent_config(agent_id)
        params = existing.get("params", {}) if isinstance(existing.get("params"), dict) else {}
        if "task" not in params and isinstance(existing.get("task_parameters"), dict):
            params["task"] = dict(existing.get("task_parameters", {}))
        normalized = dict(existing)
        normalized["selections"] = (
            dict(existing.get("selections", {})) if isinstance(existing.get("selections"), dict) else {}
        )
        normalized["params"] = params
        normalized["entity"] = self._recipe_draft.runtime_binding(agent_id)
        return normalized

    def _first_ready_binding(self) -> dict | None:
        if self._recipe_draft is None:
            return None
        for agent in self._agent_templates:
            agent_id = agent["agent_id"]
            entity = self._recipe_draft.runtime_binding(agent_id)
            runtime_entity_id = entity.get("runtime_entity_id", "")
            runtime_entity = self._runtime_entity_by_id(runtime_entity_id)
            if not runtime_entity or not runtime_entity.get("ready"):
                continue
            return {
                "agent_id": agent_id,
                "runtime_entity_id": runtime_entity["runtime_entity_id"],
                "runtime_entity_kind": runtime_entity["runtime_entity_kind"],
            }
        return None

    def _runtime_entity_by_id(self, runtime_entity_id: str) -> dict | None:
        for entity in self._runtime_entities:
            if entity.get("runtime_entity_id") == runtime_entity_id:
                return entity
        return None

    def _can_start_training(self, binding: dict | None = None) -> bool:
        if binding is None:
            binding = self._first_ready_binding()
        return bool(
            self._session_state.simulation_running
            and self._recipe_draft
            and not self._recipe_draft.dirty
            and not self._recipe_draft.save_pending
            and self._session_state.gateway_connected
            and self._session_state.backend_state is BackendLifecycle.READY
            and self.current_package_id()
            and self._current_recipe
            and self._runtime_entities
            and binding is not None
            and not self._session_state.training_run_id
        )

    def _update_buttons(self):
        can_save = bool(
            self._recipe_draft
            and self._recipe_draft.dirty
            and not self._recipe_draft.save_pending
        )
        can_start = self._can_start_training()
        self.btn_save_recipe.setEnabled(can_save)
        self.btn_start_training.setEnabled(can_start)
        self.btn_stop_training.setEnabled(bool(self._session_state.training_run_id))

    @staticmethod
    def _agent_templates_from_manifest(manifest: dict) -> list[dict]:
        templates = manifest.get("agent_templates", []) if isinstance(manifest, dict) else []
        result = []
        for index, template in enumerate(templates):
            if not isinstance(template, dict):
                continue
            agent = dict(template)
            agent["agent_id"] = str(agent.get("agent_id") or agent.get("id") or f"agent_{index}")
            result.append(agent)
        if not result:
            result.append({"agent_id": "agent_0", "refs": {}})
        return result
