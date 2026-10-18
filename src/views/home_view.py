import json
import math
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QLabel,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QFrame,
    QStackedWidget,
    QFileDialog,
    QMessageBox,
)
from qfluentwidgets import (
    SubtitleLabel,
    BodyLabel,
    StrongBodyLabel,
    CaptionLabel,
    ComboBox,
    PrimaryPushButton,
    PushButton,
    TextEdit,
    FluentIcon,
    IconWidget,
    SegmentedWidget,
    ScrollArea,
    CardWidget,
)
import pyqtgraph as pg

from src.config.ui_performance import (
    GUI_FLUSH_INTERVAL_MS,
    LOG_MAX_BLOCKS,
    METRIC_MAX_POINTS,
    METRIC_REDRAW_INTERVAL_MS,
)
from src.controllers.gateway_controller import GatewayController, PendingGuiUpdateBatch
from src.controllers.gateway_payloads import first_default_recipe_id
from src.models.runtime_session_state import BackendLifecycle, RuntimeSessionState
from src.models.runtime_telemetry import RuntimeTelemetryStore
from src.views.runtime_cards import GatewayStatusCard, SimulationConfigCard, TrainingConfigCard


SEMANTIC_COLORS = {
    "success": "#1a7f37",
    "error": "#c62828",
    "muted": "#888888",
    "warning": "#d97706",
    "info": "#2f6feb",
    "neutral": "#4b5563",
}


def _stream_envelope_data(envelope: object) -> tuple[str, dict]:
    if not isinstance(envelope, dict):
        return "", {}
    message_type = str(envelope.get("type") or "").strip()
    data = envelope.get("data")
    if isinstance(data, dict):
        return message_type, data
    return message_type, {}


def _snapshot_response_data(result: object) -> tuple[int, dict] | None:
    if not isinstance(result, dict):
        return None
    generation = result.get("generation")
    payload = result.get("payload")
    if isinstance(generation, bool) or not isinstance(generation, int):
        return None
    if not isinstance(payload, dict):
        return None
    return generation, payload


def _format_float(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    return f"{float(value):.2f}"


def _format_stream_log_line(data: dict) -> str:
    parts = []
    level = str(data.get("level") or "").strip()
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
        return " ".join(parts)
    return json.dumps(data, ensure_ascii=False)


def _format_training_metric_summary(data: dict) -> str:
    parts = []
    episode = data.get("episode")
    if isinstance(episode, int) and not isinstance(episode, bool):
        parts.append(f"Episode {episode}")
    episode_return = _format_float(data.get("episode_return"))
    if episode_return:
        parts.append(f"Return {episode_return}")
    best_episode_return = _format_float(data.get("best_episode_return"))
    if best_episode_return:
        parts.append(f"Best {best_episode_return}")
    return " | ".join(parts)


def _format_training_status_summary(data: dict) -> str:
    parts = []
    phase = str(data.get("phase") or "").strip()
    parts.append(phase or "UNKNOWN")
    episode = data.get("episode")
    if isinstance(episode, int) and not isinstance(episode, bool):
        parts.append(f"Episode {episode}")
    step = data.get("step")
    if isinstance(step, int) and not isinstance(step, bool):
        parts.append(f"Step {step}")
    latest_reward = _format_float(data.get("latest_reward"))
    if latest_reward:
        parts.append(f"Latest Reward {latest_reward}")
    best_reward = _format_float(data.get("best_reward"))
    if best_reward:
        parts.append(f"Best Reward {best_reward}")
    message = str(data.get("message") or data.get("error_message") or "").strip()
    if message:
        parts.append(message)
    return " | ".join(parts)


def _training_phase_is_active(phase: str) -> bool:
    return phase in {"STARTING", "RUNNING"}


def _training_phase_is_terminal(phase: str) -> bool:
    return phase in {"COMPLETED", "CANCELLED", "FAILED"}


def _format_gateway_response_line(action: str, result: object) -> str:
    if isinstance(result, dict):
        message = str(result.get("message") or result.get("error_message") or "").strip()
        if message:
            return f"[{action}] {message}"
        for nested_key in ("package", "recipe"):
            nested = result.get(nested_key)
            if isinstance(nested, dict):
                nested_name = str(
                    nested.get("name")
                    or nested.get(f"{nested_key}_name")
                    or nested.get(f"{nested_key}_id")
                    or ""
                ).strip()
                if nested_name:
                    return f"[{action}] {nested_name}"
        for key in ("run_id", "package_id", "recipe_id", "environment_template_id"):
            value = str(result.get(key) or "").strip()
            if value:
                return f"[{action}] {key}={value}"
        for key in ("packages", "recipes", "environments"):
            value = result.get(key)
            if isinstance(value, list):
                return f"[{action}] {len(value)} {key}"
        if "success" in result:
            return f"[{action}] {'ok' if result.get('success') else 'failed'}"
    if isinstance(result, list):
        return f"[{action}] {len(result)} items"
    return f"[{action}] {result}"


def _backend_error_state(action: str, message: str) -> str:
    if action == "backend.start":
        return "启动超时" if "timed out" in message.lower() else "启动失败"
    if action == "backend.stop":
        return "停止超时" if "timed out" in message.lower() else "停止失败"
    return "未就绪"


class ControlPanelCard(CardWidget):
    """仿真控制台：包含日志切换、曲线显示与多机遥测"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.v_layout = QVBoxLayout(self)
        self.v_layout.setSpacing(12)
        self.v_layout.setContentsMargins(20, 20, 20, 20)

        self._init_widgets()
        self._init_layout()
        self._connect_signal_slot()

    def _init_widgets(self):
        """初始化组件"""

        # --- 标题栏组件 ---
        self.head_icon = IconWidget(FluentIcon.INFO)
        self.head_icon.setFixedSize(24, 24)
        self.head_title = SubtitleLabel(text="仿真控制台")

        # --- 日志/图表切换组件 ---
        self.pivot = SegmentedWidget()
        self.pivot.addItem(routeKey="sys_log", text="系统日志")
        self.pivot.addItem(routeKey="rl_log", text="训练日志")
        self.pivot.addItem(routeKey="plot", text="训练曲线")
        self.pivot.setFixedWidth(500)

        self.training_summary_indicator = QLabel()
        self.training_summary_indicator.setFixedSize(8, 8)
        self.training_summary = CaptionLabel("训练状态：等待训练数据")
        self._set_indicator_style(self.training_summary_indicator, SEMANTIC_COLORS["muted"])
        self.training_summary.setStyleSheet(
            f"color: {SEMANTIC_COLORS['muted']};"
        )

        log_style = """
            TextEdit {
                border: 1px solid ;
                border-radius: 5px;
                font-family: 'Consolas';
                font-size: 12px;
            }
        """

        self.sys_log = TextEdit()
        self.sys_log.document().setMaximumBlockCount(LOG_MAX_BLOCKS)
        self.sys_log.setReadOnly(True)
        self.sys_log.setStyleSheet(log_style)
        self.sys_log.setPlainText("感谢使用FlySim仿真平台！")

        self.rl_log = TextEdit()
        self.rl_log.document().setMaximumBlockCount(LOG_MAX_BLOCKS)
        self.rl_log.setReadOnly(True)
        self.rl_log.setStyleSheet(log_style)
        self.rl_log.clear()
        self._log_requires_separator = {
            id(self.sys_log): True,
            id(self.rl_log): False,
        }

        self.plot_widget = TrainingPlotWidget()

        self.stacked_widget = QStackedWidget()
        self.stacked_widget.addWidget(self.sys_log)
        self.stacked_widget.addWidget(self.rl_log)
        self.stacked_widget.addWidget(self.plot_widget)

        # --- 遥测数据组件 ---
        self.tele_widgets = {}
        self.status_widgets = {}
        self.status_indicators = {}
        self._telemetry_views_by_key = {}
        self._telemetry_keys = []
        self._telemetry_selector_signature = ()
        self._rendered_telemetry_key = None
        self._rendered_status_values = {}
        self._rendered_telemetry_values = {}

        self.drone_selector = ComboBox()
        self.drone_selector.setFixedWidth(180)

    def _init_layout(self):
        """初始化布局"""

        # --- 标题栏 ---
        title_row = QHBoxLayout()
        title_row.addWidget(self.head_icon)
        title_row.addWidget(self.head_title)
        title_row.addStretch()
        self.v_layout.addLayout(title_row)

        # --- 下方内容区 ---
        content_layout = QHBoxLayout()
        content_layout.setSpacing(25)

        # --- 左侧：日志/图表 ---
        layout = QVBoxLayout()
        layout.setSpacing(10)

        nav_layout = QHBoxLayout()
        nav_layout.addWidget(self.pivot)
        nav_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addLayout(nav_layout)

        summary_row = QHBoxLayout()
        summary_row.addWidget(self.training_summary_indicator)
        summary_row.addWidget(self.training_summary)
        summary_row.addStretch()
        layout.addLayout(summary_row)

        layout.addWidget(self.stacked_widget)

        content_layout.addLayout(layout, stretch=6)

        # --- 右侧：遥测数据 ---
        layout = QVBoxLayout()
        layout.setSpacing(2)

        header_row = QHBoxLayout()
        header_row.addWidget(StrongBodyLabel("实时遥测"))
        header_row.addStretch()
        header_row.addWidget(self.drone_selector)
        layout.addLayout(header_row)

        layout.addSpacing(8)

        status_grid = QGridLayout()
        status_grid.setVerticalSpacing(8)
        status_grid.setHorizontalSpacing(8)
        self.add_status_cell(status_grid, 0, 0, "Ready")
        self.add_status_cell(status_grid, 0, 1, "Armed")
        self.add_status_cell(status_grid, 1, 0, "Mode")
        self.add_status_cell(status_grid, 1, 1, "Lifecycle")
        layout.addLayout(status_grid)

        layout.addSpacing(8)

        telemetry_grid = QGridLayout()
        telemetry_grid.setVerticalSpacing(10)
        telemetry_grid.setHorizontalSpacing(10)
        row = 0
        row = self.add_tele_group(telemetry_grid, row, "Position", ["X", "Y", "Z"])
        row = self.add_tele_group(telemetry_grid, row, "Velocity", ["VX", "VY", "VZ"])
        row = self.add_tele_group(telemetry_grid, row, "Attitude", ["Roll", "Pitch", "Yaw"])
        row = self.add_tele_group(telemetry_grid, row, "Global", ["Lat", "Lon", "AMSL"])
        row = self.add_tele_group(telemetry_grid, row, "GPS", ["Fix", "Satellites"])
        self.add_tele_group(telemetry_grid, row, "Battery", ["Remaining", "Voltage", "Current"])
        layout.addLayout(telemetry_grid)

        content_layout.addLayout(layout, stretch=4)

        self.v_layout.addLayout(content_layout)
        self.clear_telemetry()

    def _connect_signal_slot(self):
        # 日志/图表切换
        self.stacked_widget.setCurrentIndex(0)
        self.pivot.currentItemChanged.connect(
            lambda k: self.stacked_widget.setCurrentIndex(
                ["sys_log", "rl_log", "plot"].index(k)
            )
        )
        # 遥测数据切换
        self.drone_selector.currentTextChanged.connect(self.on_drone_changed)

    def append_system_log(self, text: str, *, level: str | None = None):
        self.append_system_log_batch([(text, level)])

    def append_system_log_batch(self, records: list[tuple[str, str | None]]) -> None:
        self._append_log_batch(self.sys_log, records)

    def append_training_log(self, text: str, *, level: str | None = None):
        self.append_training_log_batch([(text, level)])

    def append_training_log_batch(self, records: list[tuple[str, str | None]]) -> None:
        self._append_log_batch(self.rl_log, records)

    def set_training_summary(self, text: str, *, ok: bool = False):
        color = SEMANTIC_COLORS["success"] if ok else SEMANTIC_COLORS["muted"]
        if "失败" in text or "错误" in text or "FAILED" in text:
            color = SEMANTIC_COLORS["error"]
        self.training_summary.setText(text or "训练状态：等待训练数据")
        self.training_summary.setStyleSheet(f"color: {color};")
        self._set_indicator_style(self.training_summary_indicator, color)

    def record_training_metric(self, metric: dict):
        self.plot_widget.record_training_metric(metric)

    def _append_log(self, widget: TextEdit, text: str, *, level: str | None = None):
        self._append_log_batch(widget, [(text, level)])

    def _append_log_batch(self, widget: TextEdit, records):
        if not records:
            return
        cursor = widget.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        widget.setTextCursor(cursor)
        if self._log_requires_separator.get(id(widget), False):
            self._insert_log_text(cursor, "\n")
        for text, level in records:
            self._insert_log_text(cursor, str(text).rstrip("\n") + "\n", level=level)
        widget.setTextCursor(cursor)
        self._log_requires_separator[id(widget)] = False

    def _insert_log_text(self, cursor: QTextCursor, text: str, *, level: str | None = None):
        color = self._log_color(level)
        if color is None:
            cursor.insertText(text)
        else:
            text_format = QTextCharFormat()
            text_format.setForeground(QColor(color))
            cursor.insertText(text, text_format)

    def _log_color(self, level: str | None):
        if level is None:
            return None
        level_name = str(level).strip().lower()
        if level_name in {"error", "critical", "fatal"}:
            return SEMANTIC_COLORS["error"]
        if level_name in {"warning", "warn"}:
            return SEMANTIC_COLORS["warning"]
        if level_name in {"debug", "trace"}:
            return SEMANTIC_COLORS["muted"]
        if level_name == "info":
            return SEMANTIC_COLORS["info"]
        return SEMANTIC_COLORS["neutral"]

    def add_status_cell(self, layout, row, col, title):
        container = QWidget()
        row_layout = QHBoxLayout(container)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(5)

        indicator = QLabel()
        indicator.setFixedSize(7, 7)
        title_label = CaptionLabel(title)
        title_label.setStyleSheet(f"color: {SEMANTIC_COLORS['muted']};")
        value_label = BodyLabel("N/A")
        row_layout.addWidget(indicator)
        row_layout.addWidget(title_label)
        row_layout.addWidget(value_label)
        row_layout.addStretch()

        layout.addWidget(container, row, col)
        self.status_widgets[title] = value_label
        self.status_indicators[title] = indicator
        self._set_status_value(title, "N/A", SEMANTIC_COLORS["muted"])

    def add_tele_group(self, layout, row, title, fields):
        group_label = StrongBodyLabel(title)
        group_label.setStyleSheet(f"color: {SEMANTIC_COLORS['neutral']};")
        layout.addWidget(group_label, row, 0, 1, 3)
        row += 1
        for index, field in enumerate(fields):
            self.add_tele_cell(layout, row, index, field, "N/A")
        return row + 1

    def add_tele_cell(self, layout, row, col, title, value):
        container = QWidget()
        row_layout = QHBoxLayout(container)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(5)

        lbl_title = CaptionLabel(title)
        lbl_title.setStyleSheet(f"color: {SEMANTIC_COLORS['muted']};")

        value_label = BodyLabel(value)
        value_label.setStyleSheet(
            f"color: {SEMANTIC_COLORS['neutral']};"
            "font-family: 'Consolas';"
            "font-size: 12px;"
            "font-weight: bold;"
        )
        row_layout.addWidget(lbl_title)
        row_layout.addWidget(value_label)
        row_layout.addStretch()

        layout.addWidget(container, row, col)
        self.tele_widgets[title] = value_label

    def _set_indicator_style(self, indicator, color):
        indicator.setStyleSheet(
            f"background-color: {color}; border-radius: {indicator.width() // 2}px;"
        )

    def _set_status_value(self, field, value, color):
        rendered = (value, color)
        if self._rendered_status_values.get(field) == rendered:
            return
        self.status_widgets[field].setText(value)
        self.status_widgets[field].setStyleSheet(f"color: {color};")
        self._set_indicator_style(self.status_indicators[field], color)
        self._rendered_status_values[field] = rendered

    def _clear_status(self):
        for field in self.status_widgets:
            self._set_status_value(field, "N/A", SEMANTIC_COLORS["muted"])

    def set_telemetry_entities(self, views):
        previous_key = self.selected_telemetry_key()
        next_views = []
        for view in views or []:
            key = self._normalize_key(self._view_value(view, "key"))
            label = self._view_value(view, "label")
            if key is None or not isinstance(label, str) or not label.strip():
                continue
            next_views.append((key, label, view))

        if not next_views:
            self.clear_telemetry()
            return

        signature = tuple((key, label) for key, label, _view in next_views)
        self._telemetry_views_by_key = {key: view for key, _label, view in next_views}
        self._telemetry_keys = [key for key, _label, _view in next_views]
        selected_key = previous_key if previous_key in self._telemetry_views_by_key else min(self._telemetry_keys)
        selected_index = self._telemetry_keys.index(selected_key)

        if signature != self._telemetry_selector_signature:
            was_blocked = self.drone_selector.blockSignals(True)
            self.drone_selector.clear()
            for _key, label, _view in next_views:
                self.drone_selector.addItem(label)
            self.drone_selector.setCurrentIndex(selected_index)
            self.drone_selector.blockSignals(was_blocked)
            self._telemetry_selector_signature = signature
        elif self.drone_selector.currentIndex() != selected_index:
            self.drone_selector.setCurrentIndex(selected_index)
        self.render_telemetry(self._telemetry_views_by_key[selected_key])

    def render_telemetry(self, view):
        key = self._normalize_key(self._view_value(view, "key"))
        if key != self._rendered_telemetry_key:
            self._rendered_telemetry_key = key
            self._rendered_status_values = {}
            self._rendered_telemetry_values = {}
        telemetry = self._view_value(view, "telemetry")
        status = self._view_value(view, "status")
        self._render_status(status)
        self._render_telemetry_values(telemetry)

    def clear_telemetry(self):
        self._telemetry_views_by_key = {}
        self._telemetry_keys = []
        self._telemetry_selector_signature = ()
        self._rendered_telemetry_key = None
        self._rendered_status_values = {}
        self._rendered_telemetry_values = {}
        was_blocked = self.drone_selector.blockSignals(True)
        self.drone_selector.clear()
        self.drone_selector.blockSignals(was_blocked)
        for widget in self.tele_widgets.values():
            widget.setText("N/A")
        self._clear_status()

    def selected_telemetry_key(self):
        index = self.drone_selector.currentIndex()
        if index < 0 or index >= len(self._telemetry_keys):
            return None
        return self._telemetry_keys[index]

    def on_drone_changed(self, text):
        key = self.selected_telemetry_key()
        view = self._telemetry_views_by_key.get(key)
        if view is None:
            self._rendered_telemetry_key = None
            self._rendered_status_values = {}
            self._rendered_telemetry_values = {}
            for widget in self.tele_widgets.values():
                widget.setText("N/A")
            self._clear_status()
            return
        self.render_telemetry(view)

    def _render_status(self, status):
        if status is None:
            values = {
                field: ("N/A", SEMANTIC_COLORS["muted"])
                for field in self.status_widgets
            }
        else:
            ready = self._view_value(status, "ready")
            armed = self._view_value(status, "px4_armed")
            mode = self._view_value(status, "px4_nav_state_name")
            lifecycle = self._view_value(status, "lifecycle_state")
            values = {
                "Ready": self._format_status_value(
                    ready, "Ready", "Not ready", "success", "error"
                ),
                "Armed": self._format_status_value(
                    armed, "Armed", "Disarmed", "warning", "muted"
                ),
                "Mode": self._format_text_value(mode),
                "Lifecycle": self._format_text_value(lifecycle),
            }
        for field, (value, color) in values.items():
            self._set_status_value(field, value, color)

    def _format_status_value(
        self, value, true_text, false_text, true_color, false_color
    ):
        if value is True:
            return true_text, SEMANTIC_COLORS[true_color]
        if value is False:
            return false_text, SEMANTIC_COLORS[false_color]
        return "N/A", SEMANTIC_COLORS["muted"]

    def _format_text_value(self, value):
        text = self._format_text(value)
        color = SEMANTIC_COLORS["muted"] if text == "N/A" else SEMANTIC_COLORS["neutral"]
        return text, color

    def _render_telemetry_values(self, telemetry):
        values = {}
        values.update(
            self._format_group(
                telemetry,
                "position_valid",
                {"X": ("x_m", 2), "Y": ("y_m", 2), "Z": ("z_m", 2)},
            )
        )
        values.update(
            self._format_group(
                telemetry,
                "velocity_valid",
                {"VX": ("vx_mps", 2), "VY": ("vy_mps", 2), "VZ": ("vz_mps", 2)},
            )
        )
        values.update(
            self._format_group(
                telemetry,
                "attitude_valid",
                {"Roll": ("roll_rad", 1, "deg"), "Pitch": ("pitch_rad", 1, "deg"), "Yaw": ("yaw_rad", 1, "deg")},
            )
        )
        values.update(
            self._format_group(
                telemetry,
                "global_position_valid",
                {"Lat": ("latitude_deg", 6), "Lon": ("longitude_deg", 6), "AMSL": ("altitude_amsl_m", 2)},
            )
        )
        values.update(
            self._format_int_group(
                telemetry,
                "gps_valid",
                {"Fix": "gps_fix_type", "Satellites": "gps_satellites"},
            )
        )
        values.update(self._format_battery_group(telemetry))
        for field, widget in self.tele_widgets.items():
            value = values.get(field, "N/A")
            rendered = (value, SEMANTIC_COLORS["neutral"])
            if self._rendered_telemetry_values.get(field) == rendered:
                continue
            widget.setText(value)
            self._rendered_telemetry_values[field] = rendered

    def _format_group(self, telemetry, valid_attr, fields):
        if self._view_value(telemetry, valid_attr) is not True:
            return {field: "N/A" for field in fields}
        values = {}
        for field, spec in fields.items():
            attr = spec[0]
            decimals = spec[1]
            value = self._view_value(telemetry, attr)
            if len(spec) == 3 and spec[2] == "deg" and self._is_number(value):
                value = math.degrees(float(value))
            values[field] = self._format_number(value, decimals)
        return values

    def _format_int_group(self, telemetry, valid_attr, fields):
        if self._view_value(telemetry, valid_attr) is not True:
            return {field: "N/A" for field in fields}
        return {
            field: self._format_integer(self._view_value(telemetry, attr))
            for field, attr in fields.items()
        }

    def _format_battery_group(self, telemetry):
        if self._view_value(telemetry, "battery_valid") is not True:
            return {"Remaining": "N/A", "Voltage": "N/A", "Current": "N/A"}
        current = "N/A"
        if self._view_value(telemetry, "battery_current_valid") is True:
            current = self._format_number(self._view_value(telemetry, "battery_current_a"), 2)
        return {
            "Remaining": self._format_percent(self._view_value(telemetry, "battery_remaining")),
            "Voltage": self._format_number(self._view_value(telemetry, "battery_voltage_v"), 2),
            "Current": current,
        }

    def _format_text(self, value):
        text = str(value).strip() if value is not None else ""
        return text or "N/A"

    def _format_number(self, value, decimals):
        if not self._is_number(value):
            return "N/A"
        return f"{float(value):.{decimals}f}"

    def _format_integer(self, value):
        if not self._is_number(value):
            return "N/A"
        return str(int(value))

    def _format_percent(self, value):
        if not self._is_number(value):
            return "N/A"
        return f"{float(value) * 100:.0f}%"

    def _is_number(self, value):
        return not isinstance(value, bool) and isinstance(value, (int, float))

    def _view_value(self, source, name):
        if isinstance(source, dict):
            return source.get(name)
        return getattr(source, name, None)

    def _normalize_key(self, key):
        if isinstance(key, tuple) and len(key) == 2:
            return key
        if isinstance(key, list) and len(key) == 2:
            return tuple(key)
        return None

class TrainingPlotWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.v_layout = QVBoxLayout(self)
        self.v_layout.setContentsMargins(0, 0, 0, 0)
        self._episode_returns: dict[int, float] = {}
        self._render_dirty = False

        self.plot_widget = pg.PlotWidget(self)
        self.plot_widget.setBackground("#f9f9f9")
        self.plot_widget.setTitle("Episode Return", size="10pt")
        self.plot_widget.setLabel("bottom", "Episode")
        self.plot_widget.setLabel("left", "Return")
        self.plot_widget.showGrid(x=True, y=True, alpha=0.35)
        self._episode_return_curve = self.plot_widget.plot(
            [],
            [],
            pen=pg.mkPen(SEMANTIC_COLORS["info"], width=2),
            symbol="o",
            symbolSize=6,
            symbolBrush=SEMANTIC_COLORS["info"],
            symbolPen=pg.mkPen(SEMANTIC_COLORS["info"]),
        )

        self._redraw_timer = QTimer(self)
        self._redraw_timer.setInterval(METRIC_REDRAW_INTERVAL_MS)
        self._redraw_timer.setSingleShot(True)
        self._redraw_timer.timeout.connect(self.flush_pending_render)

        self.v_layout.addWidget(self.plot_widget)

    def record_training_metric(self, metric: dict):
        if not isinstance(metric, dict):
            return
        episode = metric.get("episode")
        episode_return = metric.get("episode_return")
        if isinstance(episode, bool) or not isinstance(episode, (int, float)):
            return
        if isinstance(episode_return, bool) or not isinstance(episode_return, (int, float)):
            return
        self._episode_returns[int(episode)] = float(episode_return)
        self._enforce_metric_cap()
        self._render_dirty = True
        if not self._redraw_timer.isActive():
            self._redraw_timer.start()

    def episode_return_points(self) -> list[tuple[int, float]]:
        return sorted(self._episode_returns.items())

    def flush_pending_render(self) -> None:
        if not self._render_dirty:
            return
        points = self.episode_return_points()
        if points:
            episodes, episode_returns = zip(*points)
            self._episode_return_curve.setData(list(episodes), list(episode_returns))
        else:
            self._episode_return_curve.setData([], [])
        self._render_dirty = False

    def _enforce_metric_cap(self) -> None:
        excess_count = len(self._episode_returns) - METRIC_MAX_POINTS
        if excess_count <= 0:
            return
        for episode in sorted(self._episode_returns)[:excess_count]:
            del self._episode_returns[episode]


class HomePage(ScrollArea):
    def __init__(self):
        super().__init__()
        self.home_widget = QWidget()
        self.home_layout = QVBoxLayout(self.home_widget)
        self.home_layout.setSpacing(10)

        self.setObjectName("flysim_home_interface")
        self.setStyleSheet("QScrollArea{border: none; background-color: transparent;}")
        self.home_widget.setStyleSheet("QWidget{background-color: transparent;}")

        self.gateway_status_card = GatewayStatusCard()
        self.gateway_controller = GatewayController(
            base_url=self.gateway_status_card.base_url_edit.text()
        )
        self._default_recipe_ids: dict[str, str] = {}
        self._pending_package_id = ""
        self._current_environment_id = ""
        self._close_after_backend_stop = False
        self._allow_close_after_backend_stop = False
        self._runtime_telemetry_store = RuntimeTelemetryStore()
        self._active_training_run_id: str | None = None

        self.simulation_card = SimulationConfigCard()
        self.training_card = TrainingConfigCard()
        self.control_panel_card = ControlPanelCard()

        self.home_layout.addWidget(self.gateway_status_card)
        self.home_layout.addWidget(self.simulation_card)
        self.home_layout.addWidget(self.training_card)
        self.home_layout.addWidget(self.control_panel_card)
        self.home_layout.addStretch()

        self.setWidget(self.home_widget)
        self.setWidgetResizable(True)

        self._connect_signals()
        self._gui_flush_timer = QTimer(self)
        self._gui_flush_timer.setInterval(GUI_FLUSH_INTERVAL_MS)
        self._gui_flush_timer.timeout.connect(self._flush_pending_gui_updates)
        self._gui_flush_timer.start()
        QTimer.singleShot(0, self.gateway_controller.bootstrap)

    def _connect_signals(self):
        self.gateway_status_card.retry_requested.connect(self.gateway_controller.bootstrap)
        self.gateway_status_card.backend_lifecycle_requested.connect(self._on_backend_lifecycle_requested)
        self.gateway_status_card.base_url_edit.editingFinished.connect(self._on_gateway_base_url_edited)

        self.simulation_card.upload_environment_requested.connect(self._on_upload_environment_requested)
        self.simulation_card.start_simulation_requested.connect(self.gateway_controller.start_simulation)
        self.simulation_card.stop_simulation_requested.connect(self.gateway_controller.stop_simulation)
        self.simulation_card.env_combo.currentIndexChanged.connect(self._on_simulation_environment_changed)

        self.training_card.upload_package_requested.connect(self._on_upload_training_package_requested)
        self.training_card.package_selected.connect(self._on_training_package_selected)
        self.training_card.recipe_selected.connect(self.gateway_controller.get_training_recipe)
        self.training_card.save_recipe_requested.connect(self.gateway_controller.save_training_recipe)
        self.training_card.start_training_requested.connect(self.gateway_controller.start_training_run)
        self.training_card.stop_training_requested.connect(self.gateway_controller.stop_training_run)

        self.gateway_controller.connection_generation_changed.connect(self._on_connection_generation_changed)
        self.gateway_controller.session_state_changed.connect(self._on_session_state_changed)
        self.gateway_controller.response_received.connect(self._on_gateway_response_received)
        self.gateway_controller.error_occurred.connect(self._on_gateway_error)

        self._on_session_state_changed(self.gateway_controller.session_state)

    def _on_gateway_base_url_edited(self):
        previous_base_url = self.gateway_controller.base_url
        updated = self.gateway_controller.set_base_url(
            self.gateway_status_card.base_url_edit.text()
        )
        if not updated:
            self.gateway_status_card.base_url_edit.setText(self.gateway_controller.base_url)
            return
        if self.gateway_controller.base_url != previous_base_url:
            self._clear_runtime_telemetry()

    def _on_connection_generation_changed(self, generation: int):
        if self.gateway_controller is not None:
            self.gateway_controller.clear_pending_gui_updates(generation)
        self._runtime_telemetry_store.begin_generation(generation)

    def _on_session_state_changed(self, state: RuntimeSessionState):
        if getattr(self, "_active_training_run_id", None) != state.training_run_id:
            self._flush_training_plot_render()
            self._active_training_run_id = state.training_run_id
        self.gateway_status_card.set_session_state(state)
        self.simulation_card.set_session_state(state)
        self.training_card.set_session_state(state)
        if state.backend_state is BackendLifecycle.READY:
            self.simulation_card.set_status("backend ready", ok=True)

    def _on_gateway_response_received(self, action: str, result: object):
        message = _format_gateway_response_line(action, result)
        if action.startswith("training."):
            self.control_panel_card.append_training_log(message)
        else:
            self.control_panel_card.append_system_log(message)

        self._apply_gateway_response(action, result)

    def _apply_gateway_response(self, action: str, result: object):
        if action == "backend.stop":
            self._flush_training_plot_render()
            self._clear_runtime_after_backend_stop()
            if self._close_after_backend_stop:
                self._close_after_backend_stop = False
                self._close_window_after_backend_stop()
            return
        if action == "simulation.list_environments":
            self._current_environment_id = ""
            self.simulation_card.set_environments(result)
            self._on_simulation_environment_changed(-1)
            return
        if action == "simulation.get_environment_semantics":
            if isinstance(result, dict):
                self.training_card.set_environment_semantics(result)
            return
        if action == "simulation.upload_environment":
            self.gateway_controller.list_environments()
            return
        if action == "simulation.start":
            return
        if action == "simulation.stop":
            self.training_card.set_runtime_entities([])
            self._clear_runtime_telemetry_contents()
            return
        if action == "runtime.entities.status":
            snapshot = _snapshot_response_data(result)
            if snapshot is None:
                return
            generation, payload = snapshot
            if self._runtime_telemetry_store.apply_bootstrap_snapshot(generation, payload):
                self._refresh_runtime_telemetry()
            return
        if action == "training.list_packages":
            self.training_card.set_packages(result)
            if self._pending_package_id and self.training_card.select_package(self._pending_package_id):
                self._pending_package_id = ""
            return
        if action == "training.upload_package" and isinstance(result, dict):
            package_id = str(result.get("package_id") or "")
            default_recipe_id = first_default_recipe_id(result)
            if package_id and default_recipe_id:
                self._default_recipe_ids[package_id] = default_recipe_id
            if package_id:
                self._pending_package_id = package_id
            self.gateway_controller.list_training_packages()
            if package_id:
                self.gateway_controller.get_training_package(package_id)
            return
        if action == "training.get_package":
            package = result.get("package", result) if isinstance(result, dict) else {}
            if isinstance(package, dict):
                package_id = str(package.get("package_id") or "")
                default_recipe_id = first_default_recipe_id(result) if isinstance(result, dict) else ""
                if package_id and default_recipe_id:
                    self._default_recipe_ids[package_id] = default_recipe_id
                self.training_card.load_package(
                    package,
                    default_recipe_id=self._default_recipe_ids.get(package_id, ""),
                )
                self.gateway_controller.list_training_recipes()
            return
        if action == "training.list_recipes":
            self.training_card.set_recipes(result)
            return
        if action == "training.get_recipe":
            if isinstance(result, dict):
                self.training_card.load_recipe(result)
            return
        if action == "training.save_recipe":
            succeeded = not (isinstance(result, dict) and result.get("success") is False)
            if succeeded:
                self.training_card.mark_recipe_saved()
                self.gateway_controller.list_training_recipes()
            else:
                self.training_card.mark_recipe_save_failed()
            return
        if action == "training.start_run":
            return
        if action == "training.stop_run":
            self._flush_training_plot_render()
            return

    def _flush_pending_gui_updates(self):
        controller = getattr(self, "gateway_controller", None)
        if controller is None:
            return
        self._apply_pending_gui_updates(controller.take_pending_gui_updates())

    def _apply_pending_gui_updates(self, batch: PendingGuiUpdateBatch):
        has_terminal_training_status = False
        for data in batch.training_statuses:
            phase = str(data.get("phase") or "").strip() if isinstance(data, dict) else ""
            has_terminal_training_status = has_terminal_training_status or _training_phase_is_terminal(phase)
            self._apply_training_status_update(data)
        for data in batch.simulation_statuses:
            self._apply_simulation_status_update(data)
        if batch.telemetry_snapshot is not None:
            self._apply_live_telemetry_snapshot(batch.generation, batch.telemetry_snapshot)
        self._append_pending_logs(
            self.control_panel_card.append_system_log_batch,
            batch.system_logs,
            batch.dropped_system_logs,
        )
        self._append_pending_logs(
            self.control_panel_card.append_training_log_batch,
            batch.training_logs,
            batch.dropped_training_logs,
        )
        if batch.training_metric is not None:
            self._apply_training_metric_update(batch.training_metric)
        if has_terminal_training_status:
            self._flush_training_plot_render()

    def _append_pending_logs(self, append_log_batch, records, dropped_count: int):
        pending_records = []
        if dropped_count:
            pending_records.append((f"[logs] dropped {dropped_count} log records", "warning"))
            records = tuple(records)[-max(0, LOG_MAX_BLOCKS - 2):]
        for text, level in records:
            pending_records.append((text, level))
        if pending_records:
            append_log_batch(pending_records)

    def _apply_training_metric_update(self, data: dict):
        self.control_panel_card.record_training_metric(data)
        summary = _format_training_metric_summary(data)
        if summary:
            self.control_panel_card.set_training_summary(summary, ok=True)
            self.training_card.set_status(summary, ok=True)

    def _apply_training_status_update(self, data: dict):
        summary = _format_training_status_summary(data)
        phase = str(data.get("phase") or "").strip()
        ok = _training_phase_is_active(phase) or phase == "COMPLETED"
        if summary:
            self.control_panel_card.set_training_summary(summary, ok=ok)
            self.training_card.set_status(summary, ok=ok)
        if _training_phase_is_terminal(phase):
            self._flush_training_plot_render()

    def _apply_simulation_status_update(self, data: dict):
        self._runtime_telemetry_store.update_simulation_status(data)
        runtime_entities = self._runtime_telemetry_store.ready_training_entity_options()
        self.training_card.set_runtime_entities(runtime_entities)
        self._refresh_runtime_telemetry()

    def _apply_live_telemetry_snapshot(self, generation: int, data: dict):
        if self._runtime_telemetry_store.apply_live_snapshot(generation, data):
            self._refresh_runtime_telemetry()

    def _on_gateway_error(self, action: str, message: str):
        self.control_panel_card.append_system_log(
            f"[ERROR] {action}: {message}\n", level="error"
        )
        if action == "training.save_recipe":
            self.training_card.mark_recipe_save_failed()

    def _on_simulation_environment_changed(self, _index: int):
        environment = self.simulation_card.selected_environment()
        if not isinstance(environment, dict):
            return
        environment_id = str(environment.get("environment_template_id") or "").strip()
        if not environment_id or environment_id == self._current_environment_id:
            return
        self._current_environment_id = environment_id
        self.gateway_controller.get_simulation_environment_semantics(environment_id)

    def _on_training_package_selected(self, package_id: str):
        self._flush_training_plot_render()
        package = self.training_card.current_package()
        if package is not None:
            self.training_card.load_package(
                package,
                default_recipe_id=self._default_recipe_ids.get(package_id, ""),
            )
        self.gateway_controller.get_training_package(package_id)

    def _on_upload_environment_requested(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择环境包", "", "压缩包 (*.zip);;所有文件 (*.*)"
        )
        if file_path:
            self.gateway_controller.upload_environment(file_path)

    def _on_upload_training_package_requested(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择训练包", "", "压缩包 (*.zip);;所有文件 (*.*)"
        )
        if file_path:
            self.gateway_controller.upload_training_package(file_path)

    def _on_backend_lifecycle_requested(self, action: str):
        if action == "start":
            self.gateway_controller.start_backend()
            return
        if action == "restart":
            self.gateway_controller.restart_backend()
            return
        if action == "cancel_start":
            self.gateway_controller.cancel_backend_start()
            return
        if action == "stop":
            self._request_stop_backend(confirm=True)

    def _request_stop_backend(self, *, confirm: bool, close_after_stop: bool = False) -> bool:
        if confirm and self._backend_stop_requires_confirmation() and not self._confirm_stop_backend():
            return False
        self._close_after_backend_stop = close_after_stop
        self.gateway_controller.stop_backend()
        return True

    def _backend_stop_requires_confirmation(self) -> bool:
        state = self._current_session_state()
        return bool(state.simulation_running or state.training_active)

    def _confirm_stop_backend(self) -> bool:
        result = QMessageBox.question(
            self,
            "停止后端",
            "停止后端会取消当前训练、停止仿真并清理相关进程。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return result == QMessageBox.StandardButton.Yes

    def prepare_close(self) -> bool:
        if self._allow_close_after_backend_stop:
            self._allow_close_after_backend_stop = False
            return self._close_gateway_controller()
        backend_state = self._current_session_state().backend_state
        if backend_state in {
            BackendLifecycle.STARTING,
            BackendLifecycle.READY,
            BackendLifecycle.STOPPING,
            BackendLifecycle.FAILED,
        }:
            choice = self._confirm_close_with_backend()
            if choice == "gui_only":
                return self._close_gateway_controller()
            if choice == "stop_and_exit":
                self._close_after_backend_stop = True
                if backend_state is BackendLifecycle.STARTING:
                    self.gateway_controller.cancel_backend_start()
                    return False
                if backend_state is BackendLifecycle.STOPPING:
                    return False
                self._request_stop_backend(confirm=False, close_after_stop=True)
                return False
            return False
        return self._close_gateway_controller()

    def _confirm_close_with_backend(self) -> str:
        box = QMessageBox(self)
        box.setWindowTitle("退出 FlySim")
        box.setText("Backend 仍在运行。")
        box.setInformativeText("是否停止后端并退出？")
        stop_button = box.addButton("停止后端并退出", QMessageBox.ButtonRole.AcceptRole)
        gui_button = box.addButton("仅退出 GUI", QMessageBox.ButtonRole.ActionRole)
        cancel_button = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(stop_button)
        box.exec()
        clicked = box.clickedButton()
        if clicked is stop_button:
            return "stop_and_exit"
        if clicked is gui_button:
            return "gui_only"
        if clicked is cancel_button:
            return "cancel"
        return "cancel"

    def _close_gateway_controller(self) -> bool:
        if self.gateway_controller is None:
            return True
        return bool(self.gateway_controller.close())

    def _close_window_after_backend_stop(self):
        self._allow_close_after_backend_stop = True
        window = self.window()
        if window is not None:
            QTimer.singleShot(0, window.close)

    def _clear_runtime_after_backend_stop(self):
        if self.gateway_controller is not None:
            self.gateway_controller.clear_pending_gui_updates()
        self.training_card.set_runtime_entities([])
        self._clear_runtime_telemetry()

    def _flush_training_plot_render(self):
        control_panel = getattr(self, "control_panel_card", None)
        plot_widget = getattr(control_panel, "plot_widget", None)
        if plot_widget is not None:
            plot_widget.flush_pending_render()

    def _refresh_runtime_telemetry(self):
        self.control_panel_card.set_telemetry_entities(self._runtime_telemetry_store.entities())

    def _clear_runtime_telemetry(self):
        self._runtime_telemetry_store.clear()
        self.control_panel_card.clear_telemetry()

    def _clear_runtime_telemetry_contents(self):
        self._runtime_telemetry_store.clear_contents()
        self.control_panel_card.clear_telemetry()

    def _current_session_state(self) -> RuntimeSessionState:
        controller = getattr(self, "gateway_controller", None)
        state = getattr(controller, "session_state", None)
        if isinstance(state, RuntimeSessionState):
            return state
        return RuntimeSessionState()
