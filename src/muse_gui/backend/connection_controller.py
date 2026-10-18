"""Qt 连接代际、客户端任务生命周期和仅供展示的目录缓存。"""

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from threading import Event
from time import monotonic

import httpx2
from PySide6.QtCore import QObject, QSaveFile, QIODevice, QStandardPaths, QThread, Signal, Slot
from websockets.exceptions import WebSocketException

from .muse_client import MuseClient


def _failure(error: Exception, method: str) -> dict:
    if isinstance(error, httpx2.HTTPStatusError):
        response = error.response
        try:
            detail = response.json() if "application/json" in response.headers.get("content-type", "") else response.text
        except json.JSONDecodeError as invalid:
            return {"kind": "protocol", "message": str(invalid), "status_code": response.status_code}
        return {"kind": "http", "status_code": response.status_code, "detail": detail, "message": str(error)}
    if (isinstance(error, (httpx2.RequestError, WebSocketException, TimeoutError))
            or method == "WS" and isinstance(error, OSError)):
        result = {"kind": "connection", "message": str(error)}
        if method == "POST":
            result["outcome"] = "unknown"
        return result
    return {"kind": "protocol" if isinstance(error, ValueError) else "local", "message": str(error)}


class _RequestWorker(QThread):
    completed = Signal(int, str, dict)

    def __init__(self, client, generation, request_id, method, operation, parent):
        super().__init__(parent)
        self.client = client
        self.generation = generation
        self.request_id = request_id
        self.method = method
        self.operation = operation

    def run(self):
        try:
            result = {"ok": True, "data": self.operation()}
        except (httpx2.HTTPError, OSError, ValueError) as error:
            result = {"ok": False, "error": _failure(error, self.method)}
        self.completed.emit(self.generation, self.request_id, result)


class _StreamWorker(QThread):
    completed = Signal(int, str, dict)

    def __init__(self, client, generation, request_id, path, parent):
        super().__init__(parent)
        self.client = client
        self.generation = generation
        self.request_id = request_id
        self.path = path
        self.stop_event = Event()

    def run(self):
        try:
            asyncio.run(self._consume())
        except (WebSocketException, OSError, ValueError, TimeoutError) as error:
            if not self.stop_event.is_set():
                self.completed.emit(self.generation, self.request_id,
                                    {"ok": False, "error": _failure(error, "WS")})

    async def _consume(self):
        async for event in self.client.stream(self.path, self.stop_event):
            if self.stop_event.is_set():
                return
            self.completed.emit(self.generation, self.request_id, {"ok": True, "data": event})


class ConnectionController(QObject):
    state_changed = Signal(dict)
    result_received = Signal(int, str, dict)

    def __init__(self, parent=None, *, client_factory=MuseClient, cache_dir: Path | None = None):
        super().__init__(parent)
        self._client_factory = client_factory
        self._cache_dir = cache_dir if cache_dir is not None else Path(
            QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)) / "catalog-cache"
        self._client = None
        self._retired = set()
        self._workers = set()
        self._requests = {}
        self._streams = {}
        self._closing = False
        self._profile = {}
        self._state = {"state": "unconnected", "generation": 0, "catalog_id": None,
                       "instance_id": None, "last_seen": None, "error": None, "binding_required": False}

    @property
    def state(self) -> dict:
        return deepcopy(self._state)

    @property
    def has_pending_requests(self) -> bool:
        return bool(self._workers)

    def connect(self, profile: dict) -> None:
        self.disconnect()
        self._closing = False
        self._profile = dict(profile)
        self._client = self._client_factory(profile["base_url"])
        self._set_state(state="checking", error=None, binding_required=False)
        client = self._client
        self._start_request("health", "GET", lambda: client.request("GET", "/health"), "/health", None)

    def disconnect(self) -> None:
        for worker in self._streams.values():
            worker.stop_event.set()
        self._streams.clear()
        self._requests.clear()
        if self._client is not None:
            self._retired.add(self._client)
            self._client = None
        self._set_state(state="disconnected", generation=self._state["generation"] + 1)
        self._release_clients()

    def request(self, request_id: str, method: str, path: str, **kwargs) -> None:
        if not self._require_connection(request_id):
            return
        client = self._client
        self._start_request(request_id, method.upper(), lambda: client.request(method, path, **kwargs),
                            path, kwargs.get("params"))

    def upload(self, request_id: str, path: str, archive: Path, resource_id: str | None = None) -> None:
        if self._require_connection(request_id):
            client = self._client
            self._start_request(request_id, "POST", lambda: client.upload(path, archive, resource_id), path, None)

    def download(self, request_id: str, path: str, destination: Path) -> None:
        if self._require_connection(request_id):
            client = self._client
            self._start_request(request_id, "GET", lambda: client.download(path, destination), path, None)

    def subscribe(self, request_id: str, path: str) -> None:
        if not self._require_connection(request_id):
            return
        if request_id in self._streams:
            raise ValueError(f"订阅已存在: {request_id}")
        worker = _StreamWorker(self._client, self._state["generation"], request_id, path, self)
        self._streams[request_id] = worker
        self._start(worker)

    def unsubscribe(self, request_id: str) -> None:
        worker = self._streams.pop(request_id, None)
        if worker is not None:
            worker.stop_event.set()

    def close(self, wait_ms: int = 1500) -> bool:
        """只回收客户端；返回 False 时宿主须保留 controller 并在稍后重试关闭。"""
        if not self._closing:
            self.disconnect()
            self._closing = True
        deadline = monotonic() + wait_ms / 1000
        for worker in list(self._workers):
            if worker.wait(max(0, int((deadline - monotonic()) * 1000))):
                self._remove_worker(worker)
        return not self._workers

    def cached(self, path: str, *, params: dict | None = None, catalog_id: str | None = None) -> dict | None:
        """返回显示副本；此方法绝不作为 request/resolve 的离线替身。"""
        identity = catalog_id if catalog_id is not None else self._state["catalog_id"]
        if identity is None:
            return None
        file = self._cache_file(identity, path, params)
        if not file.exists():
            return None
        result = json.loads(file.read_text(encoding="utf-8"))
        result["offline"] = (self._state["state"] != "connected" or identity != self._state["catalog_id"]
                             or result["instance_id"] != self._state["instance_id"])
        return result

    def _set_state(self, **updates):
        self._state.update(updates)
        self.state_changed.emit(self.state)

    def _require_connection(self, request_id):
        if self._state["state"] == "connected" and not self._closing:
            return True
        self.result_received.emit(self._state["generation"], request_id,
                                  {"ok": False, "error": {"kind": "offline", "message": "操作需要真实后端连接"}})
        return False

    def _start_request(self, request_id, method, operation, path, params):
        generation = self._state["generation"]
        key = (generation, request_id)
        if key in self._requests:
            raise ValueError(f"请求尚未结束: {request_id}")
        self._requests[key] = (method, path, params)
        self._start(_RequestWorker(self._client, generation, request_id, method, operation, self))

    def _start(self, worker):
        self._workers.add(worker)
        worker.completed.connect(self._completed)
        worker.finished.connect(self._finished)
        worker.start()

    @Slot()
    def _finished(self):
        self._remove_worker(self.sender())

    def _remove_worker(self, worker):
        if worker not in self._workers:
            return
        self._workers.remove(worker)
        if self._streams.get(worker.request_id) is worker:
            self._streams.pop(worker.request_id)
        self._release_clients()
        worker.deleteLater()

    def _release_clients(self):
        busy = {worker.client for worker in self._workers}
        for client in self._retired - busy:
            client.close()
            self._retired.remove(client)

    @Slot(int, str, dict)
    def _completed(self, generation, request_id, result):
        if generation != self._state["generation"] or self._closing:
            return
        metadata = self._requests.pop((generation, request_id), None)
        if metadata is None:
            worker = self.sender()
            if self._streams.get(request_id) is not worker:
                return
        if not result["ok"]:
            error = result["error"]
            if request_id == "health" or error["kind"] == "connection":
                self._set_state(state="failed" if request_id == "health" else "disconnected", error=error)
            self.result_received.emit(generation, request_id, result)
            return
        self._set_state(last_seen=datetime.now(timezone.utc).isoformat())
        if request_id == "health":
            self._accept_health(result["data"])
        elif metadata is not None and metadata[0] == "GET" and metadata[1].startswith("/catalog/"):
            try:
                self._save_cache(metadata[1], metadata[2], result["data"])
            except OSError as error:
                result = {"ok": False, "error": _failure(error, "CACHE")}
        self.result_received.emit(generation, request_id, result)

    def _accept_health(self, health):
        if (not isinstance(health, dict) or "api_revision" not in health
                or not all(isinstance(health.get(key), str) and health[key] for key in ("catalog_id", "instance_id"))):
            self._set_state(state="failed", error={"kind": "protocol", "message": "health 连接身份缺失或格式错误"})
            return
        if health["api_revision"] != 2:
            self._set_state(state="failed", error={"kind": "incompatible_api", "api_revision": health["api_revision"],
                                                  "message": "只支持 API revision 2"})
            return
        catalog = health["catalog_id"]
        changed = self._state["catalog_id"] is not None and self._state["catalog_id"] != catalog
        self._set_state(catalog_id=catalog, instance_id=health["instance_id"], binding_required=changed)
        expected = self._profile.get("catalog_id")
        if expected is not None and expected != catalog:
            self._set_state(state="failed", binding_required=True,
                            error={"kind": "catalog_mismatch", "message": "目录身份不同，需显式选择并重新绑定资源",
                                   "expected": expected, "actual": catalog})
            return
        self._set_state(state="connected", error=None)
        # 每次连接都重新取真值；重启后不能沿用旧 Run 或目录快照。
        for path in ("/catalog/schema", "/catalog/environments", "/catalog/training-packages", "/runs"):
            self.request(f"refresh:{path}", "GET", path)

    def _cache_file(self, catalog_id, path, params):
        identity = sha256(catalog_id.encode()).hexdigest()
        key = sha256(json.dumps([path, params], sort_keys=True).encode()).hexdigest()
        return self._cache_dir / identity / "api-2" / f"{key}.json"

    def _save_cache(self, path, params, data):
        record = {"catalog_id": self._state["catalog_id"], "api_revision": 2,
                  "instance_id": self._state["instance_id"],
                  "source": self._client.base_url, "saved_at": datetime.now(timezone.utc).isoformat(),
                  "path": path, "params": params, "data": data}
        target = self._cache_file(record["catalog_id"], path, params)
        target.parent.mkdir(parents=True, exist_ok=True)
        output = QSaveFile(str(target))
        if not output.open(QIODevice.OpenModeFlag.WriteOnly):
            raise OSError(output.errorString())
        content = json.dumps(record, ensure_ascii=False).encode("utf-8")
        if output.write(content) != len(content) or not output.commit():
            raise OSError(output.errorString())
