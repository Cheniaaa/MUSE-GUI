"""SDK JSON schema 有界子集到 Qt 控件；只转换输入，不合并默认值或判定科学语义。"""

import json
import math
from copy import deepcopy

from PySide6.QtCore import QSignalBlocker, Signal
from PySide6.QtWidgets import QHBoxLayout, QInputDialog, QTableWidgetItem, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, ComboBox, LineEdit, PushButton, StrongBodyLabel, TableWidget


MISSING = object()


def at(value, path, default=MISSING):
    """只取明确路径；缺失与 null 分开，不计算继承值。"""
    for part in path:
        if isinstance(value, dict) and part in value or isinstance(value, list) and isinstance(part, int) and 0 <= part < len(value):
            value = value[part]
        else:
            return default
    return value


def path_text(path):
    return ".".join(map(str, path))


def deref(node, root):
    seen = set()
    while "$ref" in node:
        reference = node["$ref"]
        if not reference.startswith("#/$defs/") or reference in seen:
            raise ValueError("只支持无环本地 $defs 引用")
        seen.add(reference)
        target = root
        for part in reference[2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        node = dict(target, **{key: value for key, value in node.items() if key != "$ref"})
    return node


class SchemaEditor(QWidget):
    input_committed = Signal(tuple, object, tuple, bool)
    override_removed = Signal(tuple)
    pending_changed = Signal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setLayout(QVBoxLayout())
        self.layout().setContentsMargins(0, 0, 0, 0)
        self.fields = {}
        self.choices = {}
        self.excluded_paths = set()
        self.value_reader = None
        self.effective_current = True
        self._building = False
        self.section = None
        self._pending = {}

    def set_section(self, section: dict, explicit: dict, pending: dict, *, effective_current=True) -> None:
        self._building = True
        try:
            self.effective_current = effective_current
            while self.layout().count():
                item = self.layout().takeAt(0)
                widget = item.widget()
                if widget:
                    widget.setParent(None)
                    widget.deleteLater()
            self.section = deepcopy(section)
            self._pending = dict(pending)
            self.fields = {}
            self.root_field = _Field(self, tuple(section["path"]), section["schema"], explicit,
                                     section["effective"], self._commit, self._remove)
            # section 可以仅呈现同一 request 的一部分；根容器不得整体覆盖/删除它。
            self.root_field.mode.hide()
            self.root_field.reset_button.hide()
            self.root_field.source_label.hide()
            self.layout().addWidget(self.root_field)
            for error in section["errors"]:
                relative = () if error["field"] == "$" else tuple(error["field"].split("."))
                field = self.fields.get((*section["path"], *relative))
                if field:
                    field.error_label.setText(error["message"])
        finally:
            self._building = False

    def _commit(self, path, value, pending_path, pending_subtree):
        self.effective_current = False
        self.input_committed.emit(path, value, pending_path, pending_subtree)

    def _remove(self, path):
        self.override_removed.emit(path)

    def _set_pending(self, path, text):
        key = path_text(path)
        if text is None:
            self._pending.pop(key, None)
        else:
            self._pending[key] = text
            self.effective_current = False
        self.pending_changed.emit(key, text)

    def flush(self) -> None:
        for field in list(self.fields.values()):
            field.flush()

    def field_for_path(self, path: tuple):
        wanted = path_text(path)
        candidates = [field for key, field in self.fields.items() if wanted == path_text(key) or wanted.startswith(path_text(key) + ".")]
        return max(candidates, key=lambda field: len(field.path), default=None)

    def focus_field(self, path: tuple) -> bool:
        # 数组元素或判别联合的错误定位到最近的实际控件，不伪造 DTO 字段。
        field = self.field_for_path(path)
        if field is None:
            return False
        (field.editor or field.mode).setFocus()
        relative = path[len(field.path):]
        if isinstance(field.editor, TableWidget) and len(relative) >= 2 and all(str(part).isdigit() for part in relative[:2]):
            field.editor.setCurrentCell(int(relative[0]), int(relative[1]))
        return True


class _Field(QWidget):
    def __init__(self, owner, path, schema, explicit, effective, commit, remove):
        super().__init__(owner)
        self.owner, self.path = owner, path
        self._emit_value, self._emit_remove = commit, remove
        self._explicit = explicit
        self._effective = effective
        self._value = effective if explicit is MISSING else explicit
        if self._value is not MISSING:
            self._value = deepcopy(self._value)
        self.children, self.remove_buttons = [], []
        self.editor = None
        self.type_combo = None
        self._edited = False
        self.setLayout(QVBoxLayout())
        self.layout().setContentsMargins(8, 6, 0, 6)
        self.layout().setSpacing(5)
        owner.fields[path] = self
        header = QHBoxLayout()
        self.label = StrongBodyLabel(str(path[-1]) if path else "配置", self)
        header.addWidget(self.label, 1)
        self.mode = ComboBox(self)
        self.mode.addItem("沿用下层值", userData="inherit")
        self.mode.addItem("显式覆盖", userData="override")
        self.reset_button = PushButton("恢复默认", self)
        header.addWidget(self.mode)
        header.addWidget(self.reset_button)
        self.layout().addLayout(header)
        self.source_label = CaptionLabel(self)
        self.source_label.setWordWrap(True)
        self.layout().addWidget(self.source_label)
        self.error_label = BodyLabel(self)
        self.error_label.setWordWrap(True)
        self.layout().addWidget(self.error_label)
        self.body = QWidget(self)
        self.body.setLayout(QVBoxLayout())
        self.body.layout().setContentsMargins(0, 0, 0, 0)
        self.layout().addWidget(self.body)
        self.reset_button.clicked.connect(self._reset)
        try:
            self.schema = deref(schema, owner.section["schema"])
            supported = {"$schema", "$defs", "type", "title", "description", "examples", "default", "properties", "required",
                         "additionalProperties", "items", "anyOf", "oneOf", "discriminator", "enum", "const", "minimum", "maximum",
                         "exclusiveMinimum", "exclusiveMaximum", "multipleOf", "minLength", "maxLength", "pattern", "minItems", "maxItems",
                         "minProperties", "maxProperties", "unit", "units", "frame", "rate"}
            unsupported = {key for key in self.schema if key not in supported and not key.startswith("x-")}
            if unsupported:
                raise ValueError("未支持 schema: " + ", ".join(sorted(unsupported)))
            self.branches = [deref(branch, owner.section["schema"]) for branch in self.schema.get("anyOf", self.schema.get("oneOf", []))]
            self.nullable = self.schema.get("type") == "null" or any(branch.get("type") == "null" for branch in self.branches)
            if self.nullable:
                self.mode.addItem("显式空值 null", userData="null")
            self.mode.setCurrentIndex(self.mode.findData("inherit" if explicit is MISSING else "null" if explicit is None and self.nullable else "override"))
            relative = path[len(owner.section["path"]):]
            pointer = "".join("/" + str(key).replace("~", "~0").replace("/", "~1") for key in relative)
            origin = owner.section["origins"].get(pointer)
            if origin is None:
                ancestor = max((key for key in owner.section["origins"] if pointer.startswith(key + "/")), key=len, default=None)
                if ancestor is not None:
                    origin = owner.section["origins"][ancestor] + "（容器整体来源）"
                elif any(key.startswith(pointer + "/") for key in owner.section["origins"]):
                    origin = "见子字段来源"
                else:
                    origin = "本地显式输入" if explicit is not MISSING else "未提供有效值/来源"
            notes = ["来源: " + origin, self.schema.get("description", "")]
            for key in ("unit", "units", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minItems", "maxItems"):
                if key in self.schema:
                    notes.append(f"{key}: {self.schema[key]}")
            if pointer in owner.section["missing"]:
                notes.append("必填缺失")
            self.source_label.setText(" · ".join(note for note in notes if note))
            self._render()
            self.mode.currentIndexChanged.connect(self._mode_changed)
        except (ValueError, KeyError) as error:
            self.error_label.setText(str(error))
            self.mode.setEnabled(False)

    def _render(self):
        for widget in self.body.findChildren(QWidget):
            widget.blockSignals(True)
        while self.body.layout().count():
            widget = self.body.layout().takeAt(0).widget()
            if widget:
                widget.setParent(None)
                widget.deleteLater()
        for key in list(self.owner.fields):
            if key != self.path and key[:len(self.path)] == self.path:
                del self.owner.fields[key]
        self.children, self.remove_buttons = [], []
        self.editor = None
        self.type_combo = None
        schema = self.schema
        branches = [branch for branch in self.branches if branch.get("type") != "null"]
        if branches:
            if len(branches) == 1:
                schema = branches[0]
            else:
                self.type_combo = ComboBox(self)
                discriminator = self.schema.get("discriminator", {}).get("propertyName")
                for branch in branches:
                    choice = branch.get("properties", {}).get(discriminator, {}).get("const") if discriminator else branch.get("type")
                    if choice is None:
                        raise ValueError("联合类型需要明确类型或 discriminator")
                    self.type_combo.addItem(str(choice), userData=choice)
                current = self._value.get(discriminator) if discriminator and isinstance(self._value, dict) else {str: "string", int: "integer", float: "number", bool: "boolean"}.get(type(self._value))
                current = getattr(self, "_branch_choice", current)
                selected = self.type_combo.findData(current)
                self.type_combo.setCurrentIndex(max(0, selected))
                schema = branches[self.type_combo.currentIndex()]
                self.body.layout().addWidget(self.type_combo)
                self.type_combo.currentIndexChanged.connect(lambda index: self._change_type(branches[index], discriminator))
        self.active_schema = schema
        kind = schema.get("type", "string" if "enum" in schema or "const" in schema else "json")
        value = self._value
        if kind in ("object", "array") and value is not MISSING and not isinstance(value, dict if kind == "object" else list) and not (value is None and self.nullable):
            self.error_label.setText("字段结构与 schema 不符，原值保留：" + json.dumps(value, ensure_ascii=False))
            button = PushButton("替换为不完整对象 {}" if kind == "object" else "替换为空列表 []", self)
            button.clicked.connect(lambda: self._replace_container(kind))
            self.body.layout().addWidget(button)
            return
        if kind == "object":
            properties = schema.get("properties", {})
            known = list(properties)
            for key in dict.fromkeys([*(self._effective if isinstance(self._effective, dict) else {}), *(self._explicit if isinstance(self._explicit, dict) else {})]):
                if key not in known:
                    known.append(key)
            for key in known:
                if (*self.path, key) in self.owner.excluded_paths:
                    continue
                child_schema = properties.get(key, schema.get("additionalProperties", {}))
                if child_schema is False:
                    child_schema = {"description": "未知字段：保留原值，后端裁决", "type": "json"}
                if child_schema is True:
                    child_schema = {}
                child = _Field(self.owner, (*self.path, key), child_schema,
                    at(self._explicit, (key,)), at(self._effective, (key,)), self._child_commit, self._child_remove)
                if key in schema.get("required", []):
                    child.label.setText(str(key) + " *")
                self.children.append(child)
                self.body.layout().addWidget(child)
            if schema.get("additionalProperties", True) is not False:
                row = QWidget(self)
                layout = QHBoxLayout(row)
                self.key_input = LineEdit(row)
                self.key_input.setPlaceholderText("新增映射键")
                button = PushButton("添加键", row)
                button.clicked.connect(self._add_key)
                layout.addWidget(self.key_input, 1)
                layout.addWidget(button)
                self.body.layout().addWidget(row)
        elif kind == "array":
            item_schema = deref(schema.get("items", {}), self.owner.section["schema"])
            matrix = item_schema.get("type") == "array" and deref(item_schema.get("items", {}), self.owner.section["schema"]).get("type") in ("number", "integer")
            values = value if isinstance(value, list) else []
            if matrix:
                self._matrix(values, item_schema)
            else:
                for index, item in enumerate(values):
                    effective = at(self._effective, (index,), item) if self.owner.effective_current else item
                    child = _Field(self.owner, (*self.path, index), item_schema, at(self._explicit, (index,)), effective, self._child_commit, self._child_remove)
                    child.mode.hide()
                    child.reset_button.hide()
                    self.children.append(child)
                    self.body.layout().addWidget(child)
                    button = PushButton(f"删除第 {index + 1} 项", self)
                    button.clicked.connect(lambda checked=False, index=index: self._remove_index(index))
                    self.remove_buttons.append(button)
                    self.body.layout().addWidget(button)
                button = PushButton("添加项", self)
                button.clicked.connect(lambda: self._add_item(item_schema))
                self.body.layout().addWidget(button)
        elif self.path in self.owner.choices or "enum" in schema or "const" in schema or kind == "boolean":
            self.editor = ComboBox(self)
            self.editor.addItem("未选择", userData=MISSING)
            options = self.owner.choices.get(self.path)
            if options is None:
                values = schema.get("enum", [schema["const"]] if "const" in schema else [True, False])
                options = [(json.dumps(item, ensure_ascii=False), item) for item in values]
            for label, item in options:
                self.editor.addItem(label, userData=item)
            index = self.editor.findData(value)
            if index < 0 and value is not MISSING and value is not None:
                self.editor.addItem("未匹配引用: " + str(value), userData=value)
                index = self.editor.count() - 1
            self.editor.setCurrentIndex(max(0, index))
            self.editor.currentIndexChanged.connect(lambda index: self._commit(self.editor.itemData(index)) if index > 0 else None)
            self.body.layout().addWidget(self.editor)
        elif kind in ("integer", "number", "string", "json", "null"):
            self.editor = LineEdit(self)
            raw = self.owner._pending.get(path_text(self.path))
            if raw is None:
                raw = "" if value is MISSING or value is None else str(value) if kind == "string" else json.dumps(value, ensure_ascii=False)
            self.editor.setText(raw)
            self.editor.setPlaceholderText("未设置" if kind != "json" else "JSON 值（不丢弃未知字段）")
            self.editor.textEdited.connect(self._text_edited)
            self.editor.editingFinished.connect(self.flush)
            self.body.layout().addWidget(self.editor)
            if path_text(self.path) in self.owner._pending:
                self._edited = True
                self.error_label.setText("未完成输入：" + raw)
                self.editor.setError(True)
        else:
            raise ValueError("未支持 schema 类型: " + str(kind))
        if self.mode.currentData() == "null":
            self.body.setEnabled(False)

    def _change_type(self, branch, discriminator):
        self._branch_choice = branch["properties"][discriminator]["const"] if discriminator else branch["type"]
        if discriminator:
            self._commit({discriminator: branch["properties"][discriminator]["const"]})
        self._render()
        if isinstance(self.editor, LineEdit):
            self._edited = True
            self.flush()

    def _replace_container(self, kind):
        self._commit({} if kind == "object" else [])
        self._effective = MISSING
        self._render()

    def _text_edited(self, text):
        if not self.owner._building:
            self._edited = True
            self.owner._set_pending(self.path, text)

    def _commit(self, value, *, pending_path=None, pending_subtree=False):
        if self.owner._building or value is MISSING:
            return
        self._value = deepcopy(value)
        self._explicit = deepcopy(value)
        with QSignalBlocker(self.mode):
            self.mode.setCurrentIndex(self.mode.findData("null" if value is None and self.nullable else "override"))
        origin = self.path if pending_path is None else pending_path
        self._emit_value(self.path, value, origin, pending_subtree)
        self.owner._set_pending(origin, None)
        if pending_subtree:
            prefix = path_text(origin) + "."
            for key in list(self.owner._pending):
                if key.startswith(prefix):
                    del self.owner._pending[key]
            if origin == self.path and not self.owner._building:
                self._effective = MISSING
                self._render()
        self.error_label.clear()
        self._edited = False

    def _reset(self):
        if self.owner._building:
            return
        self._edited = False
        self._value = MISSING
        self._explicit = MISSING
        self.body.setEnabled(False)
        if isinstance(self.editor, LineEdit):
            self.editor.clear()
        self._emit_remove(self.path)
        prefix = path_text(self.path)
        for key in list(self.owner._pending):
            if key == prefix or key.startswith(prefix + "."):
                self.owner._pending.pop(key)
                self.owner.pending_changed.emit(key, None)
        self._effective = MISSING
        if not self.owner._building:
            self._render()
        with QSignalBlocker(self.mode):
            self.mode.setCurrentIndex(0)
        self.error_label.setText("覆盖已删除；有效值等待后端重新检查")

    def _mode_changed(self, index):
        mode = self.mode.itemData(index)
        self.body.setEnabled(mode != "null")
        if mode == "inherit":
            self._reset()
        elif mode == "null":
            self._commit(None, pending_subtree=True)
        elif self._value is not MISSING and self._value is not None:
            self._commit(self._array_values() if self.active_schema.get("type") == "array" else self._value)
        elif self.active_schema.get("type") == "object":
            self._commit({})
        elif self.active_schema.get("type") == "array":
            self._commit([])

    def _array_values(self):
        # 列表整体覆盖必须从唯一草稿读取；其他分区可能刚修改其中的 sensor_config。
        value = self.owner.value_reader(self.path) if self.owner.value_reader is not None else MISSING
        if value is MISSING:
            value = self._value
        return [] if value is MISSING else deepcopy(value)

    def _child_commit(self, path, value, pending_path, pending_subtree):
        if self.active_schema.get("type") != "array":
            self._remember_child(path, value, remove=False)
            self._emit_value(path, value, pending_path, pending_subtree)
            return
        values = self._array_values()
        relative = path[len(self.path):]
        node = values
        for part in relative[:-1]:
            if isinstance(node, dict) and part not in node:
                node[part] = {}
            node = node[part]
        node[relative[-1]] = value
        self._commit(values, pending_path=pending_path, pending_subtree=pending_subtree)

    def _child_remove(self, path):
        if self.active_schema.get("type") != "array":
            self._remember_child(path, None, remove=True)
            self._emit_remove(path)
            return
        values = self._array_values()
        relative = path[len(self.path):]
        node = at(values, relative[:-1])
        if node is not MISSING and (not isinstance(node, dict) or relative[-1] in node):
            del node[relative[-1]]
        self._commit(values, pending_path=path, pending_subtree=True)

    def _remember_child(self, path, value, *, remove):
        explicit = deepcopy(self._explicit) if isinstance(self._explicit, dict) else {}
        relative = path[len(self.path):]
        node = explicit
        for part in relative[:-1]:
            if part not in node:
                if remove:
                    return
                node[part] = {}
            node = node[part]
        if remove:
            node.pop(relative[-1], None)
        else:
            node[relative[-1]] = deepcopy(value)
        self._explicit = explicit
        self._value = deepcopy(explicit)

    def _structural_edit_allowed(self):
        self.owner.flush()
        prefix = path_text(self.path) + "."
        pending = self.owner.value_reader(("pending_inputs",)) if self.owner.value_reader is not None else self.owner._pending
        if any(key == path_text(self.path) or key.startswith(prefix) for key in pending):
            self.error_label.setText("请先处理本列表的未完成输入，再改变行结构")
            return False
        return True

    def _remove_index(self, index):
        if self._structural_edit_allowed():
            values = self._array_values()
            del values[index]
            self._commit(values)
            self._render()

    def _add_item(self, item_schema):
        if not self._structural_edit_allowed():
            return
        if item_schema.get("type") == "object" or "$ref" in item_schema:
            value = {}
        else:
            text, accepted = QInputDialog.getText(self, "新增列表项", "输入 JSON 值（不会填入默认值）")
            if not accepted:
                return
            try:
                value = self._convert(text, "json")
            except ValueError as error:
                self.error_label.setText(str(error))
                return
        self._commit([*self._array_values(), value])
        self._render()

    def _add_key(self):
        key = self.key_input.text()
        if not key or any(child.path[-1] == key for child in self.children):
            self.error_label.setText("映射键不能为空或重复")
            return
        schema = self.active_schema.get("additionalProperties", {})
        child = _Field(self.owner, (*self.path, key), {} if schema is True else schema, MISSING, MISSING, self._child_commit, self._child_remove)
        self.children.append(child)
        self.body.layout().addWidget(child)
        self.key_input.clear()

    def _matrix(self, values, item_schema):
        self.editor = TableWidget(self)
        raw = self.owner._pending.get(path_text(self.path))
        cells = json.loads(raw) if raw is not None else [[str(value) for value in row] for row in values]
        self.editor.setRowCount(len(cells))
        self.editor.setColumnCount(max(map(len, cells), default=0))
        for row, items in enumerate(cells):
            for column, text in enumerate(items):
                self.editor.setItem(row, column, QTableWidgetItem(text))
        self.editor.setMinimumHeight(150)
        self.editor.setMaximumHeight(280)
        self.row_sums = CaptionLabel(self)
        self.body.layout().addWidget(self.editor)
        self.body.layout().addWidget(self.row_sums)
        row = QWidget(self)
        actions = QHBoxLayout(row)
        for label, axis, delta in (("加行", "row", 1), ("减行", "row", -1), ("加列", "column", 1), ("减列", "column", -1)):
            button = PushButton(label, row)
            button.clicked.connect(lambda checked=False, axis=axis, delta=delta: self._resize_matrix(axis, delta))
            actions.addWidget(button)
        self.body.layout().addWidget(row)
        self.editor.cellChanged.connect(self._matrix_edited)
        self._matrix_kind = deref(item_schema["items"], self.owner.section["schema"])["type"]
        if raw is not None:
            self._edited = True
            self.error_label.setText("矩阵含未完成输入")
        else:
            self.row_sums.setText("行和: " + ", ".join(str(sum(row)) for row in values))

    def _matrix_cells(self):
        return [[self.editor.item(row, column).text() if self.editor.item(row, column) else "" for column in range(self.editor.columnCount())] for row in range(self.editor.rowCount())]

    def _matrix_edited(self, *args):
        self._edited = True
        self.owner._set_pending(self.path, json.dumps(self._matrix_cells(), ensure_ascii=False))
        self.flush()

    def _resize_matrix(self, axis, delta):
        count = self.editor.rowCount() if axis == "row" else self.editor.columnCount()
        if axis == "row":
            self.editor.setRowCount(max(0, count + delta))
        else:
            self.editor.setColumnCount(max(0, count + delta))
        self._matrix_edited()

    @staticmethod
    def _convert(text, kind):
        if kind == "string":
            return text
        if kind == "integer":
            return int(text)
        if kind == "number":
            value = float(text)
            if not math.isfinite(value):
                raise ValueError("请输入有限数值")
            return value
        value = json.loads(text)
        json.dumps(value, allow_nan=False)
        return value

    def flush(self):
        if self.owner._building or not self.editor or not self.body.isEnabled():
            return
        if isinstance(self.editor, LineEdit):
            raw = self.editor.text()
            # setText 也可用于实际导入/控件测试；不将未触碰的空字段写成覆盖。
            kind = self.active_schema.get("type", "json")
            baseline = "" if self._value is MISSING or self._value is None else str(self._value) if kind == "string" else json.dumps(self._value, ensure_ascii=False)
            if not self._edited and raw == baseline:
                return
            try:
                value = self._convert(raw, kind)
            except ValueError as error:
                self.owner._set_pending(self.path, raw)
                self.error_label.setText(str(error))
                self.editor.setError(True)
                return
            self.editor.setError(False)
            self._commit(value)
        elif isinstance(self.editor, TableWidget) and self._edited:
            try:
                value = [[self._convert(text, self._matrix_kind) for text in row] for row in self._matrix_cells()]
            except ValueError as error:
                self.error_label.setText(str(error))
                return
            self.row_sums.setText("行和: " + ", ".join(str(sum(row)) for row in value))
            self._commit(value)
