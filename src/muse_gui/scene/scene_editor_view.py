"""场景树和数值属性；只发意图，已提交真值属于ExperimentDraft。"""
from copy import deepcopy
import math

from PySide6.QtCore import QEvent, QSignalBlocker, Qt, Signal
from PySide6.QtGui import QColor, QKeySequence
from PySide6.QtWidgets import (QAbstractItemView, QAbstractSpinBox, QApplication, QFormLayout, QGridLayout, QHBoxLayout,
    QLineEdit, QPlainTextEdit, QScrollArea, QSizePolicy, QSpinBox, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)
from qfluentwidgets import BodyLabel, CheckBox, ComboBox, LineEdit, MessageBox, PushButton
from scipy.spatial.transform import Rotation


def _numbers(fields):
    values = [float(field.text()) for field in fields]
    if not all(math.isfinite(value) for value in values):
        raise ValueError('数值必须为有限数')
    return values


class SceneEditorView(QWidget):
    selection_requested = Signal(str)
    pose_requested = Signal(str, dict)
    box_requested = Signal(str, dict)
    input_staged = Signal(str, dict)
    add_requested = Signal(dict)
    duplicate_requested = Signal(str)
    remove_requested = Signal(str)
    placement_confirmed = Signal(dict)
    placement_cancelled = Signal()
    count_requested = Signal(str, int)
    visibility_requested = Signal(str, bool)
    focus_requested = Signal()
    reset_camera_requested = Signal()
    preview_requested = Signal()
    undo_requested = Signal()
    redo_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._selected = None
        self._item = None
        self._pending = False
        layout = QVBoxLayout(self)
        self.status = BodyLabel('尚无CPU预览；不要求研究配置完整。', self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.tree = QTreeWidget(self)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.installEventFilter(self)
        self.tree.setHeaderLabels(['对象', '状态'])
        self.tree.currentItemChanged.connect(lambda item, previous: self.selection_requested.emit(
            item.data(0, Qt.ItemDataRole.UserRole)) if item is not None else None)
        layout.addWidget(self.tree)
        self.property_scroll = QScrollArea(self)
        self.property_scroll.setWidgetResizable(True)
        properties = QWidget(self.property_scroll)
        self.property_scroll.setWidget(properties)
        fields_layout = QVBoxLayout(properties)
        fields_layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.property_scroll, 1)
        groups = QHBoxLayout()
        self.group_combo = ComboBox(self)
        self.group_combo.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.group_count = QSpinBox(self)
        self.group_count.setRange(0, 2**31 - 1)
        groups.addWidget(self.group_combo)
        groups.addWidget(self.group_count)
        apply_count = PushButton('应用数量（新增待摆放/绑定）', self)
        self.apply_count_button = apply_count
        apply_count.clicked.connect(lambda: self.count_requested.emit(self.group_combo.currentText(), self.group_count.value()))
        fields_layout.addLayout(groups)
        fields_layout.addWidget(apply_count)
        self.group_combo.currentIndexChanged.connect(self._group_selected)
        self._counts = {}
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        self.reason = BodyLabel(self)
        self.reason.setWordWrap(True)
        form.addRow(self.reason)
        self.label_field = LineEdit(self)
        self.label_field.editingFinished.connect(self.flush_inputs)
        self._input_fields = {'label': self.label_field}
        self.label_field.textEdited.connect(self._stage_inputs)
        form.addRow('标签', self.label_field)
        self.position_fields, self.rotation_fields, self.size_fields = [], [], []
        for name, fields, labels in (('位置 m', self.position_fields, 'XYZ'),
                ('固定轴角度 °', self.rotation_fields, ('roll X', 'pitch Y', 'yaw Z')),
                ('实际尺寸 m', self.size_fields, 'XYZ')):
            row = QHBoxLayout()
            for index, label in enumerate(labels):
                field = LineEdit(self)
                field.setMinimumWidth(0)
                field.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
                field.setPlaceholderText(label)
                field.editingFinished.connect(self.flush_inputs)
                prefix = 'position' if fields is self.position_fields else 'rotation' if fields is self.rotation_fields else 'size'
                self._input_fields[f'{prefix}.{index}'] = field
                field.textEdited.connect(self._stage_inputs)
                fields.append(field)
                row.addWidget(field)
            form.addRow(name, row)
        self.collision = CheckBox('物理碰撞', self)
        self.rf_blocking = CheckBox('RF LoS遮挡', self)
        self.visible = CheckBox('仅本视图显示', self)
        for checkbox in (self.collision, self.rf_blocking):
            checkbox.clicked.connect(self.flush_inputs)
        self.visible.clicked.connect(lambda checked: self.visibility_requested.emit(self._selected, checked) if self._selected else None)
        form.addRow(self.collision)
        form.addRow(self.rf_blocking)
        form.addRow(self.visible)
        rf_note = BodyLabel('RF仅参与LoS几何，不代表穿透/反射。', self)
        rf_note.setWordWrap(True)
        form.addRow(rf_note)
        fields_layout.addLayout(form)
        actions = QGridLayout()
        self.confirm_button = PushButton('确认放置', self)
        self.confirm_button.clicked.connect(self._confirm)
        cancel = PushButton('取消放置 Esc', self)
        cancel.clicked.connect(self.placement_cancelled.emit)
        add = PushButton('新增box', self)
        add.clicked.connect(lambda: self.add_requested.emit({'label': '长方体', 'size_m': [1., 1., 1.],
            'collision': True, 'rf_blocking': False, 'pose': None}))
        duplicate = PushButton('复制box', self)
        duplicate.clicked.connect(lambda: self.duplicate_requested.emit(self._selected) if self._selected else None)
        remove = PushButton('删除', self)
        remove.clicked.connect(lambda: self.remove_requested.emit(self._selected) if self._selected else None)
        for index, button in enumerate((add, duplicate, remove, self.confirm_button, cancel)):
            actions.addWidget(button, index // 2, index % 2)
        fields_layout.addLayout(actions)
        navigation = QGridLayout()
        for index, (title, signal) in enumerate((('定位选中', self.focus_requested), ('重置相机', self.reset_camera_requested),
                              ('重建CPU预览', self.preview_requested))):
            button = PushButton(title, self)
            button.clicked.connect(signal.emit)
            navigation.addWidget(button, index // 2, index % 2)
        fields_layout.addLayout(navigation)
        self.message = BodyLabel('选中可编辑对象后，用Viser操作柄移动/旋转；临时拖动只在end提交。', self)
        self.message.setWordWrap(True)
        fields_layout.addWidget(self.message)
        fields_layout.addStretch()
        self.set_object(None, None)

    def set_objects(self, objects, groups):
        with QSignalBlocker(self.tree), QSignalBlocker(self.group_combo):
            self.tree.clear()
            for identity, obj in objects.items():
                item = QTreeWidgetItem([obj['label'], obj.get('reason') or ('可编辑' if obj['editable'] else '只读')])
                item.setData(0, Qt.ItemDataRole.UserRole, identity)
                self.tree.addTopLevelItem(item)
                if identity == self._selected:
                    self.tree.setCurrentItem(item)
            current = self.group_combo.currentText()
            self._counts = {group['group_id']: group['count'] for group in groups}
            self.group_combo.clear()
            self.group_combo.addItems(list(self._counts))
            if current in self._counts:
                self.group_combo.setCurrentText(current)
        self._group_selected()

    def _group_selected(self):
        exists = self.group_combo.currentText() in self._counts
        self.apply_count_button.setEnabled(exists)
        self.group_count.setEnabled(exists)
        self.group_combo.setEnabled(bool(self._counts))
        if exists:
            self.group_count.setValue(self._counts[self.group_combo.currentText()])

    def set_object(self, identity, item, *, pending=False, visible=True, inputs=None):
        inputs = inputs or {}
        self._selected, self._item, self._pending = identity, deepcopy(item), pending
        editable = item is not None and item['editable']
        self.reason.setText('未选择对象' if item is None else item.get('reason') or ('待确认位置；尚未加入草稿/预览' if pending else '可编辑'))
        pose = item.get('pose') if item else None
        box = item.get('box') if item else None
        label = inputs.get('label', box['label'] if box else '')
        if self.label_field.text() != label:
            self.label_field.setText(label)
        self.label_field.setModified('label' in inputs)
        self.label_field.setEnabled(editable and box is not None)
        position = pose['position'] if pose else [None] * 3
        rotation = Rotation.from_quat([*pose['orientation_wxyz'][1:], pose['orientation_wxyz'][0]]).as_euler(
            'xyz', degrees=True).tolist() if pose else [0., 0., 0.]
        for fields, values in ((self.position_fields, position), (self.rotation_fields, rotation),
                               (self.size_fields, box['size_m'] if box else [None] * 3)):
            prefix = 'position' if fields is self.position_fields else 'rotation' if fields is self.rotation_fields else 'size'
            for index, (field, value) in enumerate(zip(fields, values)):
                key = f'{prefix}.{index}'
                text = inputs.get(key, '' if value is None else repr(float(value)))
                if field.text() != text:
                    field.setText(text)
                field.setModified(key in inputs)
                field.setEnabled(editable and (fields is not self.size_fields or box is not None))
        for checkbox, checked in ((self.collision, box['collision'] if box else False),
                                   (self.rf_blocking, box['rf_blocking'] if box else False), (self.visible, visible)):
            key = 'collision' if checkbox is self.collision else 'rf_blocking'
            checkbox.setChecked(inputs[key] == 'true' if checkbox is not self.visible and key in inputs else checked)
            checkbox.setEnabled(item is not None if checkbox is self.visible else editable and box is not None)
        self.confirm_button.setEnabled(pending)
        with QSignalBlocker(self.tree):
            for index in range(self.tree.topLevelItemCount()):
                row = self.tree.topLevelItem(index)
                if row.data(0, Qt.ItemDataRole.UserRole) == identity:
                    self.tree.setCurrentItem(row)

    def _pose(self):
        old = self._item.get('pose')
        pose = deepcopy(old) if old else {'position': [], 'orientation_wxyz': [1., 0., 0., 0.]}
        if old is None or any(field.isModified() for field in self.position_fields):
            pose['position'] = _numbers(self.position_fields)
        if any(field.isModified() for field in self.rotation_fields):
            q = Rotation.from_euler('xyz', _numbers(self.rotation_fields), degrees=True).as_quat()
            pose['orientation_wxyz'] = [float(q[3]), *map(float, q[:3])]
        return pose

    def _inputs(self):
        inputs = {key: field.text() for key, field in self._input_fields.items() if field.isModified() and field.isEnabled()}
        box = self._item.get('box')
        if box:
            for key, checkbox in (('collision', self.collision), ('rf_blocking', self.rf_blocking)):
                if checkbox.isChecked() != box[key]:
                    inputs[key] = 'true' if checkbox.isChecked() else 'false'
        return inputs

    def _stage_inputs(self):
        if self._item is not None and self._item['editable'] and not self._pending:
            self.input_staged.emit(self._selected, self._inputs())

    def flush_inputs(self):
        if self._item is None or not self._item['editable'] or self._pending:
            return
        self._stage_inputs()
        try:
            pose = self._pose()
            if self._item.get('box'):
                box = deepcopy(self._item['box'])
                box.update(pose=pose, label=self.label_field.text(),
                    collision=self.collision.isChecked(), rf_blocking=self.rf_blocking.isChecked())
                if any(field.isModified() for field in self.size_fields):
                    box['size_m'] = _numbers(self.size_fields)
                    if any(size <= 0 for size in box['size_m']):
                        raise ValueError('实际尺寸必须大于0')
                if box != self._item['box'] or self._inputs():
                    self.box_requested.emit(self._selected, box)
            elif pose != self._item.get('pose') or self._inputs():
                self.pose_requested.emit(self._selected, pose)
        except ValueError as error:
            self.message.setText(str(error))

    def _confirm(self):
        try:
            pose = self._pose()
            sizes = _numbers(self.size_fields)
            if any(size <= 0 for size in sizes):
                raise ValueError('实际尺寸必须大于0')
            self._item['box'].update(label=self.label_field.text(), size_m=sizes,
                collision=self.collision.isChecked(), rf_blocking=self.rf_blocking.isChecked())
            self.placement_confirmed.emit(pose)
        except ValueError as error:
            self.message.setText(str(error))

    def pending_box(self):
        return deepcopy(self._item['box'])

    def show_issue(self, text):
        self.message.setTextColor(QColor('#b91c1c'), QColor('#ff9999'))
        self.message.setText(text)

    def confirm_removal(self, impacts):
        return MessageBox('确认删除及引用清理', '\n'.join(impacts), self.window()).exec()

    def eventFilter(self, watched, event):
        if watched is self.tree and event.type() == QEvent.Type.KeyPress and (
                event.key() == Qt.Key.Key_Delete or event.matches(QKeySequence.StandardKey.Undo)
                or event.matches(QKeySequence.StandardKey.Redo)):
            # 路由由实际接收控件决定，不读取其它窗口可能尚未更新的全局文本焦点。
            if event.key() == Qt.Key.Key_Delete:
                if self._selected:
                    self.remove_requested.emit(self._selected)
            elif event.matches(QKeySequence.StandardKey.Undo):
                self.undo_requested.emit()
            else:
                self.redo_requested.emit()
            return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event):
        if self._pending and event.key() == Qt.Key.Key_Escape:
            self.placement_cancelled.emit()
            return
        focus = QApplication.focusWidget()
        if isinstance(focus, (QLineEdit, QPlainTextEdit, QAbstractSpinBox)) and self.isAncestorOf(focus):
            return super().keyPressEvent(event)
        if event.key() == Qt.Key.Key_Escape:
            self.placement_cancelled.emit()
        elif event.key() == Qt.Key.Key_Delete and self._selected:
            self.remove_requested.emit(self._selected)
        elif event.matches(QKeySequence.StandardKey.Undo):
            self.undo_requested.emit()
        elif event.matches(QKeySequence.StandardKey.Redo):
            self.redo_requested.emit()
        else:
            super().keyPressEvent(event)
