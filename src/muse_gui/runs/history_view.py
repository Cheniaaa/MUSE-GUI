"""Run 历史分页与导航；复制只读取 Server 保存的显式 source。"""

from copy import deepcopy
from uuid import uuid4

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QAbstractItemView, QHBoxLayout, QTableWidgetItem, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, ComboBox, LineEdit, PushButton, TableWidget


class HistoryView(QWidget):
    open_requested = Signal(str)
    compare_requested = Signal(list)
    copy_requested = Signal(dict)

    def __init__(self, connection, parent=None):
        super().__init__(parent)
        self.connection = connection
        self._prefix = f"history:{uuid4().hex}:"
        self._serial = 0
        self._epoch = 0
        self._pending = {}
        self._page = {"items": [], "next_offset": None}
        self._filters = {}
        layout = QVBoxLayout(self)
        filters = QHBoxLayout()
        self.mode_filter = ComboBox(self)
        self.mode_filter.addItems(["全部 mode", "TRAIN", "EVALUATE", "INTERACTIVE"])
        self.state_filter = ComboBox(self)
        self.state_filter.addItems(["全部 state", "QUEUED", "PREPARING", "COMPILING", "RUNNING",
                                    "STOP_REQUESTED", "STOPPING", "COMPLETED", "CANCELLED", "FAILED", "LOST"])
        self.experiment_filter = LineEdit(self)
        self.experiment_filter.setPlaceholderText("Experiment ID（精确匹配）")
        for widget in (self.mode_filter, self.state_filter, self.experiment_filter):
            filters.addWidget(widget)
        refresh = PushButton("查询", self)
        refresh.clicked.connect(self.refresh)
        filters.addWidget(refresh)
        layout.addLayout(filters)
        self.table = TableWidget(self)
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Run", "Experiment", "mode", "state"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.doubleClicked.connect(lambda _: self.open_selected())
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table)
        row = QHBoxLayout()
        for text, operation in (("打开", self.open_selected), ("比较所选两 Run", self.compare_selected),
                                ("复制原始 source 为草稿", self.copy_selected), ("首页", self.refresh)):
            button = PushButton(text, self)
            button.clicked.connect(operation)
            row.addWidget(button)
        self.next_button = PushButton("下一页", self)
        self.next_button.clicked.connect(self.next_page)
        self.next_button.setEnabled(False)
        row.addWidget(self.next_button)
        layout.addLayout(row)
        self.status = BodyLabel("历史来自 Server；查询后显示一页，不缓存为运行真值。", self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        connection.result_received.connect(self._received)
        connection.state_changed.connect(self._connection_changed)

    def set_page(self, page: dict) -> None:
        self._page = deepcopy(page)
        self.table.setRowCount(len(page["items"]))
        for row, record in enumerate(page["items"]):
            for column, key in enumerate(("run_id", "experiment_id", "mode", "state")):
                self.table.setItem(row, column, QTableWidgetItem(record[key]))
        self.next_button.setEnabled(page["next_offset"] is not None)
        self.status.setText(f"本页 {len(page['items'])} Run；下一 offset：{page['next_offset']}")

    def refresh(self) -> None:
        self._epoch += 1
        self._pending.clear()
        self._filters = {}
        if self.mode_filter.currentIndex():
            self._filters["mode"] = self.mode_filter.currentText()
        if self.state_filter.currentIndex():
            self._filters["state"] = self.state_filter.currentText()
        if self.experiment_filter.text().strip():
            self._filters["experiment_id"] = self.experiment_filter.text().strip()
        self._request("page", "/runs", params={**self._filters, "offset": 0, "limit": 50})

    def next_page(self) -> None:
        if self._page["next_offset"] is not None:
            self._epoch += 1
            self._pending.clear()
            self._request("page", "/runs", params={**self._filters, "offset": self._page["next_offset"], "limit": 50})

    def _selected(self):
        return [self._page["items"][index.row()] for index in self.table.selectionModel().selectedRows()]

    def open_selected(self) -> None:
        selected = self._selected()
        if len(selected) == 1:
            self.open_requested.emit(selected[0]["run_id"])
        else:
            self.status.setText("请选择一个 Run。")

    def compare_selected(self) -> None:
        selected = self._selected()
        if len(selected) == 2:
            self.compare_requested.emit([item["run_id"] for item in selected])
        else:
            self.status.setText("比较需恰好选择两个 Run。")

    def copy_selected(self) -> None:
        selected = self._selected()
        if len(selected) == 1:
            self._request("copy", f"/experiments/{selected[0]['experiment_id']}/source")
        else:
            self.status.setText("请选择一个 Run，再复制其原始 source。")

    def _request(self, operation, path, **kwargs):
        self._serial += 1
        token = self._prefix + str(self._serial)
        self._pending[token] = (self.connection.state["generation"], self._epoch, operation)
        self.connection.request(token, "GET", path, **kwargs)

    def _received(self, generation, token, result):
        pending = self._pending.pop(token, None)
        if pending is None or pending[:2] != (generation, self._epoch):
            return
        if not result["ok"]:
            self.status.setText(str(result["error"]))
        elif pending[2] == "page":
            self.set_page(result["data"])
        else:
            self.copy_requested.emit(deepcopy(result["data"]))

    def _connection_changed(self, state):
        if state["state"] != "connected":
            self._pending.clear()
            self.status.setText("连接已失效；当前历史仅为旧显示，重连后请重新查询。")

    def closeEvent(self, event):
        self._pending.clear()
        super().closeEvent(event)
