"""真实目录与显式登记入口；本机源码路径只保存在 QSettings。"""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from uuid import uuid4

from PySide6.QtCore import QStandardPaths, QTimer, Signal
from PySide6.QtWidgets import QAbstractItemView, QFileDialog, QHBoxLayout, QPlainTextEdit, QTableWidgetItem, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, ComboBox, LineEdit, PushButton, TableWidget

from .package_archive import ArchiveWorker


class ResourcesView(QWidget):
    example_ready = Signal(dict)
    resource_registered = Signal(dict)
    resource_selected = Signal(dict)
    source_checked = Signal(dict)
    editing_ready = Signal(dict)

    PATHS = {"training_package": "/catalog/training-packages", "environment": "/catalog/environments", "example": "/catalog/examples"}

    def __init__(self, connection, settings, parent=None, *, archive_dir: Path | None = None):
        super().__init__(parent)
        self.connection, self.settings = connection, settings
        self.archive_dir = Path(archive_dir) if archive_dir is not None else Path(
            QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation)) / "package-archives"
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self._requests = {}
        self._operations = {}
        self._items = {key: [] for key in self.PATHS}
        self._next = {key: None for key in self.PATHS}
        self._page_epoch = 0
        self._closing = False
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.kind_selector = ComboBox(self)
        for label, kind in (("研究包", "training_package"), ("环境", "environment"), ("参考模板", "example")):
            self.kind_selector.addItem(label, userData=kind)
        row.addWidget(self.kind_selector)
        for text, operation in (("刷新目录", self.refresh), ("下一页", self.next_page),
                                ("从所选模板新建", self._example_clicked), ("查看环境编辑来源", self._editing_clicked)):
            button = PushButton(text, self)
            button.clicked.connect(operation)
            row.addWidget(button)
        self.use_selected_button = PushButton("用于当前草稿", self)
        self.use_selected_button.clicked.connect(self.use_selected_resource)
        row.addWidget(self.use_selected_button)
        layout.addLayout(row)
        self.table = TableWidget(self)
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["ID", "revision", "SHA-256 / 研究状态", "来源"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._show_selected)
        layout.addWidget(self.table)
        self.details = QPlainTextEdit(self)
        self.details.setReadOnly(True)
        layout.addWidget(self.details)
        self.source_path = LineEdit(self)
        self.source_path.setPlaceholderText("外部编辑器已保存的源码目录或 ZIP；不进入科学 request")
        layout.addWidget(self.source_path)
        paths = QHBoxLayout()
        for text, operation in (("选目录", self._choose_directory), ("选 ZIP", self._choose_zip),
                                ("关联到所选版本", self._associate_selected), ("检查关联内容", self._check_selected),
                                ("登记新资源", lambda: self._register_clicked(False)),
                                ("更新所选资源", lambda: self._register_clicked(True))):
            button = PushButton(text, self)
            button.clicked.connect(operation)
            paths.addWidget(button)
        layout.addLayout(paths)
        self.status = BodyLabel("登记仅由明确点击触发；模板导入后由主窗口确认换稿。", self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.kind_selector.currentIndexChanged.connect(self._render)
        connection.result_received.connect(self._received)
        connection.state_changed.connect(self._connection_changed)
        self._retire_timer = QTimer(self)
        self._retire_timer.setInterval(50)
        self._retire_timer.timeout.connect(self._retire)

    def _source_key(self, kind, resource_id, revision):
        catalog = self.connection.state["catalog_id"]
        return f"catalog/{catalog}/sources/{kind}/{resource_id}/{revision}"

    def remember_source(self, record: dict, source: Path) -> None:
        reference = {key: record[key] for key in ("resource_id", "revision", "sha256", "kind")}
        value = {"reference": reference, "path": str(Path(source).absolute())}
        self.settings.setValue(self._source_key(record["kind"], record["resource_id"], record["revision"]), json.dumps(value, ensure_ascii=False))
        self.settings.sync()

    def source_for(self, kind: str, resource_id: str, revision: int) -> dict | None:
        value = self.settings.value(self._source_key(kind, resource_id, revision), "", type=str)
        return json.loads(value) if value else None

    def accept_registered(self, record: dict, source: Path | None = None) -> None:
        if source is not None:
            self.remember_source(record, source)
        kind = record["kind"]
        entries = self._items[kind]
        entries[:] = [item for item in entries if (item["resource_id"], item["revision"]) != (record["resource_id"], record["revision"])]
        entries.append(deepcopy(record))
        self._render()
        self.resource_registered.emit(deepcopy(record))

    def refresh(self) -> None:
        self._page_epoch += 1
        kind = self.kind_selector.currentData()
        self._items[kind] = []
        self._next[kind] = None
        self._render()
        self._catalog_page(kind, 0)

    def next_page(self) -> None:
        kind = self.kind_selector.currentData()
        if self._next[kind] is not None:
            self._catalog_page(kind, self._next[kind])

    def _catalog_page(self, kind, offset):
        self._request("page", (kind, self._page_epoch), "GET", self.PATHS[kind], params={"offset": offset, "limit": 50})

    def new_from_example(self, example_id: str) -> None:
        self._request("example", example_id, "POST", f"/catalog/examples/{example_id}/import")

    def use_selected_resource(self) -> None:
        if self.connection.state["state"] != "connected":
            self.status.setText("连接已失效；请重连并刷新真实目录后选择资源。")
            return
        if self.kind_selector.currentData() not in ("training_package", "environment"):
            self.status.setText("模板请使用“从所选模板新建”；不能作为已登记资源绑定。")
            return
        record = self._selected()
        if record is None:
            self.status.setText("请先选择一个已登记研究包或环境版本。")
            return
        self.resource_selected.emit(deepcopy(record))

    def register_source(self, kind: str, source: Path, resource_id: str | None = None) -> None:
        if kind not in ("training_package", "environment"):
            raise ValueError("只支持研究包和环境登记")
        if self._operations:
            self.status.setText("已有来源操作正在进行，请等待或关闭后重试。")
            return
        self._start_archive("register", kind, Path(source), resource_id)

    def check_source(self, record: dict) -> None:
        association = self.source_for(record["kind"], record["resource_id"], record["revision"])
        if association is None or not Path(association["path"]).exists():
            result = {"state": "missing", "reference": deepcopy(record), "message": "来源未关联或已缺失；仍可显式使用已登记版本。"}
            self.status.setText(result["message"])
            self.source_checked.emit(result)
            return
        self._start_archive("check", record["kind"], Path(association["path"]), record["resource_id"], record)

    def _start_archive(self, mode, kind, source, resource_id, reference=None):
        token = "g4:resource:" + uuid4().hex
        temporary = tempfile.TemporaryDirectory(prefix="resource-", dir=self.archive_dir)
        worker = ArchiveWorker(source, Path(temporary.name) / "package.zip", self)
        self._operations[token] = {"mode": mode, "kind": kind, "source": source, "resource_id": resource_id,
                                   "reference": reference, "generation": self.connection.state["generation"],
                                   "worker": worker, "temporary": temporary, "cancelled": False}
        worker.finished.connect(lambda: self._archive_finished(token))
        self.status.setText(f"固定快照并计算内容身份：{source}")
        worker.start()

    def _archive_finished(self, token):
        operation = self._operations[token]
        worker = operation["worker"]
        operation["worker"] = None
        worker.deleteLater()
        if operation["cancelled"] or self._closing or operation["generation"] != self.connection.state["generation"]:
            self._finish_operation(token)
        elif worker.error is not None:
            self.status.setText(str(worker.error))
            self.source_checked.emit({"state": "error", "error": worker.error})
            self._finish_operation(token)
        elif operation["mode"] == "check":
            result = {**worker.result, "state": "unchanged" if worker.result["sha256"] == operation["reference"]["sha256"] else "changed",
                      "reference": deepcopy(operation["reference"]), "source": str(operation["source"])}
            self.status.setText(f"{result['state']} / 当前 {result['sha256']}；变化时请选择更新、使用登记版本或返回编辑。")
            self.source_checked.emit(result)
            self._finish_operation(token)
        else:
            operation["archive"] = worker.result
            self.status.setText(f"登记固定快照：{worker.result['file_count']} 文件 / {worker.result['size_bytes']} 内容字节 / {worker.result['sha256']}")
            self.connection.upload(token, self.PATHS[operation["kind"]], Path(worker.result["archive_path"]), operation["resource_id"])

    def _finish_operation(self, token):
        self._operations.pop(token)["temporary"].cleanup()

    def _request(self, operation, key, method, path, **kwargs):
        if self._closing:
            return
        if any(data[1:] == (operation, key) for data in self._requests.values()):
            return
        token = "g4:resource:" + uuid4().hex
        self._requests[token] = (self.connection.state["generation"], operation, key)
        self.connection.request(token, method, path, **kwargs)

    def _received(self, generation, token, result):
        operation = self._operations.get(token)
        if operation is not None:
            if not self._closing and generation == operation["generation"]:
                if not result["ok"]:
                    self.status.setText("登记失败或结果未知；请刷新资源目录确认：" + str(result["error"]))
                elif not operation["cancelled"]:
                    if result["data"]["sha256"] != operation["archive"]["sha256"]:
                        self.status.setText("Server 登记 hash 与固定快照不匹配；资源已登记，请保留诊断并刷新目录。")
                    else:
                        self.accept_registered(result["data"], operation["source"])
                        self.status.setText("已登记不可变内容：" + json.dumps(result["data"], ensure_ascii=False))
            self._finish_operation(token)
            return
        pending = self._requests.pop(token, None)
        if pending is None or generation != pending[0] or generation != self.connection.state["generation"] or self._closing:
            return
        if not result["ok"]:
            self.status.setText(str(result["error"]))
            return
        _, action, key = pending
        if action == "page":
            kind, epoch = key
            if epoch == self._page_epoch:
                self._items[kind].extend(result["data"]["items"])
                self._next[kind] = result["data"]["next_offset"]
                self._render()
        elif action == "example":
            self.status.setText("模板资源已登记；等待主窗口确认替换草稿。研究效果标签：" + json.dumps(result["data"]["research_status"], ensure_ascii=False))
            self.example_ready.emit(deepcopy(result["data"]))
        else:
            self.details.setPlainText(json.dumps(result["data"], ensure_ascii=False, indent=2))
            self.editing_ready.emit(deepcopy(result["data"]))

    def _connection_changed(self, state):
        if state["state"] != "connected":
            # G1 可先发同代连接失败，再给原请求发送精确错误；不提前吞掉该结果。
            self._requests = {token: data for token, data in self._requests.items() if data[0] == state["generation"]}
            for operation in self._operations.values():
                operation["cancelled"] = True
            self.status.setText("连接已失效；显示为旧目录，请重连后刷新。")
            self._retire_timer.start()

    def _retire(self):
        if not self.connection.has_pending_requests:
            for token, operation in list(self._operations.items()):
                if operation["worker"] is None and (self._closing or operation["cancelled"]):
                    self._finish_operation(token)
        if not self._operations:
            self._retire_timer.stop()

    def close_operations(self) -> bool:
        self._closing = True
        self._requests.clear()
        for operation in self._operations.values():
            operation["cancelled"] = True
        self._retire()
        if self._operations:
            self._retire_timer.start()
        return not self._operations

    def _selected(self):
        rows = self.table.selectionModel().selectedRows()
        return self._items[self.kind_selector.currentData()][rows[0].row()] if len(rows) == 1 else None

    def _render(self, *args):
        items = self._items[self.kind_selector.currentData()]
        self.table.setRowCount(len(items))
        for row, item in enumerate(items):
            if self.kind_selector.currentData() == "example":
                values = [item["id"], "", json.dumps(item["research_status"], ensure_ascii=False), item["label"]]
            else:
                association = self.source_for(item["kind"], item["resource_id"], item["revision"])
                values = [item["resource_id"], str(item["revision"]), item["sha256"], association["path"] if association else "未关联本机来源"]
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))

    def _show_selected(self):
        record = self._selected()
        if record is not None:
            self.details.setPlainText(json.dumps(record, ensure_ascii=False, indent=2))

    def _choose_directory(self):
        path = QFileDialog.getExistingDirectory(self, "选择已保存的研究目录")
        if path:
            self.source_path.setText(path)

    def _choose_zip(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择来源 ZIP", "", "ZIP (*.zip)")
        if path:
            self.source_path.setText(path)

    def _associate_selected(self):
        record = self._selected()
        if record is not None and "resource_id" in record and self.source_path.text().strip():
            self.remember_source(record, Path(self.source_path.text()))
            self.status.setText("已关联本机来源；尚未登记任何新版本。")
            self._render()

    def _check_selected(self):
        record = self._selected()
        if record is not None and "resource_id" in record:
            self.check_source(record)

    def _register_clicked(self, update):
        record = self._selected() if update else None
        kind = self.kind_selector.currentData()
        if kind == "example" or not self.source_path.text().strip() or update and record is None:
            self.status.setText("请选择环境/研究包及真实来源，更新还需选中登记版本。")
            return
        self.register_source(kind, Path(self.source_path.text()), record["resource_id"] if record else None)

    def _example_clicked(self):
        record = self._selected()
        if record is not None and self.kind_selector.currentData() == "example":
            self.new_from_example(record["id"])

    def _editing_clicked(self):
        record = self._selected()
        if record is not None and self.kind_selector.currentData() == "environment":
            self._request("editing", record["resource_id"], "GET", f"/catalog/environments/{record['resource_id']}/{record['revision']}/editing")

    def closeEvent(self, event):
        if self.close_operations():
            super().closeEvent(event)
        else:
            event.ignore()
