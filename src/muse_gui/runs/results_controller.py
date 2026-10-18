"""结果页的有界异步读取；复用 G1，不持有或控制 Worker。"""

from copy import deepcopy
from hashlib import file_digest
import json
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from .metric_series import LogBuffer, MetricSeries


class _VerifyDownload(QThread):
    completed = Signal(dict)

    def __init__(self, destination, provenance, parent):
        super().__init__(parent)
        self.destination = destination
        self.provenance = provenance

    def run(self):
        result = deepcopy(self.provenance)
        try:
            with self.destination.open("rb") as stream:
                result["actual_sha256"] = file_digest(stream, "sha256").hexdigest()
            artifact = result["artifact"]
            result["actual_size"] = self.destination.stat().st_size
            result["verified"] = (result["actual_sha256"] == artifact["sha256"]
                                  and result["actual_size"] == artifact["size"])
            if not result["verified"]:
                result["error"] = "完整文件的 SHA-256 或大小与 Server 发布的 artifact 不匹配"
            result["destination"] = str(self.destination)
            Path(str(self.destination) + ".source.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as error:
            result.update(verified=False, error=str(error))
        self.completed.emit(result)


class ResultsController(QObject):
    changed = Signal()
    error = Signal(dict)
    download_finished = Signal(dict)

    def __init__(self, connection, parent=None, *, source_url: str):
        super().__init__(parent)
        self.connection = connection
        self.source_url = source_url.rstrip("/")
        self._prefix = f"results:{uuid4().hex}:"
        self._serial = 0
        self._epoch = 0
        self._pending = {}
        self._downloads = {}
        self._verifiers = set()
        self.series = {}
        self.logs = {}
        self.runs = {}
        self.sources = {}
        self.definitions = {}
        self.artifacts = {}
        self.metric_pages = {}
        self.log_pages = {}
        self._manual_metrics = set()
        self._manual_logs = set()
        self.active = False
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(500)
        self.poll_timer.timeout.connect(self.poll)
        connection.result_received.connect(self._received)
        connection.state_changed.connect(self._connection_changed)

    @property
    def has_pending_downloads(self) -> bool:
        """宿主销毁 controller/connection 前须等待下载及本地校验收尾。"""
        return bool(self._downloads or self._verifiers)

    def show_runs(self, run_ids: list[str]) -> None:
        if len(run_ids) > 2 or len(set(run_ids)) != len(run_ids):
            raise ValueError("结果页最多选择两个不同 Run")
        self.stop()
        self.series = {run: MetricSeries() for run in run_ids}
        self.logs = {(run, channel): LogBuffer() for run in run_ids for channel in ("worker", "viser")}
        self.runs, self.sources, self.definitions, self.artifacts = {}, {}, {}, {}
        self.metric_pages, self.log_pages = {}, {}
        self._manual_metrics.clear()
        self._manual_logs.clear()
        self.active = bool(run_ids)
        for run in run_ids:
            self._request("run", run, f"/runs/{run}")
            self._request("artifacts", run, f"/runs/{run}/artifacts")
        self.poll()
        if self.active:
            self.poll_timer.start()
        self.changed.emit()

    def stop(self) -> None:
        self.active = False
        self.poll_timer.stop()
        self._epoch += 1
        self._pending.clear()

    def poll(self) -> None:
        if not self.active:
            return
        for run, series in self.series.items():
            page = self.metric_pages.get(run, {})
            if run not in self._manual_metrics and not (page.get("terminal") and page.get("eof")):
                self.next_metric_page(run)
            for channel in ("worker", "viser"):
                key = (run, channel)
                page = self.log_pages.get(key, {})
                if key not in self._manual_logs and not (page.get("terminal") and page.get("eof")):
                    self.next_log_page(run, channel)

    def next_metric_page(self, run: str) -> None:
        if self.active:
            self._request("metrics", run, f"/runs/{run}/metrics",
                          params={"offset": self.series[run].next_offset, "limit": 2000})

    def next_log_page(self, run: str, channel: str) -> None:
        if self.active:
            self._request("logs", (run, channel), f"/runs/{run}/logs",
                          params={"channel": channel, "offset": self.logs[(run, channel)].next_offset, "limit": 65536})

    def load_metric_history(self, offset: int = 0, *, run_id: str) -> None:
        """单 Run 字节 cursor 手动分页，防止历史窗口立即被尾部覆盖。"""
        if offset < 0:
            raise ValueError("cursor 必须非负")
        self._manual_metrics.add(run_id)
        self._pending = {token: data for token, data in self._pending.items() if data[2:4] != ("metrics", run_id)}
        self.series[run_id] = MetricSeries()
        self.series[run_id].next_offset = offset
        self.metric_pages.pop(run_id, None)
        self.next_metric_page(run_id)
        self.changed.emit()

    def load_log_history(self, run: str, channel: str, offset: int = 0) -> None:
        if offset < 0:
            raise ValueError("cursor 必须非负")
        key = (run, channel)
        self._manual_logs.add(key)
        self._pending = {token: data for token, data in self._pending.items() if data[2:4] != ("logs", key)}
        self.logs[key] = LogBuffer()
        self.logs[key].next_offset = offset
        self.log_pages.pop(key, None)
        self.next_log_page(run, channel)
        self.changed.emit()

    def follow_metrics(self, run: str) -> None:
        self._manual_metrics.discard(run)
        self.poll()

    def follow_logs(self, run: str, channel: str) -> None:
        self._manual_logs.discard((run, channel))
        self.poll()

    def refresh_artifacts(self) -> None:
        for run in self.series:
            self._request("artifacts", run, f"/runs/{run}/artifacts")

    def download_artifact(self, artifact: dict, destination: Path) -> None:
        token = self._token()
        path = f"/runs/{quote(artifact['run'], safe='')}/artifacts/{quote(artifact['id'], safe='')}"
        provenance = {"artifact": deepcopy(artifact), "path": path,
                      "url": self.source_url + path,
                      "catalog_id": self.connection.state["catalog_id"],
                      "instance_id": self.connection.state["instance_id"]}
        self._downloads[token] = (self.connection.state["generation"], destination, provenance)
        self.connection.download(token, path, destination)

    def _token(self):
        self._serial += 1
        return self._prefix + str(self._serial)

    def _request(self, operation, key, path, **kwargs):
        if any(value[2:4] == (operation, key) for value in self._pending.values()):
            return
        token = self._token()
        self._pending[token] = (self.connection.state["generation"], self._epoch, operation, key)
        self.connection.request(token, "GET", path, **kwargs)

    def _received(self, generation, token, result):
        download = self._downloads.pop(token, None)
        if download is not None:
            if generation == download[0]:
                if result["ok"]:
                    worker = _VerifyDownload(download[1], download[2], self.connection)
                    self._verifiers.add(worker)
                    worker.completed.connect(self.download_finished.emit)
                    worker.finished.connect(lambda worker=worker: self._verified(worker))
                    worker.start()
                else:
                    self.error.emit({"operation": "download", **result["error"]})
            return
        pending = self._pending.pop(token, None)
        if pending is None or pending[:2] != (generation, self._epoch) or not self.active:
            return
        _, _, operation, key = pending
        if not result["ok"]:
            self.stop()
            self.error.emit({"operation": operation, "run": key, **result["error"]})
            return
        data = result["data"]
        if operation == "metrics":
            was_terminal = self.metric_pages.get(key, {}).get("terminal", False)
            self.series[key].append_page(data)
            self.metric_pages[key] = data.copy()
            self.metric_pages[key].pop("records")
            if data["terminal"] and not was_terminal:
                self._request("run", key, f"/runs/{key}")
                self._request("artifacts", key, f"/runs/{key}/artifacts")
            if not data["eof"] and key not in self._manual_metrics:
                QTimer.singleShot(0, self.poll)
        elif operation == "logs":
            self.logs[key].append_page(data)
            self.log_pages[key] = {field: value for field, value in data.items() if field != "data_base64"}
            if not data["eof"] and key not in self._manual_logs:
                QTimer.singleShot(0, self.poll)
        elif operation == "run":
            self.runs[key] = data
            experiment = data["experiment_id"]
            self._request("source", key, f"/experiments/{experiment}/source")
            self._request("definition", key, f"/experiments/{experiment}")
        elif operation == "source":
            self.sources[key] = data
        elif operation == "definition":
            self.definitions[key] = data
        else:
            self.artifacts[key] = data
        self.changed.emit()

    def _verified(self, worker):
        self._verifiers.remove(worker)
        worker.deleteLater()

    def _connection_changed(self, state):
        if state["state"] != "connected":
            self.stop()
            if self._downloads:
                self.error.emit({"operation": "download", "message": "连接失效，下载结果未确认；可能存在不完整文件，请重新下载并校验。"})
                self._downloads.clear()
            self.error.emit({"operation": "connection", "message": "连接已失效；显示为旧结果，重连后重新打开 Run。"})
