"""Run 控件与只读对象面板；不拥有共享 WebEngine 或编辑草稿。"""
import json

from PySide6.QtCore import QSignalBlocker, Signal
from PySide6.QtWidgets import (QAbstractItemView, QDoubleSpinBox, QHBoxLayout, QPlainTextEdit,
                               QSpinBox, QTableWidgetItem, QVBoxLayout, QWidget)
from qfluentwidgets import BodyLabel, CheckBox, ComboBox, PushButton, TableWidget


class RunView(QWidget):
    details_requested = Signal(dict)
    results_requested = Signal(str)
    continue_requested = Signal(dict)
    evaluate_requested = Signal(dict)

    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self._settings_key = None
        self._artifact_ids = None
        layout = QVBoxLayout(self)
        self.run_status = BodyLabel("尚未打开 Run", self)
        self.connection_status = BodyLabel("", self)
        self.message = BodyLabel("", self)
        self.message.setWordWrap(True)
        for widget in (self.run_status, self.connection_status, self.message):
            layout.addWidget(widget)
        commands = QHBoxLayout()
        for attribute, text, command in (("pause_button", "暂停", "pause"), ("resume_button", "继续", "resume"),
                                          ("step_button", "单步（整个 Run 一次 decision）", "step")):
            button = PushButton(text, self)
            button.clicked.connect(lambda checked=False, command=command: controller.command(command))
            setattr(self, attribute, button)
            commands.addWidget(button)
        layout.addLayout(commands)
        reset = QHBoxLayout()
        reset.addWidget(BodyLabel("Reset world（不是单个实体）", self))
        self.reset_world_spin = QSpinBox(self)
        reset.addWidget(self.reset_world_spin)
        self.reset_button = PushButton("重置指定 world", self)
        self.reset_button.clicked.connect(lambda: controller.command("reset_world", self.reset_world_spin.value()))
        reset.addWidget(self.reset_button)
        self.cancel_button = PushButton("取消后端 Run", self)
        self.cancel_button.clicked.connect(controller.cancel)
        reset.addWidget(self.cancel_button)
        layout.addLayout(reset)
        self.command_status = BodyLabel("命令：none；暂停状态未知", self)
        self.command_status.setWordWrap(True)
        layout.addWidget(self.command_status)
        display = QHBoxLayout()
        self.enabled_check = CheckBox("启用共享运行显示", self)
        self.world_spin = QSpinBox(self)
        self.rate_spin = QDoubleSpinBox(self)
        self.rate_spin.setRange(1., 30.)
        self.rate_spin.setSuffix(" Hz")
        self.apply_display_button = PushButton("应用共享显示", self)
        self.apply_display_button.clicked.connect(lambda: controller.set_display({
            "enabled": self.enabled_check.isChecked(), "world_id": self.world_spin.value(),
            "rate_hz": self.rate_spin.value()}))
        for widget in (self.enabled_check, BodyLabel("world", self), self.world_spin,
                       self.rate_spin, self.apply_display_button):
            display.addWidget(widget)
        layout.addLayout(display)
        self.display_status = BodyLabel("等待 Server 显示状态", self)
        self.display_status.setWordWrap(True)
        self.viewer_status = BodyLabel("网页未加载", self)
        self.pairing_status = BodyLabel("选择同步未就绪", self)
        for widget in (self.display_status, self.viewer_status, self.pairing_status):
            layout.addWidget(widget)
        row = QHBoxLayout()
        self.reconnect_button = PushButton("重连本视图（不改共享设置）", self)
        self.reconnect_button.clicked.connect(controller.reconnect_view)
        row.addWidget(self.reconnect_button)
        self.camera_button = PushButton("重置本视图相机", self)
        self.camera_button.clicked.connect(lambda: controller.view_intent({"kind": "reset_camera"}))
        row.addWidget(self.camera_button)
        layout.addLayout(row)
        self.frame_status = BodyLabel("实体无实时帧", self)
        self.frame_status.setWordWrap(True)
        layout.addWidget(self.frame_status)
        self.objects_table = TableWidget(self)
        self.objects_table.setColumnCount(6)
        self.objects_table.setHorizontalHeaderLabels(["对象", "kind", "group", "slot", "role", "model"])
        self.objects_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.objects_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.objects_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.objects_table.horizontalHeader().setStretchLastSection(True)
        self.objects_table.itemSelectionChanged.connect(self._selected)
        layout.addWidget(self.objects_table)
        object_row = QHBoxLayout()
        self.focus_button = PushButton("聚焦本视图对象", self)
        self.focus_button.clicked.connect(lambda: self._object_intent("focus"))
        self.visible_check = CheckBox("本视图可见请求（非回执）", self)
        self.visible_check.setChecked(True)
        self.visible_check.clicked.connect(lambda checked: self._object_intent("visibility", visible=checked))
        object_row.addWidget(self.focus_button)
        object_row.addWidget(self.visible_check)
        layout.addLayout(object_row)
        self.object_details = QPlainTextEdit(self)
        self.object_details.setReadOnly(True)
        self.object_details.setMaximumHeight(150)
        layout.addWidget(self.object_details)
        nav = QHBoxLayout()
        self.details_button = PushButton("冻结详情", self)
        self.details_button.clicked.connect(lambda: self.details_requested.emit(controller.navigation_context()))
        self.results_button = PushButton("指标 / 日志 / 结果", self)
        self.results_button.clicked.connect(lambda: self.results_requested.emit(controller.state.run_id))
        nav.addWidget(self.details_button)
        nav.addWidget(self.results_button)
        layout.addLayout(nav)
        derive = QHBoxLayout()
        self.checkpoint_selector = ComboBox(self)
        self.checkpoint_selector.currentIndexChanged.connect(self._update_navigation)
        self.continue_button = PushButton("从所选 checkpoint 新建训练", self)
        self.continue_button.clicked.connect(lambda: self.continue_requested.emit(
            controller.navigation_context(self.checkpoint_selector.currentData())))
        self.evaluate_button = PushButton("新建评价", self)
        self.evaluate_button.clicked.connect(lambda: self.evaluate_requested.emit(
            controller.navigation_context(self.checkpoint_selector.currentData())))
        for widget in (self.checkpoint_selector, self.continue_button, self.evaluate_button):
            derive.addWidget(widget)
        layout.addLayout(derive)
        note = BodyLabel("只读 Run；关闭标签仅断开本视图。取消 Run、关闭共享显示须使用上方明确操作。", self)
        note.setWordWrap(True)
        layout.addWidget(note)
        controller.changed.connect(self.render)
        self.render()

    def render(self):
        state = self.controller.state
        ready = bool(state and state.record)
        can_command = self.controller.can_command
        for button in (self.pause_button, self.resume_button, self.step_button, self.reset_button):
            button.setEnabled(can_command)
        self.reset_world_spin.setEnabled(can_command)
        active = bool(ready and self.controller.active and state.connected)
        self.cancel_button.setEnabled(active and not state.terminal if ready else False)
        self.results_button.setEnabled(ready)
        for widget in (self.apply_display_button, self.enabled_check, self.world_spin, self.rate_spin):
            widget.setEnabled(self.controller.can_set_display)
        self.reconnect_button.setEnabled(bool(active and not state.terminal))
        paired = bool(active and state.pairing.get("paired") and state.pairing.get("connected") and not state.terminal)
        for widget in (self.focus_button, self.visible_check, self.camera_button):
            widget.setEnabled(paired)
        if not ready:
            self.run_status.setText(f"Run {state.run_id}：正在读取 Server 记录" if state else "尚未打开 Run")
            self.command_status.setText("命令：none；暂停状态未知")
            self.connection_status.setText("正在读取；不沿用上一 Run 的真值")
            self.display_status.setText("等待 Server 显示状态")
            self.viewer_status.setText("网页未加载")
            self.pairing_status.setText("选择同步未就绪")
            self.frame_status.setText("实体无实时帧")
            self.message.setText(str(state.error or "") if state else "")
            with QSignalBlocker(self.objects_table):
                self.objects_table.setRowCount(0)
            self.object_details.setPlainText("等待当前 Run 的只读对象；不沿用上一 Run 的位姿。")
            self._update_navigation()
            return
        record = state.record
        self.run_status.setText(f"Run {state.run_id} · {record['mode']} · {record['state']}")
        self.connection_status.setText("已连接 Server" if state.connected else "连接断开；以下为最后已知状态，Run 不因此失败")
        self.message.setText(str(state.error or record.get("failure") or ""))
        result = state.interaction["result"]
        self.command_status.setText(f"命令：{state.command_status}；paused={state.paused}；"
                                   f"{json.dumps(result, ensure_ascii=False) if result else '等待实际 command_result'}")
        display = state.display
        if display is not None:
            count = state.definition["topology"]["world_count"] if state.definition else None
            key = (state, display["selection_seq"], count)
            if key != self._settings_key and count is not None:
                self._settings_key = key
                self.world_spin.setMaximum(count - 1)
                self.reset_world_spin.setMaximum(count - 1)
                self.world_spin.setValue(display["requested"]["world_id"])
                self.reset_world_spin.setValue(display["effective"]["world_id"])
                self.rate_spin.setValue(display["requested"]["rate_hz"])
                self.enabled_check.setChecked(display["requested"]["enabled"])
            self.display_status.setText(f"requested={display['requested']} (seq {display['selection_seq']})\n"
                                        f"effective={display['effective']} (seq {display['effective_seq']}) · "
                                        f"{display['status']} · {'GUI请求在途' if state.display_pending else ''} · {display['error'] or ''}")
        self.viewer_status.setText(state.viewer_error or ("网页已加载（配对另见下方）" if state.page_loaded else "网页未加载"))
        self.pairing_status.setText(f"选择同步：{state.pairing}" if paired else "选择同步未就绪")
        snapshot = state.entities
        self.frame_status.setText(("实体当前帧：" + json.dumps(snapshot["frame"], ensure_ascii=False))
                                  if snapshot["available"] else "实体不可实时：" + str(snapshot["reason"]))
        with QSignalBlocker(self.objects_table):
            self.objects_table.clearSelection()
            self.objects_table.setRowCount(len(snapshot["objects"]))
            for row, obj in enumerate(snapshot["objects"]):
                for column, field in enumerate(("label", "kind", "group_id", "group_slot", "role_id", "model_id")):
                    value = obj[field]
                    self.objects_table.setItem(row, column, QTableWidgetItem("—" if value is None else str(value)))
                if obj["object_id"] == state.selected_object:
                    self.objects_table.selectRow(row)
        self._show_object()
        artifacts = [item for item in state.artifacts if item["kind"] == "checkpoint"]
        ids = [item["id"] for item in artifacts]
        if self._artifact_ids != ids:
            selected = self.checkpoint_selector.currentData()
            self._artifact_ids = ids
            with QSignalBlocker(self.checkpoint_selector):
                self.checkpoint_selector.clear()
                self.checkpoint_selector.addItem("未选择 checkpoint", userData=None)
                for artifact in artifacts:
                    self.checkpoint_selector.addItem(artifact["id"], userData=artifact["id"])
                index = self.checkpoint_selector.findData(selected)
                self.checkpoint_selector.setCurrentIndex(max(0, index))
        self._update_navigation()

    def _update_navigation(self, *args):
        state = self.controller.state
        ready = bool(state and state.record and state.source is not None and state.definition is not None)
        self.details_button.setEnabled(ready)
        self.evaluate_button.setEnabled(ready)
        self.continue_button.setEnabled(ready and self.checkpoint_selector.currentData() is not None)

    def _selected(self):
        state = self.controller.state
        rows = self.objects_table.selectionModel().selectedRows()
        if state is not None and rows:
            state.selected_object = state.entities["objects"][rows[0].row()]["object_id"]
            self._show_object()
            self._object_intent("select")

    def _object_intent(self, kind, **kwargs):
        state = self.controller.state
        if state and state.selected_object is not None:
            self.controller.view_intent({"kind": kind, "object_id": state.selected_object, **kwargs})

    def _show_object(self):
        state = self.controller.state
        selected = next((obj for obj in state.entities["objects"] if obj["object_id"] == state.selected_object), None)
        if selected is None:
            self.object_details.setPlainText("请选择一个只读对象；没有实时帧时不显示虚构位置。")
        else:
            content = {"object": selected, "entity_frame": state.entities["frame"],
                       "available": state.entities["available"], "selection_frame": state.selection_frame}
            self.object_details.setPlainText(json.dumps(content, ensure_ascii=False, indent=2))

    def closeEvent(self, event):
        if self.controller.state is not None:
            self.controller.close_tab(self.controller.state.run_id)
        super().closeEvent(event)
