"""Fluent历史面板；独立于Run实时状态和控制按钮。"""
import json
import math

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QSplitter, QTableWidgetItem,
                               QTabWidget, QVBoxLayout, QWidget)
from qfluentwidgets import BodyLabel, CheckBox, ComboBox, LineEdit, PlainTextEdit, PushButton, SpinBox, TableWidget

from muse_gui.widgets.viewer_host import ViewerHost
from .replay_controller import ReplayController


class ReplayView(QWidget):
    def __init__(self, connection, parent=None):
        super().__init__(parent)
        self.viewer = ViewerHost(self)
        self.controller = ReplayController(connection, self.viewer, self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.source_status = BodyLabel('选择已发布trajectory打开独立历史回放；Server须在线。', self)
        self.source_status.setWordWrap(True)
        layout.addWidget(self.source_status)
        controls = QHBoxLayout()
        self.world_combo, self.episode_combo = ComboBox(self), ComboBox(self)
        self.world_combo.currentIndexChanged.connect(lambda _: self._choose('world', 'world_id', self.world_combo))
        self.episode_combo.currentIndexChanged.connect(lambda _: self._choose('episode', 'episode', self.episode_combo))
        self.play_button = PushButton('播放', self)
        self.play_button.clicked.connect(lambda: self.controller.playback('pause' if self.controller.state.data['playing'] else 'play'))
        self.previous_button, self.next_button = PushButton('前一样本', self), PushButton('后一样本', self)
        self.previous_button.clicked.connect(lambda: self.controller.playback('step', delta=-1))
        self.next_button.clicked.connect(lambda: self.controller.playback('step', delta=1))
        self.close_button = PushButton('关闭历史（不取消Run）', self)
        self.close_button.clicked.connect(self.close_replay)
        for widget in (self.world_combo, self.episode_combo, self.previous_button, self.play_button, self.next_button, self.close_button):
            controls.addWidget(widget)
        layout.addLayout(controls)
        seek = QHBoxLayout()
        self.sample_spin = SpinBox(self)
        self.seek_button = PushButton('定位样本', self)
        self.seek_button.clicked.connect(lambda: self.controller.playback('seek', sample_index=self.sample_spin.value()))
        self.speed_edit = LineEdit(self)
        self.speed_edit.setText('1')
        self.speed_edit.setMaximumWidth(80)
        self.speed_button = PushButton('应用速度', self)
        self.speed_button.clicked.connect(self._speed)
        for widget in (BodyLabel('sample_index', self), self.sample_spin, self.seek_button,
                       BodyLabel('倍速', self), self.speed_edit, self.speed_button):
            seek.addWidget(widget)
        self.cursor_status = BodyLabel(self)
        self.cursor_status.setWordWrap(True)
        seek.addWidget(self.cursor_status, 1)
        layout.addLayout(seek)
        split = QSplitter(Qt.Orientation.Horizontal, self)
        split.addWidget(self.viewer)
        tabs = QTabWidget(split)
        objects = QWidget(tabs)
        objects.setLayout(QVBoxLayout())
        self.objects_table = TableWidget(objects)
        self.objects_table.setColumnCount(3)
        self.objects_table.setHorizontalHeaderLabels(['对象', 'kind', 'role'])
        self.objects_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.objects_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.objects_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.objects_table.setMaximumHeight(150)
        self.objects_table.horizontalHeader().setStretchLastSection(True)
        self.objects_table.itemSelectionChanged.connect(self._selected)
        objects.layout().addWidget(self.objects_table)
        row = QHBoxLayout()
        self.focus_button = PushButton('聚焦对象', self)
        self.focus_button.clicked.connect(lambda: self._object_intent('focus'))
        self.visible_check = CheckBox('可见请求', self)
        self.visible_check.setChecked(True)
        self.visible_check.clicked.connect(lambda checked: self._object_intent('visibility', visible=checked))
        self.camera_button = PushButton('重置相机', self)
        self.camera_button.clicked.connect(lambda: self.controller.view_intent({'kind': 'reset_camera'}))
        for widget in (self.focus_button, self.visible_check, self.camera_button):
            row.addWidget(widget)
        objects.layout().addLayout(row)
        self.diagnostic_note = BodyLabel(self)
        self.diagnostic_note.setWordWrap(True)
        objects.layout().addWidget(self.diagnostic_note)
        self.object_details = PlainTextEdit(self)
        self.object_details.setReadOnly(True)
        objects.layout().addWidget(self.object_details, 1)
        tabs.addTab(objects, '同帧对象诊断')
        self.world_details = PlainTextEdit(self)
        self.world_details.setReadOnly(True)
        tabs.addTab(self.world_details, 'World评分 / 结束')
        export_tab = QWidget(tabs)
        export_tab.setLayout(QVBoxLayout())
        note = BodyLabel('导出当前world/episode已录部分；冻结当前相机。软件渲染、固定刚体几何/headlight。相机配对就绪后可导出。', self)
        note.setWordWrap(True)
        export_tab.layout().addWidget(note)
        self.export_fields = {}
        for key, text in (('fps', 'fps'), ('width', 'width（正偶数）'), ('height', 'height（正偶数）')):
            row = QHBoxLayout()
            row.addWidget(BodyLabel(text, self))
            field = LineEdit(self)
            field.setPlaceholderText('显式输入 ' + key)
            row.addWidget(field)
            self.export_fields[key] = field
            export_tab.layout().addLayout(row)
        self.export_button, self.cancel_export_button = PushButton('导出MP4', self), PushButton('取消导出', self)
        self.export_button.clicked.connect(self._export)
        self.cancel_export_button.clicked.connect(self.controller.cancel_export)
        export_tab.layout().addWidget(self.export_button)
        export_tab.layout().addWidget(self.cancel_export_button)
        self.export_status = BodyLabel(self)
        self.export_status.setWordWrap(True)
        export_tab.layout().addWidget(self.export_status)
        export_tab.layout().addStretch()
        tabs.addTab(export_tab, 'MP4导出')
        split.addWidget(tabs)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 1)
        # 网页异步创建前host无sizeHint；先分配空间，避免零宽折叠一直保留。
        split.setSizes([2 * tabs.sizeHint().width(), tabs.sizeHint().width()])
        layout.addWidget(split, 1)
        self.message = BodyLabel(self)
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.controller.changed.connect(self.render)
        self.controller.error.connect(lambda value: self.message.setText(json.dumps(value, ensure_ascii=False)))
        self.render()

    @property
    def releasing(self):
        return self.viewer.releasing or self.controller.releasing

    def open(self, source: dict) -> None:
        self.controller.open(source)

    def close_replay(self) -> None:
        self.controller.close()

    def _choose(self, kind, field, combo):
        if combo.currentData() is not None:
            self.controller.playback(kind, **{field: combo.currentData()})

    def _speed(self):
        try:
            speed = float(self.speed_edit.text())
            if not math.isfinite(speed):
                raise ValueError('倍速必须为有限数值')
        except ValueError:
            self.message.setText('倍速请输入有限数字；有效范围由Server裁决。')
            return
        self.controller.playback('speed', speed=speed)

    def _export(self):
        try:
            options = {key: (float(field.text()) if key == 'fps' else int(field.text()))
                       for key, field in self.export_fields.items()}
        except ValueError:
            self.message.setText('fps请输入数字，width/height请输入整数。')
            return
        cursor = self.controller.state.cursor
        self.controller.export(dict(options, world_id=cursor['world_id'], episode=cursor['episode']))

    def render(self):
        state = self.controller.state
        data = state.data if state else None
        ready = self.controller.ready
        for widget in (self.world_combo, self.episode_combo, self.play_button, self.previous_button,
                       self.next_button, self.sample_spin, self.seek_button, self.speed_edit, self.speed_button):
            widget.setEnabled(ready)
        self.export_button.setEnabled(self.controller.can_export)
        export = data['export'] if data else None
        self.cancel_export_button.setEnabled(self.controller.can_cancel_export)
        paired = ready and state.pairing.get('paired') and state.pairing.get('connected')
        for widget in (self.focus_button, self.visible_check, self.camera_button):
            widget.setEnabled(bool(paired))
        self.close_button.setEnabled(self.controller.active)
        if state is None:
            return
        self.source_status.setText(json.dumps(state.source, ensure_ascii=False) + '\n'
            + (f"稀疏采样间隔：{state.recording['config']['every_n_decisions']} decisions；记录摘要：{state.recording['summary']}"
               if state.recording else '正在读取冻结录制配置；不把稀疏reward/cost视作区间累计。'))
        self.message.setText(str(state.error or (data['error'] if data else '') or ''))
        if not ready or state.cursor is None:
            self.cursor_status.setText(data['status'] if self.controller.active and data else '历史未打开 / 已关闭')
            with QSignalBlocker(self.objects_table):
                self.objects_table.setRowCount(0)
            self.object_details.clear()
            self.world_details.clear()
            return
        cursor = state.cursor
        with QSignalBlocker(self.world_combo):
            self.world_combo.clear()
            for world in data['worlds']:
                self.world_combo.addItem('world ' + world, userData=int(world))
            self.world_combo.setCurrentIndex(self.world_combo.findData(cursor['world_id']))
        episodes = data['episodes'][str(cursor['world_id'])]
        with QSignalBlocker(self.episode_combo):
            self.episode_combo.clear()
            for episode in episodes:
                self.episode_combo.addItem(f"episode {episode['episode']} · {'完整' if episode['complete'] else '已录部分'}", userData=episode['episode'])
            self.episode_combo.setCurrentIndex(self.episode_combo.findData(cursor['episode']))
        self.sample_spin.setMaximum(data['worlds'][str(cursor['world_id'])]['sample_count'] - 1)
        if not self.sample_spin.hasFocus():
            self.sample_spin.setValue(cursor['sample_index'])
        self.play_button.setText('暂停' if data['playing'] else '播放')
        frame = data['diagnostics']
        self.cursor_status.setText(('定位中 / 命令待应用；' if state.requested_command else '')
            + f"实际 world={cursor['world_id']} · episode={cursor['episode']} · sample={cursor['sample_index']}\n"
            + f"run_decision={frame['run_decision']} · decision={frame['decision']} · tick={frame['tick']} · "
            + f"sim_time={frame['sim_time']}s · {frame['phase']} · speed={data['speed']}")
        with QSignalBlocker(self.objects_table):
            self.objects_table.clearSelection()
            self.objects_table.setRowCount(len(data['objects']))
            for row, obj in enumerate(data['objects']):
                for column, key in enumerate(('label', 'kind', 'role_id')):
                    self.objects_table.setItem(row, column, QTableWidgetItem(str(obj[key]) if obj[key] is not None else '—'))
                if obj['object_id'] == state.selected_object:
                    self.objects_table.selectRow(row)
        self.world_details.setPlainText('scope=world；初态/reset动作和评分不适用；稀疏值不是区间累计。\n'
                                       + json.dumps(dict(cursor=cursor, phase=frame['phase'], world=frame['world']), ensure_ascii=False, indent=2))
        self.export_status.setText(('相机已就绪' if data['camera_ready'] else '等待浏览器相机配对/更新')
                                  + '\n' + json.dumps(export, ensure_ascii=False))
        self._show_object()

    def _selected(self):
        rows = self.objects_table.selectionModel().selectedRows()
        if rows:
            self.controller.state.selected_object = self.controller.state.data['objects'][rows[0].row()]['object_id']
            self._show_object()
            self._object_intent('select')

    def _object_intent(self, kind, **options):
        state = self.controller.state
        if state and state.selected_object is not None:
            self.controller.view_intent(dict(kind=kind, object_id=state.selected_object, **options))

    def _show_object(self):
        details = self.controller.state.object_diagnostics()
        self.object_details.setPlainText(json.dumps(details, ensure_ascii=False, indent=2) if details else '请选择对象；只展示已应用样本。')
        self.diagnostic_note.setText('无学习数据（对象未绑定role）' if details and details['role'] is None
            else '初态/reset行：动作和评分不适用' if details and details['phase'] != 'step'
            else 'scope=role / role_slot；单个已录decision的动作、reward与命名cost，不是区间累计。')

    def closeEvent(self, event):
        self.close_replay()
        super().closeEvent(event)
