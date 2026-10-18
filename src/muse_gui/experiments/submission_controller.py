"""一次捕获、一次开始；本地下一版草稿不属于正在提交的请求。"""

from copy import deepcopy
from pathlib import Path
import tempfile
from typing import Literal
from uuid import uuid4

from PySide6.QtCore import QObject, QTimer, Signal

from muse_gui.catalog.package_archive import ArchiveWorker


class SubmissionController(QObject):
    stage_changed = Signal(str)
    completed = Signal(dict, int)
    failed = Signal(str, dict)
    runs_found = Signal(dict)

    def __init__(self, draft, connection, resources, parent=None, *, flush):
        super().__init__(parent)
        self.draft, self.connection, self.resources, self.flush = draft, connection, resources, flush
        self._pending = None
        self._lookup = None
        self.last_experiment_id = None
        self.stage = "idle"
        self._retire_timer = QTimer(self)
        self._retire_timer.setInterval(50)
        self._retire_timer.timeout.connect(self._retire)
        connection.result_received.connect(self._received)
        connection.state_changed.connect(self._connection_changed)

    @property
    def busy(self) -> bool:
        return self._pending is not None

    def _set_stage(self, stage):
        self.stage = stage
        self.stage_changed.emit(stage)

    def start(self, run_options: dict, source_choice: Literal["update", "registered"] | None) -> None:
        if self.busy:
            self.failed.emit("busy", {"kind": "busy", "message": "已有捕获正在提交，不重复发起。"})
            return
        self.flush()
        revision, document = self.draft.capture()
        if document["pending_inputs"]:
            self._set_stage("pending_inputs")
            self.failed.emit("pending_inputs", {"kind": "input", "fields": list(document["pending_inputs"]), "message": "请先处理未完成输入。"})
            return
        state = self.connection.state
        if state["state"] != "connected" or document["catalog_id"] not in (None, state["catalog_id"]):
            self._set_stage("connection")
            self.failed.emit("connection", {"kind": "connection", "message": "需要同一 catalog 的真实连接；不按显示名重绑资源。"})
            return
        self.last_experiment_id = None
        self._pending = {"id": uuid4().hex, "generation": state["generation"], "revision": revision,
                         "write_revision": revision, "document": document, "request": deepcopy(document["request"]),
                         "run_options": deepcopy(run_options), "source_choice": source_choice, "cancelled": False,
                         "token": None, "operation": None, "worker": None, "temporary": None, "retiring": False}
        self._set_stage("source_check")
        if self._stop_if_cancelled():
            return
        request = document["request"]
        association = self.resources.source_for("training_package", request.get("training_package_id"), request.get("training_package_revision"))
        if association is None or not Path(association["path"]).exists():
            if source_choice == "registered":
                self._set_stage("source_missing_using_registered")
                self._derive()
            else:
                self._fail("source_choice_required", {"kind": "source", "message": "源码未关联或已缺失；可显式使用已登记版本，或关联来源后更新。"})
            return
        pending = self._pending
        pending["source"] = Path(association["path"])
        pending["reference"] = association["reference"]
        pending["temporary"] = tempfile.TemporaryDirectory(prefix="submission-", dir=self.resources.archive_dir)
        worker = ArchiveWorker(pending["source"], Path(pending["temporary"].name) / "package.zip", self)
        pending["worker"] = worker
        worker.finished.connect(lambda: self._archive_finished(pending["id"]))
        worker.start()

    def _archive_finished(self, identity):
        pending = self._pending
        if pending is None or pending["id"] != identity:
            return
        worker = pending["worker"]
        pending["worker"] = None
        worker.deleteLater()
        if self._stop_if_cancelled():
            return
        if worker.error is not None:
            self._fail("source_check", worker.error)
            return
        pending["archive"] = worker.result
        changed = worker.result["sha256"] != pending["reference"]["sha256"]
        choice = pending["source_choice"]
        if changed and choice not in ("update", "registered"):
            self._fail("source_choice_required", {"kind": "source_changed", "message": "来源已变化：请选择更新后运行、使用已登记版本或返回编辑。",
                                                 "registered_sha256": pending["reference"]["sha256"], "current_sha256": worker.result["sha256"]})
        elif changed and choice == "update":
            self._set_stage("updating_resource")
            if self._stop_if_cancelled():
                return
            token = self._begin_request("upload")
            self.connection.upload(token, "/catalog/training-packages", Path(worker.result["archive_path"]), pending["reference"]["resource_id"])
        else:
            self._set_stage("source_changed_using_registered" if changed else "source_unchanged")
            self._derive()

    def _derive(self):
        if self._stop_if_cancelled():
            return
        edit = self._pending["document"]["environment_edit"]
        if edit.get("upserts") or edit.get("removed"):
            self._request("derive", "POST", "/catalog/environments/derive", json=deepcopy(edit))
        else:
            self._freeze()

    def _freeze(self):
        pending = self._pending
        self._request("freezing_experiment", "POST", "/experiments", json=deepcopy(pending["request"]), params={"label": pending["document"]["name"]})

    def _begin_request(self, operation):
        token = "g4:submission:" + uuid4().hex
        self._pending["token"] = token
        self._pending["operation"] = operation
        return token

    def _request(self, operation, method, path, **kwargs):
        self._set_stage(operation)
        if self._stop_if_cancelled():
            return
        token = self._begin_request(operation)
        self.connection.request(token, method, path, **kwargs)

    def _adopt_reference(self, kind, record):
        pending = self._pending
        pending["request"][kind + "_id"] = record["resource_id"]
        pending["request"][kind + "_revision"] = record["revision"]
        # 仅采用仍匹配捕获/上一次自身写入的草稿；任意用户编辑后永不覆盖下一版。
        updates = [(('request',), pending["request"])]
        if kind == "environment":
            updates.append((('environment_edit',), {}))
        for path, value in updates:
            if self.draft.revision != pending["write_revision"]:
                return
            before = self.draft.revision
            old = self.draft.document[path[0]]
            self.draft.set_value(path, value)
            pending["write_revision"] = before + (old != value)

    def _received(self, generation, token, result):
        if self._lookup is not None and token == self._lookup[0]:
            expected = self._lookup[1]
            self._lookup = None
            if generation == expected:
                if result["ok"]:
                    self.runs_found.emit(result["data"])
                else:
                    self.failed.emit("lookup", result["error"])
            return
        pending = self._pending
        if pending is None or token != pending["token"] or generation != pending["generation"]:
            return
        operation = pending["operation"]
        if operation == "freezing_experiment" and result["ok"]:
            self.last_experiment_id = result["data"]["experiment_id"]
        if not result["ok"]:
            error = deepcopy(result["error"])
            if operation == "creating_run" and (error.get("outcome") == "unknown" or error["kind"] in ("submission_unknown", "protocol")):
                self._fail("submission_unknown", {**error, "experiment_id": self.last_experiment_id,
                                                  "lookup_params": {"experiment_id": self.last_experiment_id},
                                                  "submitted_revision": pending["revision"]})
            elif pending["cancelled"] and operation != "creating_run":
                self._finish()
                self._set_stage("cancelled")
            else:
                self._fail(operation, error)
            return
        if operation == "creating_run":
            revision = pending["revision"]
            self._finish()
            self._set_stage("completed")
            self.completed.emit(deepcopy(result["data"]), revision)
        elif pending["cancelled"]:
            self._finish()
            self._set_stage("cancelled")
        elif operation == "upload":
            record = result["data"]
            if record["sha256"] != pending["archive"]["sha256"]:
                self._fail("updating_resource", {"kind": "integrity", "message": "Server hash 与捕获的固定快照不匹配；已登记资源保留，不继续冻结。"})
                return
            self._adopt_reference("training_package", record)
            self.resources.accept_registered(record, pending["source"])
            self._derive()
        elif operation == "derive":
            self._adopt_reference("environment", result["data"])
            self.resources.accept_registered(result["data"])
            self._freeze()
        else:
            self._request("creating_run", "POST", "/runs", json={**pending["run_options"], "experiment_id": self.last_experiment_id})

    def _finish(self):
        pending, self._pending = self._pending, None
        if pending is not None and pending["temporary"] is not None:
            pending["temporary"].cleanup()
        self._retire_timer.stop()

    def _stop_if_cancelled(self):
        # 阶段/资源信号允许宿主同步取消或关闭，发出下一原子请求前重新检查所有权。
        if self._pending is None:
            return True
        if not self._pending["cancelled"]:
            return False
        self._finish()
        self._set_stage("cancelled")
        return True

    def _fail(self, stage, error):
        error = {**error, "experiment_id": self.last_experiment_id}
        self._finish()
        self._set_stage(stage)
        self.failed.emit(stage, error)

    def cancel(self) -> None:
        if self._pending is None:
            return
        self._pending["cancelled"] = True
        if self._pending["operation"] == "creating_run":
            self._set_stage("awaiting_run_result")
        else:
            self._set_stage("cancelled")

    def find_submitted_runs(self, offset: int = 0) -> None:
        if self.last_experiment_id is None:
            return
        token = "g4:lookup:" + uuid4().hex
        self._lookup = (token, self.connection.state["generation"])
        self.connection.request(token, "GET", "/runs", params={"experiment_id": self.last_experiment_id, "offset": offset, "limit": 50})

    def _connection_changed(self, state):
        if state["state"] != "connected" and self._pending is not None:
            identity = self._pending["id"]
            # G1 先发送连接状态、再发送同一请求的错误；先让该响应决定精确阶段。
            QTimer.singleShot(0, lambda: self._connection_lost(identity))

    def _connection_lost(self, identity):
        pending = self._pending
        if pending is None or pending["id"] != identity:
            return
        pending["cancelled"] = True
        pending["retiring"] = True
        stage = "submission_unknown" if pending["operation"] == "creating_run" else "connection"
        self._set_stage(stage)
        self.failed.emit(stage, {"kind": stage, "message": "连接代际已失效，停止后续步骤；已发出的原子请求结果不能假定未发生。",
                                 "experiment_id": self.last_experiment_id})
        self._retire_timer.start()
        self._retire()

    def _retire(self):
        pending = self._pending
        if pending is not None and pending["retiring"] and pending["worker"] is None and not self.connection.has_pending_requests:
            self._finish()

    def close(self) -> bool:
        """False 时宿主保留本 controller/资源页/G1 并在 Qt 事件循环中重试。"""
        self.cancel()
        if self._pending is not None:
            self._pending["retiring"] = True
            self._retire_timer.start()
            self._retire()
        return not self.busy
