"""研究配置宽表单；descriptor 只决定可见入口，有效值/来源仍来自 inspection。"""

import json
import re
from copy import deepcopy

from PySide6.QtCore import QSignalBlocker, QTimer, Signal
from PySide6.QtGui import QTextOption
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, ComboBox, MessageBox, PlainTextEdit, PushButton, StrongBodyLabel, SubtitleLabel

from .schema_editor import SchemaEditor, at, path_text


class ResearchConfigView(QWidget):
    value_committed = Signal(tuple, object)
    input_committed = Signal(tuple, object, tuple, bool)
    override_removed = Signal(tuple)
    pending_changed = Signal(str, object)
    inspect_requested = Signal()
    composition_requested = Signal(str)
    model_requested = Signal(str, str)
    field_located = Signal(QWidget)

    FIXED_FIELDS = ("scenario", "topology", "root_seed", "preview")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setLayout(QVBoxLayout())
        self.layout().setContentsMargins(4, 4, 4, 4)
        self.layout().setSpacing(14)
        self.layout().addWidget(SubtitleLabel("研究配置", self))
        row = QHBoxLayout()
        row.addWidget(StrongBodyLabel("实验组合", self))
        self.composition_combo = ComboBox(self)
        self.composition_combo.setMinimumWidth(180)
        self.composition_combo.currentIndexChanged.connect(self._composition_selected)
        row.addWidget(self.composition_combo, 1)
        self.inspect_button = PushButton("检查当前草稿", self)
        self.inspect_button.clicked.connect(self.inspect_requested.emit)
        row.addWidget(self.inspect_button)
        self.layout().addLayout(row)
        self.status_label = BodyLabel("尚未取得 Server schema；可继续打开或保存草稿。", self)
        self.status_label.setWordWrap(True)
        self.layout().addWidget(self.status_label)
        self.model_combos = {}
        self.model_rows = {}
        for kind in ("radio", "sensing"):
            row = QWidget(self)
            layout = QHBoxLayout(row)
            layout.addWidget(StrongBodyLabel(kind + " 模型", row))
            combo = ComboBox(row)
            combo.currentIndexChanged.connect(lambda index, kind=kind: self._model_selected(kind, index))
            layout.addWidget(combo, 1)
            self.model_combos[kind] = combo
            self.model_rows[kind] = row
            self.layout().addWidget(row)
            row.hide()
        self.details_view = PlainTextEdit(self)
        self.details_view.setReadOnly(True)
        self.details_view.setWordWrapMode(QTextOption.WrapMode.WrapAnywhere)
        self.details_view.setMinimumWidth(0)
        self.layout().addWidget(self.details_view)
        self.forms = QWidget(self)
        self.forms.setLayout(QVBoxLayout())
        self.forms.layout().setContentsMargins(0, 0, 0, 0)
        self.layout().addWidget(self.forms)
        self.errors_host = QWidget(self)
        self.errors_host.setLayout(QVBoxLayout())
        self.errors_host.layout().setContentsMargins(0, 0, 0, 0)
        self.layout().addWidget(self.errors_host)
        self._editors = []
        self.selection_editors = {}
        self.fixed_editor = self.run_editor = None
        self._catalog = None
        self._document = None
        self.report = None
        self.value_reader = None
        self.stale = True
        self.inspect_button.setEnabled(False)

    def set_catalog(self, **catalog):
        self._catalog = deepcopy(catalog)
        self.inspect_button.setEnabled(True)
        if self._document is not None:
            self.set_document(self._document)

    @property
    def descriptor(self):
        if self._catalog is None or self._document["catalog_id"] not in (None, self._catalog["catalog_id"]):
            return None
        request = self._document["request"]
        return next((record["metadata"] for record in self._catalog["training_packages"]
                     if (record["resource_id"], record["revision"]) == (request.get("training_package_id"), request.get("training_package_revision"))), None)

    @property
    def composition(self):
        descriptor = self.descriptor if self._catalog else None
        return descriptor["compositions"].get(self._document["request"].get("composition_id")) if descriptor else None

    def _composition_selected(self, index):
        value = self.composition_combo.itemData(index)
        if value is not None:
            self.composition_requested.emit(value)

    def _model_selected(self, kind, index):
        reference = self.model_combos[kind].itemData(index)
        if reference is not None:
            self.model_requested.emit(kind, reference)

    def set_document(self, document: dict, *, inspection_current=False):
        identity_fields = ("training_package_id", "training_package_revision", "composition_id", "model_choices")
        if self._document and (document["draft_id"] != self._document["draft_id"] or any(at(document["request"], (key,)) != at(self._document["request"], (key,)) for key in identity_fields)):
            self.report = None
        self._document = deepcopy(document)
        self.stale = not inspection_current
        if self._catalog is None:
            return
        self._clear(self.forms)
        self._editors = []
        self.selection_editors = {}
        request = document["request"]
        descriptor = self.descriptor
        composition = self.composition
        with QSignalBlocker(self.composition_combo):
            self.composition_combo.clear()
            self.composition_combo.addItem("选择已登记组合", userData=None)
            if descriptor:
                for identity in descriptor["compositions"]:
                    self.composition_combo.addItem(identity, userData=identity)
            self.composition_combo.setCurrentIndex(max(0, self.composition_combo.findData(request.get("composition_id"))))
        model_details = []
        for kind, combo in self.model_combos.items():
            binding = composition.get("model_bindings", {}).get(kind) if composition else None
            self.model_rows[kind].setVisible(binding is not None)
            with QSignalBlocker(combo):
                combo.clear()
                if binding:
                    for reference in binding["options"]:
                        combo.addItem(reference, userData=reference)
                    selected = request.get("model_choices", {}).get(kind, binding["default"])
                    combo.setCurrentIndex(combo.findData(selected))
                    meta = next((item["descriptor"] for item in self._catalog["networks"] if item["kind"] == kind and item["reference"] == selected), None) if selected.startswith("builtin:") else descriptor.get(kind + "_models", {}).get(selected)
                    model_details.append({"kind": kind, "reference": selected, "descriptor": {key: meta[key] for key in ("assumptions", "input_fields", "output_spec", "state_scope") if key in meta} if meta is not None else None})
        self.details_view.setPlainText("来源与只读能力\n" + json.dumps({"catalog_id": document["catalog_id"], "environment": {key: request.get(key) for key in ("environment_id", "environment_revision")}, "package": {key: request.get(key) for key in ("training_package_id", "training_package_revision")}, "task": composition.get("task") if composition else None,
            "algorithm": composition.get("algorithm") if composition else None, "models": model_details}, ensure_ascii=False, indent=2))
        public = deepcopy(self._catalog["schema"]["experiment_request"])
        unknown = {key: value for key, value in request.items() if key not in public["properties"]}
        public["properties"] = {key: node for key, node in public["properties"].items() if key in self.FIXED_FIELDS}
        public["required"] = [key for key in public.get("required", []) if key in self.FIXED_FIELDS]
        explicit = {key: value for key, value in request.items() if key in self.FIXED_FIELDS}
        fixed_section = {"missing": [], "errors": []}
        fixed_origins = {}
        for section in (self.report or {}).get("sections", []):
            if len(section["path"]) == 1 and section["path"][0] in self.FIXED_FIELDS:
                key = section["path"][0]
                fixed_origins.update({"/" + key + pointer: origin for pointer, origin in section["origins"].items()})
                fixed_section["missing"].extend("/" + key + pointer for pointer in section["missing"])
                fixed_section["errors"].extend({"field": key + ("." + error["field"] if error["field"] != "$" else ""), "message": error["message"]} for error in section["errors"])
        self.fixed_editor = self._editor(("request",), public, explicit, self._fixed_effective(), fixed_origins, fixed_section)
        self.fixed_editor.root_field.label.setText("场景与 Experiment 参数")
        for path, title in ((("request", "root_seed"), "seed · root_seed"), (("request", "topology", "world_count"), "B · world_count"), (("request", "scenario", "scene", "physics_dt"), "physics_dt · s"), (("request", "scenario", "scene", "control_substeps"), "control_substeps · physics ticks / decision")):
            if path in self.fixed_editor.fields:
                self.fixed_editor.fields[path].label.setText(title)
        if unknown:
            self._editor(("request",), {"type": "object", "properties": {key: {"type": "json", "description": "公共 schema 未声明：原值保留，后端裁决"} for key in unknown}, "additionalProperties": False}, unknown, {}, {})
        run = deepcopy(self._catalog["schema"]["run_request"])
        run["properties"].pop("experiment_id", None)
        run["required"] = [key for key in run.get("required", []) if key != "experiment_id"]
        self.forms.layout().addWidget(StrongBodyLabel("运行参数 · RunOptions（预算不乘 world 数）", self))
        self.run_editor = self._editor(("run_options",), run, document["run_options"], {}, {})
        self.run_editor.root_field.label.setText("RunOptions · 只属于下一次运行")
        if composition:
            for owner, kind, selections in self._selections(composition):
                for selection in selections:
                    path = ("request", "term_config", *owner, kind, selection["selection_id"])
                    self.forms.layout().addWidget(StrongBodyLabel(path_text(path[2:]) + " · " + selection["term"], self))
                    properties = {"enabled": {"type": "boolean", "description": "必选项：不允许禁用" if selection["required"] else "禁用保留已填参数"}}
                    if kind == "observation":
                        properties["history_length"] = {"type": "integer", "minimum": 0}
                    if kind == "reward":
                        properties["weight"] = {"type": "number"}
                    effective = {key: selection[key] for key in ("history_length", "weight") if key in selection}
                    effective["enabled"] = selection["default_enabled"]
                    resolved = next((item for item in (self.report or {}).get("resolved_experiment", {}).get("selections", []) if (tuple(item["owner_path"]), item["term_kind"], item["selection_id"]) == (owner, kind, selection["selection_id"])), None) if (self.report or {}).get("resolved_experiment") else None
                    if resolved:
                        effective.update({key: resolved[key] for key in properties})
                    override = at(document, path, {})
                    origins = {"/" + key: "override" if key in override else "composition" if key in selection or key == "enabled" else "resolved（未提供来源分层）" for key in effective}
                    editor = self._editor(path, {"type": "object", "properties": properties, "additionalProperties": False}, {key: value for key, value in override.items() if key in properties}, effective, origins)
                    self.selection_editors[path] = editor
                    if selection["required"]:
                        field = editor.fields[(*path, "enabled")]
                        field.mode.setEnabled(False)
                        field.editor.setEnabled(False)
        if self.report:
            for section in self.report["sections"]:
                if len(section["path"]) == 1 and section["path"][0] in ("scenario", "topology", "preview"):
                    continue
                path = ("request", *section["path"])
                editor = self._editor(path, section["schema"], at(document, path, {}), section["effective"], section["origins"], section)
                if path == ("request", "algorithm_config") and composition:
                    algorithm = descriptor.get("algorithms", {}).get(composition.get("algorithm"))
                    schedule = algorithm.get("training_schedule") if algorithm else None
                    if schedule and (*path, schedule["collection_steps"]) in editor.fields:
                        editor.fields[(*path, schedule["collection_steps"])].label.setText("H · " + schedule["collection_steps"])
        groups = at(document, ("request", "scenario", "entity_groups"), [])
        inspected = {tuple(section["path"]) for section in (self.report or {}).get("sections", [])}
        if isinstance(groups, list):
            for index, group in enumerate(groups):
                if not isinstance(group, dict):
                    continue
                path = ("request", "scenario", "entity_groups", index, "sensor_config")
                profile = next((item for item in self._catalog["sensors"] if item["sensor_profile_id"] == group.get("sensor_profile_id")), None)
                if profile is not None and path[1:] not in inspected:
                    self._editor(path, profile["config_schema"], at(document, path, {}), {}, {})
        if not inspection_current:
            self.mark_stale()

    def _fixed_effective(self):
        # 未检查时没有有效默认值；不在客户端展开 schema default。
        return {section["path"][0]: section["effective"] for section in (self.report or {}).get("sections", []) if len(section["path"]) == 1 and section["path"][0] in self.FIXED_FIELDS}

    def _editor(self, path, schema, explicit, effective, origins, section=None):
        editor = SchemaEditor(self.forms)
        editor.choices = self._choices(self._document["request"])
        editor.value_reader = self.value_reader
        if path == ("request",):
            groups = at(self._document, ("request", "scenario", "entity_groups"), [])
            if isinstance(groups, list):
                editor.excluded_paths = {("request", "scenario", "entity_groups", index, "sensor_config") for index in range(len(groups))}
        editor.value_committed.connect(self.value_committed.emit)
        editor.input_committed.connect(self.input_committed.emit)
        editor.override_removed.connect(self.override_removed.emit)
        editor.pending_changed.connect(self.pending_changed.emit)
        data = dict(section or {"missing": [], "errors": []}, path=list(path), schema=schema, effective=effective, origins=origins)
        editor.set_section(data, explicit, self._document["pending_inputs"], effective_current=not self.stale)
        self._editors.append(editor)
        self.forms.layout().addWidget(editor)
        return editor

    @staticmethod
    def _selections(composition):
        for role, data in composition.get("agent_roles", {}).items():
            for kind in ("observation", "reward", "cost"):
                yield ("roles", role), kind, data[kind + "_terms"]
        for group, data in composition.get("critic_inputs", {}).items():
            yield ("critic_inputs", group), "observation", data["observation_terms"]
        for kind in ("reward", "cost", "termination"):
            yield ("world",), kind, composition.get("world_" + kind + "_terms", [])

    def _choices(self, request):
        choices = {}
        scenario = request.get("scenario", {})
        if not isinstance(scenario, dict):
            return choices
        groups = scenario.get("entity_groups", [])
        if not isinstance(groups, list):
            return choices
        models = self._catalog["entity_models"]
        for index, group in enumerate(groups):
            if not isinstance(group, dict):
                continue
            base = ("request", "scenario", "entity_groups", index)
            choices[(*base, "model_id")] = [(record["resource_id"], record["resource_id"]) for record in models]
            model = next((record for record in models if (record["resource_id"], record["revision"]) == (group.get("model_id"), group.get("model_revision"))), None)
            if model:
                for field, entries, identity, allowed in (("sensor_profile_id", self._catalog["sensors"], "sensor_profile_id", "sensor_profile_ids"), ("controller_id", self._catalog["controllers"], "id", "controller_ids")):
                    choices[(*base, field)] = [(entry[identity], entry[identity]) for entry in entries if entry[identity] in model["metadata"][allowed]]
        groups = [group for group in groups if isinstance(group, dict)]
        group_choices = [(group["group_id"], group["group_id"]) for group in groups if "group_id" in group]
        endpoints = request.get("scenario", {}).get("radio_geometry") or {}
        endpoint_choices = [(name, name) for name in endpoints.get("endpoints", {})]
        resources = [(item["resource_id"], item["resource_id"]) for item in (request.get("scenario", {}).get("network") or {}).get("resources", []) if "resource_id" in item]
        links = [(item["link_id"], item["link_id"]) for item in endpoints.get("links", []) if "link_id" in item]

        def visit(node, path):
            if isinstance(node, dict):
                for key, value in node.items():
                    current = (*path, key)
                    if key == "group_id" and "entity_groups" not in path:
                        choices[current] = group_choices
                    elif key in ("tx", "rx"):
                        choices[current] = endpoint_choices
                    elif key == "device_id":
                        group = next((group for group in groups if group.get("group_id") == node.get("group_id")), None)
                        model = next((model for model in models if group and (model["resource_id"], model["revision"]) == (group.get("model_id"), group.get("model_revision"))), None)
                        if model:
                            choices[current] = [(name, name) for name in model["metadata"].get("radio_devices", {})]
                    elif key == "link_id" and "communication_allocations" in path or key == "link_id" and "services" in path:
                        choices[current] = links
                    elif key in ("resource_ids", "endpoints", "passive_endpoints") and isinstance(value, list):
                        for index in range(len(value)):
                            choices[(*current, index)] = resources if key == "resource_ids" else endpoint_choices
                    visit(value, current)
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    visit(value, (*path, index))
        visit(request, ("request",))
        if self.composition:
            for role in self.composition.get("agent_roles", {}):
                choices[("request", "role_config", role, "group_id")] = group_choices
        return choices

    def update_choices(self, document):
        previous_groups = at(self._document, ("request", "scenario", "entity_groups"), []) if self._document else []
        groups = at(document, ("request", "scenario", "entity_groups"), [])
        def sensor_profiles(items):
            return [(item.get("group_id"), item.get("model_id"), item.get("model_revision"), item.get("sensor_profile_id")) for item in items if isinstance(item, dict)] if isinstance(items, list) else []
        profile_changed = sensor_profiles(previous_groups) != sensor_profiles(groups)
        self._document = deepcopy(document)
        if self.fixed_editor:
            choices = self._choices(document["request"])
            previous = self.fixed_editor.choices
            self.fixed_editor.choices = choices
            for path, field in list(self.fixed_editor.fields.items()):
                if path in choices and choices[path] != previous.get(path):
                    field._render()
        if profile_changed:
            self.report = None
            QTimer.singleShot(0, self, lambda: self.set_document(self._document))

    def flush(self):
        for editor in list(self._editors):
            editor.flush()

    def show_report(self, report: dict):
        self.flush()
        self.report = deepcopy(report)
        self.set_document(self._document, inspection_current=True)
        self.stale = False
        self.status_label.setText("已取得本次 inspection；" + ("Experiment 解析完整；RunOptions 尚未提交校验，尚未创建 Run。" if report["resolved_experiment"] else "仍有缺项/错误，不代表可运行。"))
        self._clear(self.errors_host)
        for error in report["errors"]:
            button = PushButton(error["field"] + ": " + error["message"], self.errors_host)
            button.clicked.connect(lambda checked=False, field=error["field"]: self.focus_error(field))
            self.errors_host.layout().addWidget(button)
        resolved = report["resolved_experiment"]
        if resolved:
            scene = resolved["scenario"]["scene"]
            timing = {"B": resolved["topology"]["world_count"], "seed": resolved["root_seed"], "physics_dt_s": scene["physics_dt"], "control_substeps": scene["control_substeps"], "decision_s": scene["physics_dt"] * scene["control_substeps"], "H": resolved["training_schedule"]["collection_steps"] if resolved["training_schedule"] is not None else None}
            self.details_view.setPlainText(self.details_view.toPlainText() + "\n已解析能力 / activation / 时间预算\n" + json.dumps({"activation": resolved["activation"], "timing": timing}, ensure_ascii=False, indent=2))

    def focus_error(self, field: str) -> bool:
        parts = tuple(re.sub(r"\[(\d+)\]", r".\1", field).split("."))
        path = parts if parts[0] in ("request", "run_options") else ("request", *parts)
        candidates = [(editor, editor.field_for_path(path)) for editor in self._editors]
        candidates = [(editor, widget) for editor, widget in candidates if widget is not None]
        if not candidates:
            self.show_error("错误字段尚未呈现控件：" + field)
            return False
        editor, widget = max(candidates, key=lambda pair: len(pair[1].path))
        editor.focus_field(path)
        self.field_located.emit(widget)
        return True

    def focus_section(self, section: str):
        if self._catalog is None:
            self.show_error("尚未取得 Server schema")
            return
        widget = None
        if section == "composition":
            widget = self.composition_combo
        elif section == "execution":
            widget = self.run_editor
        elif section == "selections":
            widget = next(iter(self.selection_editors.values()), None)
        elif section == "costs":
            widget = next((editor for path, editor in self.selection_editors.items() if path[-2] in ("cost", "termination")), None)
        elif section == "models":
            widget = next((row for row in self.model_rows.values() if not row.isHidden()), None)
            if widget is None:
                widget = self.fixed_editor.fields.get(("request", "scenario", "network"))
        if widget is None:
            self.show_error("当前组合尚未声明此配置入口：" + section)
            return
        widget.setFocus()
        self.field_located.emit(widget)

    def mark_stale(self):
        self.stale = True
        for editor in self._editors:
            editor.effective_current = False
        self.status_label.setText("草稿已修改 / 未检查。绑定、能力与有效值必须重新由 Server 确认。")

    def show_error(self, error):
        self.status_label.setText(error)

    def confirm_change(self, title, details):
        return bool(MessageBox(title, details, self.window()).exec())

    @staticmethod
    def _clear(host):
        while host.layout().count():
            item = host.layout().takeAt(0)
            widget = item.widget()
            if widget:
                for editor in ([widget] if isinstance(widget, SchemaEditor) else widget.findChildren(SchemaEditor)):
                    editor._building = True
                widget.setParent(None)
                widget.deleteLater()
