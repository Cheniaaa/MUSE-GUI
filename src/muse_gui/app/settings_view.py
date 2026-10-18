"""设置只保存客户端profile并发出意图；入口组装负责连接与launcher。"""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFormLayout, QHBoxLayout, QSpinBox, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, ComboBox, LineEdit, PushButton, SubtitleLabel


class SettingsView(QWidget):
    profile_changed = Signal(dict)
    start_requested = Signal(dict)
    connect_requested = Signal(dict)
    status_requested = Signal(dict)
    stop_requested = Signal(dict, str)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._instance_id = None
        self._catalog_id = None
        self._bound_url = None
        layout = QVBoxLayout(self)
        layout.addWidget(SubtitleLabel("后端连接与本机设置", self))
        form = QFormLayout()
        self.mode_combo = ComboBox(self)
        self.mode_combo.addItem("WSL 本机后端", userData="local")
        self.mode_combo.addItem("仅连接已有地址", userData="connect")
        form.addRow("模式", self.mode_combo)
        self.url_edit = LineEdit(self)
        form.addRow("Server 地址", self.url_edit)
        self.local_fields = QWidget(self)
        local = QFormLayout(self.local_fields)
        self.fields = {}
        for name, label in (("distribution", "WSL distribution"), ("user", "Linux user"),
                ("project_dir", "Linux 项目目录"), ("venv_dir", "Linux venv 目录"),
                ("data_dir", "Linux 数据目录"), ("host", "HTTP / Viser 可达 host")):
            field = LineEdit(self.local_fields)
            self.fields[name] = field
            local.addRow(label, field)
        for name, label in (("http_port", "HTTP 端口"), ("viewer_port", "Viser 端口")):
            field = QSpinBox(self.local_fields)
            field.setRange(1, 65535)
            self.fields[name] = field
            local.addRow(label, field)
        layout.addLayout(form)
        layout.addWidget(self.local_fields)
        buttons = QHBoxLayout()
        self.save_button = PushButton("保存配置", self)
        self.start_button = PushButton("启动或连接本机后端", self)
        self.connect_button = PushButton("连接", self)
        self.status_button = PushButton("本机状态", self)
        self.stop_button = PushButton("停止本机后端", self)
        for button in (self.save_button, self.start_button, self.connect_button, self.status_button, self.stop_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.status_label = BodyLabel("未连接；首次请填写实际 WSL/Python 路径，不会自动安装依赖。", self)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.log_label = BodyLabel(self)
        self.log_label.setWordWrap(True)
        layout.addWidget(self.log_label)
        explanation = BodyLabel("关闭 GUI 只断开客户端，后端 Run 会继续。独立停止会取消活动 Run，保留排队项；停止失败不代表已停。", self)
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        layout.addStretch()
        self.save_button.clicked.connect(self._save)
        self.start_button.clicked.connect(lambda: self.start_requested.emit(self.profile()))
        self.connect_button.clicked.connect(lambda: self.connect_requested.emit(self.profile()))
        self.status_button.clicked.connect(lambda: self.status_requested.emit(self.profile()))
        self.stop_button.clicked.connect(lambda: self.stop_requested.emit(self.profile(), self._instance_id))
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        self.set_profile(settings.settings.value("backend/profile", {}))

    def set_profile(self, profile: dict) -> None:
        self.mode_combo.setCurrentIndex(1 if profile.get("mode") == "connect" else 0)
        for name, field in self.fields.items():
            if name in ("http_port", "viewer_port"):
                field.setValue(profile.get(name, 8000 if name == "http_port" else 8080))
            else:
                field.setText(profile.get(name, "127.0.0.1" if name == "host" else ""))
        self.url_edit.setText(profile.get("base_url", "http://127.0.0.1:8000"))
        self._catalog_id = profile.get("catalog_id")
        self._bound_url = profile.get("base_url")
        self._instance_id = None
        self._mode_changed()

    def profile(self) -> dict:
        value = {name: field.value() if name in ("http_port", "viewer_port") else field.text()
                 for name, field in self.fields.items()}
        value["mode"] = self.mode_combo.currentData()
        host = value["host"]
        value["base_url"] = self.url_edit.text() if value["mode"] == "connect" else (
            f"http://{'[' + host + ']' if ':' in host else host}:{value['http_port']}")
        if self._catalog_id is not None and value["base_url"] == self._bound_url:
            value["catalog_id"] = self._catalog_id
        return value

    def set_connection_state(self, state: dict) -> None:
        self.status_label.setText("连接：" + state["state"] + ("；" + str(state["error"]) if state.get("error") else ""))
        if state["state"] == "connected":
            self._instance_id = state["instance_id"]
            self._catalog_id = state["catalog_id"]
            self._bound_url = self.profile()["base_url"]
        elif state["state"] in ("disconnected", "failed"):
            self._instance_id = None
        self._mode_changed()

    def set_backend_state(self, state: dict) -> None:
        self.status_label.setText("本机：" + state["state"] + ("；" + str(state["error"]) if state.get("error") else ""))
        if state["state"] == "running":
            self._instance_id = state["instance_id"]
        elif state["state"] in ("stopped", "stale"):
            self._instance_id = None
        self.log_label.setText("\n".join(str(state[key]) for key in (
            "start_stdout_log", "start_stderr_log", "stdout_log", "stderr_log") if key in state))
        self._mode_changed()

    def _mode_changed(self):
        local = self.mode_combo.currentData() == "local"
        self.local_fields.setVisible(local)
        self.url_edit.setEnabled(not local)
        self.start_button.setVisible(local)
        self.status_button.setVisible(local)
        self.stop_button.setVisible(local)
        self.stop_button.setEnabled(local and self._instance_id is not None)

    def _save(self):
        profile = self.profile()
        self.settings.settings.setValue("backend/profile", profile)
        self.settings.settings.sync()
        self.profile_changed.emit(profile)
